# 影片資料庫時間軸管線

目前實作的流程：

```text
影片 URL（YouTube／Facebook／其他 yt-dlp 支援站台） → yt-dlp → FFmpeg
                         ├─ 音訊片段 → Groq Whisper
                         └─ steipete slides／Tesseract → MiniMax M3
                                      ↓
                              時間軸合併 → MiniMax M3 摘要
```

## 專案邊界

目前刻意維持單一可執行入口；`pipeline.py` 內的函式依責任分成五段，避免為了分檔增加部署負擔：

```text
輸入／工作目錄
  ├─ yt-dlp：來源下載
  ├─ FFmpeg：ffprobe、音訊切段、固定間隔截圖
  ├─ native／steipete：畫面候選與 OCR
  ├─ Groq／MiniMax：逐字稿、視覺理解、摘要
  └─ knowledge.md／Open Notebook：知識來源輸出與同步
```

每個工作目錄的雜湊同時包含來源與本地抽取設定；調整切段或畫面設定會建立新的工作目錄，不會誤用舊快取。JSON 與 Markdown 產物以暫存檔置換，避免中斷時留下半份索引。

三層快取讓中斷的批次可以直接續跑，不會重複付費：

| 產物 | 判準 |
|---|---|
| `source.*`、`audio/`、`frames/` | `.complete` sentinel；沒有標記就清掉重做（半份會靜默毀掉時間軸） |
| `transcript.json`、`visual.json`、`summary.json` | 上次 `manifest.status == "completed"` 才重用 |

實測同一支影片重跑：首次 82.6 秒，重跑 1.7 秒且零 API 呼叫。要強制重算加 `--refresh`。

## 先做本地驗證

```powershell
cd 'D:\Users\Administrator\Desktop\04_AI內容與產品\影片知識庫'
python pipeline.py --self-test
python pipeline.py --video 'D:\path\to\video.mp4' --dry-run
python pipeline.py --video 'D:\path\to\video.mp4' --dry-run --slide-extractor steipete
```

`--dry-run` 不會下載、不會上傳、不會呼叫雲端 API，但會用本機 `ffprobe`／`ffmpeg` 產生音訊片段與候選截圖。輸出在 `jobs\<12字元雜湊>\`。

## 批次建立頻道知識庫

先建立 URL 清單；每行一個影片 URL，空白行與 `#` 開頭的註解會忽略，重複 URL 會去除：

```powershell
yt-dlp --flat-playlist --print '%(webpage_url)s' --skip-download 'https://www.facebook.com/...' > urls.txt
yt-dlp --flat-playlist --print '%(webpage_url)s' --skip-download 'https://www.youtube.com/@頻道名/videos' > urls.txt
python pipeline.py --url-file .\urls.txt --slide-extractor steipete --sync-open-notebook --notebook-id 'notebook:abc'
```

批次會逐支重用同一條單片管線，輸出 `batches\<雜湊>\batch-manifest.json`；單支失敗會記錄並繼續處理其他 URL。先用 `--dry-run` 或 `--local-only` 可確認本機產物，不會下載或上傳雲端。

## 完整流程

先安裝下載器：

```powershell
python -m pip install yt-dlp
```

複製 `.env.example` 為 `.env`，填入兩個 API 金鑰，再執行：

```powershell
python pipeline.py --url 'https://www.facebook.com/...' --workers 2 --frame-interval 10
```

畫面抽取器預設是零額外安裝的 `native`；要使用已驗證的場景偵測＋Tesseract OCR，指定：

```powershell
python pipeline.py --url 'https://www.facebook.com/...' --slide-extractor steipete
```

這會使用 PATH 上的 `summarize`，否則透過 `npx -y @steipete/summarize` 執行。Node 24+ 與 Tesseract 必須可用；專用 `slides` 子命令只處理畫面，不會重複呼叫 Groq 轉錄。

必要環境變數：

```text
GROQ_API_KEY=...
MINIMAX_API_KEY=...
```

`.env` 會覆蓋繼承來的環境變數（只有非空值才覆蓋）。這點很重要：`MINIMAX_BASE_URL` 這種通用名稱常被別的工具設成不相容的值（例如指向 `/anthropic` 相容端點），若讓全域設定贏過專案設定，照著這份 README 建 `.env` 也修不好。`.env` 同時會從**專案目錄**與 `--root` 指定的輸出目錄讀取，後者優先。

逐字稿語言預設 `zh`。處理外語影片要加 `--language auto` 交給 Whisper 自動偵測，否則會把外語音訊硬「翻譯」成中文，產出無意義的逐字稿：

```powershell
python pipeline.py --url '...' --language auto
```

### 轉錄後端

`--transcribe-backend local` 走本機 `faster-whisper`：不需要 `GROQ_API_KEY`，也不受 Groq 的地區／VPN 封鎖影響（Groq 會擋資料中心出口 IP，開 VPN 時會回 `403 Access denied`）。

```powershell
python pipeline.py --url '...' --transcribe-backend local --whisper-model small
```

模型大小用 `--whisper-model`（`tiny`／`base`／`small`／`medium`／`large-v3`）；Groq 那邊用 `--groq-model`。同一段英文音訊實測，原文是 *"here we are in front of the elephants … really long trunks"*：

| 後端／模型 | 開頭 | 關鍵字 | 耗時 |
|---|---|---|---|
| Groq `whisper-large-v3-turbo` | one of ❌ | fronts ❌ | 28.9s |
| 本機 `base` | one of ❌ | punks ❌ | — |
| 本機 `small` | one of ❌ | trunks ✅ | — |
| **Groq `whisper-large-v3`**（預設） | **in front of** ✅ | **trunks** ✅ | 59.3s |

turbo 不只聽錯關鍵字，還把 "in front of" 縮成 "one of"，整句語意都變了。慢一倍換全對，對「處理一次、往後反覆查詢」的知識庫划算，所以預設用非 turbo 版。

預設跑 CPU。`device="cuda"` 在建構時不會報錯，缺 `cublas64_12.dll` 要等真的開始轉錄才炸，那時音訊已經切完、時間也花掉了——所以要用 GPU 得明確設 `WHISPER_DEVICE=cuda`（另需 CUDA runtime 與 cuDNN）。

可選環境變數：

```text
GROQ_MODEL=whisper-large-v3-turbo
GROQ_LANGUAGE=zh
MINIMAX_MODEL=MiniMax-M3
MINIMAX_BASE_URL=https://api.minimax.io/v1
```

只做下載與本地處理可用：

```powershell
python pipeline.py --url 'https://www.facebook.com/...' --local-only
```

## 產物

- `manifest.json`：狀態、來源、設定與產物索引。
- `audio\audio_*.mp3`：單聲道 16 kHz、每段預設 15 分鐘。
- `frames\frame_*.jpg`：定時截圖；`frame_candidates.json` 記錄去重與 OCR 判斷。
- `transcript.json`：Groq 帶時間戳逐字稿。
- `visual.json`：MiniMax 結構化畫面觀察。
- `timeline.json`：語音與畫面事件依時間排序。
- `summary.json`：MiniMax 摘要、章節、步驟、工具名詞與待查證資訊。
- `knowledge.md`：單支影片的知識來源（也是匯入 Open Notebook 用的格式）。

專案根目錄另有 `library.db`：跨影片的全文索引，所有影片共用一份。

PaddleOCR 與 PySceneDetect 暫不列為必要依賴：`native` 模式提供本地 fallback；`steipete` 模式提供場景偵測與 Tesseract OCR。兩者都只把候選畫面交給後續 MiniMax 視覺階段。

## 本地搜尋索引（本專案的資料庫）

跑過的影片會自動寫進專案根目錄的 `library.db`（SQLite FTS5，標準函式庫，無額外安裝，不需要任何服務或 port）。這一個檔就是整個知識庫，複製走即完整搬移。要重掃既有的 `jobs\`：

```powershell
python pipeline.py --index
python pipeline.py --search '時間軸合併'
python pipeline.py --search 'ffmpeg' --search-limit 50
```

搜尋涵蓋影片標題、逐字稿、畫面觀察與摘要，結果附時間戳與來源 URL。索引用 `trigram` 斷詞：`unicode61` 會把整句中文吃成單一 token，中文查詢一律落空。

`trigram` 的索引窗口是 3 字元，`AI`、`影片` 這類兩字詞 `MATCH` 必定落空，因此短於 3 字元的查詢會自動回退全表掃描（輸出會標示模式），並依「標題 → 摘要 → 逐字稿／畫面」與命中次數排序。實測 5 萬筆片段約 80 ms、線性成長，沒有效能疑慮：

```powershell
python pipeline.py --search 'AI'      # 命中 N 筆（全表掃描：查詢短於 3 字元）
python pipeline.py --search '時間軸'   # 命中 N 筆（索引）
```

`library.db` 可直接用任何 SQLite 工具開；`videos` 表一支影片一列，`events` 表是全文索引。

## 問答與總覽

兩者都需要 `MINIMAX_API_KEY`，不需要額外服務。

```powershell
python pipeline.py --ask '這些影片有提過怎麼設定 ffmpeg 嗎？'
python pipeline.py --digest
```

`--ask` 先讓模型把問句拆成檢索詞（整句丟進 FTS5 是 phrase 比對，長問句必定 0 筆），再用檢索到的片段生成答案，並標註 `[1]`、`[2]` 對應到影片標題與時間戳。提示詞限定只能引用片段內容，資訊不足時會直說找不到，不會臆造。

`--digest` 把各支影片的摘要合成 `digest.md`：主題分群、跨影片反覆出現的術語、建議觀看順序、涵蓋落差。只吃跑過完整流程的 job，`--local-only`／`--dry-run` 的 job 沒有真摘要會被跳過。

> 呼叫 MiniMax 一律不設 `max_tokens`。MiniMax-M3 是 reasoning 模型，`<think>` 會先吃掉配額，設了上限常常在真正的答案輸出前就用完，回傳半截 JSON。

## 語意檢索

關鍵字檢索只找得到字面相同的詞。補上向量後，「厚皮動物」也能命中講大象的片段：

```powershell
python pipeline.py --embed                          # 補算向量，只算還沒有的
python pipeline.py --search '厚皮動物' --semantic
python pipeline.py --ask '他對長鼻子有什麼評論？' --semantic
```

兩種後端，用 `--embed-backend` 切換：

| 後端 | 預設模型 | 需要 | 成本 |
|---|---|---|---|
| `openrouter`（預設） | `nvidia/nemotron-3-embed-1b:free`（2048 維） | `OPENROUTER_API_KEY` | **0**（實測 `cost=0`） |
| `local` | `BAAI/bge-m3`（1024 維） | `sentence-transformers` + `torch` | **0**，且離線、內容不外傳 |

```powershell
python pipeline.py --embed                        # 免費雲端
python pipeline.py --embed --embed-backend local  # 本機
```

其他可用模型：`baai/bge-m3`、`qwen/qwen3-embedding-8b`（皆 US$0.01/M）、`nvidia/llama-nemotron-embed-vl-1b-v2:free`。

OpenRouter 的 `/embeddings` 與 `/rerank` 端點在模型清單裡查不到、官方文件也沒寫，但實測可用。要注意 chat 的 `:free` 清單與 embedding 的模型池是分開的：chat 那些 id 打 embeddings 會回 `does not exist`；而 NVIDIA 那幾顆**必須加 `:free` 後綴**才有 provider，不加會回 `No endpoints found`。

本機後端首次使用要下載約 2.2 GB 模型。CPU 實測約 94 筆/秒；裝的若是 `torch+cpu` 就吃不到 GPU，要 GPU 得另外裝 CUDA 版。

`--embed` 只補算「這個模型還沒算過」的片段：重跑不重複付費，換模型則自動重算。`vectors` 表記錄了模型名稱，查詢時只用相符的向量——混用不同模型的向量算出來的相似度沒有意義。

### 為什麼不直接嵌入截圖

`nvidia/llama-nemotron-embed-vl-1b-v2:free` 確實能讀圖（把 data URL 直接放進 `input` 即可，token 數會隨圖片變化；實測動物園實景 vs 投影片 = 0.13、投影片 vs 投影片 = 0.63，圖文對齊也正確）。

但實測**嵌 MiniMax 的文字描述比直接嵌圖更準**：

| 查詢 | 直接嵌圖 | 嵌文字描述 |
|---|---|---|
| 穿紅色外套的男生 | 0.267 | **0.331** |
| 金屬欄杆 | 0.065 | **0.192** |
| 動物在吃東西 | 0.196 | **0.300** |

原因是雙重劣勢：查詢是中文，而該模型的圖文對齊在英文遠強於中文，「圖片→中文查詢」同時跨模態又跨語言；而 MiniMax 產出的繁中描述與查詢是同模態同語言。若你的查詢主要是英文，這個結論可能反過來。

### 相似度門檻

各家模型的分數尺度差很大，實測同一組語料：

| | 正確命中 | 無關對照 |
|---|---|---|
| `baai/bge-m3` | 0.61–0.75 | **0.34** |
| `nvidia/nemotron-3-embed-1b:free` | 0.29–0.77 | **0.004** |

寫死絕對門檻必定誤殺其中一種，所以預設取「最高分的一半」再加一個很低的地板（`SIMILARITY_FLOOR`）。要固定值就用 `--min-similarity`。

輸出會標示相似度分數，低於 `0.35` 的視為不相關直接濾掉（`pipeline.py` 的 `MIN_SIMILARITY`）—— 少了下限，每次查詢都會回傳整個索引，末幾筆只是「最不像的那些」而不是命中。

`--ask --semantic` 會直接拿問句比對語意，跳過「先抽關鍵詞」那一步：少一次 API 呼叫，命中也更準。

## Open Notebook 知識庫

> **選用，預設不啟用。** 開源自架的 Open Notebook（不是 Google NotebookLM），要另外用 Docker 跑起來。本專案的資料庫是上面那個 `library.db` 單檔，查詢用 `--search` 就夠；只有想要筆記介面與 RAG 問答時才需要這一節。以下步驟經實測可用，但沒跑這套服務完全不影響管線。

部署（`docker-compose.yml` 需要 `lfnovo/open_notebook:v1-latest` 與 `surrealdb/surrealdb:v2`）：

```powershell
docker compose up -d
# API 就緒的判準是 http://127.0.0.1:5055/docs 回 200，不是容器 Running
```

SurrealDB 不必映射到 host（`open_notebook` 走 docker 內部網路 `ws://surrealdb:8000/rpc`）。少了這層映射還能避開 host 上 8000 埠的衝突。

API 路徑經實測（v1-latest）：上傳是 `/api/sources`，處理狀態輪詢是 `/api/commands/jobs/{command_id}`，且 `command_id` 必須保留 `command:` 前綴。認證用 `Authorization: Bearer <OPEN_NOTEBOOK_PASSWORD>`。



每支影片會產生一份 `knowledge.md`，包含摘要、章節、操作步驟、OCR、畫面觀察與時間軸。將多支影片指定到同一個 Notebook，即可把頻道累積成可搜尋知識庫。

先在 Open Notebook 建立 Notebook，取得 `notebook_id`，並設定本機 API 密碼：

```powershell
$env:OPEN_NOTEBOOK_PASSWORD = '只放在目前 PowerShell 工作階段'
python pipeline.py --url 'https://www.facebook.com/...' `
  --slide-extractor steipete `
  --sync-open-notebook `
  --notebook-id 'notebook:abc'
```

也可用環境變數設定：

```text
OPEN_NOTEBOOK_URL=http://127.0.0.1:5055
OPEN_NOTEBOOK_SOURCES_PATH=/sources
OPEN_NOTEBOOK_NOTEBOOK_ID=notebook:abc
OPEN_NOTEBOOK_PASSWORD=不要提交到版本控制
```

同步是明確的 `--sync-open-notebook` 選項；dry-run 與 local-only 只產生 `knowledge.md`，不會上傳。

重跑同一支影片是**替換**而非新增：`manifest.json` 的 `open_notebook.source_id` 記著上次建立的條目，下次同步會先刪掉它再上傳，`replaced` 旗標會標示有沒有真的替換掉。少了這層，每次重跑都會在 notebook 疊一份同名 source。

兩個前提：`manifest.json` 不能刪（那是唯一的對應記錄），以及若你在 Open Notebook 介面手動刪過該條目，這次同步會刪不到（404 會被忽略）而新增一份。手動刪掉的孤兒條目只能自己在介面清。
