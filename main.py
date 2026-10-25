"""GitHub 每週熱門五選:主流程。

用法:
    python main.py --dry-run                 只挑專案,不呼叫 LLM、不產生網頁
    python main.py --dry-run --inspect owner/repo   另外印出指定專案的判斷細節
    python main.py                           完整執行:挑選 + 中文介紹,寫入 data/weeks/
    python main.py --no-llm                  不呼叫 LLM,示範降級版本
    python main.py --refresh-llm             忽略本週快取,重新產生介紹
"""
import argparse
import json
import logging
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

import fetcher
import selector
import summarizer

ROOT = Path(__file__).resolve().parent
TPE = timezone(timedelta(hours=8))
log = logging.getLogger("github_weekly")


def setup_logging(week: str) -> Path:
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    path = log_dir / f"{week}_{datetime.now(TPE):%Y%m%d_%H%M%S}.log"
    log.setLevel(logging.INFO)
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.WARNING)
    ch.setFormatter(logging.Formatter("  ⚠ %(message)s"))
    log.addHandler(fh)
    log.addHandler(ch)
    return path


def current_week(now: datetime) -> str:
    y, w, _ = now.isocalendar()
    return f"{y}-W{w:02d}"


def gather(cfg: dict, week: str, llm=None) -> dict:
    """抓候選 → 過濾 → 評分 →(LLM 分類修正)→ 挑選。回傳整理好的結果(還沒有中文介紹)。"""
    cands = fetcher.fetch_candidates(cfg)
    stats = fetcher.fetch_candidates.stats

    hist = selector.load_history(ROOT / "data" / "history.json")
    rec_featured, rec_quick = selector.recent_recommended(hist, week, cfg["exclude_recommended_weeks"])
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
        c["anomaly"] = selector.growth_anomaly(c, cfg)
        if c["anomaly"]:
            c["health"]["flags"].insert(0, "成長異常")
            log.info("成長異常 %s:%s", c["full_name"], ";".join(c["anomaly"]))
        c["category"] = selector.classify_category(c, cfg)
        c["interest"] = selector.classify_interest(c, cfg["interests"], cfg)
        kept.append(c)

    # 分類修正必須在挑選之前,類別上限才會用到修正後的類別
    cat_changes = summarizer.fix_categories(llm, kept, cfg) if llm else {}

    selector.score_candidates(kept)
    featured = selector.select_picks(kept, cfg)
    quick = selector.select_quick(kept, featured, cfg)
    with ThreadPoolExecutor(max_workers=8) as ex:
        langs = list(ex.map(lambda c: fetcher.fetch_languages(c["full_name"]), featured + quick))
    for c, lg in zip(featured + quick, langs):
        c["languages"] = lg
    log.info("GitHub API 呼叫次數:%d", fetcher.api_calls)
    return {"stats": stats, "candidates": cands, "kept": kept, "excluded": excluded,
            "featured": featured, "quick": quick, "cat_changes": cat_changes}


def write_up(res: dict, cfg: dict, week: str, llm) -> dict:
    """產生中文介紹,組成週資料 JSON,存檔並更新 history。llm 為 None 時全部走降級版本。"""
    featured, quick = res["featured"], res["quick"]
    with ThreadPoolExecutor(max_workers=5) as ex:
        readmes = list(ex.map(lambda c: fetcher.fetch_readme(c["full_name"], cfg["readme_max_chars"]), featured))
    projects = []
    for c, readme in zip(featured, readmes):
        c["readme"] = readme
        s = summarizer.summarize(c, cfg, llm)
        h = c["health"]
        projects.append({
            "rank": c["rank"],
            "slot": c["slot"],
            "matched_interest": c["matched_interest"],
            "category": c["category"],
            "full_name": c["full_name"],
            "url": c["url"],
            "description_raw": c["description_raw"],
            "language": c.get("language"),
            "license": c.get("license"),
            "stars_total": c.get("stars_total"),
            "stars_this_week": c.get("stars_this_week"),
            "health": {"score": h["score"], "last_push_days": h["last_push_days"], "commits_30d": h["commits_30d"],
                       "open_issues": h["open_issues"], "contributors": h["contributors"], "flags": h["flags"]},
            **s,
        })

    lines = summarizer.quick_one_liners(llm, quick)
    quick_picks = [{
        "full_name": c["full_name"],
        "url": c["url"],
        "language": c.get("language"),
        "category": c["category"],
        "stars_total": c.get("stars_total"),
        "stars_this_week": c.get("stars_this_week"),
        "health_score": c["health"]["score"],
        "flags": c["health"]["flags"],
        "one_liner": lines[c["full_name"]][0],
        "one_liner_status": lines[c["full_name"]][1],
    } for c in quick]

    week_data = {
        "week": week,
        "generated_at": datetime.now(TPE).isoformat(timespec="seconds"),
        "source": res["stats"]["source"],
        "excluded_count": len(res["excluded"]),
        "projects": projects,
        "quick_picks": quick_picks,
    }
    out = ROOT / "data" / "weeks" / f"{week}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(week_data, ensure_ascii=False, indent=2), encoding="utf-8")
    selector.update_history(ROOT / "data" / "history.json", week, featured, quick)
    log.info("已寫入 %s 並更新 history.json", out.relative_to(ROOT))
    return week_data


def _stars(c: dict) -> str:
    if c.get("stars_this_week") is not None:
        return f"+{c['stars_this_week']:,}"
    return f"約 +{int(c['momentum']):,}(估)"


def print_report(res: dict, week: str, inspect: list[str]) -> None:
    st = res["stats"]
    print(f"\n===== {week} dry-run 結果(資料來源:{st['source']})=====")
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
        print(f"  {c['rank']}. {c['full_name']}")
        print(f"     slot={c['slot']}  category={c['category']}  interest={c['matched_interest'] or '—'}")
        print(f"     健康度 {h['score']}  本週 {_stars(c)}  總星 {c['stars_total']:,}"
              + (f"  警示:{'、'.join(h['flags'])}" if h["flags"] else ""))

    print("\n【速覽 10 個】")
    for i, c in enumerate(res["quick"], 1):
        h = c["health"]
        print(f"  {i:>2}. {c['full_name']}  [{c['category']}]  健康度 {h['score']}  本週 {_stars(c)}"
              + (f"  警示:{'、'.join(h['flags'])}" if h["flags"] else ""))

    kept = res["kept"]
    print(f"\n【健康度分布】(保留的 {len(kept)} 個候選)")
    buckets = [("0–39", 0, 39), ("40–59", 40, 59), ("60–79", 60, 79), ("80–99", 80, 99), ("100", 100, 100)]
    for label, lo, hi in buckets:
        n = sum(1 for c in kept if lo <= c["health"]["score"] <= hi)
        print(f"  {label:<6} {n:>3} 個({n * 100 / max(len(kept), 1):.1f}%)")

    anomalies = [c for c in kept if c["anomaly"]]
    print(f"\n【成長異常】共 {len(anomalies)} 個(不進精選)")
    featured_names = {c["full_name"] for c in res["featured"]}
    quick_names = {c["full_name"] for c in res["quick"]}
    for c in anomalies:
        where = "速覽" if c["full_name"] in quick_names else ("精選" if c["full_name"] in featured_names else "未入選")
        print(f"  - {c['full_name']}({where}):{';'.join(c['anomaly'])}")

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
        print(f"  總星 {c.get('stars_total')}  本週 +{c.get('stars_this_week')}  建立 {c.get('age_days')} 天"
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
            print(f"  警示:{'、'.join(h['flags']) or '無'}")
            if c["anomaly"]:
                print(f"  成長異常觸發條件:{';'.join(c['anomaly'])}")

    print(f"\nGitHub API 呼叫 {fetcher.api_calls} 次")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="GitHub 每週熱門五選")
    ap.add_argument("--dry-run", action="store_true", help="只挑專案,不呼叫 LLM、不產生網頁")
    ap.add_argument("--trending-url", help="覆蓋 config 的 Trending 網址(測試備援用)")
    ap.add_argument("--inspect", action="append", default=[], help="印出指定 repo 的判斷細節")
    ap.add_argument("--no-llm", action="store_true", help="不呼叫 LLM,全部使用降級介紹(示範/測試用)")
    ap.add_argument("--refresh-llm", action="store_true", help="忽略本週快取,強制重新產生 LLM 內容")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    if args.trending_url:
        cfg["trending_url"] = args.trending_url

    week = current_week(datetime.now(TPE))
    log_path = setup_logging(week)
    print(f"開始執行 {week},紀錄檔:{log_path.relative_to(ROOT)}")

    llm = None
    if not args.dry_run and not args.no_llm:
        llm = summarizer.LLM(cfg, week, ROOT / "data" / "cache", refresh=args.refresh_llm)
        ok = llm.verify_key()
        print(f"Gemini 金鑰驗證:{'成功' if ok else '失敗(本次全部改用降級介紹)'}")
        if not ok:
            log.error("Gemini 金鑰驗證失敗,改用降級介紹")
            llm.enabled = False

    res = gather(cfg, week, llm)
    print_report(res, week, args.inspect)
    if args.dry_run:
        return

    week_data = write_up(res, cfg, week, llm)
    print_llm_report(res, week_data, llm)


def print_llm_report(res: dict, wd: dict, llm) -> None:
    print("\n===== 中文介紹 =====")
    if llm:
        models = Counter(llm.used_models)
        print(f"LLM 呼叫 {llm.calls} 次;型號:" + ("、".join(f"{m}×{n}" for m, n in models.items()) or "無(全部來自快取)"))
    else:
        print("LLM:未使用(--no-llm),全部為降級版本")
    if res["cat_changes"]:
        print(f"\n【分類修正】{len(res['cat_changes'])} 個專案改了類別:")
        for name, (old, new) in res["cat_changes"].items():
            print(f"  - {name}:{old} → {new}")
    print("\n【精選】")
    for p in wd["projects"]:
        pr = p["practicality"]
        print(f"  {p['rank']}. {p['full_name']} [{p['category']}] ({p['intro_status']})")
        print(f"     {p['intro']['one_liner']}")
        print(f"     難度 {pr['difficulty']}|Windows {pr['windows_ok']}|GPU {pr['needs_gpu']}|"
              f"付費 API {pr['needs_paid_api']}|寫程式 {pr['needs_coding']}")
        print(f"     {pr['verdict']}")
    print("\n【速覽】")
    for i, q in enumerate(wd["quick_picks"], 1):
        warn = "  ⚠成長異常" if "成長異常" in q["flags"] else ""
        print(f"  {i:>2}. {q['full_name']} [{q['category']}] ({q['one_liner_status']}){warn}")
        print(f"      {q['one_liner']}")
    print(f"\n已寫入 data/weeks/{wd['week']}.json")


if __name__ == "__main__":
    main()
