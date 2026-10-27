# 專案固定規則

- **中文檔案的讀寫**：只能用編輯工具，或先寫成 UTF-8 檔案再執行（`python -X utf8`）。不得使用 PowerShell 重導向（`>`、`Out-File`、`Set-Content`）或 heredoc。
- **Gemini 額度**：本機與雲端共用同一把 Gemini 金鑰與每日額度。週日下午到週一下午（以及每月 1 日前一天下午到當天下午），不要在本機做會呼叫 Gemini 的測試；`--dry-run`、`--no-llm`、`--build-only` 不受限制。
- **版控**：測試用的假資料不得 commit；push 前先 `git pull --rebase`。
- **GitHub 操作**：任何 GitHub 操作（push、觸發 workflow、改設定）都要先經 Orvil 同意。
- **工作方式**：依階段執行，每個階段完成後停下回報，不得自行進入下一階段。
