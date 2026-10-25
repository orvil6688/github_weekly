"""GitHub 每週熱門五選:主流程。

用法:
    python main.py --dry-run                 只挑專案,不呼叫 LLM、不產生網頁
    python main.py --dry-run --inspect owner/repo   另外印出指定專案的判斷細節
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


def gather(cfg: dict, week: str) -> dict:
    """抓候選 → 過濾 → 評分 → 挑選。回傳整理好的結果(還沒有中文介紹)。"""
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
        c["category"] = selector.classify_category(c, cfg)
        c["interest"] = selector.classify_interest(c, cfg["interests"], cfg)
        kept.append(c)

    selector.score_candidates(kept)
    featured = selector.select_picks(kept, cfg)
    quick = selector.select_quick(kept, featured, cfg)
    log.info("GitHub API 呼叫次數:%d", fetcher.api_calls)
    return {"stats": stats, "candidates": cands, "kept": kept, "excluded": excluded,
            "featured": featured, "quick": quick}


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
        print(f"  {i:>2}. {c['full_name']:<42} {c['category']:<8} 健康度 {c['health']['score']:>3}  本週 {_stars(c)}")

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
        print(f"  結果:{'排除 — ' + why if why else '保留,健康度 ' + str(c['health']['score'])}")

    print(f"\nGitHub API 呼叫 {fetcher.api_calls} 次")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="GitHub 每週熱門五選")
    ap.add_argument("--dry-run", action="store_true", help="只挑專案,不呼叫 LLM、不產生網頁")
    ap.add_argument("--trending-url", help="覆蓋 config 的 Trending 網址(測試備援用)")
    ap.add_argument("--inspect", action="append", default=[], help="印出指定 repo 的判斷細節")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    if args.trending_url:
        cfg["trending_url"] = args.trending_url

    week = current_week(datetime.now(TPE))
    log_path = setup_logging(week)
    print(f"開始執行 {week},紀錄檔:{log_path.relative_to(ROOT)}")

    res = gather(cfg, week)
    print_report(res, week, args.inspect)

    if not args.dry_run:
        print("\n(中文介紹與網頁產生尚未實作,將於階段 3、4 加入)")


if __name__ == "__main__":
    main()
