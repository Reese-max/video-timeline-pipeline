# Widget browser smoke checks

The automated contract tests use the shipped `web/widget.html` with a mocked MCP host. They do not prove that ChatGPT renders or accepts the widget. The browser harness also uses mock tools and a locally generated 30-second MP4; it never calls providers or uploads to Drive.

```powershell
node tests/test_widget_handoff.cjs
node tests/test_widget_library_experiment.cjs
node tests/widget_browser_harness.cjs --check
node tests/widget_browser_harness.cjs
```

Open the printed `WIDGET_HARNESS_URL` in a browser. Stop the harness with Ctrl+C; it removes its temporary MP4.

1. At **320px 手機寬度**, choose **已有完整結果**. Press **讀取版面與焦點檢查**. `documentWidth` should not exceed `clientWidth`; controls and long source text should wrap.
2. Use Tab / Shift+Tab to reach native buttons, the segment selector, player and evidence filter. Focus must be visible. Use Enter / Space on an evidence timestamp. For `0:35–0:39`, the second segment should be selected and its player time should be 5 seconds. The unknown timestamp must be plain text, not a seek button.
3. Choose **待分析**. It must only call `get_prepared_video` until **開始分析** is pressed. Watch progress, then cancel and resume. Provider and Drive calls are mocked. Cancellation must preserve the ability to resume; a completed result must not send a ChatGPT message automatically.
4. Choose **已有部分結果**. Gaps must be visible and `upload_prepared_video_to_drive` absent from the call counts until **上傳到我的 Drive** is clicked.
5. With complete evidence, click **交給 ChatGPT 整理**, then **重新開啟（保留狀態）**. `messages` must remain 1 and the widget should show that the request was already sent. Reopening must not analyze or upload again.
6. Repeat at **900px 桌面寬度**. Inspect the transcript, visible text, quality limitations, segment link and Drive handoff without clipping.

## Verification scope recorded on 2026-09-26

- Contract/race suite: passed locally, including standard MCP Apps initialization/message shape, cancellation, operation-only recovery, stale preparation responses, expired deadlines versus expired media, changed analysis signatures, and explicit Drive/summary actions.
- Harness script syntax and actual ffmpeg fixture generation: passed locally.
- Real browser/mobile layout and keyboard check: not completed in this run. Initial computer-use attempts timed out. After network recovery, browser inventory succeeded and reported Edge; the in-app browser was unavailable. Creating the Edge tab timed out after 30 seconds, leaving an `about:blank` tab, and attaching that exact tab timed out after 60 seconds. No harness page, keyboard interaction or real player seek could be inspected.
- Actual ChatGPT host rendering, follow-up delivery and production deployment: separate checks; the mock suite makes no claim about them.

## #59 ChatGPT 檔案庫實驗

離線 suite：`node tests/test_widget_library_experiment.cjs`。以下真實宿主步驟尚未執行；需在部署後另行記錄，不能以假的宿主測試填為成功。

1. 準備 [#59 指定測試影片](../docs/ideas/README.md)，展開「實驗：存到 ChatGPT 檔案庫」。先點 `segment_001.mp4`（約 4.4 MB），再點 `segment_000.mp4`（約 13.2 MB）。各自記錄 fileId、實際大小、耗時、現算 SHA-256、是否取得下載網址與完整錯誤；紀錄中遮蔽簽章網址。
2. 分別核對預期 SHA-256：`segment_001` 為 `bfe01467050db6569ea7664debf13ee85613262aa10cbb7d1605b1a300642763`；`segment_000` 為 `53572b408974589c7fded269dcbacbe38f71d2cb8944c92083a8f109fadb26b4`。這只核對上傳前 bytes；若取回檔案庫檔案，另算其 SHA-256 並記錄是否一致。
3. 每片上傳後檢查檔案庫介面是否真的出現檔案，再按「請模型讀這個檔案」。`segment_001` 應問片段第 10 秒（原片 2:10）；`segment_000` 應問第 30／65 秒（原片 0:30／1:05）。逐項對照 [原始畫面標準答案](../docs/ideas/04-uploadfile-library-experiment.md#4-驗證步驟)，不把答案貼進提問。回報「看不到」也應保留，不記為成功。
4. 模型無法直接讀取時，在同一個對話從檔案庫附加影片後再問；仍失敗才另開一般對話附加。分開記錄三種情況，不以另一種成功覆蓋原本失敗。
5. library 上傳失敗後確認不會自動重送；另點一般上傳備援，應保留兩次結果並標明 `library:false`。記錄遇到的帳號能力、型別／大小限制與錯誤，不從成功回傳推論檔案庫或模型驗收通過。
