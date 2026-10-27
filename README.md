# GitHub 每週熱門五選

每週（以及每月）從 GitHub 熱門專案中挑出 **精選 5 個**（附繁體中文白話介紹、實用性評估、應用點子、試用指令）和 **速覽 10 個**（一句話說明），產生一個可以直接雙擊打開的網頁。

---

## 怎麼看網頁

用檔案總管打開 `site` 資料夾，雙擊 `index.html`（最新一週）或 `monthly.html`（最新一個月）就可以看，不需要網路。

- 「展開完整介紹」可以看詳細內容；最下面的「給 Claude Code 的試用指令」有「複製」按鈕，複製後直接貼給 Claude Code 就能試用該專案
- 牛皮紙吊牌「推薦安裝」：AI 判斷值得你安裝，而且通過程式門檻（Windows 可用、免付費 API、難度不高、健康度足夠、沒有主要用途上的版權／法律／資安風險）。吊牌下寫「先看注意事項」時，代表被不當使用才會有風險
- 名稱下方的「注意事項 N」：展開後最上方有說明；左側有細直線的，代表正常用法就有風險
- 速覽的「可能適合」：門檻比精選更嚴（難度 2 以下），只是提示，不是正式推薦
- 頁首的「只看推薦」可以只留下有吊牌的精選

## 怎麼手動執行

在這個資料夾開啟終端機（PowerShell），輸入：

```
python main.py
```

約 2–5 分鐘會跑完，結果會寫進 `data/weeks/` 並重新產生網頁。每月熱門則是：

```
python main.py --period monthly
```

期間是「執行當下的上個月」，結果寫進 `data/months/`。

其他常用指令（都可以加 `--period monthly`）：

| 指令 | 用途 |
|---|---|
| `python main.py --dry-run` | 只看會挑出哪些專案，不產生中文介紹、不產生網頁（不花 Gemini 額度） |
| `python main.py --build-only` | 只用現有資料重新產生全部網頁（改版面時用，不抓資料、不呼叫 AI） |
| `python main.py --fill-missing` | 只補介紹產生失敗、或由輕量模型產生的項目，不改變入選名單 |
| `python main.py --refresh-llm` | 重新產生本期的中文介紹（同一期重跑預設會沿用上次結果） |
| `python main.py --no-llm` | 完全不呼叫 AI，只用官方原文（測試用） |
| `--week 2026-W43`／`--month 2026-10` | 指定要處理的週次或月份（補跑舊的一期時用） |

第一次在新電腦上執行前，先安裝需要的套件：

```
python -m pip install -r requirements.txt
```

## 金鑰設定

把 `.env.example` 複製一份改名成 `.env`，填入：

- `GEMINI_API_KEY`：Gemini 金鑰（Google AI Studio 申請）
- `GITHUB_TOKEN`：可以留空，程式會自動向已登入的 gh 取得

`.env` 不會被上傳到 GitHub。雲端執行時，金鑰放在 GitHub 的 Secrets。

## 雲端自動執行（GitHub Actions）

網頁網址：https://orvil6688.github.io/github_weekly/

| 排程（台灣時間） | cron（UTC） | 做什麼 |
|---|---|---|
| 每週一 08:17 | `17 0 * * 1` | 週完整：挑選本週專案、產生介紹、部署網頁 |
| 每週一 16:47 | `47 8 * * 1` | 週補跑：補齊降級或輕量模型產生的項目；沒有改善就不 commit、不部署 |
| 每月 1 日 10:17 | `17 2 1 * *` | 月完整：挑選上個月的專案 |
| 每月 1 日 16:47 | `47 8 1 * *` | 月補跑 |

補跑排在 16:47，是因為 Gemini 免費額度在美國太平洋時間午夜重置（台灣時間 15:00，冬令時間 16:00），補跑時額度已經重置。GitHub 的排程可能延遲幾分鐘到幾十分鐘，屬於正常現象。

也可以手動執行：repo 頁面 → **Actions** → 左側「GitHub 每週熱門五選」→ **Run workflow** → 選擇模式：

| 模式 | 什麼時候用 |
|---|---|
| **週補跑**（預設） | 平常要升級內容時用：補齊降級或輕量模型產生的項目，入選名單不變 |
| **只重建網頁** | 只改了網頁樣式時用：不抓資料、不呼叫 Gemini、不 commit，直接重新部署 |
| 週完整 | 會重新挑選本週名單，一般不需要手動執行 |
| 月完整／月補跑 | 每月 1 日會自動執行，一般不需要手動執行 |

網站頂部的「推薦紀錄」（`history.html`）列出每一週、每一個月的推薦安裝與精選，點「查看這一週」可以回到當時的完整頁面。

### 額度注意事項

- **本機與雲端共用同一把 Gemini 金鑰，也共用每日免費額度。** 本機測試用掉的額度，雲端當天就不能用。
- **週日下午到週一下午之間，盡量不要在本機做會呼叫 Gemini 的測試**（`python main.py`、`--fill-missing`、`--refresh-llm`），把額度留給週一的自動執行。每月 1 日前一天下午到當天下午也一樣。
- 不會呼叫 Gemini 的指令可以隨時用：`--dry-run`、`--no-llm`、`--build-only`。
- 額度用完時，程式會依序改用備援型號，最後退到輕量模型；當天下午的補跑或之後手動「週補跑」會再嘗試升級。

## 怎麼修改設定（config.json）

用記事本或 VS Code 打開 `config.json` 修改，存檔後下次執行就生效。

### 興趣領域與個人背景

`interests` 是你的興趣清單，影響「領域相關」的挑選和「可以怎麼用在我的專案」的點子；`user_profile` 是給 AI 判斷「推薦安裝」時參考的個人背景。

```json
"interests": [
  "Python 自動化與 bot",
  "股票與量化交易",
  ...
]
```

**新增或改名一個興趣時，記得同時在 `interest_keywords` 加上對應的英文關鍵字**，程式才判斷得出哪些專案屬於這個領域。例如：

```json
"interest_keywords": {
  "股票與量化交易": ["stock", "trading", "quant", "backtest"],
  ...
}
```

### 配額

| 設定 | 意思 | 目前 |
|---|---|---|
| `slots.hot` | 精選中「熱門」的數量 | 2 |
| `slots.domain` | 精選中「領域相關」的數量 | 2 |
| `slots.rising` | 精選中「潛力新星」的數量 | 1 |
| `quick_count` | 速覽的數量 | 10 |
| `featured_category_cap` | 精選中同一類別最多幾個 | 2 |
| `quick_category_cap` | 速覽中同一類別最多幾個 | 4 |
| `featured_min_health` | 健康度低於這個分數不能進精選 | 50 |

`slots` 三個數字加起來要等於 `featured_count`（目前 5）。

### 推薦門檻（recommend_gate）

| 設定 | 意思 |
|---|---|
| `max_difficulty` | 精選難度上限（3）；難度 3 時還要完全不用寫程式（`difficulty_3_requires_coding`） |
| `quick_max_difficulty` | 速覽「可能適合」的難度上限（2） |
| `min_health` | 健康度下限（60） |
| `blocking_risk_types` | 正常用法就有這些風險時不推薦（版權、法律、資安） |

### 其他常調整的

| 設定 | 意思 |
|---|---|
| `stale_days` | 超過幾天沒更新就排除（180） |
| `exclude_recommended_weeks` | 精選過的專案幾週內不再出現（12）；月度用 `monthly.exclude_recommended_months`（3） |
| `rising_max_total_stars` / `rising_created_within_days` | 「潛力新星」的條件：總星數上限、建立幾天內（月度在 `monthly` 裡另外設定） |
| `noise_keywords` | 名稱含這些字就當成清單／教學類排除 |
| `trial_sandbox_root` | 試用指令中，Claude Code 操作的資料夾（例如 `C:\sandbox`，改成你自己的試用資料夾） |
| `llm_model` / `llm_fallback_models` / `lite_models` | 使用的 Gemini 型號與備援順序；`lite_models` 產生的內容之後補跑會嘗試升級 |

## 健康度怎麼算

總分 100：最近更新 20、近 30 天 commit 15、貢獻者人數 20、fork÷star 比例 20、正式版本 10、授權 15。
低於 60 分用紅色顯示。

「成長異常」代表星數成長速度不太自然、又缺乏社群佐證，這類專案不會進精選，只放在速覽並保留警示。
「爆發成長」代表星數暴增，但 fork 比例與貢獻者人數都證明社群是真的，照常參與挑選。

## 開發注意事項

- **讀寫含中文的檔案時，不得使用 PowerShell 的重導向或文字指令**（`>`、`>>`、`Out-File`、`Set-Content`、`Get-Content -Raw` 再寫回），**也不要用 Git Bash 的 heredoc 把中文程式碼交給 Python**。這些方式會用系統編碼讀寫，把中文弄成亂碼或在檔案開頭加上 BOM。請改用編輯器，或寫成 UTF-8 檔案後用 `python -X utf8` 執行。
- 所有 `.py`、`.j2`、`.json`、`.md` 檔案一律存成 **UTF-8（無 BOM）**。
- 需要把程式輸出存成檔案時，用 `cmd /c "python main.py > logs\輸出.txt 2>&1"`，由 Python 自己輸出 UTF-8。

## 檔案說明

| 檔案／資料夾 | 內容 |
|---|---|
| `main.py` | 主流程 |
| `fetcher.py` | 抓 GitHub 熱門清單與專案資料 |
| `selector.py` | 過濾、健康度評分、推薦門檻、挑選 |
| `summarizer.py` | 呼叫 Gemini 產生中文介紹 |
| `builder.py`、`templates/` | 產生網頁 |
| `data/weeks/`、`data/months/` | 每週、每月結果 |
| `data/history.json`、`data/history_monthly.json` | 推薦紀錄（避免重複推薦） |
| `site/` | 產生的網頁 |
| `logs/` | 執行紀錄（排除了哪些專案、為什麼，都在這裡） |
