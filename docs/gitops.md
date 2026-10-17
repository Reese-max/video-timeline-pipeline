# 安全同步與部署

GitHub `main` 是正式程式碼來源。既有開發目錄、`.env`、`.video-state`、`mcp_jobs`、影音、SQLite 與快取保留在原裝置；同步不會推送開發內容或鏡像執行資料。

## CI

沿用 `.github/workflows/cost-timezone.yml`，名稱為 `Video Timeline CI`。每次 push／main PR 執行 Ubuntu、Windows Python 3.12、Node 24、FFmpeg 完整離線測試；Windows 另跑 Git 同步 fixture。缺少必要工具或測試失敗即失敗。

```powershell
python -X utf8 -B scripts/check_offline.py
pwsh -NoProfile -NonInteractive -File tests/test_windows_sync.ps1
```

## Windows

將 `sync-windows.ps1` 與 `install-windows-sync.ps1` 放在開發目錄外的固定 scripts 目錄後安裝。執行器需要 PowerShell 7；安裝器使用原生 Windows PowerShell 5.1 也可。若系統拒絕註冊工作，須由有權限的使用者安裝，腳本不自行提權。

```powershell
powershell.exe -NoProfile -File scripts\install-windows-sync.ps1 -RepoPath C:\Users\Administrator\video-timeline-pipeline
pwsh -NoProfile -NonInteractive -File scripts\sync-windows.ps1 -RepoPath C:\Users\Administrator\video-timeline-pipeline -CheckOnly
```

每 15 分鐘與登入時執行。Interactive 工作需要使用者登入；S4U 模式可加開機觸發，但必須另驗證該執行情境的 Git 憑證。帳密不寫入工作設定。receipt 預設在 `%LOCALAPPDATA%\VideoTimelinePipeline\git-sync`。

每次只 fetch，之後才檢查：分支為 main、乾淨、沒有 Git 操作、HEAD 是新 main 的祖先，且 fast-forward 不覆蓋 ignored 檔案，才更新。dirty／分支不同／ahead／diverged 只記 `FETCH_ONLY`。90 秒 Git timeout 與檔案鎖避免排程重疊。

## Grok Linux

沿用 sand-desktop MCP supervisor；穩定啟動器從 `/home/box/video-timeline-deploy/current` 選版本，`MCP_ROOT` 永遠指向原本 `/home/box/video-timeline-pipeline`。首次安裝先備份既有啟動器與外部 URL heal 腳本，再把 current 指向原目錄；只更新 supervisor 註冊，不重啟現行程序。

控制器 `deploy_linux.py`、`sync-linux.sh` 安裝在 deploy home；用既有開機入口恢復 sand-desktop 的 MCP 與同步工作。只註冊同步 loop 到易失的 `/tmp` registry 不足以證明開機恢復。不得同時啟用另一套 MCP service。

應用程式 release 自動更新；穩定控制器與共用啟動器的變更需另經備份、測試與受控安裝，不會在自身執行途中換掉控制器。

```bash
python3 scripts/deploy_linux.py --check-only
python3 scripts/deploy_linux.py --prepare-only
bash /home/box/video-timeline-deploy/sync-linux.sh --once
```

每輪完成後等 300 秒再檢查；完整準備期間不重疊。只接受目前 main SHA 的該 CI workflow 最新 push attempt 成功，PR CI 不能替代 main push CI。獨立 bare mirror、release `<sha>` 與 venv 跑完整離線及 HTTP MCP smoke；通過前不接正式 `.env`。

切換前建立 drain，確認 queued／running 都是 0，取得部署鎖、啟動鎖、writer 鎖與 SQLite writer transaction。忙碌時延後；不終止影片工作。使用 pidfd 核對 PID、cwd、MCP argv 後只送 TERM；拒絕強制殺死。原子換 current，驗本機與動態發現的公開 URL、SHA、13 份 runtime SHA-256 與工具 schema。失敗切回原版本並驗證；回滾失敗保留 drain，等待調查。

部署紀錄是 deploy home 的 `last-result.json`、`events.jsonl`、`last-health.json`、`last-rollback.json` 與 release 的 private test log、`prepared.json`。已失敗的完整準備目錄會保留，不自動刪除；須先調查該目錄再安排重試。URL 改變時部署延後並記原因，要求核對 ChatGPT 連線設定，舊 heal 不得直接 kill 或 fuser。

## 驗收界線

離線 Python／Widget、實際 MCP HTTP 與工具 schema、正式部署與回滾、ChatGPT UI 是不同證據。`/health` 不代表付費影片分析端到端成功。伺服器 SHA／Widget resource URI 也不是 ChatGPT 外掛封裝版本。外部來源的 yt-dlp 下載與所有 redirect 並非本次已完成的完整 SSRF 驗證。
