# Video MCP background jobs and media lifecycle

## User workflow

1. Call `prepare_video_for_chatgpt(url, background=true)`. The result includes an
   `operation_id` immediately; poll `get_video_job` at the suggested interval.
2. When preparation completes, open the widget and press **開始分析**. The widget
   queues `analyze_prepared_video(..., background=true)` and displays progress.
3. Review transcript and sampled observations. Clicking a timestamp selects the
   relevant MP4 segment and seeks to the corresponding local offset.
4. Press **交給 ChatGPT 整理** to request a summary of the saved evidence. Remounting
   the widget does not rerun analysis or automatically resend that request.
5. **上傳到我的 Drive（選用）** is an explicit separate action. Missing evidence,
   low confidence or a failed provider does not trigger an upload.

The older default `background=false` retains the synchronous tool result shape
for existing clients. It uses the same pipeline, cache and storage rules, but is
not a durable queued operation; use background mode for disconnect/recovery and
hard cancellation. A single heavy worker runs at a time; legacy synchronous calls
share its execution lock. At most 64 pending/running operations are accepted.

## Progress, cancellation and restart

`get_video_job(operation_id)` returns `queued`, `running`, `completed`, `failed`,
`cancelled`, `expired`, or `interrupted`. Completed results are under `result`, with
`result_kind` set to `prepared`, `analysis` or `drive`. Progress describes stages,
not an estimated remaining duration. `cancel_video_job` stops local work; it does
not undo an upload that the provider has already accepted.

`resume_video_job` creates a new operation from a terminal failure and reuses
valid stage caches. Repeated submissions with identical inputs reuse an active
operation. SQLite stores the queue in `.video-state`, whose files are private.
Preparation and analysis that were running at server restart return to the queue.
An uncertain Drive write becomes `interrupted` and requires explicit resume;
existing verified Drive file IDs are reused by the upload layer.

Each background operation runs in its own process group. Cancellation, the total
deadline, and loss of the parent process terminate the worker and descendants.
`MCP_JOB_TIMEOUT_SECONDS` defaults to 3600. External programs and HTTP retries also
obey `VIDEO_PROCESS_TIMEOUT_SECONDS` (3600) and `VIDEO_API_TIMEOUT_SECONDS` (900),
bounded by the remaining job deadline. These time limits do not guarantee that
an already accepted remote API request is unbilled or cancelled remotely.

## Sampling and cache correctness

The MCP visual stage uses at most 240 low-resolution probes across the full
timeline, with time-window representatives and scene-change weighting. Only up
to 40 selected full-resolution JPEGs are written. Frame timestamps come from
FFmpeg PTS, including the last decodable frame. The returned `sampling` object
reports actual counts and sparse sampling; a 40-frame budget cannot establish
that every event in a long video was observed. This limits frame preparation and
disk use; it does not promise a percentage reduction in provider charges.

`cache_signature` covers source SHA-256, code/prompt/schema versions, vision model
and endpoint, ASR backend/model/language, local ASR settings, audio chunk length
and sampling configuration. Credentials are excluded. A same-size source with
the same modification time but different bytes invalidates the cache. Partial
results are saved and available for review; only complete matching results skip
analysis. `get_prepared_video` never restores a completed analysis under a stale
signature. Successful stage caches can be reused when resuming failed work.

## Storage policy and expiring links

Defaults are 20 GiB total managed media storage, 4 GiB per job, 1 GiB minimum free
space, a minimum seven-day completed-media retention, and 24-hour signed media links. These
are serialized preflight and post-stage checks, not a kernel filesystem quota:
an external program can temporarily exceed its estimate. Source downloads also
have the existing 512 MiB maximum. Exhaustion produces a visible error.

Whole-job deletion is disabled, including the old `MCP_STORAGE_CLEANUP_ENABLED`
path: `StoragePolicy.cleanup` now only returns a legacy lifecycle plan and cannot
remove directories. Use the guarded command below, after deploying and restarting
both MCP and background workers with the shared writer lock:

```bash
.venv/bin/python media_retention.py --jobs-root /home/box/video-timeline-pipeline/mcp_jobs
# Only after reviewing a successful dry run:
.venv/bin/python media_retention.py --jobs-root /home/box/video-timeline-pipeline/mcp_jobs --apply
```

The command deletes only recognized generated media from 12-hex jobs with matching
completed analysis, usable quality and source SHA-256. Every job file must be older
than seven days; later writes/use extend retention. Summaries, transcripts, JSON,
billing receipts, job directories and all Drive files survive. Drive handoffs,
`.keep-media`, failures, queued/running work, invalid evidence, live sharing leases
and unsafe paths are retained. Unknown queue ownership fails closed. Cleanup holds
the shared writer and lifecycle locks plus a SQLite transaction preventing queue
submissions/status changes during deletion. New `analysis/runs/<hash>/` media are
covered without removing their evidence caches. Each affected job retains its
`media-retention.json` receipt. Deleted media cannot be recovered locally; expired
links require downloading the source again. No paid API is called by cleanup.

Health reports `runtime_sha256`, `writer_busy` and the retention policy for rollout
verification; healthy status alone is not proof of successful cleanup.

Every generated URL carries an HMAC signature over its exact job, path and expiry.
GET, HEAD and byte ranges use the same signature. Missing, expired, duplicated or
tampered query fields are refused. Refresh links using `get_prepared_video`.
`MEDIA_EXPIRED` means the underlying media is no longer retained and requires new
preparation; an operation deadline can instead be resumed.

For deployment only, `MCP_LEGACY_MEDIA_UNTIL` can be one fixed Unix timestamp giving
older unsigned links a short grace period. Do not recalculate it at every restart.
The HMAC secret in `.video-state/media-signing.key` must remain private and be
preserved across restarts; rotating it invalidates old links.

## Verification boundaries

Unit tests include provider and host fixtures. FFmpeg tests decode real synthetic
short and long timelines. Linux tests additionally cover process-group cleanup
and fd-based media deletion. The widget browser harness is a mock host, not proof
that an actual ChatGPT host consumed MP4 bytes or rendered a final answer. A live
deployment check must separately establish MCP initialization, tool schemas,
background work, public signed media GET/HEAD/Range, and any real provider calls.

2026-10-01 integration: Linux's 190 tests and 30 mocked-host widget cases pass.
Windows's pre-existing `test_cancel_kills_descendants` still fails with this
machine's Python environment; do not treat Windows worker tree cancellation as
verified. This deployment targets Linux and does not change Windows process control.
