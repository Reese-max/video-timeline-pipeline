# 04｜實驗：用 `window.openai.uploadFile` 把影片存進 ChatGPT 檔案庫

## 1. 目的

不經過 Google Drive，讓 ChatGPT 直接讀到伺服器上的 MP4。如果成功，甚至可能不用另開一般對話。

## 2. 官方文件與社群查證（2026-10-10）

| 關鍵點 | 結果 | 來源 |
|---|---|---|
| `uploadFile` 支援非圖片檔 | ✅ 2026-03-09 更新「now supports non-image file types」 | Plugin UI changelog |
| `{ library: true }` 存進使用者的 ChatGPT 檔案庫 | ✅ 2026-03-24 新增；但「when that library is available to the current user」，不是所有帳號都有 | changelog、Reference |
| 小工具能不能自動抓伺服器的檔案再上傳 | ❓ 文件寫的是「Upload a user-selected file」，沒說程式產生的 File 可不可以 | Reference |
| 檔案大小上限 | ❓ 文件沒寫（segment_000 有 13.2 MB） | Reference |
| 上傳後模型在同一個對話看不看得到 | ⚠️ 社群回報用 `imageIds` 讓模型看圖失敗，OpenAI 2026-02-22 說會查，之後沒有修好的消息 | 社群討論串 |
| `getFileDownloadUrl({fileId})` | ✅ 可以拿到暫時的下載網址，適用於小工具上傳的檔案 | Reference |

**推論：** 最可能的用法是「小工具把 MP4 存進檔案庫 → 使用者在對話裡從檔案庫把影片附加進來 → ChatGPT 讀取」。模型應該不會自動看到小工具上傳的檔案。

## 3. 測試按鈕（加進現有的 Video Timeline 小工具）

```js
async function saveSegmentToLibrary(segmentUrl, fileName) {
  if (!window.openai?.uploadFile) throw new Error('此環境沒有 uploadFile');
  const t0 = performance.now();
  const res = await fetch(segmentUrl);          // 小工具的 CSP 允許連線網域要包含伺服器網址
  if (!res.ok) throw new Error('下載失敗 ' + res.status);
  const blob = await res.blob();
  const file = new File([blob], fileName, { type: 'video/mp4' });
  let r;
  try {
    r = await window.openai.uploadFile(file, { library: true });
  } catch (e) {
    // library 不可用時，退回一般上傳再試一次，兩次的錯誤都要記錄
    console.warn('library 上傳失敗：', e);
    r = await window.openai.uploadFile(file);
  }
  const fileId = r?.fileId ?? r;                // 回傳格式文件沒寫清楚，兩種都處理
  let downloadUrl = null;
  try { downloadUrl = (await window.openai.getFileDownloadUrl({ fileId }))?.downloadUrl ?? null; } catch (_) {}
  return { fileId, size: blob.size, ms: Math.round(performance.now() - t0), hasDownloadUrl: !!downloadUrl };
}
```

**要求：**
- 每個片段一個按鈕；成功時在畫面上顯示 `fileId`、大小、耗時，失敗時顯示完整錯誤訊息（不要吞掉）。
- 先測 segment_001.mp4（4.4 MB），再測 segment_000.mp4（13.2 MB）。
- 另外加一個按鈕：用 `setWidgetState({ modelContent: { uploaded_file_ids: [...] } })` 加上 `sendFollowUpMessage`，請模型讀這個檔案，測試同一個對話裡模型看不看得到。

## 4. 驗證步驟

1. 按按鈕 → 確認回傳 `fileId`，檔案也出現在 ChatGPT 檔案庫。
2. 在掛外掛的同一個對話，從檔案庫附加這支影片，問驗證題；不行的話再改到一般對話試。
3. 驗證題和標準答案：
   - segment_000 的 SHA-256 → `53572b408974589c7fded269dcbacbe38f71d2cb8944c92083a8f109fadb26b4`
   - segment_001 的 SHA-256 → `bfe01467050db6569ea7664debf13ee85613262aa10cbb7d1605b1a300642763`
   - 0:30 → 上傳資料頁「个人健康档案 PRIVATE WORKBENCH」，右側三步：私密保存原件／视觉模型识别／人工核对
   - 1:05 → CareArk 視窗「正在本机运行：http://127.0.0.1:58320/」
   - 2:10（segment_001 第 10 秒）→ 「我／妈／+新增成员」選單，¥1522.40，记录医院 7
4. 記錄：每一步成功或失敗、錯誤訊息、耗時、檔案大小上限（如果碰到的話）。

## 5. 這次沒能實測的原因（2026-10-10）

- 小工具的程式在使用者的伺服器上，Claude 改不了。
- 想在 Claude 的雲端環境另架測試伺服器，但環境的安全政策不允許開對外通道。
- Chrome 擴充功能連線失敗，沒辦法操作 ChatGPT。

**下一步：** Grok Bot 部署測試按鈕之後，把 Chrome 擴充功能連上，由 Claude 跑第 4 節的驗證。

## 來源

- <https://developers.openai.com/plugins/changelog>
- <https://developers.openai.com/apps-sdk/reference>
- <https://community.openai.com/t/window-openai-uploadfile-and-setwidgetstate-and-imageids/1372230>
- <https://community.openai.com/t/please-make-window-openai-uploadfile-file-support-csv-type/1370003>
