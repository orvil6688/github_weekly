"""呼叫 Gemini 產生繁中介紹:分類修正(批次)、精選介紹(每個 1 次)、速覽一句話(批次)。

- 每次執行最多 llm_max_calls 次「計數呼叫」(503 等暫時性錯誤不計入),另有 llm_hard_cap 次總請求硬上限
- 503 等暫時性錯誤:同一項目依序等 5、15、30 秒重試,仍失敗才換下一個型號
- 額度用完(429)或型號不開放(404)才整次停用該型號;格式錯誤在同型號重試 1 次
- 成功結果快取在 data/cache/<週次>/llm_cache.json,鍵為「repo 名稱::呼叫類型」
- 任何失敗都回傳降級版本,不中斷流程
"""
import json
import logging
import os
import re
import time
from pathlib import Path

import requests
from opencc import OpenCC

log = logging.getLogger("github_weekly")

GEMINI = "https://generativelanguage.googleapis.com/v1beta/models"
NOT_RECOMMEND_WORDS = ["超熱門", "必試", "必備", "推薦", "爆紅", "神器", "必看", "強烈", "最強", "首選"]
UNRESERVED_WORDS = ["非常值得", "強烈推薦", "必試", "必備", "絕對值得", "大推"]
RISK_TYPES = ["版權", "法律", "隱私", "資安", "費用", "其他"]
BACKOFF_SECONDS = [5, 15, 30]
TERM_RULE = "用語統一:英文 agent 一律寫「AI 代理」,不要寫成「AI 代理人」「AI Agent」「智慧代理」「智慧助手」等其他說法。"

_cc = OpenCC("s2twp")
_s2t = OpenCC("s2t")
_t2s = OpenCC("t2s")
# 繁體中文也會用到、但 s2t 會改掉的字;判斷「是不是簡體」時不算它們
AMBIGUOUS = set("斗台才干里面后制志卷系松云余表只准谷丑佣朴")


def _tw_str(s: str) -> str:
    """s2twp 的詞彙表套在已是繁體的文字上會誤改(「權限」→「許可權」、「斗數」→「鬥數」),所以:
    - 沒有簡體字:不動
    - 以簡體為主(例如官方原文):整段 s2twp,含台灣用語轉換
    - 以繁體為主、夾雜少數簡體字:只逐字轉換那些簡體字
    """
    simp = [c for c in s if c not in AMBIGUOUS and _s2t.convert(c) != c]
    if not simp:
        return s
    trad = sum(1 for c in s if _t2s.convert(c) != c)
    if len(simp) > trad:
        return _cc.convert(s)
    return "".join(_s2t.convert(c) if c in simp else c for c in s)


def to_tw(obj, skip_keys=("url", "full_name")):
    """把資料中所有文字轉成台灣繁體,並統一「AI 代理」用語。網址與 repo 名稱不動。"""
    if isinstance(obj, str):
        s = _tw_str(obj)
        s = re.sub(r"AI\s*代理人|AI\s*[Aa]gents?|智慧代理(?:人)?", "AI 代理", s)
        return s
    if isinstance(obj, list):
        return [to_tw(x, skip_keys) for x in obj]
    if isinstance(obj, dict):
        return {k: (v if k in skip_keys else to_tw(v, skip_keys)) for k, v in obj.items()}
    return obj


class FormatError(Exception):
    """回應不是合格 JSON 或欄位不合規格。"""


class Transient(RuntimeError):
    """503 等暫時性錯誤:不計入呼叫上限,退避後重試。"""


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
        self.hard_cap = cfg.get("llm_hard_cap", 20)
        self.calls = 0      # 計數呼叫(不含暫時性錯誤)
        self.requests = 0   # 實際送出的請求總數
        self.transient = 0  # 暫時性錯誤次數(503 等)
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
        # --refresh-llm 只重新產生介紹與速覽;分類(temperature 0)固定沿用快取
        if self.refresh and kind != "category":
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
    def _request(self, model: str, prompt: str, temperature: float) -> str:
        """送出一次 generateContent。回傳文字。

        暫時性錯誤丟 Transient(不計入 calls);額度/不開放丟 ModelGone;金鑰問題丟 PermissionError。
        """
        self.requests += 1
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": temperature},
        }
        try:
            r = requests.post(f"{GEMINI}/{model}:generateContent", headers={"x-goog-api-key": self.key},
                              json=body, timeout=180)
        except requests.RequestException as e:
            self.transient += 1
            raise Transient(f"連線失敗({e.__class__.__name__})") from None
        if r.status_code in (500, 503, 504):
            self.transient += 1
            raise Transient(f"HTTP {r.status_code} {_err_msg(r)}")
        self.calls += 1
        if r.status_code in (401, 403) or (r.status_code == 400 and "API key" in r.text):
            raise PermissionError(f"金鑰無效或沒有權限(HTTP {r.status_code})")
        if r.status_code in (404, 429):
            raise ModelGone(f"HTTP {r.status_code} {_err_msg(r)}")
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

    def ask(self, label: str, prompt: str, validate, temperature: float = 0.4):
        """送出提示詞並用 validate(parsed) 檢查。成功回傳驗證後結果,失敗回傳 None。

        同一項目:格式錯誤重試 1 次(同型號);503 等暫時性錯誤依 BACKOFF_SECONDS 等待後重試同型號,
        等待用完才換下一個型號;額度/不開放錯誤直接換型號。
        """
        if not self.enabled:
            return None
        format_tries = 0
        backoff_step = 0
        skip_here: set[str] = set()  # 退避用完仍暫時性失敗的型號:只在這個項目跳過
        while True:
            alive = [m for m in self.models if m not in self.dead_models and m not in skip_here]
            if not alive:
                log.warning("[%s] 所有型號都無法使用,改用降級版本", label)
                return None
            if self.calls >= self.max_calls:
                log.warning("[%s] 已達本次呼叫上限 %d 次,改用降級版本", label, self.max_calls)
                return None
            if self.requests >= self.hard_cap:
                log.warning("[%s] 已達總請求硬上限 %d 次,改用降級版本", label, self.hard_cap)
                return None
            model = alive[0]
            try:
                text = self._request(model, prompt, temperature)
                self.used_models.append(model)
                log.info("LLM 請求 #%d(計數 %d)[%s] 型號 %s 成功", self.requests, self.calls, label, model)
                return validate(_parse_json(text))
            except PermissionError as e:
                log.error("LLM 請求 #%d [%s] 型號 %s:%s,本次停用 LLM", self.requests, label, model, e)
                self.enabled = False
                return None
            except ModelGone as e:
                log.warning("LLM 請求 #%d [%s] 型號 %s 額度用完或不開放(%s),本次不再使用", self.requests, label, model, e)
                self.dead_models.add(model)
            except Transient as e:
                if backoff_step < len(BACKOFF_SECONDS):
                    wait = BACKOFF_SECONDS[backoff_step]
                    backoff_step += 1
                    log.warning("LLM 請求 #%d [%s] 型號 %s 暫時無法使用(%s),等 %d 秒後重試",
                                self.requests, label, model, e, wait)
                    time.sleep(wait)
                else:
                    log.warning("LLM 請求 #%d [%s] 型號 %s 暫時無法使用(%s),等待用完,這一項改用下一個型號",
                                self.requests, label, model, e)
                    skip_here.add(model)
            except FormatError as e:
                format_tries += 1
                log.warning("LLM 請求 #%d [%s] 型號 %s 格式不合格:%s%s", self.requests, label, model, e,
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

        got = llm.ask("分類修正", prompt, validate, temperature=0) or {}
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
  "risk_notes": [{"type": "版權 | 法律 | 隱私 | 資安 | 費用 | 其他(擇一)", "note": "一句白話說明"}],
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
6. risk_notes:判斷這個專案「合理的使用方式」是否涉及以下風險——未授權內容(影視、音樂、直播來源)、冒用他人聲音或肖像、反向工程或破解、蒐集他人資料、需要系統管理員等高權限、可能產生意外費用(例如自動呼叫付費 API)。每個風險一筆,type 從「版權、法律、隱私、資安、費用、其他」擇一。沒有風險就回傳 [],不得硬湊。
7. practicality.verdict 必須考慮 risk_notes:有版權或法律風險時,不得寫「非常值得」「強烈推薦」這類無保留的推薦,要點出風險。
8. {TERM_RULE}
9. 只輸出一個 JSON 物件,格式如下,不要多餘文字:
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
    # 注意事項:type 不在清單內就歸「其他」
    risks = obj.get("risk_notes")
    if not isinstance(risks, list):
        raise FormatError("risk_notes 不是清單")
    risks = [{"type": r.get("type") if r.get("type") in RISK_TYPES else "其他", "note": str(r["note"]).strip()}
             for r in risks if isinstance(r, dict) and str(r.get("note") or "").strip()]
    if any(r["type"] in ("版權", "法律") for r in risks):
        bad = [w for w in UNRESERVED_WORDS if w in prac["verdict"]]
        if bad:
            raise FormatError(f"有版權/法律風險,verdict 卻寫了「{bad[0]}」")
    return {"intro": intro, "practicality": prac, "ideas_for_me": ideas, "glossary": gl,
            "risk_notes": risks, "trial_goal": str(obj.get("trial_goal") or "資料不足")}


def build_trial_prompt(repo: dict, cfg: dict, trial_goal: str, needs_paid_api: str,
                       risk_notes: list[dict] | None = None) -> str:
    """產生給 Claude Code 的試用指令。安全規則固定寫在程式裡,不交給 LLM 決定。"""
    name = repo["full_name"].split("/")[-1]
    folder = f"{cfg['trial_sandbox_root'].rstrip(chr(92))}\\{name}"
    paid = ("\n- ⚠ 這個專案很可能需要付費 API 金鑰：遇到要填金鑰的步驟就停止，先問我，不要自己申請或填入。"
            if needs_paid_api in ("yes", "optional") else "")
    warn = ""
    if risk_notes:
        lines = "\n".join(f"- 【{r['type']}】{r['note']}" for r in risk_notes)
        warn = f"## ⚠ 注意事項\n{lines}\n\n"
    return f"""{warn}# 任務：試用 GitHub 專案 {repo['full_name']}

專案網址：{repo['url']}
目標：只跑 README 官方的最小範例，確認它能在我的 Windows 電腦上運作。
預期：{trial_goal}

## 規則（全部必須遵守）
- 只在 `{folder}` 這個資料夾裡操作；資料夾不存在就建立。不得讀寫、移動或刪除這個資料夾以外的任何檔案。
- 用 `git clone {repo['url']}.git "{folder}"` 取得程式碼。
- 若官方提供的安裝方式是 .exe、.msi 等安裝程式，或需要安裝到上述資料夾以外的位置：不得自行下載或執行，停止並回報官方下載頁網址、檔案名稱與發布者，由我決定是否自己安裝。
- 若是 Python 專案，一律先在該資料夾建立虛擬環境（`python -m venv .venv`）再安裝套件，不得裝到全域。Node 專案不得使用 `npm install -g`。
- 安裝前先閱讀安裝腳本（install.sh、setup.py、package.json 的 scripts 等）與相依清單。若發現可疑行為——下載不明執行檔、要求系統管理員權限、修改系統設定或登錄檔、改動 PATH 以外的系統環境——立刻停止，把可疑內容回報給我。
- 只執行 README 中官方提供的最小範例。跑出結果後就停止，回報你做了哪些步驟、看到什麼結果；不得自行延伸、修改程式或嘗試其他功能。
- 需要任何付費 API 金鑰（OpenAI、Anthropic、Google 等）時，先停止並詢問我。{paid}
- 遇到錯誤最多嘗試修正 2 次，仍失敗就停止，回報錯誤訊息與你的判斷。

## 回報格式
1. 執行了哪些指令
2. 最小範例的輸出（或截圖說明）
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
            "getting_started": f"請參考官方 README：{repo['url']}#readme",
            "tech_stack": repo.get("language") or nd,
            "why_trending": nd,
        },
        "practicality": {"difficulty": None, "windows_ok": "unknown", "needs_gpu": "unknown",
                         "needs_paid_api": "unknown", "needs_coding": "unknown",
                         "verdict": "AI 介紹產生失敗，以下為官方原文描述，請自行判斷"},
        "ideas_for_me": [],
        "glossary": [],
        "risk_notes": [],
        "trial_prompt": build_trial_prompt(repo, cfg, nd, "unknown"),
        "intro_status": "fallback",
    }


def summarize(repo: dict, cfg: dict, llm: LLM | None = None) -> dict:
    """回傳 intro / practicality / ideas_for_me / glossary / trial_prompt。失敗重試 1 次,仍失敗回傳降級版本,不中斷流程。"""
    if llm is not None:
        hit = llm.cached(repo["full_name"], "intro")
        if hit is not None and "risk_notes" not in hit:
            hit = None  # 舊版快取(沒有注意事項欄位)視為沒有快取
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
                "risk_notes": hit["risk_notes"],
                "trial_prompt": build_trial_prompt(repo, cfg, hit["trial_goal"], hit["practicality"]["needs_paid_api"],
                                                   hit["risk_notes"]),
                "intro_status": "ai",
            }
    return fallback_summary(repo, cfg)


# ---------- 3. 速覽一句話(批次) ----------

def _is_anomaly(r: dict) -> bool:
    flags = r.get("flags") or (r.get("health") or {}).get("flags") or []
    return bool(r.get("anomaly")) or "成長異常" in flags


def quick_one_liners(llm: LLM | None, repos: list[dict]) -> dict[str, dict]:
    """10 個一起送出,一次產生一句話與風險標籤。

    回傳 {full_name: {"one_liner": str, "risk_flag": str | None, "status": "ai" | "fallback"}}。
    """
    out = {}
    todo = []
    for r in repos:
        hit = llm.cached(r["full_name"], "quick") if llm else None
        if isinstance(hit, dict):  # 舊版快取是純文字(沒有 risk_flag),視為沒有快取
            out[r["full_name"]] = {**hit, "status": "ai"}
        else:
            todo.append(r)
    if todo and llm and llm.enabled:
        items = [{"repo": r["full_name"], "desc": (r.get("description_raw") or "")[:300],
                  "topics": (r.get("topics") or [])[:8], "lang": r.get("language"),
                  "growth_anomaly": _is_anomaly(r)} for r in todo]
        prompt = (
            "替下列每個 GitHub 專案寫一句繁體中文(台灣用語)白話說明,讓非工程師一看就懂它是做什麼的,"
            "40 字以內。只根據提供的描述,不得捏造;描述太少就寫「官方描述不足」加上你能確定的部分。\n"
            "growth_anomaly 為 true 的專案:星數成長異常,只能中性描述功能,"
            f"不得使用肯定或推薦語氣(例如:{'、'.join(NOT_RECOMMEND_WORDS)})。\n"
            "另外判斷 risk_flag:若專案的合理用途涉及未授權內容(影視、音樂、直播來源)、冒用他人聲音或肖像、"
            "反向工程或破解、蒐集他人資料、需要高權限、可能產生意外費用,給一個 2–6 字的短標籤"
            "(例如「版權疑慮」「隱私疑慮」「破解疑慮」「可能產生費用」);沒有就填 null,不得硬湊。\n"
            f"{TERM_RULE}\n"
            '輸出 JSON 物件,鍵是 repo 名稱,值是 {"one_liner": "一句話", "risk_flag": "短標籤或 null"},'
            "必須包含每一個 repo。\n\n"
            + json.dumps(items, ensure_ascii=False)
        )
        anomaly_names = {r["full_name"] for r in todo if _is_anomaly(r)}

        def validate(obj):
            if not isinstance(obj, dict):
                raise FormatError("不是 JSON 物件")
            got = {}
            for r in todo:
                v = obj.get(r["full_name"])
                if isinstance(v, dict) and isinstance(v.get("one_liner"), str) and v["one_liner"].strip():
                    flag = v.get("risk_flag")
                    flag = flag.strip() if isinstance(flag, str) and flag.strip() and flag.strip() != "null" else None
                    got[r["full_name"]] = {"one_liner": v["one_liner"].strip(), "risk_flag": flag}
            if len(got) < len(todo):
                raise FormatError(f"只回傳 {len(got)}/{len(todo)} 個")
            for name in anomaly_names:
                bad = [w for w in NOT_RECOMMEND_WORDS if w in got[name]["one_liner"]]
                if bad:
                    raise FormatError(f"{name} 是成長異常專案卻用了推薦語氣「{bad[0]}」")
            return got

        got = llm.ask("速覽一句話", prompt, validate) or {}
        for name, v in got.items():
            llm.store(name, "quick", v)
            out[name] = {**v, "status": "ai"}
    for r in repos:
        if r["full_name"] not in out:
            out[r["full_name"]] = {"one_liner": (r.get("description_raw") or "資料不足")[:120],
                                   "risk_flag": None, "status": "fallback"}
    return out
