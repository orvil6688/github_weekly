"""抓取資料:Trending 熱門清單、Search API 備援與潛力新星、repo 細節、README。"""
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("github_weekly")

API = "https://api.github.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) github_weekly"

_session = requests.Session()
_token = None
_token_loaded = False
_lock = threading.Lock()
api_calls = 0  # 本次執行呼叫 GitHub API 的次數


def _get_token() -> str | None:
    """先讀環境變數 GITHUB_TOKEN(.env 或 Actions),沒有就問 gh CLI。token 只放記憶體,不寫檔不寫 log。"""
    global _token, _token_loaded
    if _token_loaded:
        return _token
    _token_loaded = True
    _token = os.environ.get("GITHUB_TOKEN", "").strip() or None
    source = "環境變數 GITHUB_TOKEN"
    if not _token:
        try:
            out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=15)
            _token = out.stdout.strip() or None
            source = "gh CLI"
        except (OSError, subprocess.SubprocessError):
            _token = None
    if _token:
        log.info("GitHub token 來源:%s", source)
    else:
        log.warning("找不到 GitHub token,改用未登入模式(每小時只有 60 次,可能不夠用)")
    return _token


def _api_get(path: str, params: dict | None = None, accept: str = "application/vnd.github+json"):
    """呼叫 GitHub API,遇到 5xx 或連線錯誤重試 2 次。回傳 Response(呼叫端自行判斷狀態碼)。"""
    global api_calls
    headers = {"Accept": accept, "User-Agent": UA, "X-GitHub-Api-Version": "2022-11-28"}
    token = _get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = path if path.startswith("http") else API + path
    for attempt in range(3):
        with _lock:
            api_calls += 1
        try:
            r = _session.get(url, headers=headers, params=params, timeout=20)
        except requests.RequestException as e:
            log.warning("API 連線失敗 %s:%s", path, e.__class__.__name__)
            time.sleep(1 + attempt)
            continue
        if r.status_code >= 500:
            time.sleep(1 + attempt)
            continue
        if r.status_code == 403 and r.headers.get("X-RateLimit-Remaining") == "0":
            log.error("GitHub API 額度用完:%s", path)
        return r
    return None


# ---------- Trending ----------

def _num(text: str | None) -> int | None:
    m = re.search(r"\d+", (text or "").replace(",", ""))
    return int(m.group()) if m else None


def parse_trending(base_url: str, since: str, language: str | None = None) -> list[dict]:
    """抓一頁 Trending 並解析。失敗時丟出例外,由呼叫端決定怎麼處理。"""
    url = f"{base_url.rstrip('/')}/{language}" if language else base_url
    r = _session.get(url, params={"since": since}, headers={"User-Agent": UA, "Accept-Language": "en-US"}, timeout=20)
    r.raise_for_status()
    rows = BeautifulSoup(r.text, "html.parser").select("article.Box-row")
    items = []
    for a in rows:
        link = a.select_one("h2 a")
        if not link:
            continue
        name = re.sub(r"\s+", "", link.get_text())
        desc = a.select_one("p")
        lang = a.select_one('[itemprop="programmingLanguage"]')
        stars = a.select_one('a[href$="/stargazers"]')
        gain = a.select_one("span.d-inline-block.float-sm-right")
        items.append({
            "full_name": name,
            "description_raw": desc.get_text(strip=True) if desc else "",
            "language": lang.get_text(strip=True) if lang else None,
            "stars_total": _num(stars.get_text()) if stars else None,
            "gain": _num(gain.get_text()) if gain else None,
        })
    if not items:
        raise ValueError("頁面內找不到任何專案(版面可能改了)")
    return items


# ---------- Search API ----------

def _search(q: str, per_page: int = 50) -> list[dict]:
    r = _api_get("/search/repositories", {"q": q, "sort": "stars", "order": "desc", "per_page": per_page})
    if r is None or r.status_code != 200:
        log.warning("Search API 失敗(%s):%s", r.status_code if r is not None else "無回應", q)
        return []
    out = []
    for it in r.json().get("items", []):
        out.append({
            "full_name": it["full_name"],
            "description_raw": it.get("description") or "",
            "language": it.get("language"),
            "stars_total": it.get("stargazers_count"),
        })
    return out


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d")


def fetch_candidates(cfg: dict) -> list[dict]:
    """回傳合併去重後的候選清單(含來源標記 hot_source / rising_source)。Trending 解析失敗時自動改用 Search API,不中斷。

    每筆候選另有 sources(來自哪些榜)、stars_this_week、stars_today。
    來源統計放在 fetch_candidates.stats,供 main 顯示。
    """
    base = cfg["trending_url"]
    pages = [("trending:weekly", "weekly", None), ("trending:daily", "daily", None)]
    pages += [(f"trending:{lang}", "weekly", lang) for lang in cfg.get("trending_languages", [])]

    merged: dict[str, dict] = {}
    stats: dict[str, int] = {}

    def add(item: dict, source: str):
        key = item["full_name"].lower()
        c = merged.setdefault(key, {
            "full_name": item["full_name"],
            "url": f"https://github.com/{item['full_name']}",
            "description_raw": item.get("description_raw", ""),
            "language": item.get("language"),
            "stars_total": item.get("stars_total"),
            "stars_this_week": None,
            "stars_today": None,
            "sources": [],
            "hot_source": None,
            "rising_source": None,
        })
        c["sources"].append(source)
        if not c["description_raw"] and item.get("description_raw"):
            c["description_raw"] = item["description_raw"]
        return c

    trending_total = 0
    for source, since, lang in pages:
        try:
            items = parse_trending(base, since, lang)
        except Exception as e:  # noqa: BLE001 - 任何解析錯誤都不能中斷流程
            log.warning("Trending 抓取失敗 [%s]:%s", source, e)
            stats[source] = 0
            continue
        stats[source] = len(items)
        trending_total += len(items)
        for it in items:
            c = add(it, source)
            c["hot_source"] = "trending"
            if since == "weekly":
                c["stars_this_week"] = max(c["stars_this_week"] or 0, it["gain"] or 0)
            else:
                c["stars_today"] = it["gain"]
        time.sleep(0.5)

    if trending_total == 0:
        log.warning("Trending 全部失敗,改用 GitHub Search API 備援")
        q = f"stars:>1000 pushed:>={_days_ago(7)} created:>={_days_ago(365)}"
        items = _search(q, 50)
        stats["search_api(備援熱門)"] = len(items)
        for it in items:
            add(it, "search_api")["hot_source"] = "search_api"

    # 潛力新星:近 N 天建立、總星數不超過上限
    q = (f"created:>={_days_ago(cfg['rising_created_within_days'])} "
         f"stars:100..{cfg['rising_max_total_stars']}")
    items = _search(q, 30)
    stats["search_api(潛力新星)"] = len(items)
    for it in items:
        add(it, "search_rising")["rising_source"] = "search_api"

    fetch_candidates.stats = {
        "per_source": stats,
        "raw_total": sum(stats.values()),
        "deduped": len(merged),
        "source": "trending" if trending_total else "search_api",
    }
    return list(merged.values())


# ---------- repo 細節 ----------

def fetch_readme(full_name: str, max_chars: int) -> str:
    r = _api_get(f"/repos/{full_name}/readme", accept="application/vnd.github.raw+json")
    if r is None or r.status_code != 200:
        return ""
    return r.text[:max_chars]


def fetch_repo_detail(full_name: str, cfg: dict, with_readme: bool = False) -> dict:
    """回傳 repo 基本資料、語言組成、最後 push、近 30 天 commit 數、issue 數、授權、README(截斷)。

    取不到基本資料時回傳 {"error": 原因}。README 預設不抓(只有挑中的專案才需要)。
    """
    r = _api_get(f"/repos/{full_name}")
    if r is None or r.status_code != 200:
        return {"error": f"無法取得 repo 資料(HTTP {r.status_code if r is not None else '無回應'})"}
    j = r.json()
    now = datetime.now(timezone.utc)
    pushed = datetime.fromisoformat(j["pushed_at"].replace("Z", "+00:00")) if j.get("pushed_at") else None
    created = datetime.fromisoformat(j["created_at"].replace("Z", "+00:00"))

    # 語言組成(百分比,取前 3)
    langs = {}
    lr = _api_get(f"/repos/{full_name}/languages")
    if lr is not None and lr.status_code == 200 and lr.json():
        total = sum(lr.json().values())
        langs = {k: round(v * 100 / total, 1) for k, v in sorted(lr.json().items(), key=lambda x: -x[1])[:3]}

    # 近 30 天 commit 數:per_page=1 時,Link 標頭最後一頁的頁碼就是總數
    commits_30d = 0
    since = (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cr = _api_get(f"/repos/{full_name}/commits", {"since": since, "per_page": 1})
    if cr is not None and cr.status_code == 200:
        m = re.search(r'[?&]page=(\d+)>; rel="last"', cr.headers.get("Link", ""))
        commits_30d = int(m.group(1)) if m else len(cr.json())

    detail = {
        "full_name": j["full_name"],
        "description_raw": j.get("description") or "",
        "language": j.get("language"),
        "languages": langs,
        "topics": j.get("topics", []),
        "license": (j.get("license") or {}).get("spdx_id") if j.get("license") else None,
        "stars_total": j.get("stargazers_count"),
        "forks": j.get("forks_count"),
        "open_issues": j.get("open_issues_count"),
        "archived": j.get("archived", False),
        "fork": j.get("fork", False),
        "created_at": j["created_at"],
        "age_days": max(1, (now - created).days),
        "last_push_days": (now - pushed).days if pushed else None,
        "commits_30d": commits_30d,
        "homepage": j.get("homepage"),
    }
    if detail["license"] == "NOASSERTION":
        detail["license"] = "其他"
    if with_readme:
        detail["readme"] = fetch_readme(full_name, cfg["readme_max_chars"])
    return detail
