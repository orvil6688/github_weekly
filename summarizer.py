"""呼叫 Gemini 產生繁中介紹:分類修正(批次)、精選介紹(每個 1 次)、速覽一句話(批次)。

- 每次執行最多 llm_max_calls 次請求(含重試與換型號)
- 額度/可用性錯誤才換下一個型號;格式錯誤在同型號重試 1 次
- 成功結果快取在 data/cache/<週次>/llm_cache.json,鍵為「repo 名稱::呼叫類型」
- 任何失敗都回傳降級版本,不中斷流程
"""
import json
import logging
import os
import re
from pathlib import Path

import requests

log = logging.getLogger("github_weekly")

GEMINI = "https://generativelanguage.googleapis.com/v1beta/models"
NOT_RECOMMEND_WORDS = ["超熱門", "必試", "必備", "推薦", "爆紅", "神器", "必看", "強烈", "最強", "首選"]


class FormatError(Exception):
    """回應不是合格 JSON 或欄位不合規格。"""


class ModelGone(RuntimeError):
    """額度用完(429)或型號不開放(404):本次執行後續都不再用這個型號。"""


def _err_msg(r) -> str:
    try:
        return r.json()["error"]["message"][:120]
    except (ValueError, KeyError, TypeError):
        return ""


class LLM:
    def __init__(self, cfg: dict, week: str, cache_root: Path, enabled: bool = True, refresh: bool = False):
        self.cfg = cfg
        self.key = os.environ.get("GEMINI_API_KEY", "").strip()
        self.enabled = enabled and bool(self.key)
        self.models = [cfg["llm_model"]] + cfg.get("llm_fallback_models", [])
        self.dead_models: set[str] = set()  # 本次執行已確定額度用完或不可用的型號
        self.max_calls = cfg.get("llm_max_calls", 10)
        self.calls = 0
        self.used_models: list[str] = []
        self.refresh = refresh
        self.cache_path = cache_root / week / "llm_cache.json"
        self.cache = {}
        if self.cache_path.exists():
            self.cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
        if enabled and not self.key:
            log.warning("沒有 GEMINI_API_KEY,全部改用降級介紹")

    # ---------- 快取 ----------
    def cached(self, name: str, kind: str):
        if self.refresh:
            return None
        return self.cache.get(f"{name}::{kind}")

    def store(self, name: str, kind: str, value) -> None:
        self.cache[f"{name}::{kind}"] = value
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- 金鑰驗證 ----------
    def verify_key(self) -> bool:
        """用最小請求(讀取型號資訊,不產生內容、不計入呼叫數)確認金鑰可用。"""
        if not self.key:
            return False
        try:
            r = requests.get(f"{GEMINI}/{self.models[0]}", headers={"x-goog-api-key": self.key}, timeout=20)
        except requests.RequestException:
            return False
        return r.ok

    # ---------- 呼叫 ----------
    def _request(self, model: str, prompt: str) -> str:
        """送出一次 generateContent。回傳文字;額度/可用性問題丟 RuntimeError,金鑰問題丟 PermissionError。"""
        self.calls += 1
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4},
        }
        try:
            r = requests.post(f"{GEMINI}/{model}:generateContent", headers={"x-goog-api-key": self.key},
                              json=body, timeout=180)
        except requests.RequestException as e:
            raise RuntimeError(f"連線失敗({e.__class__.__name__})") from None
        if r.status_code in (401, 403) or (r.status_code == 400 and "API key" in r.text):
            raise PermissionError(f"金鑰無效或沒有權限(HTTP {r.status_code})")
        if r.status_code in (404, 429):
            raise ModelGone(f"HTTP {r.status_code} {_err_msg(r)}")
        if r.status_code in (500, 503, 504):
            raise RuntimeError(f"HTTP {r.status_code} {_err_msg(r)}")
        if not r.ok:
            raise FormatError(f"HTTP {r.status_code}")
        data = r.json()
        cands = data.get("candidates") or []
        if not cands:
            raise FormatError("沒有回應內容")
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text.strip():
            raise FormatError(f"回應是空的(finishReason={cands[0].get('finishReason')})")
        return text

    def ask(self, label: str, prompt: str, validate):
        """送出提示詞並用 validate(parsed) 檢查。成功回傳驗證後結果,失敗回傳 None。

        同一項目:格式錯誤重試 1 次(同型號);額度/可用性錯誤換下一個型號(不算格式重試)。
        """
        if not self.enabled:
            return None
        format_tries = 0
        skip_here: set[str] = set()  # 暫時性錯誤(503 等):只在這個項目跳過,下一項目再試
        while True:
            alive = [m for m in self.models if m not in self.dead_models and m not in skip_here]
            if not alive:
                log.warning("[%s] 所有型號都無法使用,改用降級版本", label)
                return None
            if self.calls >= self.max_calls:
                log.warning("[%s] 已達本次呼叫上限 %d 次,改用降級版本", label, self.max_calls)
                return None
            model = alive[0]
            try:
                text = self._request(model, prompt)
                self.used_models.append(model)
                log.info("LLM 呼叫 #%d [%s] 型號 %s", self.calls, label, model)
                result = validate(_parse_json(text))
                return result
            except PermissionError as e:
                log.error("LLM 呼叫 #%d [%s] 型號 %s:%s,本次停用 LLM", self.calls, label, model, e)
                self.enabled = False
                return None
            except ModelGone as e:
                log.warning("LLM 呼叫 #%d [%s] 型號 %s 額度用完或不開放(%s),本次不再使用", self.calls, label, model, e)
                self.dead_models.add(model)
            except RuntimeError as e:
                log.warning("LLM 呼叫 #%d [%s] 型號 %s 暫時無法使用(%s),這一項改用下一個型號", self.calls, label, model, e)
                skip_here.add(model)
            except FormatError as e:
                format_tries += 1
                log.warning("LLM 呼叫 #%d [%s] 型號 %s 格式不合格:%s%s", self.calls, label, model, e,
                            ",重試" if format_tries < 2 else ",放棄")
                if format_tries >= 2:
                    return None


def _parse_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise FormatError(f"不是合格 JSON({e.msg})") from None


# ---------- 1. 分類修正(批次) ----------

def fix_categories(llm: LLM, repos: list[dict], cfg: dict) -> dict[str, tuple[str, str]]:
    """一次送出所有候選,讓 LLM 從 categories 選一個。回傳 {full_name: (舊類別, 新類別)} 只含有變動的。"""
    cats = cfg["categories"]
    todo = [r for r in repos if llm.cached(r["full_name"], "category") is None]
    if todo:
        items = [{"repo": r["full_name"], "desc": (r.get("description_raw") or "")[:200],
                  "topics": (r.get("topics") or [])[:8], "lang": r.get("language")} for r in todo]
        prompt = (
            "你是 GitHub 專案分類員。請替下列每個專案,從固定類別清單中選出最貼切的一個。\n"
            f"類別清單(只能用這些字串,一字不差):{json.dumps(cats, ensure_ascii=False)}\n"
            "判斷原則:看專案「主要是做什麼的」,不是看用到什麼技術。"
            "例如用 AI 做資安稽核 → 資安;後端 Web 框架 → 開發工具;Claude Code 外掛市集 → 開發工具;"
            "真的歸不了類才用「其他」。\n"
            "輸出 JSON 物件,鍵是 repo 名稱,值是類別字串,必須包含每一個 repo。\n\n"
            + json.dumps(items, ensure_ascii=False)
        )

        def validate(obj):
            if not isinstance(obj, dict):
                raise FormatError("不是 JSON 物件")
            names = {r["full_name"] for r in todo}
            got = {k: v for k, v in obj.items() if k in names and v in cats}
            if len(got) < len(names) * 0.8:
                raise FormatError(f"只有 {len(got)}/{len(names)} 個回傳合格類別")
            return got

        got = llm.ask("分類修正", prompt, validate) or {}
        for name, cat in got.items():
            llm.store(name, "category", cat)

    changes = {}
    for r in repos:
        new = llm.cache.get(f"{r['full_name']}::category")
        if new and new != r["category"]:
            changes[r["full_name"]] = (r["category"], new)
            r["category"] = new
        r["category_source"] = "ai" if new else "rule"
    return changes


# ---------- 2. 精選介紹 ----------

INTRO_SCHEMA = """{
  "intro": {
    "one_liner": "一句話說明(30 字內)",
    "problem": "解決什麼問題(2–3 句)",
    "features": ["功能 1", "功能 2", "功能 3"],
    "audience": "適合誰",
    "getting_started": "怎麼開始用(白話,不要貼指令)",
    "tech_stack": "使用技術",
    "why_trending": "為什麼最近紅;看不出就寫「資料不足」"
  },
  "practicality": {
    "difficulty": 1到5的整數(1=下載就能用,5=需要大量程式能力),
    "windows_ok": "yes | no | wsl | unknown",
    "needs_gpu": "yes | no | optional | unknown",
    "needs_paid_api": "yes | no | optional | unknown",
    "needs_coding": "none | light | heavy",
    "verdict": "一句話:非工程師值不值得試"
  },
  "ideas_for_me": [{"interest": "必須是興趣清單中的原字串", "idea": "具體應用點子,2–3 句"}],
  "glossary": [{"term": "名詞", "explain": "一句話白話解釋"}],
  "trial_goal": "README 官方最小範例是什麼、跑起來應該看到什麼(一句話;看不出就寫「資料不足」)"
}"""

ENUMS = {
    "windows_ok": {"yes", "no", "wsl", "unknown"},
    "needs_gpu": {"yes", "no", "optional", "unknown"},
    "needs_paid_api": {"yes", "no", "optional", "unknown"},
    "needs_coding": {"none", "light", "heavy"},
}


def _intro_prompt(repo: dict, cfg: dict) -> str:
    meta = {
        "repo": repo["full_name"],
        "description": repo.get("description_raw"),
        "language": repo.get("language"),
        "languages": repo.get("languages"),
        "topics": repo.get("topics"),
        "license": repo.get("license"),
        "stars_total": repo.get("stars_total"),
        "stars_this_week": repo.get("stars_this_week"),
        "created_days_ago": repo.get("age_days"),
        "health_flags": repo["health"]["flags"],
        "homepage": repo.get("homepage"),
    }
    return f"""你要替一位「非工程師」讀者介紹一個 GitHub 專案。讀者用 Windows,懂一點電腦但不會寫程式。

規則:
1. 全部用繁體中文(台灣用語),白話、具體,以非工程師看得懂為準;英文專有名詞可保留原文。
2. 不知道的寫「資料不足」或 unknown,不得捏造任何功能、數字或用途。
3. practicality 依 README 的安裝需求判斷(作業系統、GPU、是否需要 OpenAI/Anthropic 等付費 API 金鑰、是否要寫程式),看不出就填 unknown。
4. ideas_for_me 只能從下面的興趣清單挑 1–2 個「最相關」的領域,提出具體點子;都不相關就回傳空陣列 [],不得硬湊。interest 欄位必須是清單中的原字串。
5. glossary 只收你在 intro 文字中「實際寫出來」、非工程師可能看不懂的技術名詞,最多 5 個;沒有就回傳 []。
6. 只輸出一個 JSON 物件,格式如下,不要多餘文字:
{INTRO_SCHEMA}

興趣清單:{json.dumps(cfg["interests"], ensure_ascii=False)}

專案資料:
{json.dumps(meta, ensure_ascii=False)}

README(可能已截斷):
<<<README
{repo.get("readme") or "(沒有 README)"}
README>>>"""


def _validate_intro(obj, cfg: dict) -> dict:
    if not isinstance(obj, dict):
        raise FormatError("不是 JSON 物件")
    intro, prac = obj.get("intro"), obj.get("practicality")
    if not isinstance(intro, dict) or not isinstance(prac, dict):
        raise FormatError("缺少 intro 或 practicality")
    for k in ("one_liner", "problem", "audience", "getting_started", "tech_stack", "why_trending"):
        if not isinstance(intro.get(k), str) or not intro[k].strip():
            raise FormatError(f"intro.{k} 缺少或不是文字")
    if not isinstance(intro.get("features"), list) or not intro["features"]:
        raise FormatError("intro.features 不是清單")
    intro["features"] = [str(f) for f in intro["features"]][:6]
    try:
        prac["difficulty"] = int(str(prac.get("difficulty")).strip()[0])
    except (ValueError, IndexError):
        raise FormatError("practicality.difficulty 不是 1–5") from None
    if not 1 <= prac["difficulty"] <= 5:
        raise FormatError("practicality.difficulty 不是 1–5")
    for k, allowed in ENUMS.items():
        v = str(prac.get(k, "")).strip().lower()
        prac[k] = v if v in allowed else "unknown"
    if not isinstance(prac.get("verdict"), str) or not prac["verdict"].strip():
        raise FormatError("practicality.verdict 缺少")
    # 應用點子:只保留興趣清單內的,最多 2 個(不合規的丟掉,不算格式錯誤)
    ideas = obj.get("ideas_for_me") if isinstance(obj.get("ideas_for_me"), list) else []
    ideas = [i for i in ideas if isinstance(i, dict) and i.get("interest") in cfg["interests"] and i.get("idea")][:2]
    # 名詞解釋:只保留真的出現在介紹文字中的,最多 5 個
    text = json.dumps(intro, ensure_ascii=False).lower()
    gl = obj.get("glossary") if isinstance(obj.get("glossary"), list) else []
    gl = [g for g in gl if isinstance(g, dict) and g.get("term") and g.get("explain")
          and str(g["term"]).lower() in text][:5]
    return {"intro": intro, "practicality": prac, "ideas_for_me": ideas, "glossary": gl,
            "trial_goal": str(obj.get("trial_goal") or "資料不足")}


def build_trial_prompt(repo: dict, cfg: dict, trial_goal: str, needs_paid_api: str) -> str:
    """產生給 Claude Code 的試用指令。安全規則固定寫在程式裡,不交給 LLM 決定。"""
    name = repo["full_name"].split("/")[-1]
    folder = f"{cfg['trial_sandbox_root'].rstrip(chr(92))}\\{name}"
    paid = ("\n- ⚠ 這個專案很可能需要付費 API 金鑰:遇到要填金鑰的步驟就停止,先問我,不要自己申請或填入。"
            if needs_paid_api in ("yes", "optional") else "")
    return f"""# 任務:試用 GitHub 專案 {repo['full_name']}

專案網址:{repo['url']}
目標:只跑 README 官方的最小範例,確認它能在我的 Windows 電腦上運作。
預期:{trial_goal}

## 規則(全部必須遵守)
- 只在 `{folder}` 這個資料夾裡操作;資料夾不存在就建立。不得讀寫、移動或刪除這個資料夾以外的任何檔案。
- 用 `git clone {repo['url']}.git "{folder}"` 取得程式碼。
- 若是 Python 專案,一律先在該資料夾建立虛擬環境(`python -m venv .venv`)再安裝套件,不得裝到全域。Node 專案不得使用 `npm install -g`。
- 安裝前先閱讀安裝腳本(install.sh、setup.py、package.json 的 scripts 等)與相依清單。若發現可疑行為——下載不明執行檔、要求系統管理員權限、修改系統設定或登錄檔、改動 PATH 以外的系統環境——立刻停止,把可疑內容回報給我。
- 只執行 README 中官方提供的最小範例。跑出結果後就停止,回報你做了哪些步驟、看到什麼結果;不得自行延伸、修改程式或嘗試其他功能。
- 需要任何付費 API 金鑰(OpenAI、Anthropic、Google 等)時,先停止並詢問我。{paid}
- 遇到錯誤最多嘗試修正 2 次,仍失敗就停止,回報錯誤訊息與你的判斷。

## 回報格式
1. 執行了哪些指令
2. 最小範例的輸出(或截圖說明)
3. 你對「非工程師能不能自己用」的一句話評語"""


def fallback_summary(repo: dict, cfg: dict) -> dict:
    """降級版本:只用官方描述與 README 開頭,不做任何推測。"""
    desc = (repo.get("description_raw") or "").strip()
    readme = repo.get("readme") or ""
    para = ""
    for block in re.split(r"\n\s*\n", readme):
        clean = re.sub(r"<[^>]+>|!\[[^\]]*\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)", r"\1", block).strip()
        if len(clean) > 60 and not clean.startswith(("#", "|", "```")):
            para = clean[:400]
            break
    nd = "資料不足"
    return {
        "intro": {
            "one_liner": desc or para[:120] or nd,
            "problem": para or nd,
            "features": [nd],
            "audience": nd,
            "getting_started": f"請參考官方 README:{repo['url']}#readme",
            "tech_stack": repo.get("language") or nd,
            "why_trending": nd,
        },
        "practicality": {"difficulty": None, "windows_ok": "unknown", "needs_gpu": "unknown",
                         "needs_paid_api": "unknown", "needs_coding": "unknown",
                         "verdict": "AI 介紹產生失敗,以下為官方原文描述,請自行判斷"},
        "ideas_for_me": [],
        "glossary": [],
        "trial_prompt": build_trial_prompt(repo, cfg, nd, "unknown"),
        "intro_status": "fallback",
    }


def summarize(repo: dict, cfg: dict, llm: LLM | None = None) -> dict:
    """回傳 intro / practicality / ideas_for_me / glossary / trial_prompt。失敗重試 1 次,仍失敗回傳降級版本,不中斷流程。"""
    if llm is not None:
        hit = llm.cached(repo["full_name"], "intro")
        if hit is None and llm.enabled:
            hit = llm.ask(f"介紹 {repo['full_name']}", _intro_prompt(repo, cfg), lambda o: _validate_intro(o, cfg))
            if hit is not None:
                llm.store(repo["full_name"], "intro", hit)
        if hit is not None:
            return {
                "intro": hit["intro"],
                "practicality": hit["practicality"],
                "ideas_for_me": hit["ideas_for_me"],
                "glossary": hit["glossary"],
                "trial_prompt": build_trial_prompt(repo, cfg, hit["trial_goal"], hit["practicality"]["needs_paid_api"]),
                "intro_status": "ai",
            }
    return fallback_summary(repo, cfg)


# ---------- 3. 速覽一句話(批次) ----------

def quick_one_liners(llm: LLM | None, repos: list[dict]) -> dict[str, tuple[str, str]]:
    """10 個一起送出。回傳 {full_name: (一句話, "ai" | "fallback")}。"""
    out = {}
    todo = []
    for r in repos:
        hit = llm.cached(r["full_name"], "quick") if llm else None
        if hit:
            out[r["full_name"]] = (hit, "ai")
        else:
            todo.append(r)
    if todo and llm and llm.enabled:
        items = [{"repo": r["full_name"], "desc": (r.get("description_raw") or "")[:300],
                  "topics": (r.get("topics") or [])[:8], "lang": r.get("language"),
                  "growth_anomaly": bool(r.get("anomaly"))} for r in todo]
        prompt = (
            "替下列每個 GitHub 專案寫一句繁體中文(台灣用語)白話說明,讓非工程師一看就懂它是做什麼的,"
            "40 字以內。只根據提供的描述,不得捏造;描述太少就寫「官方描述不足」加上你能確定的部分。\n"
            "growth_anomaly 為 true 的專案:星數成長異常,只能中性描述功能,"
            f"不得使用肯定或推薦語氣(例如:{'、'.join(NOT_RECOMMEND_WORDS)})。\n"
            "輸出 JSON 物件,鍵是 repo 名稱,值是一句話,必須包含每一個 repo。\n\n"
            + json.dumps(items, ensure_ascii=False)
        )
        anomaly_names = {r["full_name"] for r in todo if r.get("anomaly")}

        def validate(obj):
            if not isinstance(obj, dict):
                raise FormatError("不是 JSON 物件")
            got = {}
            for r in todo:
                v = obj.get(r["full_name"])
                if isinstance(v, str) and v.strip():
                    got[r["full_name"]] = v.strip()
            if len(got) < len(todo):
                raise FormatError(f"只回傳 {len(got)}/{len(todo)} 個")
            for name in anomaly_names:
                bad = [w for w in NOT_RECOMMEND_WORDS if w in got[name]]
                if bad:
                    raise FormatError(f"{name} 是成長異常專案卻用了推薦語氣「{bad[0]}」")
            return got

        got = llm.ask("速覽一句話", prompt, validate) or {}
        for name, line in got.items():
            llm.store(name, "quick", line)
            out[name] = (line, "ai")
    for r in repos:
        if r["full_name"] not in out:
            out[r["full_name"]] = ((r.get("description_raw") or "資料不足")[:120], "fallback")
    return out
