# Changelog

## Unreleased

- 變更：yt-dlp 下限從 `2026.8.19` 提高到 `2026.10.21`，並新增 `.github/dependabot.yml`（每週檢查，只管 yt-dlp，有新版就開 PR 提高下限）。原因：A1 自動部署只在 `requirements.txt` 改變時才更新套件，CI 卻每次全新安裝，結果 CI 測的是 2026.10.21、A1 正式環境與 candidate 測試環境都還停在 2026.8.19。

- 新增：A1 健康探針（`scripts/a1-health/`，先原樣匯入 A1 目前安裝的版本）另外回報 `video_mcp_health/deploy_health`，自動部署需要人處理時為 0，搭配 OCI 告警 `a1-deploy-stuck`（WARNING）。判定條件：最近一次自動部署 `FAILED`／`ROLLED_BACK`；服務在 drain 但 `/proc/locks` 顯示沒有部署執行持有部署鎖（涵蓋手動執行）；正在執行的版本從探針第一次發現落後 GitHub main 起超過 18 小時（起算時間存在 unit 新增的 `StateDirectory`，新的 merge 不會重新起算，涵蓋 CI 失敗時自動部署只記 `DEFERRED` 而沒人發現的情況）。`mcp_health` 先送出，部署檢查之後另外送，所以檢查變慢或出錯都不影響原本的告警；讀不到狀態檔或 GitHub 時該項本輪略過。新增 `tests/test_a1_health_probe.py`，`docs/gitops.md` 補監控說明與安裝步驟。

- 變更：A1 的自動部署程式與 systemd unit 納入版控（`scripts/a1-autodeploy/`，先原樣匯入 A1 目前安裝的版本），安裝與流程說明寫進 `docs/gitops.md`，並新增 `tests/test_a1_autodeploy.py`（只在 Linux 執行）。同時把 `http.client.HTTPException`（例如回應被截斷的 `IncompleteRead`）納入網路暫時錯誤的重試。測試涵蓋：重試間隔與次數、非網路錯誤不重試、`ls-remote` 短暫失敗後仍判定最新、CI 未完成直接延後不重試、candidate 與正式 checkout 的 `git fetch` 重試、`git fetch` 在 drain 之前且持續失敗時不 drain、切換與回滾都不再 `git fetch`、CI 查詢收到截斷回應時會重試。部署程式不會被自動部署更新，改動須依 `docs/gitops.md` 在 A1 手動安裝後才生效。

- 修正：Facebook 影片的標題不再帶互動數與作者。yt-dlp 給的 Facebook 標題是「`2K views · 22 reactions | 貼文內文 | 作者`」，原本整串原樣寫進 `manifest.json` 的 `source.title`，連帶出現在報告、影片庫與 Drive 檔名（例如「2K views _ 22 reactions _ …」）。`pipeline.source_title()` 現在只在 `extractor` 為 `facebook*` 時，去掉開頭的互動數（views／plays／reactions／likes／comments／shares 與「次觀看／次播放／個心情／則留言／次分享」，以 `·` 串接、`|` 結尾）與結尾的「` | {uploader}`」，貼文內文完整保留（全文索引仍可搜尋）；去掉後是空字串就沿用原標題。其他平台不受影響。已準備好的工作不會自動改名，重新準備（或 CLI 重跑）時才會套用新標題；已上傳 Drive 的工作仍沿用原本的 Drive 檔名前綴，不會重傳。

- 修正：`media_retention.py` 不再因為「早已被重跑取代」的失敗紀錄永久保留媒體。舊規則只要佇列裡同一個 job 有任何一筆非 `completed` 的紀錄就永遠跳過（`queue_failure_or_review`），例如先被使用者取消、之後同一個 prepare 重跑完成的 job，本機媒體永遠不會清。現在 failed／cancelled／expired／interrupted 的紀錄，只有在之後出現**同一 job、同一請求（`input_hash` 相同，因此也是同一種 operation）、建立時間更晚且已 `completed`** 的紀錄時才不再保護；別種 operation 成功、不同參數的重跑成功、或「先成功後失敗」都仍保護。`queued`／`running` 與沒有 `job_id` 的紀錄維持原本「整次清理中止」的行為。其餘條件（分析完成、品質、Drive 備份重新核對等）不變。

- 修正：遷移腳本的三個 MEDIUM 與三個 LOW（Devin 複審）。**G1 被修復階段擋下的 job 不再在資料夾階段被改寫**：修復階段的 `repair_problem` 現在會把 job 記進 `repair_blocked`，資料夾階段遇到 manifest 指向該資料夾的 job 就整個資料夾 `folder_kept`（reason=`repair_blocked`）、不搬不改寫不丟並計問題，下次執行不會把問題洗掉；而且 `rewrite_local()` 與修復階段在改寫 manifest 前，會逐一 `_get_file` 驗證 manifest 記錄的**所有** file_id（files 與 source_file）：未在垃圾桶、`parents == [共用資料夾]`、sha256 相符、名稱是「{前綴} - {file_name}」，任何一個不符就不改寫、不丟資料夾、計問題（`manifest_verify_failed`）。**G2 重複檔丟垃圾桶改成檔案級保護**：啟動時收集 `protected_file_ids`（所有本機 `drive-manifest.json` 的 `files[*].file_id`／`source_file.file_id`，以及所有 `reports/*/drive-*.json` 的 `file_id`），`trashed_identical_duplicate` 只有在「舊資料夾未被任何 manifest 指到」且「該檔 id 不在 protected_file_ids」時才可丟，否則依原規則視為衝突（整個資料夾不動、計問題、`name_conflict` 的 reason=`protected_file_id`）；`--trash-job` 是使用者明確授權，不受此限，但會 emit 一起被丟掉的 protected file id（`trash_job_protected_files`）。**G3 本機寫入錯誤收斂**：`repair_manifest()`→`rewrite_manifest()`、`mark_manifest_trashed()`（含 `backup()`）、`write_report_state()` 與 `backup()` 本身的 `OSError` 都會被捕捉，分別 emit `manifest_rewrite_failed`／`report_state_write_failed`／`backup_failed`、計問題、把該 job 標成 blocked（資料夾階段 `folder_kept`）並**繼續處理其他 job**；備份失敗視同改寫失敗（沒有備份就不改寫）。

- 修正：遷移腳本的三個 LOW（Devin 複審）。**G4**：資料夾階段結束後，會再對 manifest 已指向共用資料夾的 job 跑一次 `repair_report_states()`，讓「媒體已搬、報告仍在舊資料夾」的中斷窗口一次 `--apply` 就補完新 key 報告狀態檔（同一舊資料夾內多份同名報告也各自補完）。**G5**：區分「manifest 不存在」（孤兒，只算資訊）與「manifest 存在但讀不到／JSON 壞掉／不是 dict」（`manifest_invalid`：計問題，該 job 的所有舊資料夾 `folder_kept`，不搬、不丟）。**G6**：`_find_shared_video_folder` 有多個同名候選時，優先沿用 `drive-video-folder.json` 或任一本機 manifest 的 folder_id 指到的那個，否則維持最早建立者，並在 `drive_storage` 端 print 一行繁中警告、遷移腳本端 emit `multiple_shared_candidates` 並計問題（dry-run 也要顯示）。

- 變更：Google Drive 改用**一個共用「Video Timeline」資料夾**，`GOOGLE_DRIVE_FOLDER_ID`（留白＝根目錄）底下不再每支影片建一個資料夾。所有影片的分段、原片與 A 報告都放在這個資料夾的同一層，檔名是「標題 - job_id - 原檔名」（例如 `第一支影片 - 0123456789ab - segment_000.mp4`、`... - source.mp4`、`... - A_server_analysis.md`）。共用資料夾的檔案 ID 記在 `mcp_jobs/drive-video-folder.json`，**先預留 ID 再建立資料夾**，當機後沿用同一 ID；資料夾不見（404）、已在垃圾桶、不是資料夾、已被移出 `GOOGLE_DRIVE_FOLDER_ID`，或 `GOOGLE_DRIVE_FOLDER_ID` 與狀態檔不符時，都會另配新 ID 重建，不會把影片寫進舊資料夾。`drive-manifest.json` 新增 `drive_prefix`，`files[*]`／`source_file` 新增 `drive_name`；`file_name` 仍維持本機檔名（`backup_complete`、還原與清理流程不變）。舊格式（每 job 資料夾）的 manifest 不再重用，會重新上傳到共用資料夾。A 報告在 Drive 上的檔名同樣帶前綴，並以檔名核對（`_matches_report`），舊格式分段仍用 `A_server_analysis.md`；B 交接文字與合併指令會指名完整 Drive 檔名。舊資料夾用一次性腳本 `scripts/migrate_drive_single_folder.py` 搬移（預設 dry-run、零寫入；`--apply` 才執行，只搬移不刪檔，清空後舊資料夾移入垃圾桶，`--trash-job` 可整個丟進垃圾桶並把 manifest 標成 `drive_trashed`）。

- 修正：遷移腳本的重跑收斂與資料安全。舊流程先丟舊資料夾才改寫 manifest，兩步之間當掉就會留下「指向已丟資料夾」的 manifest 而且永遠補不回來；現在每個舊資料夾的順序是**全部檔案搬移並逐檔核對 → 改寫 manifest 與報告狀態檔 → 最後才把清空的舊資料夾丟垃圾桶**，manifest 改寫失敗時資料夾會保留（`folder_kept`）且算問題、結束碼非零。另外新增**以本機 manifest 為準的修復階段**（每次執行都跑，dry-run 只印計畫）：`status == "drive_ready"` 且 `folder_id` 不是共用資料夾的 job，會逐一 `_get_file` 核對 manifest 記錄的每個 file_id——檔案都已在共用資料夾、SHA-256 相符、檔名是「{前綴} - {file_name}」且前綴一致時，就用這個前綴改寫 manifest 與報告狀態檔；還有檔案在舊資料夾則交給資料夾階段處理；檔案不見／在垃圾桶／在別處則回報問題且不改寫。報告狀態檔也會補寫缺的新 key 狀態檔（已存在且相同就略過，不同就回報不覆寫；名稱不符就略過、不算問題）。真正的失敗（搬移核對失敗、衝突、子資料夾、資料夾未清空、manifest 改寫失敗、報告狀態檔核對失敗或衝突、`--trash-job` 找不到、修復階段發現檔案不見）都會讓結束碼非零；沒有 manifest 的孤兒資料夾、或 manifest 指向別的資料夾的重複 job 舊版資料夾只算資訊。

- 修正：遷移腳本的同名衝突判定。改為**整個舊資料夾一起預檢**：任何一個檔案衝突就一個檔都不搬（`folder_kept`＋問題），不會搬一半。檔名以 `A_server_analysis.md` 結尾的報告檔**不算衝突**（同一個 job 的多份報告版本本來就同名，照常搬入，也修掉「重跑時已搬入的報告被誤判成衝突」）；其他檔案若 SHA-256 完全相同、而且沒有任何本機 manifest 指著該舊資料夾，舊資料夾裡那份會移入垃圾桶（`trashed_identical_duplicate`，只算資訊）視同已處理，不阻擋資料夾清空；其餘才算衝突。`--trash-job` 找不到時改在「沒有舊資料夾就提早結束」之前回報（已 `drive_trashed` 的 job 重跑只算資訊）。舊版資料夾的前綴改用**該資料夾自己的名稱**解析標題，不再沿用同 job 第一個資料夾的標題。

- 修正：共用資料夾只會有一個。狀態檔遺失、毀損、parent 不符，或預留的 ID 在 Drive 上 404 時，`drive_storage._video_folder` 先用 `_list_children(parent)` 找未在垃圾桶、名稱剛好是 `Video Timeline` 的既有資料夾沿用（多個時取 `createdTime` 最早、同時間取 id 最小）並寫回狀態檔，沒有才預留新 ID 建立；已在垃圾桶的共用資料夾不會被 list 出來，因此維持原本「另建新資料夾、絕不寫進舊資料夾」的語意。遷移腳本的 `find_shared` 改用同一套選擇規則（共用 `drive_storage._find_shared_video_folder`）。B 交接文字（`build_drive_handoff_text`／`completed_drive_state`）的檔案清單改顯示 Drive 上的實際檔名（分段時間表仍用本機檔名，兩者以「{drive_name}（{file_name}）」對應）。文件同步：dry-run 說明改成「不寫入，但仍需要 Drive 憑證並會讀取資料夾清單」、補上「`--trash-job` 之後重新準備同一支影片會重新上傳到共用資料夾」、並說明重跑會修復中斷的遷移。

- 修正：影片畫面分析的 token 預算（`MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET`）從 80 萬改成 **36 萬**，因為 80 萬的前提是錯的。2026-10-22 A1 實測（25.5 分鐘影片、MiniMax-M3、`/v1/responses/input_tokens` 估算端點＋實際 chat 對照）確認 MiniMax 對**單一請求的影片輸入**上限約 38.8～39.5 萬 token，與 1,000,000 context 無關：`detail=high` 最後可通過 387,558 token（0.56 fps）、`detail=default` 最後可通過 394,629 token（1.99 fps），再多就被 `HTTP 400 invalid params, 410 (2013)` 拒絕（實際 chat 約 40 萬輸入時 0.3 秒內就被拒）；`max_completion_tokens` 不計入（40 萬輸入＋524,288 輸出仍在 100 萬內卻照樣被拒）。新預算留約 7% 餘裕於最低可通過值。連帶修正：`high`、1 fps 的分段長度從 1140 秒＝19 分鐘變成 **480 秒＝8 分鐘**（單次請求最長約 584 秒＝9.7 分鐘），`default`、1 fps 的分段長度為 **1800 秒＝30 分鐘**（單次請求最長約 2011 秒＝33 分鐘）；`MINIMAX_VIDEO_CHUNKING=off` 時的降 fps／降 `default` 安全網與「單段超預算就不送出」防護都改以新預算判斷（單段防護仍用完整預算，不打折）。新增回歸測試 `tests/test_video_mp4_input_limit.py` 釘住實測邊界、每個 detail 的分段 token、25.5 分鐘影片切成 4 段，以及 `MINIMAX_VIDEO_CHUNKING=off` 時送出的 token 仍在預算內。

- 新增：長片改成**分段分析**（預設開啟），長片不再為了塞進單一請求的影片輸入上限而降低取樣率或畫質。觸發條件是「要求的 fps（`ANALYZE_VIDEO_FPS`，夾在 0.2–5）× 影片長度 × 該 detail 每格 token」超過 36 萬 token 預算（`MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET`）；未超過時維持原本的單次流程，行為完全不變。分段長度為 `floor(預算 × 0.9 ÷ (每格 token × fps) ÷ 60) × 60` 秒（至少 60 秒；`high`、1 fps 約 480 秒＝8 分鐘；`default`、1 fps 約 1800 秒＝30 分鐘），可用 `MINIMAX_VIDEO_CHUNK_SECONDS` 覆寫成更短的正整數秒（不得短於 60 秒，也不得超過依預算算出的上限；非法值即使影片短到不需要分段也會報錯）。切段用 ffmpeg stream copy（`-map 0:v:0 -map 0:a:0? -c copy -f segment -segment_time … -segment_list … -segment_list_type csv -reset_timestamps 1`）切到 `work_dir/minimax-chunks/`，每段的實際起點以 csv 為準（stream copy 在關鍵畫面切開）；csv 驗證失敗或 ffmpeg 失敗直接報錯，不會自行改成重新編碼。每段**至少一次上傳與一次呼叫**（時間覆蓋不足的重試與畫面補漏可能再多 1–2 次呼叫），每段都維持原本要求的 fps 與 detail、不得再降 fps 或 detail，並依序執行（取消／逾時／預算阻擋照常往上拋）；**成本與時間隨段數增加**。分段快取以「來源指紋＋該段起訖＋要求參數」為鍵，中途失敗重跑時已完成的分段直接取用快取、不重送；合併結果另寫入頂層快取（鍵含 `chunked` 與 `chunk_seconds`），下次同一支影片同一設定直接取用。合併時把每段的 `timestamp`、`data.timestamp`（若為數字）加上該段實際起點、`timestamp_original` 保留模型原始回答不加偏移（`chunk_start` 已足以換算）、維持依時間遞增，並在 observation 加上 `chunk_index`（從 1 起算）與 `chunk_start`；合併後的 observation 的 `source_video` 指向原片並另存 `chunk_source`（分段檔名）供追查。合併成功後整個 `minimax-chunks/` 會被刪除（分段檔對續跑沒有價值，重跑一定重切）；失敗時保留，且 `media_retention.py` 的 `MEDIA`／`DERIVED` 樣式新增 `analysis/runs/<64 hex>/vision-video-work/minimax-chunks/` 底下的分段 mp4 與壓縮備援，失敗殘留可被既有清理規則刪除。分段期間 stdout 印「· MiniMax 分段分析 k/n（mm:ss–mm:ss）」。新增環境變數 `MINIMAX_VIDEO_CHUNKING`（`on` 預設／`off`）；`off` 維持上一個 commit 的降 fps／降 detail 行為，非法值以繁中 `RuntimeError` 報錯且不呼叫 MiniMax。MCP sampling 統計新增 `chunked`／`chunk_seconds`／`chunk_count`，分段時另記錄各段方法清單 `analysis_methods` 與 `chunk_compressed`（任一段走壓縮路徑時為 true），`analysis_cache_signature` 也納入這兩個設定，避免切換模式後重用另一模式的結果。

- 修正：分段分析在**每段開始前**先檢查取消與逾時（`ExecutionCancelled`／`DeadlineExceeded`），不再等到下一次請求才發現；被取消或逾時的工作不會再上傳下一段，也不多付一段的錢。

- 修正：分段分析成功合併後刪除整個 `minimax-chunks/`（含 `chunk-*.mp4`、`chunks.csv`、`chunk-*-work/`）。分段檔對續跑沒有價值（`split_video_mp4_into_chunks` 重跑一定先刪舊檔重切），留在原片旁會讓每支長片永久多留約一份原片大小。失敗時保留（下次會重切），且失敗殘留已納入 `media_retention.py` 的清理樣式。

- 修正：`MINIMAX_VIDEO_CHUNK_SECONDS` 覆寫加下限 60 秒（太短的分段只會增加上傳與呼叫次數）；測試需要更短的分段時改用內部常數 `VIDEO_MP4_MIN_CHUNK_SECONDS`，不再靠環境變數繞過下限。

- 修正：`ANALYZE_VIDEO_DETAIL=high` 時每格 token 成本被低估，長片會超出 MiniMax 單一請求的影片輸入上限、整個畫面分析失敗（該上限實測約 39 萬 token，見上方修正；`high`、1 fps 約 9.7 分鐘以上的影片就可能爆）。2026-10-22 A1 實測（同一支 59.2 秒影片、fps=1、MiniMax-M3 的 prompt tokens）：`default` 每格約 179、`high` 每格約 616（3.44 倍），`low` 尚未量測故保守沿用 `default` 的 179。fps 上限改依實際使用的 detail 計算（`video_mp4_fps_for_duration(fps, duration, detail)`；`MINIMAX_VIDEO_TOKENS_PER_FRAME` 保留為 default 值，既有測試意圖不變）。所有呼叫點都帶入實際 detail：`pipeline.analyze_video_mp4`（fps 改在 detail 解析之後計算）、MCP sampling 統計（新增 `sampling.detail_requested`，`sampling.detail` 記錄實際送出的 detail）與畫面空檔補漏（沿用同一組 detail／fps）。若影片長到連 0.2 fps 都會超出預算（`high` 約 49 分鐘以上），該次畫面分析自動改用 `default` 重新計算 fps，stdout 印出一行繁中說明；降為 `default` 後在 0.2 fps 仍超預算時，說明訊息會如實指出「仍會超過預算」並建議改用分段分析（`MINIMAX_VIDEO_CHUNKING=on`）。

- 修正：重新編碼的縮放運算式改成 `trunc(min(L,iw)/2)*2`，寬高兩個維度都強制偶數（libx264 的 yuv420p 不接受奇數尺寸，例如 853x480、1279x719 的奇數寬）；分段驗證階段的 ValueError（例如 copy 產生的清單時間不是數字）現在會被判成可退回的錯誤，改用重新編碼；退回範圍收窄成只有「copy 的 ffmpeg 執行失敗」與「分段驗證失敗」，來源指紋、寫標記等前置錯誤直接往上拋，不再白跑一次重新編碼；清掉分段檔時容忍檔案已被刪除，不會用新的 FileNotFoundError 蓋掉原本的退回原因；copy 與 transcode 一律用 `-avoid_negative_ts disabled`（make_zero 會依來源首個時間戳把切點時間整體平移、與播放時間軸對不上，已改回 disabled）。

- 改進：影片分段預設改成「直接剪開」（`MCP_SEGMENT_MODE=copy`，stream copy），不再整支重新編碼；copy 失敗（ffmpeg 失敗或分段沒通過既有驗證：列數、檔名、時間連續、最後一段接近影片長度）時，同一次呼叫內自動清掉半成品並退回重新編碼，stdout 印出一行繁中原因；取消與逾時照常往上拋，不退回。A1 主機（2 OCPU）實測 25.5 分鐘 VP9 1080x1920：重新編碼（veryfast crf 28）約 14–15 分鐘，stream copy 只要 1.1 秒（13 段、邊界在 120 秒倍數，VP9 分段可正常產生預覽圖）；若必須重新編碼，長邊縮到 1280 為 2.63 倍速、檔案小 39%（現行 1080p 為 1.82 倍速），`ultrafast` 沒有更快且檔案大 3 倍。新增 `MCP_SEGMENT_MAX_LONG_SIDE`（預設 1280，0 = 不縮放；非 0 時必須 ≥ 240 且為偶數，只縮小不放大、維持比例、寬高皆偶數，直式／橫式與手機影片旋轉 metadata 都正確）與 `MCP_SEGMENT_X264_PRESET`（預設 `veryfast`，只接受 `superfast`／`veryfast`／`faster`／`fast`／`medium`）；非法環境變數直接報錯且不呼叫 ffmpeg。copy 模式不做續跑（重切只要幾秒），transcode 模式保留中斷續切；模式或設定改變時舊分段全部重切（記在 `segments/.split-params.json`）。分段可能維持原編碼（例如 VP9，部分瀏覽器／iOS 無法播放）；需要到處都能播放的 H.264 分段請設 `MCP_SEGMENT_MODE=transcode`。

- 新增：Bright Data Facebook `--facebook-brightdata-mode {auto,reels,posts}` 與訂閱／OPML 模式保存；Reels（`gd_lyclm3ey2q6rww027t`）在 auto 且全為 `dead_page` 時，當次切換 Pages Posts（`gd_lkaxegm826bjpoo9m5`），既有訂閱刷新會在送出前先保存 posts 模式，即使後續失敗也不再先試 Reels；新增（含 OPML 匯入）僅於整次成功時建立訂閱，失敗不留半成品，重試沿用回執。兩次 trigger 獨立預留／結算、分開回執；只有送出前額度拒絕可依既有授權走 Apify 備援。
- 新增：posts 本機篩選影片及日期，使用 Facebook 貼文／影片網址而非 CDN；非影片貼文同樣計費。成功保存後以 `facebook_seen_post_ids` 記住含非影片的有效貼文 ID，去重保留最近 500 筆，下次與影片 ID 一起排除，減少重複計費。

- 修正：Facebook／Instagram Bright Data 作者盤點先結算含錯誤的全部 record，再以 `error_count`／`errors` 回報；全部錯誤時 CLI 非零結束、RSS 記錄失敗且不推進 snapshot，部分錯誤保留正常項目，重試沿用回執不重送計費 trigger。

- 新增：Facebook 粉專 Reels 可用 `--facebook-provider brightdata --facebook-max-items N`，結果預算支援訂閱更新與 OPML；與 IG 共用單次提交／snapshot 續查、本機日期篩選及 RSS 付費盤點間隔。`RSS_FACEBOOK_FALLBACK_PROVIDER=apify` 僅在帳本送出前拒絕且 Apify 另獲放行時備援，送出後失敗不另開計費工作。
- 新增：`BRIGHTDATA_MONTHLY_CREDIT_LIMIT`（4500）、`BRIGHTDATA_CREDIT_LEDGER`（預設使用者共用 SQLite）、`BRIGHTDATA_POST_DAILY_LIMIT`（20），原子預留最壞結果筆數、依實際交付含 error records 結算，未知結果保留預留；`--brightdata-usage` 輸出月／日用量 JSON。免費層每月 5,000 credits，由 Scraper／SERP／Unlocker／Browser 共用，每月 1 日 UTC 重置且不累積，無儲值用完即停止。
- 新增：`BRIGHTDATA_ALLOWED_POST_URLS=*` 允許既有驗證範圍內任何公開單篇貼文，仍需 `allow_paid=true`；共用月額度或每日上限擋下時回 `quota_exhausted`／`BRIGHTDATA_QUOTA` 與 `resets_at`，到期前禁止重試或換網址繞過。`RSS_PAID_DISCOVERY=brightdata` 可放行 FB／IG 排程刷新。
- 新增：`RSS_PAID_DISCOVERY_MIN_HOURS` 限制排程自行放行的付費盤點（如 Apify 粉專）每個訂閱的最短間隔；未到間隔記為 `deferred`，明確 `--allow-batch-cloud` 不受限。
- 修正：Apify 粉專盤點成功後寫快照時因 `max_total_charge_usd` 是 Decimal 而崩潰（10-21 A1 實測：run 已計費、結果卻沒存下）；改存字串並加回歸測試。
- 新增：`RSS_PAID_DISCOVERY`（如 `apify`）讓排程刷新只對列出的付費盤點供應商自行放行，其他供應商仍須 `--allow-batch-cloud`。
- 新增：RSS 處理完成的影片把 `knowledge.md` 存到 Drive「RSS 自動摘要」資料夾（ID 先存檔、同內容沿用、不覆寫被改動的檔案；Drive 失敗只記錄不讓影片失敗；`RSS_DRIVE_UPLOAD=false` 可關閉）；完成通知加上前 300 字摘要與 Drive 連結。
- 介面：v17 widget 視覺更新（卡片式版面、主次按鈕、狀態與缺漏提示框、時間碼標籤、逐字稿／畫面觀察分色、跟隨 ChatGPT 淺色／深色主題）；Drive 交接改一顆「複製並開新一般對話」（先複製再嘗試開分頁並顯示連結備援，複製失敗不開分頁並顯示手動複製與連結，保留「只複製」）；其餘 id、文字與行為不變，UI URI 升為 v17 並保留 v15、v16。另修正報告區兩個簡體字（独立、实际）。
- 改進 A／B 韌性（依 job c9289f71a482 實測：MiniMax 連續 4 次 HTTP 529）：語音可靠、畫面失敗的 partial 報告標 `evidence_scope=speech_only` 並免摘要自動上傳 Drive（報告開頭註明只含語音）；快照對所有可自動上傳的報告回 `awaiting_segments`，widget v16 自動追蹤、提示建議加跑 B（`quality.route_b_reasons`）、複製改三段備援；保留 v14／v15 widget URI 避免升版後「無法開啟應用程式」；HTTP 429／529 拒絕記 `zero_rejected` 不佔預算；MiniMax 同一影片 24 小時內重跑重用 `file_id`。

- 改進 A／B 工作流（依 job e47517f85346 實測）：分析證據完整時，A 報告不必等 ChatGPT 摘要即自動上傳 Drive；B 交接內容改為一次貼上（先獨立輸出，再讀同資料夾 A 報告合併）；合併規則改為畫面文字採 B、A 不同讀法放附錄、語音採 A；逐字稿只有低信心片尾套語（如「ご視聴ありがとうございました」，avg_logprob < -0.3）、只有符號（♪）或沒有語音時，`quality` 標 `speech_reliable=false`、`recommend_route_b=true`。只有證據完整且 `quality.status=evidence_available` 才免摘要上傳；交接內容要求 B 先核對報告 job_id。widget 未改動（報告排程後需手動重新取得狀態）。

- 修正 #58：v15 widget 在摘要已提交但 Drive 片段／worker 尚未完成（`awaiting_segments`）時，以唯讀快照追蹤至報告排程，再接續既有上傳工作輪詢。查詢核對 job、cache signature、分析與報告雜湊；最多 60 次，失敗或逾時停止並提示手動刷新，不重送摘要、分析或上傳。新增提交／通知／恢復、錯誤與綁定不符、等待上限及換影片／關閉的回歸測試；真實 Drive 與 ChatGPT 驗收仍待執行。

- 新增 #59：v14 widget 提供可收合的 ChatGPT 檔案庫實驗，每個分段須明確點擊才 fetch／uploadFile；一般上傳亦須另行點擊。顯示上傳資訊、SHA-256 與完整遮蔽網址的錯誤；隔離連點、換 job 及實驗狀態保存失敗，模型讀取要求走既有訊息橋接與統一保存。新增獨立 Node 測試並納入離線檢查，固定既有 CSP／成功媒體 CORS 前提；檔案庫與模型可讀性仍需真實 ChatGPT 驗證。已知限制：上傳卡住時需重新載入介面。

- 補強 #58：v14 widget 在 A 分析完成後自動讀取報告，明示摘要提交前尚未上傳；提交摘要後以 1–10 秒退避輪詢報告工作，驗證完成後顯示「A 已完成」並啟用合併複製。失敗保留完整錯誤，換 job／關閉停止輪詢。合併指令採 §4.3 原文，以「合併：」開頭並保留 file_id／SHA-256／未獨立驗證聲明；README 說明 paired worker 與 caller_submitted 上傳條件。UI URI 同步升為 `ui://video-timeline-pipeline/v14-<媒體網址 hash>.html`。

- 修正 #56 follow-up B：CLI 搜尋／問答／章節與知識檔章節／待查證資訊不再提供不可靠畫面時間碼的跳轉；本機索引簽章升至 2，以 NULL 保存不可信時間並排除 `timestamp_*` 診斷文字，保留畫面內容且不更動摘要快取。

- 新增 #57（A／B 並行 §4.1–§4.2）：prepare 完成後 widget 自動同時啟動 `analyze_prepared_video`（A）與 `upload_prepared_video_to_drive`（B，Drive 先送、互不等待、失敗隔離；未設定 Drive 時只跑分析）。Drive 完成即顯示交接內容，只放檔案 ID／網址／大小與原片分段時間，不含任何 A 的結果，並提供「一鍵複製」「開新一般對話」按鈕。伺服器在 Drive 結果與 `get_prepared_video` 附上同一份 `handoff_text`（B 固定輸出格式：一句話重點、分段表、限制說明；結尾提醒收到「合併」前不要讀取 A，#58 實作 A 檔案）。paired worker 允許同一 job 的分析與 Drive 片段上傳並行，最多兩個 worker；widget 雙軌追蹤兩路工作。離線驗證 `python3 -m pytest -q` 與 `node tests/test_widget_handoff.cjs`（39 項）皆通過。

- 修正 #56：`video_mp4` 畫面證據的時間碼不再被壓縮在前幾秒。提示詞明定 timestamp 是從 0 起算的原片秒數且事件須覆蓋全片到片尾；解析接受純數字、`MM:SS`／中文時間，無法解析的事件直接丟棄（不再靜默記為 0.0）；事件峰值不到 `min(片長-30 秒, 片長一半)` 或超出片長 30 秒以上時沿用已上傳檔案 ID 只重問一次，仍對不上就以 `finish_reason=timestamp_coverage` 標示未完成、不寫入可重用快取。短片（單一事件即達豁免門檻）不受影響，仍只呼叫一次。

- 修正 #52：`video_mp4` 視覺分析不再以裸子字串 `exceed` 誤判檔案過大。`is_minimax_video_size_error` 排除 HTTP 429、`rate limit exceeded`、`exceeds daily limit`、預算阻擋及配額等非大小錯誤，並讓限流與預算超限錯誤直接冒泡，避免多餘壓縮與重複上傳重試。

- 成本守衛接到每一條已啟用的付費傳輸（#4）：Groq 轉錄、MiniMax 視覺／摘要／關鍵詞／問答／digest／分類、OpenRouter embeddings、Meta Graph 分頁、Apify Actor 啟動、Bright Data trigger、Open Notebook 同步、RSS 通知 Webhook 與 Resend／SMTP／Webhook 寄送。送出前原子預留、超額時不開連線、送出後入帳；超時或結果不明不結清預留。嚴格模式（`BUDGET_HARD_STOP=true` 且 `DAILY_BUDGET_USD>0`）只放行帶供應商端可執行上限的操作：目前僅 Apify Actor run 傳入 `maxTotalChargeUsd`；Groq、MiniMax、OpenRouter 等估算不冒充硬上限而會被拒絕；未開嚴格模式時守衛只記錄估算、不保證不超出供應商費用。

- 修正 #18：預算期間改以 `COST_TIMEZONE`（IANA 時區，預設 Asia/Taipei）的本地日／月為界；無效時區明確失敗而非退回 UTC。預算狀態與成本報告輸出 `accounting_timezone` 供稽核；新增凍結時鐘測試涵蓋台北跨 UTC 日界、本地午夜 rollover 與 DST 時區。無效時區在開啟／初始化 operations.db 之前就明確失敗，不留下已遷移的資料庫。未結清的預算保留與已入帳費用改用同一段 UTC 時間窗過濾，因此變更 `COST_TIMEZONE` 時未結清金額不會脫離當期預算；新增 `(account_scope, created_at, status)` 索引維持該查詢走索引。無效時區時 `--budget-status`／`--cost-report` 以一行訊息與 exit 1 回報，不再丟出 traceback。離線驗證 `python3 -m pytest -q`：140 項通過（另 88 個 subtests）；`python3 -m compileall -q .` 通過，皆 exit 0。全程僅用凍結時鐘與本機 SQLite，未呼叫任何供應商或網路。`requirements.txt` 新增 `tzdata`，供沒有系統 IANA 資料庫的環境（如 Windows）讓 `zoneinfo` 正常運作。
- 整合既有背景佇列、簽名媒體網址與抽樣更新；停用整工作目錄刪除，清理與同步／背景操作共用鎖，保護佇列失敗、使用中及分享租約，保留新式分析路徑的文字結果與回執。
- 新增 MCP 影音暫存 7 天清理指令（預設 dry-run），只移除完成且不需覆核工作的已知影音產物，保留摘要、逐字稿、回執與 Drive；沿用既有 OS 鎖阻擋清理／MCP 競態，媒體清除後仍可重新下載。
- MiniMax 僅接受完整的 `finish_reason=stop`；截斷或欄位不完整的畫面觀察保留未完成狀態，不寫入可重用快取。逐張與整批視覺快取均檢查 schema，知識檔與 manifest 明確標示視覺完成狀態；以假回應驗證，真實 MiniMax 尚待實測。
- 2026-09-24：統一 Bright Data 單篇與 IG 作者採集的單次提交／持久回執；未知回應禁止重送，已知 snapshot 可續跑，作者下一輪以成功落盤的 snapshot 推進。基於 `703e649`，本批尚未推送或部署；離線驗證 `python -X utf8 -m unittest discover -s tests -q`：116 項、114 通過／2 項平台略過；`node tests/test_widget_handoff.cjs`：21 項通過；`git diff --check` 通過，皆 exit 0。下一驗收為審閱、推送與部署後的非新增計費 smoke test；不沿用先前部署的線上驗收。
- 新增 ChatGPT MP4 檔案引用交接工具 `ingest_video_segments`：驗證 `download_url`／`file_id`、暴露完整 `_meta["openai/fileParams"]` 與 `outputSchema`，未完成交接時不啟動摘要。
- 新增 Facebook Page 作者盤點與追蹤，透過 Meta Graph API 分頁取得影片及 Reels，再沿用既有 RSS 佇列與影音摘要管線。
- 新增可選 Apify 公開 Page 發現器，含 Bearer Token、Actor 費用上限、非同步輪詢、資料集分頁與影片貼文過濾。
- 新增 Bright Data 公開 Instagram 作者 Reel 盤點與訂閱，沿用既有去重、重試及 Groq→MiniMax 處理流程。

## 0.2.0 — 2026-09-03

- 新增 Groq 時間戳轉錄與 MiniMax 繁中摘要、畫面理解。
- 新增 RSS／Atom／Podcast 收件匣、快取、續跑與失敗重試。
- 預設 Groq／摘要 workers 4、MiniMax 畫面 workers 8。
- 新增自動抽幀與穩定畫面快取。
- VidBee sidecar 同時輸出字幕與相鄰筆記檔。

## 0.1.0

- 建立本機影片時間軸、字幕、筆記與搜尋索引管線。
