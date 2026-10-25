# GitHub 每週熱門五選

每週從 GitHub 熱門專案中挑出 **精選 5 個**（附繁體中文白話介紹、實用性評估、應用點子、試用指令）和 **速覽 10 個**（一句話說明），產生一個可以直接雙擊打開的網頁。

---

## 怎麼看網頁

用檔案總管打開 `site` 資料夾，雙擊 `index.html` 就可以看，不需要網路。

- 卡片上的「展開完整介紹」可以看詳細內容
- 展開後最下面的「給 Claude Code 的試用指令」有「複製」按鈕，複製後直接貼給 Claude Code 就能試用該專案
- 有 ⚠ 標記的專案代表有版權、法律、費用等需要注意的地方，展開後最上方有說明

## 怎麼手動執行

在這個資料夾開啟終端機（PowerShell），輸入：

```
python main.py
```

約 2–5 分鐘會跑完，結果會寫進 `data/weeks/` 並重新產生 `site/index.html`。

其他常用指令：

| 指令 | 用途 |
|---|---|
| `python main.py --dry-run` | 只看會挑出哪些專案，不產生中文介紹、不產生網頁（不花 Gemini 額度） |
| `python main.py --build-only` | 只用現有資料重新產生網頁（改版面時用，不抓資料、不呼叫 AI） |
| `python main.py --fill-missing` | 本週有專案的中文介紹產生失敗時，只補那幾個，不改變入選名單 |
| `python main.py --refresh-llm` | 重新產生本週的中文介紹（同一週重跑預設會沿用上次結果） |
| `python main.py --no-llm` | 完全不呼叫 AI，只用官方原文（測試用） |

第一次在新電腦上執行前，先安裝需要的套件：

```
python -m pip install -r requirements.txt
```

## 金鑰設定

把 `.env.example` 複製一份改名成 `.env`，填入：

- `GEMINI_API_KEY`：Gemini 金鑰（Google AI Studio 申請）
- `GITHUB_TOKEN`：可以留空，程式會自動向已登入的 gh 取得

`.env` 不會被上傳到 GitHub。

## 怎麼修改設定（config.json）

用記事本或 VS Code 打開 `config.json` 修改，存檔後下次執行就生效。

### 興趣領域

`interests` 是你的興趣清單，影響「領域相關」的挑選和「可以怎麼用在我的專案」的點子：

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

### 其他常調整的

| 設定 | 意思 |
|---|---|
| `stale_days` | 超過幾天沒更新就排除（180） |
| `exclude_recommended_weeks` | 精選過的專案幾週內不再出現（12） |
| `rising_max_total_stars` / `rising_created_within_days` | 「潛力新星」的條件：總星數上限、建立幾天內 |
| `noise_keywords` | 名稱含這些字就當成清單／教學類排除 |
| `trial_sandbox_root` | 試用指令中，Claude Code 操作的資料夾（`D:\sandbox`） |

## 健康度怎麼算

總分 100：最近更新 20、近 30 天 commit 15、貢獻者人數 20、fork÷star 比例 20、正式版本 10、授權 15。
80 分以上綠色、50–79 黃色、50 以下紅色。

「成長異常」代表星數成長速度不太自然（例如平均每天超過 1,000 顆星），這類專案不會進精選，只放在速覽並保留警示。

## 開發注意事項

- **讀寫含中文的檔案時，不得使用 PowerShell 的重導向或文字指令**（`>`、`>>`、`Out-File`、`Set-Content`、`Get-Content -Raw` 再寫回）。Windows PowerShell 5.1 預設用系統編碼讀寫，會把中文弄成亂碼，或在檔案開頭加上 BOM。請改用編輯器、Python（`encoding="utf-8"`），或 Git Bash。
- 所有 `.py`、`.j2`、`.json`、`.md` 檔案一律存成 **UTF-8（無 BOM）**。
- 需要把程式輸出存成檔案時，用 `cmd /c "python main.py > logs\輸出.txt 2>&1"`，由 Python 自己輸出 UTF-8。

## 檔案說明

| 檔案／資料夾 | 內容 |
|---|---|
| `main.py` | 主流程 |
| `fetcher.py` | 抓 GitHub 熱門清單與專案資料 |
| `selector.py` | 過濾、健康度評分、挑選 |
| `summarizer.py` | 呼叫 Gemini 產生中文介紹 |
| `builder.py`、`templates/` | 產生網頁 |
| `data/weeks/` | 每週結果 |
| `site/` | 產生的網頁 |
| `logs/` | 執行紀錄（排除了哪些專案、為什麼，都在這裡） |
