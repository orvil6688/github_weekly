"""套模板輸出靜態 HTML:site/index.html(最新一週)與 site/archive/<週次>.html。

所有資料嵌入 HTML、連結一律用相對路徑,本地雙擊與 GitHub Pages 都能用。
"""
import json
import logging
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

log = logging.getLogger("github_weekly")

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
WEEKS = ROOT / "data" / "weeks"

SLOT_LABELS = {"hot": "熱門", "domain": "領域相關", "rising": "潛力新星", "backfill": "補位"}


def _num(n) -> str:
    return f"{n:,}" if isinstance(n, int) else "—"


def _week_stars(item: dict) -> str:
    if item.get("stars_this_week") is not None:
        return f"+{item['stars_this_week']:,}"
    if item.get("stars_this_week_est") is not None:
        return f"約 +{item['stars_this_week_est']:,}（估）"
    return "—"


def _health_class(score) -> str:
    if score is None:
        return "low"
    return "good" if score >= 80 else ("mid" if score >= 50 else "low")


def _badges(pr: dict) -> list[tuple[str, str]]:
    """實用性徽章:(文字, 樣式)。yes=好、no=不好、maybe=要注意。"""
    win = {"yes": ("Windows 可用", "yes"), "no": ("不支援 Windows", "no"),
           "wsl": ("Windows 需 WSL", "maybe")}.get(pr.get("windows_ok"), ("Windows ?", ""))
    gpu = {"yes": ("需要 GPU", "no"), "no": ("不需 GPU", "yes"),
           "optional": ("GPU 可選", "maybe")}.get(pr.get("needs_gpu"), ("GPU ?", ""))
    paid = {"yes": ("需付費 API", "no"), "no": ("免付費 API", "yes"),
            "optional": ("付費 API 可選", "maybe")}.get(pr.get("needs_paid_api"), ("付費 API ?", ""))
    code = {"none": ("免寫程式", "yes"), "light": ("需一點程式", "maybe"),
            "heavy": ("需大量程式", "no")}.get(pr.get("needs_coding"), ("程式需求 ?", ""))
    return [win, gpu, paid, code]


def _week_range(week: str) -> str:
    y, w = week.split("-W")
    mon = date.fromisocalendar(int(y), int(w), 1)
    sun = date.fromisocalendar(int(y), int(w), 7)
    return f"{mon:%Y/%m/%d}–{sun:%m/%d}"


def _generated(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M") + "（台灣時間）"
    except (TypeError, ValueError):
        return ts or "—"


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html", "j2"]),
                      trim_blocks=True, lstrip_blocks=True)
    env.globals.update(slot_label=lambda s: SLOT_LABELS.get(s, s), num=_num, week_stars=_week_stars,
                       health_class=_health_class, badges=_badges)
    return env


def _render(env: Environment, wd: dict, all_weeks: list[str], is_archive: bool) -> str:
    # 首頁列出最新以外的週次;存檔頁列出自己與最新以外的週次(另有「最新一週」連結)
    latest = all_weeks[-1] if all_weeks else wd["week"]
    if is_archive:
        others = [w for w in reversed(all_weeks) if w != wd["week"] and w != latest]
    else:
        others = [w for w in reversed(all_weeks) if w != wd["week"]]
    return env.get_template("page.html.j2").render(
        wd=wd, is_archive=is_archive, root="../" if is_archive else "",
        other_weeks=others, latest=latest,
        week_range=_week_range(wd["week"]), generated=_generated(wd.get("generated_at")))


def _load_week(week: str) -> dict:
    # utf-8-sig:週資料若被記事本等工具存成帶 BOM 的 UTF-8 也能讀
    return json.loads((WEEKS / f"{week}.json").read_text(encoding="utf-8-sig"))


def build_site(week_data: dict, all_weeks: list[str]) -> None:
    """產生 index.html 與 archive 頁;連結一律使用相對路徑,確保本地雙擊與 GitHub Pages 都能用。

    index.html 顯示 all_weeks 中最新的一週;每一週(含最新)都有自己的 archive 頁,
    所以每次都重建全部 archive,讓各頁的「過往週次」清單保持一致。
    """
    env = _env()
    all_weeks = sorted(set(all_weeks) | {week_data["week"]})
    (SITE / "archive").mkdir(parents=True, exist_ok=True)

    datas = {week_data["week"]: week_data}
    for w in all_weeks:
        if w not in datas:
            datas[w] = _load_week(w)

    latest = datas[all_weeks[-1]]
    (SITE / "index.html").write_text(_render(env, latest, all_weeks, False), encoding="utf-8")
    for w in all_weeks:
        (SITE / "archive" / f"{w}.html").write_text(_render(env, datas[w], all_weeks, True), encoding="utf-8")
    log.info("網頁已產生:site/index.html 與 %d 個 archive 頁", len(all_weeks))


def build_all() -> Path:
    """只用現有的週資料重新產生網頁(--build-only)。"""
    weeks = sorted(p.stem for p in WEEKS.glob("*.json"))
    if not weeks:
        raise FileNotFoundError("data/weeks/ 裡沒有任何週資料")
    build_site(_load_week(weeks[-1]), weeks)
    return SITE / "index.html"
