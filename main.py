"""GitHub 每週/每月熱門五選:主流程。

用法:
    python main.py --dry-run                 只挑專案,不呼叫 LLM、不產生網頁
    python main.py --dry-run --inspect owner/repo   另外印出指定專案的判斷細節
    python main.py                           完整執行:挑選 + 中文介紹,寫入 data/weeks/
    python main.py --no-llm                  不呼叫 LLM,示範降級版本
    python main.py --refresh-llm             忽略本期快取,重新產生精選介紹與速覽(分類沿用快取)
    python main.py --fill-missing            只補本期 JSON 中降級或由輕量模型產生的項目(雲端補跑用)
    python main.py --build-only              只用現有資料重新產生網頁
    以上都可加 --period monthly 改為每月熱門(期間為上個月,寫入 data/months/)
"""
import argparse
import copy
import json
import logging
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

import builder
import fetcher
import selector
import summarizer

ROOT = Path(__file__).resolve().parent
TPE = timezone(timedelta(hours=8))
log = logging.getLogger("github_weekly")

# 從週資料沿用到月資料的欄位(推薦門檻會重新計算)
REUSE_FIELDS = ("intro", "practicality", "ideas_for_me", "glossary", "risk_notes", "trial_prompt",
                "intro_status", "model")


# ---------- 期間(週/月) ----------

def make_period(kind: str, now: datetime, month: str | None = None, week: str | None = None) -> dict:
    """weekly:本週(ISO 週),可用 week 指定;monthly:執行當下的上個月(台灣時區),可用 month 指定。

    指定期間主要給 --fill-missing / --refresh-llm 補跑舊的一期用。
    """
    if kind == "weekly":
        if not week:
            y, w, _ = now.isocalendar()
            week = f"{y}-W{w:02d}"
        return {"type": "weekly", "id": week, "label": "本週", "days": 7,
                "dir": ROOT / "data" / "weeks", "history": ROOT / "data" / "history.json"}
    if not month:
        first = now.date().replace(day=1)
        month = (first - timedelta(days=1)).strftime("%Y-%m")
    return {"type": "monthly", "id": month, "label": "本月", "days": 30,
            "dir": ROOT / "data" / "months", "history": ROOT / "data" / "history_monthly.json"}


def period_cfg(cfg: dict, P: dict) -> dict:
    """月度用 config.monthly 覆蓋對應參數,並設定 Trending 期間與去重期數。"""
    c = copy.deepcopy(cfg)
    if P["type"] == "monthly":
        m = cfg.get("monthly", {})
        c["trending_since"] = "monthly"
        c["rising_created_within_days"] = m.get("rising_created_within_days", c["rising_created_within_days"])
        c["rising_max_total_stars"] = m.get("rising_max_total_stars", c["rising_max_total_stars"])
        c["exclude_n"] = m.get("exclude_recommended_months", 3)
    else:
        c["trending_since"] = "weekly"
        c["exclude_n"] = cfg["exclude_recommended_weeks"]
    c["period_days"] = P["days"]
    return c


def setup_logging(pid: str) -> Path:
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    path = log_dir / f"{pid}_{datetime.now(TPE):%Y%m%d_%H%M%S}.log"
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("  ⚠ %(message)s"))
    log.addHandler(fh)
    log.addHandler(ch)
    return path


# ---------- 抓取與挑選 ----------

def gather(cfg: dict, P: dict, llm=None) -> dict:
    """抓候選 → 過濾 → 評分 →(LLM 分類修正)→ 挑選。回傳整理好的結果(還沒有中文介紹)。"""
    cands = fetcher.fetch_candidates(cfg)
    stats = fetcher.fetch_candidates.stats

    hist = selector.load_history(P["history"])
    rec_featured, rec_quick = selector.recent_recommended(hist, P["id"], cfg["exclude_n"])
    for c in cands:
        c["recently_featured"] = c["full_name"].lower() in rec_featured
        c["recently_quick"] = c["full_name"].lower() in rec_quick

    excluded: list[tuple[str, str]] = []

    def drop(c, reason):
        excluded.append((c["full_name"], reason))
        log.info("排除 %s:%s", c["full_name"], reason)

    # 初篩:不需要 API 的條件先過濾,省下呼叫次數
    survivors = []
    for c in cands:
        bad, why = selector.is_noise(c, cfg)
        drop(c, why) if bad else survivors.append(c)

    with ThreadPoolExecutor(max_workers=8) as ex:
        details = list(ex.map(lambda c: fetcher.fetch_repo_detail(c["full_name"], cfg), survivors))

    kept = []
    for c, d in zip(survivors, details):
        trending_desc = c["description_raw"]
        c.update({k: v for k, v in d.items() if k != "full_name"})
        c["description_raw"] = c["description_raw"] or trending_desc
        bad, why = selector.is_noise(c, cfg)
        if bad:
            drop(c, why)
            continue
        c["health"] = selector.health_score(c)
        c["growth_level"], c["growth_reasons"] = selector.growth_anomaly(c, cfg)
        c["anomaly"] = c["growth_level"] == "成長異常"  # 只有成長異常不進精選
        if c["growth_level"]:
            c["health"]["flags"].insert(0, c["growth_level"])
            log.info("%s %s:%s", c["growth_level"], c["full_name"], ";".join(c["growth_reasons"]))
        c["category"] = selector.classify_category(c, cfg)
        c["interest"] = selector.classify_interest(c, cfg["interests"], cfg)
        kept.append(c)
    for c in kept:  # 替代方案(規則 A):先看描述;挑中的專案之後再看 README 第一段
        selector.add_alternative_flag(c, [c["description_raw"]], kept)

    # 分類修正必須在挑選之前,類別上限才會用到修正後的類別
    cat_changes = summarizer.fix_categories(llm, kept, cfg) if llm else {}

    selector.score_candidates(kept, P["days"])
    featured = selector.select_picks(kept, cfg)
    quick = selector.select_quick(kept, featured, cfg)
    with ThreadPoolExecutor(max_workers=8) as ex:
        langs = list(ex.map(lambda c: fetcher.fetch_languages(c["full_name"]), featured + quick))
    for c, lg in zip(featured + quick, langs):
        c["languages"] = lg
    reuse = find_weekly_reuse(featured, P)
    log.info("GitHub API 呼叫次數:%d", fetcher.api_calls)
    return {"stats": stats, "candidates": cands, "kept": kept, "excluded": excluded,
            "featured": featured, "quick": quick, "cat_changes": cat_changes, "reuse": reuse}


def weeks_in_month(month: str) -> list[str]:
    """與該月份有重疊的 ISO 週次。"""
    y, m = map(int, month.split("-"))
    first = date(y, m, 1)
    last = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
    weeks, d = [], first - timedelta(days=first.weekday())
    while d <= last:
        iy, iw, _ = d.isocalendar()
        weeks.append(f"{iy}-W{iw:02d}")
        d += timedelta(days=7)
    return weeks


def find_weekly_reuse(featured: list[dict], P: dict) -> dict[str, dict]:
    """月度:月精選若在該月任一週是週精選(且介紹是 AI 產生),回傳 {repo: 週資料中的專案物件 + reused_from}。"""
    if P["type"] != "monthly":
        return {}
    names = {c["full_name"] for c in featured}
    found = {}
    for w in reversed(weeks_in_month(P["id"])):  # 新的週優先
        path = ROOT / "data" / "weeks" / f"{w}.json"
        if not path.exists():
            continue
        wd = json.loads(path.read_text(encoding="utf-8-sig"))
        for p in wd.get("projects", []):
            if p["full_name"] in names and p["full_name"] not in found and p.get("intro_status") == "ai":
                found[p["full_name"]] = {**p, "reused_from": w}
    return found


# ---------- 中文介紹與存檔 ----------

def write_up(res: dict, cfg: dict, P: dict, llm) -> dict:
    """產生中文介紹,組成期間資料 JSON,存檔並更新 history。llm 為 None 時全部走降級版本(沿用週介紹不受影響)。"""
    featured, quick = res["featured"], res["quick"]
    with ThreadPoolExecutor(max_workers=5) as ex:
        readmes = list(ex.map(lambda c: fetcher.fetch_readme(c["full_name"], cfg["readme_max_chars"]), featured))
    projects = []
    for c, readme in zip(featured, readmes):
        c["readme"] = readme
        selector.add_alternative_flag(c, [selector.first_paragraph(readme)], res["kept"])
        reused = res["reuse"].get(c["full_name"])
        if reused:
            s = {k: copy.deepcopy(reused.get(k)) for k in REUSE_FIELDS}
            s["recommend"] = {k: reused.get("recommend", {}).get(k) for k in ("llm_says", "llm_reason")}
            s["reused_from"] = reused["reused_from"]
            log.info("沿用週介紹 %s:%s", c["full_name"], reused["reused_from"])
        else:
            s = summarizer.summarize(c, cfg, llm)
        h = c["health"]
        projects.append({
            "rank": c["rank"],
            "slot": c["slot"],
            "matched_interest": c["matched_interest"],
            "category": c["category"],
            "category_model": c.get("category_model"),
            "full_name": c["full_name"],
            "url": c["url"],
            "description_raw": c["description_raw"],
            "language": c.get("language"),
            "license": c.get("license"),
            "stars_total": c.get("stars_total"),
            "stars_this_week": c.get("stars_this_week"),
            "stars_this_week_est": int(c["momentum"]) if c.get("stars_this_week") is None else None,
            "health": {"score": h["score"], "last_push_days": h["last_push_days"], "commits_30d": h["commits_30d"],
                       "open_issues": h["open_issues"], "contributors": h["contributors"], "flags": h["flags"]},
            **s,
        })
    for p in projects:
        apply_recommend(p, "featured", cfg)

    add_readme_heads(quick, cfg)
    for c in quick:
        selector.add_alternative_flag(c, [selector.first_paragraph(c.get("readme_head", ""))], res["kept"])
    lines = summarizer.quick_one_liners(llm, quick, cfg)
    quick_picks = []
    for c in quick:
        q = {
            "full_name": c["full_name"],
            "url": c["url"],
            "description_raw": c["description_raw"],
            "language": c.get("language"),
            "category": c["category"],
            "category_model": c.get("category_model"),
            "stars_total": c.get("stars_total"),
            "stars_this_week": c.get("stars_this_week"),
            "stars_this_week_est": int(c["momentum"]) if c.get("stars_this_week") is None else None,
            "health_score": c["health"]["score"],
            "flags": c["health"]["flags"],
        }
        set_quick_fields(q, lines[c["full_name"]])
        apply_recommend(q, "quick", cfg)
        quick_picks.append(q)

    now = datetime.now(TPE)
    data = {
        "period": P["id"],
        "period_type": P["type"],
        "generated_at": now.isoformat(timespec="seconds"),
        # Trending 統計的實際期間:執行時間往前 7 天(週)或 30 天(月)
        "coverage": {"from": (now - timedelta(days=P["days"])).strftime("%Y-%m-%d"), "to": now.strftime("%Y-%m-%d")},
        "source": res["stats"]["source"],
        "candidate_count": res["stats"]["deduped"],
        "excluded_count": len(res["excluded"]),
        "projects": projects,
        "quick_picks": quick_picks,
    }
    if P["type"] == "weekly":
        data["week"] = P["id"]  # 舊欄位,保留相容
    save_period(data, P, cfg)
    selector.update_history(P["history"], P["id"], featured, quick)
    log.info("已更新 %s", P["history"].name)
    return data


def add_readme_heads(items: list[dict], cfg: dict) -> None:
    """速覽批次需要 README 開頭:每個專案 1 次 GitHub API。"""
    n = cfg.get("quick_readme_chars", 2000)
    with ThreadPoolExecutor(max_workers=5) as ex:
        heads = list(ex.map(lambda c: fetcher.fetch_readme(c["full_name"], n), items))
    for c, h in zip(items, heads):
        c["readme_head"] = h


def set_quick_fields(q: dict, v: dict) -> None:
    q.update({"one_liner": v["one_liner"], "risk_flag": v["risk_flag"], "difficulty": v["difficulty"],
              "windows_ok": v["windows_ok"], "needs_paid_api": v["needs_paid_api"],
              "recommend": dict(v["recommend"]), "model": v.get("model"), "one_liner_status": v["status"]})


def apply_recommend(item: dict, tier: str, cfg: dict) -> None:
    """LLM 判斷 + 程式門檻雙重通過:精選給「推薦安裝」徽章(badge),速覽只標「可能適合」(maybe_fit)。"""
    rec = item.setdefault("recommend", {"llm_says": False, "llm_reason": ""})
    rec.pop("badge", None)
    rec.pop("maybe_fit", None)
    if tier == "featured":
        selector.normalize_risks(item)
    passed, fails = selector.recommend_gate(item, cfg, tier)
    rec["gate_passed"] = passed
    rec["gate_fail_reasons"] = fails
    ok = bool(rec.get("llm_says")) and passed
    if tier == "featured":
        rec["badge"] = ok
        rec["has_misuse_risk"] = selector.has_misuse_risk(item)
    else:
        rec["maybe_fit"] = ok
    if not ok:
        why = fails if rec.get("llm_says") else ["LLM 判斷不推薦"] + fails
        log.info("不標示%s %s:%s", "推薦" if tier == "featured" else "可能適合", item["full_name"], ";".join(why))


def save_period(data: dict, P: dict, cfg: dict) -> dict:
    """標記輕量模型產生的項目、轉成台灣繁體,寫入 data/weeks 或 data/months(就地更新 data)。"""
    lite = set(cfg.get("lite_models", []))
    for it in data["projects"] + data["quick_picks"]:
        it["lite_model"] = it.get("model") in lite
    data.update(summarizer.to_tw(data))
    out = P["dir"] / f"{P['id']}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("已寫入 %s", out.relative_to(ROOT))
    return data


def fill_missing(cfg: dict, P: dict, llm) -> dict | None:
    """只補缺項:降級(fallback)的項目重新產生;由輕量模型產生的項目嘗試升級。

    不重新抓候選、不重新分類、不重新挑選;入選名單維持不變。給雲端補跑用(雲端沒有快取)。
    升級時排除輕量模型;非輕量模型都不能用時保留原本內容,不覆蓋成更差的結果。
    """
    path = P["dir"] / f"{P['id']}.json"
    if not path.exists():
        print(f"找不到 {path.relative_to(ROOT)},沒有可補的內容")
        return None
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    lite = set(cfg.get("lite_models", []))

    # 月度:沿用週介紹的精選,一律從對應週資料重新複製最新內容(不呼叫 LLM)
    recopied = refresh_reused(data) if P["type"] == "monthly" else []
    rest = [p for p in data["projects"] if not p.get("reused_from")]
    fb = [p for p in rest if p.get("intro_status") != "ai"]
    up = [p for p in rest if p.get("intro_status") == "ai" and p.get("model") in lite]
    q_fb = [q for q in data["quick_picks"] if q.get("one_liner_status") != "ai" or not q.get("one_liner")]
    q_up = [q for q in data["quick_picks"] if q.get("one_liner_status") == "ai" and q.get("model") in lite]
    if recopied:
        print(f"重新複製週介紹:{'、'.join(recopied)}")
    if not (recopied or fb or up or q_fb or q_up):
        # 雲端補跑靠「data/ 沒有變動」判斷要不要 commit 與部署,所以這裡完全不寫檔
        print("沒有缺項,不需補跑")
        return None
    print(f"需要補的:精選 降級 {len(fb)}、輕量 {len(up)};速覽 降級 {len(q_fb)}、輕量 {len(q_up)}")

    improved = []  # 從降級變成 AI、或從輕量模型升級成功的項目
    for p in fb + up:
        upgrade = p in up
        p["readme"] = fetcher.fetch_readme(p["full_name"], cfg["readme_max_chars"])
        s = summarizer.summarize(p, cfg, llm, force=upgrade, exclude=lite if upgrade else None)
        p.pop("readme")
        if s["intro_status"] != "ai" or (upgrade and s.get("model") in lite):
            log.info("%s,保留原本內容:%s", "升級失敗" if upgrade else "仍無法產生介紹", p["full_name"])
            continue
        p.update(s)
        p.pop("reused_from", None)
        improved.append(p["full_name"])

    if q_fb or q_up:
        batch = q_fb + q_up
        add_readme_heads(batch, cfg)
        # 有降級項目時允許輕量模型(總比降級好);只有升級項目時排除輕量模型
        lines = summarizer.quick_one_liners(llm, batch, cfg, force=bool(q_up), exclude=None if q_fb else lite)
        for q in batch:
            q.pop("readme_head", None)
            v = lines[q["full_name"]]
            upgrade = q in q_up
            if v["status"] != "ai" or (upgrade and v.get("model") in lite):
                log.info("%s,保留原本內容:%s", "升級失敗" if upgrade else "仍無法產生一句話", q["full_name"])
                continue
            set_quick_fields(q, v)
            improved.append(q["full_name"])

    if not (recopied or improved):
        # 沒有任何改善(例如金鑰錯誤或額度用完):不寫檔、不更新 filled_at,workflow 會跳過 commit 與部署
        print("補跑沒有任何項目改善,不寫入資料")
        return None
    print(f"改善 {len(improved)} 項:{'、'.join(improved)}")

    for p in data["projects"]:
        apply_recommend(p, "featured", cfg)
    for q in data["quick_picks"]:
        apply_recommend(q, "quick", cfg)
    data["filled_at"] = datetime.now(TPE).isoformat(timespec="seconds")
    save_period(data, P, cfg)
    build_pages()
    return data


def refresh_reused(data: dict) -> list[str]:
    """把月資料中有 reused_from 的精選,從該週資料重新複製介紹與推薦判斷。回傳更新過的 repo 名稱。"""
    done = []
    for p in data["projects"]:
        week = p.get("reused_from")
        path = ROOT / "data" / "weeks" / f"{week}.json"
        if not week or not path.exists():
            continue
        wd = json.loads(path.read_text(encoding="utf-8-sig"))
        src = next((x for x in wd.get("projects", []) if x["full_name"] == p["full_name"]), None)
        if not src or src.get("intro_status") != "ai":
            continue
        new_rec = {k: (src.get("recommend") or {}).get(k) for k in ("llm_says", "llm_reason")}
        old_rec = {k: (p.get("recommend") or {}).get(k) for k in ("llm_says", "llm_reason")}
        if all(p.get(k) == src.get(k) for k in REUSE_FIELDS) and new_rec == old_rec:
            continue  # 週資料沒有變動
        for k in REUSE_FIELDS:
            p[k] = copy.deepcopy(src.get(k))
        p["recommend"] = new_rec
        done.append(f"{p['full_name']}(← {week},型號 {p.get('model')})")
        log.info("重新複製週介紹 %s ← %s", p["full_name"], week)
    return done


def build_pages() -> None:
    builder.build_all()
    print("網頁已產生:site/index.html、site/monthly.html、site/history.html")


# ---------- 報表 ----------

def _stars(c: dict) -> str:
    if c.get("stars_this_week") is not None:
        return f"+{c['stars_this_week']:,}"
    return f"約 +{int(c['momentum']):,}(估)"


def _near(value, threshold, pct=0.10) -> bool:
    return value is not None and abs(value - threshold) <= abs(threshold) * pct


def print_near_thresholds(res: dict, cfg: dict) -> None:
    """列出距離任一門檻 10% 以內的候選,方便判斷門檻要不要調整。"""
    print("\n【門檻邊界】(距離門檻 10% 以內)")
    rows = []
    for c in res["kept"]:
        stars = c.get("stars_total") or 0
        per_day = stars / max(c.get("age_days") or 1, 1)
        fork_ratio = (c.get("forks") or 0) / max(stars, 1)
        notes = []
        if _near(per_day, cfg["anomaly_stars_per_day"]):
            notes.append(f"每日星數 {per_day:,.0f}(爆發/異常門檻 {cfg['anomaly_stars_per_day']:,})")
        if c.get("growth_level") or per_day >= cfg["anomaly_stars_per_day"] * 0.9:
            if _near(fork_ratio, cfg["burst_min_fork_ratio"]):
                notes.append(f"fork÷star {fork_ratio:.3f}(爆發成長門檻 {cfg['burst_min_fork_ratio']})")
            if _near(c.get("contributors"), cfg["burst_min_contributors"]):
                notes.append(f"貢獻者 {c['contributors']}(爆發成長門檻 {cfg['burst_min_contributors']})")
        if _near(fork_ratio, cfg["anomaly_min_fork_ratio"]):
            notes.append(f"fork÷star {fork_ratio:.4f}(成長異常門檻 {cfg['anomaly_min_fork_ratio']})")
        if _near(c["health"]["score"], cfg["featured_min_health"]):
            notes.append(f"健康度 {c['health']['score']}(精選門檻 {cfg['featured_min_health']})")
        if _near(c["health"]["score"], cfg["recommend_gate"]["min_health"]):
            notes.append(f"健康度 {c['health']['score']}(推薦門檻 {cfg['recommend_gate']['min_health']})")
        if notes:
            rows.append((c["full_name"], notes))
    for name, notes in rows:
        print(f"  - {name}:{';'.join(notes)}")
    if not rows:
        print("  (沒有)")


def print_report(res: dict, P: dict, inspect: list[str], cfg: dict) -> None:
    st, label = res["stats"], P["label"]
    print(f"\n===== {P['id']} 挑選結果(資料來源:{st['source']})=====")
    print("\n【候選來源】")
    for src, n in st["per_source"].items():
        print(f"  {src:<24} {n:>3} 筆")
    print(f"  合計 {st['raw_total']} 筆,去重後 {st['deduped']} 筆")

    ex = res["excluded"]
    print(f"\n【排除】共 {len(ex)} 筆,保留 {len(res['kept'])} 筆。前 10 筆原因:")
    for name, why in ex[:10]:
        print(f"  - {name}:{why}")
    reasons = Counter(why.split(":")[0].split("(")[0].split("「")[0] for _, why in ex)
    print("  排除原因統計:" + "、".join(f"{k} {v}" for k, v in reasons.most_common()))

    print("\n【精選 5 個】")
    for c in res["featured"]:
        h = c["health"]
        reused = res["reuse"].get(c["full_name"])
        print(f"  {c['rank']}. {c['full_name']}" + (f"  ← 可沿用 {reused['reused_from']} 週介紹" if reused else ""))
        print(f"     slot={c['slot']}  category={c['category']}  interest={c['matched_interest'] or '—'}")
        print(f"     健康度 {h['score']}  {label} {_stars(c)}  總星 {c['stars_total']:,}"
              + (f"  標籤:{'、'.join(h['flags'])}" if h["flags"] else ""))

    print("\n【速覽 10 個】")
    for i, c in enumerate(res["quick"], 1):
        h = c["health"]
        print(f"  {i:>2}. {c['full_name']}  [{c['category']}]  健康度 {h['score']}  {label} {_stars(c)}"
              + (f"  標籤:{'、'.join(h['flags'])}" if h["flags"] else ""))
    if P["type"] == "monthly":
        print(f"\n【沿用週介紹】{len(res['reuse'])} 個(檢查的週次:{'、'.join(weeks_in_month(P['id']))})")

    kept = res["kept"]
    print(f"\n【健康度分布】(保留的 {len(kept)} 個候選)")
    buckets = [("0–39", 0, 39), ("40–59", 40, 59), ("60–79", 60, 79), ("80–99", 80, 99), ("100", 100, 100)]
    for lb, lo, hi in buckets:
        n = sum(1 for c in kept if lo <= c["health"]["score"] <= hi)
        print(f"  {lb:<6} {n:>3} 個({n * 100 / max(len(kept), 1):.1f}%)")

    featured_names = {c["full_name"] for c in res["featured"]}
    quick_names = {c["full_name"] for c in res["quick"]}
    for level, note in (("爆發成長", "社群有佐證,照常參與挑選"), ("成長異常", "不進精選")):
        group = [c for c in kept if c["growth_level"] == level]
        print(f"\n【{level}】共 {len(group)} 個({note})")
        for c in group:
            where = "速覽" if c["full_name"] in quick_names else ("精選" if c["full_name"] in featured_names else "未入選")
            print(f"  - {c['full_name']}({where}):{';'.join(c['growth_reasons'])}")
    alts = [(c["full_name"], f) for c in kept for f in c["health"]["flags"] if f.endswith("的替代方案")]
    print(f"\n【替代方案標籤】共 {len(alts)} 個(依描述判斷;入選者另看 README 第一段)")
    for name, f in alts:
        print(f"  - {name}:{f}")

    print_near_thresholds(res, cfg)

    print("\n【類別分布】(全部保留的候選 / 精選 / 速覽)")
    all_c = Counter(c["category"] for c in res["kept"])
    f_c = Counter(c["category"] for c in res["featured"])
    q_c = Counter(c["category"] for c in res["quick"])
    for cat, n in all_c.most_common():
        print(f"  {cat:<10} {n:>3}  /  {f_c.get(cat, 0)}  /  {q_c.get(cat, 0)}")
    print(f"  歸到「其他」:{all_c.get('其他', 0)} 筆")

    for name in inspect:
        print(f"\n【檢查 {name}】")
        hit = [c for c in res["candidates"] if c["full_name"].lower() == name.lower()]
        if not hit:
            print("  不在本次候選中")
            continue
        c = hit[0]
        why = next((w for n, w in ex if n.lower() == name.lower()), None)
        print(f"  來源:{', '.join(c['sources'])}")
        print(f"  總星 {c.get('stars_total')}  {label} +{c.get('stars_this_week')}  建立 {c.get('age_days')} 天"
              f"  近 30 天 commit {c.get('commits_30d')}  open issues {c.get('open_issues')}"
              f"  最後 push {c.get('last_push_days')} 天前  授權 {c.get('license')}")
        print(f"  fork {c.get('forks')}  貢獻者 {'>500(清單太大)' if c.get('contributors_too_large') else c.get('contributors')}"
              f"  近期作者(抽樣){c.get('recent_authors')}  release {c.get('releases')} 次"
              f"(最新 {c.get('last_release_days')} 天前)")
        if why:
            print(f"  結果:排除 — {why}")
        else:
            h = c["health"]
            where = ("精選" if c["full_name"] in featured_names else
                     "速覽" if c["full_name"] in quick_names else "未入選")
            print(f"  結果:保留,{where};健康度 {h['score']}(" +
                  "、".join(f"{k} {v}" for k, v in h["parts"].items()) + ")")
            print(f"  標籤:{'、'.join(h['flags']) or '無'}")
            if c["growth_level"]:
                print(f"  成長判定:{c['growth_level']}——{';'.join(c['growth_reasons'])}")

    print(f"\nGitHub API 呼叫 {fetcher.api_calls} 次")


def print_llm_report(res: dict, data: dict, llm, P: dict) -> None:
    print("\n===== 中文介紹 =====")
    if llm:
        models = Counter(llm.used_models)
        print(f"LLM 計數呼叫 {llm.calls} 次(上限 {llm.max_calls})、實際請求 {llm.requests} 次(硬上限 {llm.hard_cap})、"
              f"暫時性錯誤(503 等){llm.transient} 次")
        print("成功使用的型號:" + ("、".join(f"{m}×{n}" for m, n in models.items()) or "無(全部來自快取或未呼叫)"))
        for kind in ("分類修正", "精選介紹", "速覽批次"):
            rows = [t for t in llm.trace if t[0] == kind]
            sent = [t for t in rows if t[1] != "-"]
            if rows:
                detail = Counter(f"{m} {o}" if m != "-" else o for _, m, o in rows)
                print(f"  {kind}:實際送出 {len(sent)} 次 —— " + "、".join(f"{k}×{n}" for k, n in detail.items()))
            else:
                print(f"  {kind}:0 次(全部來自快取)")
    else:
        print("LLM:未使用(--no-llm),沿用週介紹以外全部為降級版本")
    if res.get("cat_changes"):
        print(f"\n【分類修正】{len(res['cat_changes'])} 個專案改了類別:")
        for name, (old, new) in res["cat_changes"].items():
            print(f"  - {name}:{old} → {new}")
    print("\n【精選】")
    for p in data["projects"]:
        pr = p["practicality"]
        src = f"沿用 {p['reused_from']}" if p.get("reused_from") else p["intro_status"]
        print(f"  {p['rank']}. {p['full_name']} [{p['category']}] ({src}, 型號 {p.get('model')})")
        print(f"     {p['intro']['one_liner']}")
        print(f"     難度 {pr['difficulty']}|Windows {pr['windows_ok']}|GPU {pr['needs_gpu']}|"
              f"付費 API {pr['needs_paid_api']}|寫程式 {pr['needs_coding']}")
        print(f"     {pr['verdict']}")
        for r in p.get("risk_notes") or []:
            print(f"     ⚠【{r['type']}|{r.get('scope')}】{r['note']}")
    print("\n【速覽】")
    for i, q in enumerate(data["quick_picks"], 1):
        warn = "".join(f"  ⚠{f}" for f in q["flags"])
        warn += f"  ⚠{q['risk_flag']}" if q.get("risk_flag") else ""
        print(f"  {i:>2}. {q['full_name']} [{q['category']}] ({q['one_liner_status']}, 型號 {q.get('model')}){warn}")
        print(f"      {q['one_liner']}")

    print("\n【推薦判斷】")
    for tier, items in (("精選", data["projects"]), ("速覽", data["quick_picks"])):
        for it in items:
            r = it.get("recommend") or {}
            if tier == "精選":
                mark = "✅ 推薦安裝" + ("(請看注意事項)" if r.get("has_misuse_risk") else "") if r.get("badge") else "—"
            else:
                mark = "可能適合" if r.get("maybe_fit") else "—"
            print(f"  [{tier}] {it['full_name']}:LLM {'推薦' if r.get('llm_says') else '不推薦'}|"
                  f"門檻 {'通過' if r.get('gate_passed') else '未過'}|{mark}")
            if r.get("gate_fail_reasons"):
                print(f"         未通過:{';'.join(r['gate_fail_reasons'])}")
    print(f"\n已寫入 {P['dir'].relative_to(ROOT)}/{P['id']}.json")


# ---------- 進入點 ----------

def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="GitHub 每週/每月熱門五選")
    ap.add_argument("--period", choices=["weekly", "monthly"], default="weekly", help="週度(預設)或月度")
    ap.add_argument("--month", help="月度期間(YYYY-MM),預設為執行當下的上個月")
    ap.add_argument("--week", help="週度期間(YYYY-Www),預設為本週;補跑舊的一週時使用")
    ap.add_argument("--dry-run", action="store_true", help="只挑專案,不呼叫 LLM、不產生網頁")
    ap.add_argument("--trending-url", help="覆蓋 config 的 Trending 網址(測試備援用)")
    ap.add_argument("--inspect", action="append", default=[], help="印出指定 repo 的判斷細節")
    ap.add_argument("--no-llm", action="store_true", help="不呼叫 LLM,全部使用降級介紹(示範/測試用)")
    ap.add_argument("--refresh-llm", action="store_true", help="忽略本期快取,重新產生精選介紹與速覽(分類沿用快取)")
    ap.add_argument("--fill-missing", action="store_true", help="只補本期 JSON 中降級或輕量模型產生的項目,不重新挑選")
    ap.add_argument("--build-only", action="store_true", help="只用現有資料重新產生網頁,不抓資料、不呼叫 LLM")
    args = ap.parse_args()

    if args.build_only:
        build_pages()
        return

    load_dotenv(ROOT / ".env")
    base_cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    P = make_period(args.period, datetime.now(TPE), args.month, args.week)
    cfg = period_cfg(base_cfg, P)
    if args.trending_url:
        cfg["trending_url"] = args.trending_url

    log_path = setup_logging(P["id"])
    print(f"開始執行 {P['id']}({P['type']}),紀錄檔:{log_path.relative_to(ROOT)}")

    cache_root = ROOT / "data" / "cache"
    llm = None
    if args.dry_run or args.no_llm:
        # 不呼叫 LLM;但本期已有的分類快取照樣套用,讓挑選結果與正式執行一致
        llm = summarizer.LLM(cfg, P["id"], cache_root, enabled=False)
    else:
        llm = summarizer.LLM(cfg, P["id"], cache_root, refresh=args.refresh_llm)
        ok = llm.verify_key()
        print(f"Gemini 金鑰驗證:{'成功' if ok else '失敗(本次全部改用降級介紹)'}"
              f"(金鑰指紋 {summarizer.key_fingerprint(llm.key)})")
        if not ok:
            log.error("Gemini 金鑰驗證失敗,改用降級介紹")
            llm.enabled = False

    if args.fill_missing:
        data = fill_missing(cfg, P, llm)
        if data:
            print_llm_report({}, data, llm, P)
        return

    res = gather(cfg, P, llm)
    print_report(res, P, args.inspect, cfg)
    if args.dry_run:
        return

    # --no-llm:分類快取可用,但介紹與速覽一律降級(不讀介紹快取)
    data = write_up(res, cfg, P, None if args.no_llm else llm)
    print_llm_report(res, data, None if args.no_llm else llm, P)
    build_pages()


if __name__ == "__main__":
    main()
