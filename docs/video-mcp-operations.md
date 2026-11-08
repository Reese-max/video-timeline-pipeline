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

Whole-video MiniMax analysis (`ANALYZE_VISUAL_MODE=video_mp4`, the only mode) always
sends `max_completion_tokens` (`ANALYZE_VIDEO_MAX_TOKENS`, default and ceiling
524288); without it MiniMax applies a small server default and long videos end
with `finish_reason=length`. The call waits up to `ANALYZE_VIDEO_TIMEOUT_SECONDS`
(3600), still bounded by `VIDEO_API_TIMEOUT_SECONDS` and the job deadline, so long
videos need those raised too.

Long videos are analysed in segments instead of being downsampled. The trigger is
the requested frame rate (the `ANALYZE_VIDEO_FPS` value, clamped to 0.2-5) times the
video duration times the measured cost of one frame at the requested detail
(`detail=high` ~616 tokens, `default`/`low` ~179) exceeding the ~360k token budget.
That budget is MiniMax's measured limit for the video input of a *single*
request (~390k prompt tokens, measured 2026-10-22 on A1: `detail=high` passed up
to 387,558 tokens and `detail=default` up to 394,629; more is refused with
HTTP 400 `invalid params, 410 (2013)`), not the 1M context window -
`max_completion_tokens` does not count against it. 360k keeps about 7% headroom
below the lowest measured pass value.
When it triggers, the video is cut with an ffmpeg stream copy (no re-encode) and
each segment is uploaded and analysed on its own with the requested fps and detail,
which is never lowered for a segment. Segment length is
`floor(budget * 0.9 / (tokens per frame * fps) / 60) * 60` seconds (at least 60;
480 s for `high` at 1 fps, 1800 s for `default` at 1 fps) and can be shortened with
`MINIMAX_VIDEO_CHUNK_SECONDS` (positive seconds, at least 60 and at most the
budget-derived limit). An illegal `MINIMAX_VIDEO_CHUNK_SECONDS` is rejected before any
call, even for a video short enough to be analysed in one piece.
A single un-chunked call is bounded by the same budget: at 1 fps `high` covers
about 584 s (9.7 min) and `default` about 2011 s (33 min); anything longer is
split into segments of the length above.
`MINIMAX_VIDEO_CHUNKING=off` restores the old behaviour: lower the fps first, and
fall back to `detail=default` when even 0.2 fps does not fit (the note then says
plainly that the budget is still exceeded).

Every segment costs at least one upload and one chat call; a retry for an
insufficient time coverage or an event gap fill can add one or two more calls, so both
the price and the wall clock grow with the number of segments; the job deadline and
`ANALYZE_VIDEO_TIMEOUT_SECONDS` must cover the whole run. Segments are analysed in
order, and cancellation, deadlines and budget blocks still travel upwards; a
cancelled or expired job is noticed before the next segment is uploaded.
Segment results are cached per segment (the key carries the source fingerprint and
the segment bounds), so a failed run only resends the segments that did not finish;
the merged result is cached under a key that carries `chunked` and `chunk_seconds`.
Once the merge succeeds the whole `minimax-chunks/` directory is deleted - the
segments are worthless for a rerun, which cuts them again - so only a failed run
leaves them behind; `media_retention.py` also knows how to remove those leftovers.

## Sampling and cache correctness

The MCP visual stage uploads the whole video to MiniMax, or its segments when the
video is analysed in chunks; frame sampling and the
frame fallback were removed, and `ANALYZE_VISUAL_MODE=frames` now raises. If the
video cannot be uploaded (even after compression) or the answer is truncated,
the visual stage is reported incomplete instead of switching to images. Visual
events are the model's summary of the whole video, not a frame-by-frame record;
the returned `sampling` object reports the fps, detail and output ceiling sent,
plus `chunked`, `chunk_seconds` and `chunk_count` when the video is analysed in
segments (the chunk count is the estimate from the planned segment length; the
real count depends on the keyframes ffmpeg cuts on).

`cache_signature` covers source SHA-256, code/prompt/schema versions, vision model
and endpoint, ASR backend/model/language, local ASR settings, audio chunk length
and whole-video settings (fps, detail, output ceiling, chunking mode and chunk
seconds). Credentials are excluded. A same-size source with
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

### Drive-primary media

When Drive is configured, Drive is the long-term copy. The Drive job uploads the
whole source video next to the segments and verifies both by size and SHA-256
(`drive-manifest.json` `source_file`). Submitting a background analysis also queues
the Drive job, so the backup does not depend on the widget.

A job with a complete, verified backup is cleaned by `media_retention.py` instead
of waiting seven days, once all of these hold: an analysis result exists for the
same source fingerprint (any status — Drive restores the source for re-analysis),
no playback link is still valid (`link_until`, at most `MCP_MEDIA_TTL_SECONDS`),
the job was not prepared or analyzed within `MCP_DRIVE_BACKED_GRACE_SECONDS`
(default 3600), every local copy still matches its backup, and Drive confirms each
file is untrashed with the same bytes in the job folder. Any doubt keeps the media.
Run the timer hourly so cleanup follows analysis closely; the command loads `.env`
for the Drive credentials.

Cleaned media comes back automatically: preparing the same URL, analyzing, or
uploading the report downloads the source and segments from Drive, verifies
size/SHA-256, restores the source mtime so the prepared fingerprint and analysis
cache still match, and regenerates previews. Without a complete backup the old
path applies (download the source URL again). `get_prepared_video` reports
`MEDIA_EXPIRED` with `drive_backup: true` while media is archived on Drive.

Health reports `runtime_sha256`, `writer_busy` and the retention policy for rollout
verification; healthy status alone is not proof of successful cleanup.

### One shared Drive folder

Every video now shares one folder: `Video Timeline` under `GOOGLE_DRIVE_FOLDER_ID`
(empty means My Drive root). Segments, the whole source and the A report sit on the
same level and carry the job prefix in the file name
(`<title> - <job_id> - segment_000.mp4`, `... - source.mp4`,
`... - A_server_analysis.md`). The folder ID is stored in
`mcp_jobs/drive-video-folder.json` before the folder is created, so a crash reuses
the same ID. A folder that is gone, trashed, no longer a folder, or no longer under
the configured parent is never written into again. A lost or broken state file, and
a reserved ID that is gone on Drive, first look for the shared folder that is still
on Drive and reuse it (oldest `createdTime` wins, then the lowest ID); only when
there is none a new ID is reserved and a new folder is created. There is never a
second shared folder.

The old layout — one `Video Timeline - <title> - <job_id>` folder per video — is moved
into the shared folder by a one-off script. It only moves files; it never deletes
them. The emptied folders go to the trash.

Stop the MCP service (or make sure no Drive upload is in flight) before running it:
an uploading job would write back into a folder the script is emptying. Run the dry
run first, read the plan, then apply.

```bash
# 1. Plan: no Drive write, no local write.
python scripts/migrate_drive_single_folder.py --jobs-root mcp_jobs

# 2. Apply the plan.
python scripts/migrate_drive_single_folder.py --jobs-root mcp_jobs --apply

# 3. Two whole job folders to the trash instead of moving their files.
python scripts/migrate_drive_single_folder.py --jobs-root mcp_jobs --apply --trash-job <job_id>
```

The dry run writes nothing at all — no Drive write and no local file — but it still
needs Drive credentials and it does read the Drive folder listing to print the plan.

Each step prints one JSON object per line. The script:

- moves every file of an old folder into the shared folder and renames it to
  `<title> - <job_id> - <old name>`, then re-reads the file to check its parent,
  name and SHA-256;
- pre-checks the whole folder before moving anything: one conflict keeps the folder
  and its files exactly where they are, so a folder is never half migrated;
- treats a report named `A_server_analysis.md` as never conflicting — the same job
  ships several report versions under that one name, so they are moved in as they
  are;
- treats another file with the same name and the same SHA-256 as a duplicate only
  when no local manifest points at that old folder **and** no local record still
  references that file id: the copy in the old folder goes to the trash
  (`trashed_identical_duplicate`) and does not block the folder; a file that is still
  referenced by a `drive-manifest.json` or by a report state is kept and the whole
  folder is treated as a conflict (`name_conflict` with
  `reason: protected_file_id`);
- skips a file whose name already exists in the shared folder under a different ID
  and different bytes, and keeps the old folder in place (the same goes for
  subfolders);
- rewrites `drive-manifest.json` (folder, `drive_prefix`, `drive_name`) and writes a
  new report upload state under the new folder key, keeping the old state file. Every
  rewritten JSON gets a `<name>.bak-YYYYMMDD` copy first;
- rewrites the manifest and the report states **before** the emptied folder goes to
  the trash, so an interrupted run never leaves a manifest pointing at a trashed
  folder;
- repairs what an interrupted run left behind: every run reads the local
  `drive-manifest.json` files, and a job whose files already sit in the shared
  folder gets its manifest and report states rewritten from the real Drive names;
- checks every file id a manifest records before it rewrites it: the file has to be
  outside the trash, sit in the shared folder, match its SHA-256 and carry the name
  `<prefix> - <file name>`. One mismatch and nothing is rewritten, the folder is
  kept and the run ends non-zero;
- keeps a job that the repair phase blocked (`repair_problem`: a recorded file is
  missing, in the trash or somewhere else) completely untouched in the folder phase:
  the old folder stays, the manifest still points at it, and the next run reports the
  same problem instead of quietly dropping the reference;
- writes the missing report state file after the folder phase, so a run whose media
  reached the shared folder while its report state stayed in the old folder is
  finished by a single `--apply`;
- treats a local manifest that cannot be read (broken JSON, not an object) as a
  problem (`manifest_invalid`): the job's old folders are kept and nothing is moved
  or trashed. A folder with no manifest at all stays an orphan and is only info;
- contains a local write failure: a failed manifest write, a failed backup or a
  failed report state write is reported (`manifest_rewrite_failed`, `backup_failed`,
  `report_state_write_failed`), blocks that one job and lets the other jobs finish.
  A backup that fails means no rewrite at all;
- reports several folders carrying the shared name as an ambiguity
  (`multiple_shared_candidates`, also on stderr from `drive_storage`) and keeps using
  the one the state file or a local manifest already points at, otherwise the oldest
  one;
- is safe to re-run: finished work is not repeated, and a second `--apply` writes
  nothing to Drive.

`--trash-job` drops the whole old folder and marks the local manifest
`drive_trashed`, so `backup_complete` and `completed_drive_state` stop treating it as
a valid backup. A `--trash-job` that matches no old folder is reported as a problem
(the exit code is non-zero) before the script stops early. The drop is the user's
explicit decision, so it also removes files that are still referenced locally; those
file ids are listed (`trash_job_protected_files`) so nothing disappears silently.
Preparing the same video again after `--trash-job` uploads it anew into the shared
folder, with the usual `<title> - <job_id> - <file name>` names; the trashed copy
stays in the trash.

The RSS folder (`RSS 自動摘要`) is not part of this migration.

Every generated URL carries an HMAC signature over its exact job, path and expiry.
GET, HEAD and byte ranges use the same signature. Missing, expired, duplicated or
tampered query fields are refused. Refresh links using `get_prepared_video`;
when retention removed the source and segments but kept the manifest and
analysis records, both `get_prepared_video` and a completed `prepare` result
polled via `get_video_job` report `MEDIA_EXPIRED` instead of stale links.
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
