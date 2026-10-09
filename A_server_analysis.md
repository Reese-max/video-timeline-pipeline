# Server Analysis Summary

## Overview

This document summarizes the server-side analysis results from the video timeline pipeline processing. The analysis covers media processing, OCR extraction, transcript verification, and storage metrics.

## Analysis Results

### Processed Analyses

| Analysis ID | Duration | Whisper Segments | OCR Frames | Non-empty OCR | Timeline Entries |
|-------------|----------|------------------|------------|---------------|------------------|
| fb1GFg77Fady | 379.67s | 206 | 759 | 712 | 8 |
| fb1DX92QYSd4 | - | - | - | - | - |
| fb1A1MzR6LyS | - | - | - | - | - |
| fb1BQgfm4gyN | - | - | - | - | - |
| fb1JSooDpiph | - | - | - | - | - |

### Key Metrics

- **Total OCR Frames**: 759 across processed analyses
- **Non-empty Subtitle OCR**: 712 frames with content
- **Non-empty Full OCR**: 657 frames with full content
- **Timeline Entries**: 8 key moments extracted

### Storage

- Analysis files stored in `analyses/` directory
- Each analysis includes: summary.md, ocr_index.tsv, whisper.txt, whisper.json, metadata.json, ocr_index_raw.tsv, transcript_verified.txt, timeline.json, analysis_stats.json
- OCR index files contain timestamped text extraction with hash-based deduplication

### Pipeline Stages

1. **Video Download**: Source videos fetched via yt-dlp/threads_downloader
2. **Audio Extraction**: FFmpeg-based audio chunking at specified intervals
3. **Frame Extraction**: Fixed-interval frame sampling with average-hash deduplication
4. **OCR Processing**: PaddleOCR for text extraction from frames
5. **Transcript Generation**: Whisper-based speech-to-text
6. **Timeline Synthesis**: Keyframe selection and event tagging

### Drive Storage

Analysis metadata prepared for Google Drive upload via `drive_storage.py`:
- Source fingerprint based on video content hash
- Segment metadata with size and SHA-256 checksums
- Folder structure: `Video Timeline - {title} - {job_id}`

### Merge Instructions

This change adds a new analysis summary document (`A_server_analysis.md`) to the repository. To merge:

```bash
# Review the changes
git diff main..devin/issue-58

# Apply the change
git checkout main
git pull origin main
git checkout -B devin/issue-58
git merge --ff-only devin/issue-58  # or cherry-pick the A_server_analysis.md commit

# Verify the file is present
cat A_server_analysis.md
```

## Conventional Commit

This change follows the conventional commit format for the issue loop workflow.