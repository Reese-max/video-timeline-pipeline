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

## A1（正式環境）

正式服務在 A1 的 `/home/ubuntu/video-timeline-pipeline`（systemd `video-mcp`）。自動部署程式與 unit 放在 `scripts/a1-autodeploy/`，安裝於 `/usr/local/lib/video-mcp-autodeploy/autodeploy.py` 與 `/etc/systemd/system/video-mcp-autodeploy.{service,timer}`，以 root 每 6 小時（主機時區 00／06／12／18 點，A1 為 UTC；隨機延遲 15 分鐘）執行。

流程：`git ls-remote` 取 main SHA → 該 SHA 的 `Video Timeline CI` push run 成功 → 在獨立 candidate checkout 跑完整離線 unittest → 正式 checkout 先 `git fetch` → drain（拒絕新工作，等 queued／running 與執行中的 `video-rss` 歸零）→ checkout 並重啟 → 驗本機 `/health` 與工具清單（不得少工具）、公開 `/health` 的 SHA → 解除 drain。切換後任何失敗都回滾到原 SHA。回滾的 git 部分只做本機 checkout、不再 `git fetch`；但若這次部署改過 requirements，回滾會用 pip 把套件裝回部署前的版本，最後也會驗公開 `/health`，這兩步仍需要網路。

`ls-remote`、CI 查詢與兩處 `git fetch` 遇到暫時錯誤（git 失敗、DNS／socket 錯誤、HTTP 回應不完整、逾時）會間隔 30、60、90 秒重試，共 4 次。結果狀態：
- `UP_TO_DATE`／`DEPLOYED`：已是最新或已部署。
- `DEFERRED`（exit 0，下一輪再試）：該 SHA 的 CI 不是成功（尚未完成、失敗或找不到 run；**CI 失敗也只會每輪安靜地延後**）、drain 等超過 `AUTODEPLOY_DRAIN_SECONDS`（預設 3 小時），或讀不到 `video-rss` 狀態。
- `FAILED`（exit 1）：切換前的錯誤，例如網路重試用完、candidate 測試失敗、正式 checkout 有本機修改，或開始前就有殘留的 drain 旗標；這些情況都不會 drain 或切換。回滾本身失敗時 drain 旗標會留著，之後每輪都會因旗標存在而 `FAILED`，須人工處理。
- `ROLLED_BACK`（exit 1）：切換後檢查失敗，已回滾到原 SHA。

結果在 `/var/lib/video-mcp-autodeploy/last.json`，紀錄用 `journalctl -u video-mcp-autodeploy`；要立即部署可執行 `sudo systemctl start video-mcp-autodeploy.service`。

套件只在 `requirements.txt` 內容改變時才會更新（先存 `pip freeze` 供回滾，再 `pip install -r`）；CI 每次都全新安裝，所以 `>=` 下限沒跟上時，CI 測的是新版、A1 跑的仍是舊版。yt-dlp 幾乎每版都在修網站抓取，因此 `.github/dependabot.yml` 每週檢查一次，有新版就開 PR 調高下限，合併後照一般流程經 CI 與 candidate 測試再上 A1。

自動部署不會更新部署程式本身；部署前跑的測試用的是 repo 裡的副本，不代表 A1 已安裝的版本有同樣行為。修改 `scripts/a1-autodeploy/` 並合併、部署後，在 A1 從已部署的 checkout 先檢查語法再安裝：

```bash
cd /home/ubuntu/video-timeline-pipeline
python3 -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding="utf-8").read(), sys.argv[1])' scripts/a1-autodeploy/autodeploy.py \
  && sudo install -m 755 scripts/a1-autodeploy/autodeploy.py /usr/local/lib/video-mcp-autodeploy/autodeploy.py \
  && sudo install -m 644 scripts/a1-autodeploy/video-mcp-autodeploy.service scripts/a1-autodeploy/video-mcp-autodeploy.timer /etc/systemd/system/ \
  && sudo systemctl daemon-reload
```

回歸測試：`python3 -m unittest discover -s tests -p test_a1_autodeploy.py -v`（只在 Linux 執行，Windows 會跳過）。

### YouTube 出口

YouTube 對雲端 IP（含 A1）一律回「Sign in to confirm you're not a bot」，換 yt-dlp 版本或 player client 都沒用；同一支影片從家用網路不帶 cookie 就抓得到。所以 A1 的 `.env` 設 `YTDLP_YOUTUBE_PROXY`，只有 YouTube 網址（`youtube.com`、`youtu.be` 等，含字幕抓取）會加 `--proxy`，其他網站照舊直連。

出口是家中常開電腦上的 `scripts/youtube-egress/egress_proxy.py`（只用標準庫的 HTTP CONNECT proxy，經 Tailscale 連線）。A1 的 MCP 是公開的，所以它只回應允許的來源 IP（A1 的 Tailscale 位址），只接受 `CONNECT` 到 YouTube／Google 網域的 443 埠，不接受 IP 位址，網域解析到內網、Tailscale 或其他非公開位址也拒絕；不會成為進入家中網路的跳板。在那台電腦以系統管理員身分執行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\youtube-egress\install-windows.ps1 -ListenAddress <該機 Tailscale IP> -AllowClient 100.82.40.33
```

它會建立開機自動執行的排程工作（S4U，不需登入、不存密碼，結束時每分鐘重啟）與只允許 A1 的防火牆規則，紀錄在 `%LOCALAPPDATA%\youtube-egress\egress.log`；`-Uninstall` 移除工作與規則。那台電腦關機或 Tailscale 不通時，YouTube 下載會失敗，其他網站不受影響。

### 監控與告警

`scripts/a1-health/probe.py` 安裝於 `/usr/local/lib/video-mcp-health/probe.py`，由 `video-mcp-health.timer` 每 5 分鐘以 DynamicUser 執行（OCI SDK 在 A1 的 `/opt/video-mcp-health/venv`，不在 `requirements.txt`），用 instance principal 寫 OCI Monitoring 命名空間 `video_mcp_health`：

- `mcp_health`（`check=local` 本機 `/health`、`check=public` 經公開 DNS 打公開網址）：告警 `a1-mcp-unhealthy`（任一 check 為 0 持續 10 分鐘）與 `a1-health-missing`（指標中斷），CRITICAL。
- `deploy_health`：自動部署需要人處理時為 0，告警 `a1-deploy-stuck`（`deploy_health[5m].min() < 1` 持續 15 分鐘，WARNING，每 24 小時重送）。三種情況：
  - 最近一次自動部署是 `FAILED` 或 `ROLLED_BACK`。這個結果會維持到下一次自動部署（最多約 6 小時 15 分），期間持續告警；手動 `--check` 或之後的 `DEFERRED` 會覆寫它。
  - 服務在 drain，但沒有任何部署執行持有 `/run/video-mcp-autodeploy.lock`（從 `/proc/locks` 判斷，涵蓋手動執行，探針本身不拿鎖），例如回滾失敗留下旗標。
  - 正在執行的版本落後 GitHub main 超過 18 小時。起算點是探針**第一次發現落後**的時間，記在 `StateDirectory` 的 `/var/lib/video-mcp-health/behind.json`；之後 main 再有新的 merge 也不會重新起算，部署或追上 main 才重置。涵蓋 main 的 CI 一直失敗、自動部署只記 `DEFERRED` 的情況。18 小時容許正常的最壞情形：剛錯過一輪、下一輪 drain 等滿 3 小時而延後、再下一輪才部署。

  `mcp_health` 先送出後才做部署檢查並另外送 `deploy_health`，所以部署檢查變慢或出錯都不會影響 `mcp_health`。讀不到 `last.json` 或 GitHub（`commits/main`，只取 SHA，每 5 分鐘一次）時該項本輪略過；整個部署檢查出錯時本輪不送 `deploy_health`。

修改後同樣從已部署的 checkout 安裝：

```bash
cd /home/ubuntu/video-timeline-pipeline
python3 -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding="utf-8").read(), sys.argv[1])' scripts/a1-health/probe.py \
  && sudo install -m 644 scripts/a1-health/probe.py /usr/local/lib/video-mcp-health/probe.py \
  && sudo install -m 644 scripts/a1-health/video-mcp-health.service scripts/a1-health/video-mcp-health.timer /etc/systemd/system/ \
  && sudo systemctl daemon-reload
```

回歸測試：`python -m unittest discover -s tests -p test_a1_health_probe.py -v`（以假的 `oci` 模組執行，不連 OCI）。

## 驗收界線

離線 Python／Widget、實際 MCP HTTP 與工具 schema、正式部署與回滾、ChatGPT UI 是不同證據。`/health` 不代表付費影片分析端到端成功。伺服器 SHA／Widget resource URI 也不是 ChatGPT 外掛封裝版本。外部來源的 yt-dlp 下載與所有 redirect 並非本次已完成的完整 SSRF 驗證。
