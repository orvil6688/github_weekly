"""過濾雜訊、健康度評分、類別與興趣分類、配額挑選、推薦紀錄去重。"""
import json
import logging
import re
from datetime import date
from pathlib import Path

log = logging.getLogger("github_weekly")

# 同分時優先選比較「專門」的類別;AI 與 LLM 幾乎什麼都沾得上邊,放最後
CATEGORY_TIEBREAK = ["資安", "金融與交易", "遊戲與圖形", "資料與爬蟲", "效率工具",
                     "前端與網頁", "系統與基礎建設", "開發工具", "AI 與 LLM"]
LANG_CATEGORY = {"CSS": "前端與網頁", "HTML": "前端與網頁", "Vue": "前端與網頁", "Svelte": "前端與網頁",
                 "Astro": "前端與網頁", "Shell": "系統與基礎建設", "Dockerfile": "系統與基礎建設",
                 "HCL": "系統與基礎建設", "Nix": "系統與基礎建設", "GDScript": "遊戲與圖形",
                 "Jupyter Notebook": "資料與爬蟲"}


# ---------- 關鍵字比對 ----------

def _texts(repo: dict) -> tuple[str, str]:
    """回傳 (一般文字, topics 文字),都轉小寫;連字號另外補一份空白版本方便比對。"""
    name = repo["full_name"].split("/")[-1]
    body = f"{name} {name.replace('-', ' ').replace('_', ' ')} {repo.get('description_raw') or ''}".lower()
    topics = " ".join(repo.get("topics") or []).lower()
    topics = f"{topics} {topics.replace('-', ' ')}"
    return body, topics


def _hits(keywords: list[str], body: str, topics: str) -> int:
    score = 0
    for kw in keywords:
        pat = r"(?<![a-z0-9])" + re.escape(kw.lower()) + r"(?![a-z0-9])"
        if re.search(pat, body):
            score += 1
        if re.search(pat, topics):
            score += 2
    return score


def classify_category(repo: dict, cfg: dict) -> str:
    """規則分類(topics、描述、語言)。判斷不出就回傳「其他」;階段 3 再用 LLM 批次修正。"""
    body, topics = _texts(repo)
    scores = {cat: _hits(kws, body, topics) for cat, kws in cfg["category_keywords"].items()}
    best = max(scores.values(), default=0)
    if best > 0:
        tied = [c for c, s in scores.items() if s == best]
        return min(tied, key=lambda c: CATEGORY_TIEBREAK.index(c) if c in CATEGORY_TIEBREAK else 99)
    return LANG_CATEGORY.get(repo.get("language") or "", "其他")


def classify_interest(repo: dict, interests: list[str], cfg: dict | None = None) -> str | None:
    """判斷是否屬於 Orvil 的興趣領域;先用 topics/描述關鍵字,判斷不出再用 LLM。回傳對應的 interest 字串或 None。

    階段 2 只做關鍵字比對;LLM 補判在階段 3 加入。
    """
    kw_map = (cfg or {}).get("interest_keywords", {})
    body, topics = _texts(repo)
    best, best_score = None, 0
    for interest in interests:
        s = _hits(kw_map.get(interest, []), body, topics)
        if s > best_score:
            best, best_score = interest, s
    return best


# ---------- 雜訊過濾 ----------

def is_noise(repo: dict, cfg: dict) -> tuple[bool, str]:
    """回傳 (是否排除, 原因)。原因用繁中,寫入 log。

    只檢查 repo 裡已有的欄位,所以抓細節前可以先做一輪便宜的初篩。
    """
    if repo.get("recently_featured"):
        return True, f"近 {cfg['exclude_recommended_weeks']} 週已精選推薦過"
    name = repo["full_name"].split("/")[-1].lower()
    tokens = set(re.split(r"[-_.\s]+", name))
    for kw in cfg["noise_keywords"]:
        if kw in tokens:
            return True, f"名稱含雜訊關鍵字「{kw}」(清單/教學/題庫類)"
    desc = (repo.get("description_raw") or "").lower()
    for ph in cfg.get("noise_desc_phrases", []):
        if re.search(r"(?<![a-z])" + re.escape(ph) + r"(?![a-z])", desc):
            return True, f"描述含「{ph}」,像是清單或教學內容"
    if "error" in repo:
        return True, repo["error"]
    if "last_push_days" not in repo:  # 還沒抓細節,初篩到此為止
        return False, ""
    if repo.get("archived"):
        return True, "已封存(作者不再維護)"
    if repo.get("fork"):
        return True, "是別人專案的分支(fork)"
    if not repo.get("language"):
        return True, "沒有程式碼,像是純文件"
    if repo["last_push_days"] is not None and repo["last_push_days"] > cfg["stale_days"]:
        return True, f"已 {repo['last_push_days']} 天沒更新(超過 {cfg['stale_days']} 天)"
    week = repo.get("stars_this_week") or 0
    if week >= cfg["suspicious_week_stars"] and repo["commits_30d"] < 3 and (repo.get("open_issues") or 0) < 5:
        return True, (f"疑似刷星:本週 +{week} 星,但近 30 天只有 {repo['commits_30d']} 個 commit、"
                      f"{repo.get('open_issues') or 0} 個 issue")
    return False, ""


# ---------- 健康度 ----------

PERMISSIVE = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "MPL-2.0", "Unlicense", "0BSD", "Zlib"}


def _tier(value, steps: list[tuple[float, int]], default: int = 0) -> int:
    """value 達到門檻就給對應分數;steps 依門檻由高到低排列。"""
    for threshold, pts in steps:
        if value >= threshold:
            return pts
    return default


def health_score(repo: dict) -> dict:
    """回傳 health 物件(score 0–100、各項得分 parts 與 flags)。

    配分:最近更新 20、近 30 天 commit 15、貢獻者 20、fork÷star 20、正式 release 10、授權 15。
    """
    flags = []
    push = repo.get("last_push_days")
    if push is None:
        recency = 0
    else:
        recency = _tier(-push, [(-3, 20), (-7, 17), (-14, 14), (-30, 10), (-90, 5), (-180, 2)])
    if push is not None and push > 30:
        flags.append(f"{push} 天沒更新")

    commits = repo.get("commits_30d") or 0
    activity = _tier(commits, [(100, 15), (50, 12), (20, 9), (10, 6), (3, 3)])
    if commits < 3:
        flags.append("近 30 天幾乎沒有更新")

    contrib = repo.get("contributors")
    missing = repo.get("contributors") is None and not repo.get("contributors_too_large") \
        or repo.get("releases") is None
    if repo.get("contributors_too_large"):
        people = 20
    elif contrib is None:
        people = 10  # 額度不足沒抓到,給中間分
    else:
        people = _tier(contrib, [(200, 20), (100, 17), (50, 14), (20, 10), (10, 7), (5, 4), (2, 2)])
        if contrib <= 1:
            flags.append("只有一位作者")

    stars = repo.get("stars_total") or 0
    fork_ratio = (repo.get("forks") or 0) / max(stars, 1)
    forks = _tier(fork_ratio, [(0.15, 20), (0.10, 16), (0.06, 12), (0.03, 8), (0.01, 4)])

    rel_days = repo.get("last_release_days")
    if repo.get("releases") is None:
        release = 5  # 額度不足沒抓到,給中間分
    elif not repo.get("releases"):
        release = 0
        flags.append("沒有正式版本")
    else:
        release = _tier(-(rel_days if rel_days is not None else 9999), [(-30, 10), (-90, 7), (-365, 4)], default=2)

    lic_id = repo.get("license")
    if not lic_id:
        lic = 0
        flags.append("無授權")
    elif lic_id in PERMISSIVE:
        lic = 15
    elif "GPL" in lic_id:
        lic = 10  # 可用但有義務(修改後散布要開源)
    else:
        lic = 5   # 自訂或少見授權,需要自己看條款

    issues = repo.get("open_issues") or 0
    if issues > 100 and issues / max(stars, 1) > 0.05:
        flags.append("issue 堆積")
    if missing:
        flags.append("資料不完整")

    parts = {"最近更新": recency, "commit 活躍度": activity, "貢獻者": people,
             "fork 比例": forks, "正式版本": release, "授權": lic}
    return {
        "score": sum(parts.values()),
        "parts": parts,
        "last_push_days": push,
        "commits_30d": commits,
        "contributors": "500+" if repo.get("contributors_too_large") else contrib,
        "open_issues": issues,
        "flags": flags,
    }


def growth_anomaly(repo: dict, cfg: dict) -> list[str]:
    """成長異常判斷(只標記,不排除)。回傳觸發的條件說明,沒有觸發就是空清單。"""
    reasons = []
    stars = repo.get("stars_total") or 0
    per_day = stars / max(repo.get("age_days") or 1, 1)
    if per_day > cfg["anomaly_stars_per_day"]:
        reasons.append(f"平均每天 {per_day:,.0f} 星(門檻 {cfg['anomaly_stars_per_day']:,})")
    fork_ratio = (repo.get("forks") or 0) / max(stars, 1)
    if fork_ratio < cfg["anomaly_min_fork_ratio"]:
        reasons.append(f"fork÷star 只有 {fork_ratio:.4f}(門檻 {cfg['anomaly_min_fork_ratio']})")
    authors = repo.get("recent_authors")
    if (repo.get("commits_30d") or 0) > cfg["anomaly_commits_30d"] and authors is not None \
            and authors < cfg["anomaly_min_contributors"]:
        reasons.append(f"近 30 天 {repo['commits_30d']:,} 個 commit,但近期作者只有 {authors} 位"
                       f"(門檻 {cfg['anomaly_commits_30d']:,} commit / {cfg['anomaly_min_contributors']} 人)")
    return reasons


# ---------- 推薦紀錄 ----------

def _week_monday(week: str) -> date:
    y, w = week.split("-W")
    return date.fromisocalendar(int(y), int(w), 1)


def load_history(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"entries": []}


def recent_recommended(history: dict, current_week: str, weeks: int) -> tuple[set, set]:
    """回傳近 N 週內 (精選過的, 速覽過的) repo 名稱集合(小寫)。"""
    cur = _week_monday(current_week)
    featured, quick = set(), set()
    for e in history.get("entries", []):
        if e["week"] == current_week:
            continue  # 同一週重跑時不要把自己排掉
        if 0 <= (cur - _week_monday(e["week"])).days < weeks * 7:
            (featured if e["tier"] == "featured" else quick).add(e["full_name"].lower())
    return featured, quick


def update_history(path: Path, week: str, featured: list[dict], quick: list[dict]) -> None:
    """把本週推薦寫入 history.json;同一週重跑會覆蓋該週舊紀錄。"""
    h = load_history(path)
    h["entries"] = [e for e in h["entries"] if e["week"] != week]
    h["entries"] += [{"week": week, "full_name": p["full_name"], "tier": "featured"} for p in featured]
    h["entries"] += [{"week": week, "full_name": p["full_name"], "tier": "quick"} for p in quick]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(h, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- 挑選 ----------

def _momentum(repo: dict) -> tuple[float, bool]:
    """回傳 (每週新增星數, 是否為估算值)。"""
    if repo.get("stars_this_week") is not None:
        return float(repo["stars_this_week"]), False
    if repo.get("stars_today") is not None:
        return float(repo["stars_today"]) * 7, True
    return (repo.get("stars_total") or 0) / max(repo.get("age_days") or 1, 1) * 7, True


def score_candidates(cands: list[dict]) -> None:
    """替每個候選加上 momentum 與 composite(健康度 50% + 本週熱度排名 50%)。"""
    for c in cands:
        c["momentum"], c["momentum_est"] = _momentum(c)
    ordered = sorted(cands, key=lambda c: c["momentum"])
    n = max(len(ordered) - 1, 1)
    for i, c in enumerate(ordered):
        c["composite"] = 0.5 * c["health"]["score"] + 50 * i / n


def _take(pool, n, chosen, cap, slot, relax=False, skip=None):
    names = {c["full_name"] for c in chosen}
    got = 0
    for c in pool:
        if got >= n:
            break
        if c["full_name"] in names or (skip and skip(c)):
            continue
        same_cat = sum(1 for x in chosen if x["category"] == c["category"])
        if same_cat >= cap and not relax:
            continue
        chosen.append({**c, "slot": slot})
        names.add(c["full_name"])
        got += 1
    return got


def select_picks(candidates: list[dict], cfg: dict) -> list[dict]:
    """依 slots 配額挑 5 個,湊不滿時以熱門補位並標 slot='backfill'。

    同一 category 最多 featured_category_cap 個;補位仍湊不滿才放寬並記 log。
    被標記「成長異常」或健康度低於 featured_min_health 的專案不進精選(仍可進速覽)。
    """
    min_health = cfg.get("featured_min_health", 0)
    candidates = [c for c in candidates if not c.get("anomaly") and c["health"]["score"] >= min_health]
    cap = cfg["featured_category_cap"]
    slots = cfg["slots"]
    hot = sorted([c for c in candidates if c.get("hot_source")],
                 key=lambda c: (c["momentum_est"], -c["momentum"]))
    domain = sorted([c for c in candidates if c.get("interest")], key=lambda c: -c["composite"])
    rising = sorted([c for c in candidates
                     if c.get("age_days", 9999) <= cfg["rising_created_within_days"]
                     and (c.get("stars_total") or 0) <= cfg["rising_max_total_stars"]],
                    key=lambda c: -(c["stars_total"] / c["age_days"]))

    chosen: list[dict] = []
    short = {}
    short["hot"] = slots["hot"] - _take(hot, slots["hot"], chosen, cap, "hot")
    # 領域相關:先盡量讓兩個專案對應不同興趣,不夠再允許重複
    got = _take(domain, slots["domain"], chosen, cap, "domain",
                skip=lambda c: c["interest"] in {x["interest"] for x in chosen if x["slot"] == "domain"})
    got += _take(domain, slots["domain"] - got, chosen, cap, "domain")
    short["domain"] = slots["domain"] - got
    short["rising"] = slots["rising"] - _take(rising, slots["rising"], chosen, cap, "rising")

    need = cfg["featured_count"] - len(chosen)
    if need > 0:
        missing = "、".join(f"{k} 缺 {v}" for k, v in short.items() if v > 0)
        log.info("精選配額湊不滿(%s),由熱門候選補位", missing)
        need -= _take(hot, need, chosen, cap, "backfill")
    if need > 0:
        all_ranked = sorted(candidates, key=lambda c: -c["composite"])
        need -= _take(all_ranked, need, chosen, cap, "backfill")
    if need > 0:
        log.info("精選:同類別上限 %d 個導致湊不滿,放寬上限補 %d 個", cap, need)
        _take(sorted(candidates, key=lambda c: -c["composite"]), need, chosen, cap, "backfill", relax=True)

    for i, c in enumerate(chosen, 1):
        c["rank"] = i
        c["matched_interest"] = c["interest"] if c["slot"] == "domain" else None
    return chosen


def select_quick(candidates: list[dict], featured: list[dict], cfg: dict) -> list[dict]:
    """從精選以外的候選,依健康度與本週熱度挑 quick_count 個;同類別最多 quick_category_cap 個。"""
    taken = {c["full_name"] for c in featured}
    pool = sorted([c for c in candidates if c["full_name"] not in taken and not c.get("recently_quick")],
                  key=lambda c: -c["composite"])
    n, cap = cfg["quick_count"], cfg["quick_category_cap"]
    chosen: list[dict] = []
    got = _take(pool, n, chosen, cap, "quick")
    if got < n:
        log.info("速覽:同類別上限 %d 個導致湊不滿,放寬上限補 %d 個", cap, n - got)
        _take(pool, n - got, chosen, cap, "quick", relax=True)
    return chosen
