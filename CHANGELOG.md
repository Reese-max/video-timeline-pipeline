# Changelog

## Unreleased

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
