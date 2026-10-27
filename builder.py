"""套模板輸出靜態 HTML。

- site/index.html:最新一週
- site/monthly.html:最新一個月(還沒有月資料時顯示說明頁)
- site/archive/<期間>.html:每一週與每一個月各一頁(例如 2026-W43.html、2026-10.html)

所有資料嵌入 HTML、連結一律用相對路徑,本地雙擊與 GitHub Pages 都能用。
"""
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

log = logging.getLogger("github_weekly")

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
WEEKS = ROOT / "data" / "weeks"
MONTHS = ROOT / "data" / "months"

SLOT_LABELS = {"hot": "熱門", "domain": "領域相關", "rising": "潛力新星", "backfill": "補位"}


def _num(n) -> str:
    return f"{n:,}" if isinstance(n, int) else "—"


def _period_stars(item: dict) -> str:
    """本期新增星數(週資料與月資料都存在 stars_this_week)。"""
    if item.get("stars_this_week") is not None:
        return f"+{item['stars_this_week']:,}"
    if item.get("stars_this_week_est") is not None:
        return f"約 +{item['stars_this_week_est']:,}（估）"
    return "—"


def _period_gain(item: dict) -> tuple[str, bool]:
    """左欄價格標用:(數字文字, 是否為估算)。「（估）」改放在說明文字後面,數字本身不換行。"""
    if item.get("stars_this_week") is not None:
        return f"+{item['stars_this_week']:,}", False
    if item.get("stars_this_week_est") is not None:
        return f"約 +{item['stars_this_week_est']:,}", True
    return "—", False


def _health_class(score) -> str:
    """低於 60(推薦門檻)用警示色,其他維持主要文字色。"""
    return "low" if score is None or score < 60 else "good"


def _flag_class(flag: str) -> str:
    if flag == "成長異常":
        return "alert"
    if flag == "爆發成長" or flag.endswith("的替代方案"):
        return "link"
    return "muted"


def _specs(pr: dict) -> list[tuple[str, str]]:
    """實用性規格:一行純文字的各項 (文字, 樣式)。樣式 bad=不符合(--alert)、unk=未知(--muted)、空字串=正常。"""
    diff = pr.get("difficulty")
    d = (f"難度 {diff}/5", "bad" if diff > 3 else "") if isinstance(diff, int) else ("難度未知", "unk")
    win = {"yes": ("Windows 可用", ""), "no": ("不支援 Windows", "bad"),
           "wsl": ("Windows 需 WSL", "bad")}.get(pr.get("windows_ok"), ("Windows 未知", "unk"))
    gpu = {"yes": ("需要 GPU", "bad"), "no": ("不需 GPU", ""),
           "optional": ("GPU 可選", "")}.get(pr.get("needs_gpu"), ("GPU 未知", "unk"))
    paid = {"yes": ("需付費 API", "bad"), "no": ("免付費 API", ""),
            "optional": ("付費 API 可選", "")}.get(pr.get("needs_paid_api"), ("付費 API 未知", "unk"))
    code = {"none": ("免寫程式", ""), "light": ("需一點程式", ""),
            "heavy": ("需大量程式", "bad")}.get(pr.get("needs_coding"), ("程式需求未知", "unk"))
    return [d, win, gpu, paid, code]


def _repo_wbr(full_name: str) -> Markup:
    """owner/repo:在 / 後面放 <wbr>,讓窄螢幕優先從這裡換行,不把單字從中間切斷。"""
    owner, _, repo = full_name.partition("/")
    return Markup("{}/<wbr>{}").format(owner, repo)


def _title(pid: str) -> str:
    if "-W" in pid:
        y, w = pid.split("-W")
        return f"{y} 第 {int(w)} 週"
    y, m = pid.split("-")
    return f"{y} 年 {int(m)} 月"


def _facts(data: dict, ptype: str, pid: str) -> tuple[str, str]:
    """頁首兩行白話說明:(挑選範圍, 資料來源與產生時間)。"""
    c = data.get("coverage")
    if ptype == "monthly":
        span = "近 30 天" if c else f" {_title(pid)}"
    elif c:
        f, t = (datetime.strptime(c[k], "%Y-%m-%d") for k in ("from", "to"))
        span = f" {f:%m/%d} 到 {t:%m/%d} "
    else:
        y, w = pid.split("-W")
        mon = date.fromisocalendar(int(y), int(w), 1)
        span = f" {mon:%m/%d} 到 {mon + timedelta(days=6):%m/%d} "
    n = data.get("candidate_count")
    line1 = (f"從{span}的 {n} 個熱門專案中挑出" if n else f"從{span}的熱門專案中挑出") + \
            f",已排除 {data.get('excluded_count', 0)} 個清單、教學或久未更新的專案。"
    src = "GitHub Trending" if data.get("source") == "trending" else "GitHub Search API(備援)"
    try:
        made = datetime.fromisoformat(data["generated_at"]).strftime("%m/%d %H:%M")
    except (KeyError, TypeError, ValueError):
        made = "—"
    return line1.replace(",", "，").replace("(", "（").replace(")", "）"), f"資料來源 {src}，產生於 {made}"


def _pid(data: dict) -> str:
    return data.get("period") or data["week"]


def _range(pid: str) -> str:
    if "-W" in pid:
        y, w = pid.split("-W")
        mon = date.fromisocalendar(int(y), int(w), 1)
        return f"{mon:%Y/%m/%d}–{mon + timedelta(days=6):%m/%d}"
    y, m = pid.split("-")
    return f"{y} 年 {int(m)} 月"


def _generated(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M") + "（台灣時間）"
    except (TypeError, ValueError):
        return ts or "—"


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html", "j2"]),
                      trim_blocks=True, lstrip_blocks=True)
    env.globals.update(slot_label=lambda s: SLOT_LABELS.get(s, s), num=_num, period_stars=_period_stars,
                       period_gain=_period_gain,
                       health_class=_health_class, flag_class=_flag_class, specs=_specs, repo_wbr=_repo_wbr)
    return env


def _load(folder: Path, pid: str) -> dict:
    # utf-8-sig:資料若被記事本等工具存成帶 BOM 的 UTF-8 也能讀
    return json.loads((folder / f"{pid}.json").read_text(encoding="utf-8-sig"))


def _ids(folder: Path) -> list[str]:
    return sorted(p.stem for p in folder.glob("*.json")) if folder.exists() else []


def _render(env, data: dict | None, ptype: str, weeks: list[str], months: list[str], is_archive: bool) -> str:
    pid = _pid(data) if data else None
    latest = (weeks[-1] if weeks else None) if ptype == "weekly" else (months[-1] if months else None)
    recommended = [p["full_name"].split("/")[-1] for p in (data or {}).get("projects", [])
                   if (p.get("recommend") or {}).get("badge")]
    return env.get_template("page.html.j2").render(
        wd=data, ptype=ptype, pid=pid, label="本週" if ptype == "weekly" else "本月",
        is_archive=is_archive, is_latest=(pid == latest), root="../" if is_archive else "",
        weeks=list(reversed(weeks)), months=list(reversed(months)),
        has_weeks=bool(weeks), has_months=bool(months), recommended=recommended,
        title=_title(pid) if pid else "", facts=_facts(data, ptype, pid) if data else ("", ""),
        period_range=_range(pid) if pid else "", generated=_generated(data.get("generated_at")) if data else "")


def _history_entry(data: dict) -> dict:
    """推薦紀錄頁的一期:標題、資料期間、推薦安裝、精選、可能適合(名稱連到 GitHub)。"""
    pid = _pid(data)
    c = data.get("coverage")
    if c:
        f, t = (datetime.strptime(c[k], "%Y-%m-%d") for k in ("from", "to"))
        rng = f"{f:%m/%d}–{t:%m/%d}"
    else:
        rng = _range(pid)

    def short(items):
        return [(it["full_name"].split("/")[-1], it["url"]) for it in items]

    projects, quick = data.get("projects", []), data.get("quick_picks", [])
    return {
        "pid": pid, "title": _title(pid), "range": rng,
        "recommended": short([p for p in projects if (p.get("recommend") or {}).get("badge")]),
        "featured": short(projects),
        "maybe": short([q for q in quick if (q.get("recommend") or {}).get("maybe_fit")]),
    }


def build_all() -> Path:
    """用現有的週資料與月資料重新產生全部網頁(--build-only 與正常執行的最後一步都用這個)。"""
    env = _env()
    weeks, months = _ids(WEEKS), _ids(MONTHS)
    (SITE / "archive").mkdir(parents=True, exist_ok=True)
    wdata = {w: _load(WEEKS, w) for w in weeks}
    mdata = {m: _load(MONTHS, m) for m in months}
    for old in (SITE / "archive").glob("*.html"):  # 清掉已經沒有對應資料的舊存檔頁
        if old.stem not in wdata and old.stem not in mdata:
            old.unlink()

    index = _render(env, wdata[weeks[-1]] if weeks else None, "weekly", weeks, months, False)
    (SITE / "index.html").write_text(index, encoding="utf-8")
    monthly = _render(env, mdata[months[-1]] if months else None, "monthly", weeks, months, False)
    (SITE / "monthly.html").write_text(monthly, encoding="utf-8")
    for w, d in wdata.items():
        (SITE / "archive" / f"{w}.html").write_text(_render(env, d, "weekly", weeks, months, True), encoding="utf-8")
    for m, d in mdata.items():
        (SITE / "archive" / f"{m}.html").write_text(_render(env, d, "monthly", weeks, months, True), encoding="utf-8")
    groups = [
        {"type": "weekly", "title": "每週", "empty": "尚無週次紀錄，第一份會在週一產生。",
         "entries": [_history_entry(wdata[w]) for w in reversed(weeks)]},
        {"type": "monthly", "title": "每月", "empty": "尚無月度紀錄，第一份會在每月 1 日產生。",
         "entries": [_history_entry(mdata[m]) for m in reversed(months)]},
    ]
    (SITE / "history.html").write_text(env.get_template("history.html.j2").render(groups=groups), encoding="utf-8")
    log.info("網頁已產生:index、monthly 與 %d 個存檔頁", len(weeks) + len(months))
    return SITE / "index.html"


def build_site(week_data: dict, all_weeks: list[str]) -> None:
    """產生 index.html 與 archive 頁;連結一律使用相對路徑,確保本地雙擊與 GitHub Pages 都能用。

    資料一律從 data/weeks 與 data/months 讀取(呼叫前請先存檔),參數保留為相容舊介面。
    """
    build_all()
