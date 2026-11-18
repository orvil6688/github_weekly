# 專案固定規則

1. **測試資料**：測試一律使用明顯是假的期別（例如 `2099-W01`、`2099-01`，用 `--week`／`--month` 指定），不得使用當週、當月或任何已存在於 `data/` 的期別。測試資料用完即刪（包含被更新的 `data/history*.json`，用 `git checkout` 還原），不得 commit。
2. **中文檔案的讀寫**：只能用編輯工具，或先寫成 UTF-8 檔案再執行（`python -X utf8`）。不得使用 PowerShell 重導向（`>`、`Out-File`、`Set-Content`）或 heredoc。
3. **Gemini 額度**：本機與雲端共用同一把 Gemini 金鑰與每日額度。週日下午到週一下午（以及每月 1 日前一天下午到當天下午），不要在本機做會呼叫 Gemini 的測試；`--dry-run`、`--no-llm`、`--build-only` 不受限制。
4. **GitHub 操作**：push 前先 `git pull --rebase`。任何 GitHub 操作（push、觸發 workflow、改設定）都要先經 Orvil 同意。
5. **工作方式**：依階段執行，每個階段完成後停下回報，不得自行進入下一階段。
