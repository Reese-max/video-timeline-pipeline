# Widget browser smoke checks

The automated contract tests use the shipped `web/widget.html` with a mocked MCP host. They do not prove that ChatGPT renders or accepts the widget. The browser harness also uses mock tools and a locally generated 30-second MP4; it never calls providers or uploads to Drive.

```powershell
node tests/test_widget_handoff.cjs
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
