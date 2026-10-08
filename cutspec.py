"""Evidence-backed paper-cut / NLE handoff (issue #11 research spike).

Canonical evidence -> CutSpec -> read-only preview -> deterministic
validation -> versioned export (CSV + OTIO) -> CutReceipt verification.

This module never calls network APIs, never mutates source media, and never
re-binds a stale spec to a different source revision. All AI/candidate
generation stays upstream; everything here is deterministic.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import io
import json
import math
import sys
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Dict, List, Optional

import pipeline


CUTSPEC_SCHEMA_VERSION = 1
CUTRECEIPT_SCHEMA = "cut-receipt/1"

# Selection authority for each segment. "suggested" is allowed in the spec but
# carries no extra privileges: every path through this module is deterministic.
SELECTION_REASONS = {"manual", "query_result", "evidence_reel", "suggested"}

# Timebases (frame-rate rationals) covered by the frozen fixture matrix.
# Anything else parses but is marked UNTESTED_TIMEBASE instead of being
# claimed frame-accurate.
TESTED_TIMEBASES = {"24000/1001", "24/1", "25/1", "30000/1001", "30/1"}

REJECT = "reject"
DEGRADED = "degraded"
INFO = "info"


@dataclass
class SourceInfo:
    videoPath: str
    contentHash: str  # sha256 of the frozen source bytes
    fingerprint: str  # pipeline.file_fingerprint: "size:mtime_ns"
    durationSec: float
    fps: float
    timebase: str  # frame-rate rational, e.g. "30000/1001"
    vfr: bool = False


@dataclass
class CutSegment:
    segmentId: str
    sourceRange: Dict[str, float]  # {"startSec": ..., "endSec": ...} in source time
    timelineRange: Dict[str, float]  # derived, cumulative placement on output timeline
    evidenceRef: str  # evidence identity: transcript/timeline span id, never empty
    selection: str  # one of SELECTION_REASONS
    reason: str
    confidence: float
    note: str = ""


@dataclass
class CutSpec:
    cutSpecVersion: int = CUTSPEC_SCHEMA_VERSION
    specHash: str = ""
    source: SourceInfo = field(
        default_factory=lambda: SourceInfo("", "", "", 0.0, 0.0, "0/1", False)
    )
    segments: List[CutSegment] = field(default_factory=list)
    intendedDurationSec: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "cutSpecVersion": self.cutSpecVersion,
            "specHash": self.specHash,
            "source": {
                "videoPath": self.source.videoPath,
                "contentHash": self.source.contentHash,
                "fingerprint": self.source.fingerprint,
                "durationSec": self.source.durationSec,
                "fps": self.source.fps,
                "timebase": self.source.timebase,
                "vfr": self.source.vfr,
            },
            "segments": [
                {
                    "segmentId": s.segmentId,
                    "sourceRange": dict(s.sourceRange),
                    "timelineRange": dict(s.timelineRange),
                    "evidenceRef": s.evidenceRef,
                    "selection": s.selection,
                    "reason": s.reason,
                    "confidence": s.confidence,
                    "note": s.note,
                }
                for s in self.segments
            ],
            "intendedDurationSec": self.intendedDurationSec,
        }

    def canonical_payload(self) -> Dict[str, Any]:
        """Hash input: content identity + segments, not locators.

        `specHash`, `videoPath`, and `fingerprint` are excluded — a path or
        mtime is not a source revision, so copying the file must not change
        the spec identity. Non-finite floats canonicalize to null so a bad
        segment still hashes deterministically instead of emitting NaN.
        """
        payload = self.to_dict()
        payload.pop("specHash")
        payload["source"].pop("videoPath")
        payload["source"].pop("fingerprint")
        return _sanitize_for_hash(payload)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)

    @classmethod
    def from_json(cls, json_str: str) -> "CutSpec":
        return cls.from_dict(json.loads(json_str))

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CutSpec":
        source_data = data.get("source", {})
        source = SourceInfo(
            videoPath=source_data.get("videoPath", ""),
            contentHash=source_data.get("contentHash", ""),
            fingerprint=source_data.get("fingerprint", ""),
            durationSec=float(source_data.get("durationSec", 0.0)),
            fps=float(source_data.get("fps", 0.0)),
            timebase=source_data.get("timebase", "0/1"),
            vfr=bool(source_data.get("vfr", False)),
        )
        segments = [
            CutSegment(
                segmentId=s.get("segmentId", ""),
                sourceRange=dict(s.get("sourceRange", {})),
                timelineRange=dict(s.get("timelineRange", {})),
                evidenceRef=s.get("evidenceRef", ""),
                selection=s.get("selection", ""),
                reason=s.get("reason", ""),
                confidence=float(s.get("confidence", 0.0)),
                note=s.get("note", ""),
            )
            for s in data.get("segments", [])
        ]
        return cls(
            cutSpecVersion=int(data.get("cutSpecVersion", CUTSPEC_SCHEMA_VERSION)),
            specHash=data.get("specHash", ""),
            source=source,
            segments=segments,
            intendedDurationSec=float(data.get("intendedDurationSec", 0.0)),
        )

    def compute_spec_hash(self) -> str:
        canonical = json.dumps(
            self.canonical_payload(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def update_hash(self) -> None:
        self.specHash = self.compute_spec_hash()


def _sanitize_for_hash(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _sanitize_for_hash(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_for_hash(v) for v in value]
    return value


@dataclass
class ValidationIssue:
    code: str
    severity: str  # reject | degraded | info
    segmentId: Optional[str]
    detail: str

    def __str__(self) -> str:
        target = f"[{self.segmentId}] " if self.segmentId else ""
        return f"{self.severity}:{self.code} {target}{self.detail}"


def parse_timebase(timebase: str) -> Fraction:
    """Parse a frame-rate rational like "30000/1001" into a Fraction."""
    try:
        rate = Fraction(str(timebase).strip())
    except (ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"unsupported timebase: {timebase!r}") from exc
    if rate <= 0:
        raise ValueError(f"non-positive timebase: {timebase!r}")
    return rate


def seconds_to_frames(seconds: float, timebase: str) -> int:
    """Nearest frame index for a source-time offset; half-up rounding."""
    rate = parse_timebase(timebase)
    exact = Fraction(str(seconds)) * rate
    return int(math.floor(exact + Fraction(1, 2)))


def frames_to_seconds(frames: int, timebase: str) -> float:
    rate = parse_timebase(timebase)
    return float(Fraction(int(frames)) / rate)


def _canonical_rate(avg_frame_rate: str, r_frame_rate: str) -> tuple[str, float, bool]:
    """Return (timebase_str, fps_float, vfr) from ffprobe stream fields."""
    try:
        avg = Fraction(avg_frame_rate)
    except (ValueError, ZeroDivisionError):
        avg = Fraction(0)
    try:
        base = Fraction(r_frame_rate)
    except (ValueError, ZeroDivisionError):
        base = Fraction(0)
    rate = avg if avg > 0 else base
    vfr = bool(avg > 0 and base > 0 and avg != base)
    return (f"{rate.numerator}/{rate.denominator}", float(rate), vfr)


def source_from_video(video_path: Path | str, probe: Optional[dict] = None) -> SourceInfo:
    """Derive SourceInfo identity from the actual media via pipeline helpers."""
    path = Path(video_path)
    if probe is None:
        probe = pipeline.probe_video(path)
    video_stream = next(
        (s for s in probe.get("streams", []) if s.get("codec_type") == "video"), None
    )
    duration = pipeline.video_duration(probe)
    if video_stream is not None:
        timebase, fps, vfr = _canonical_rate(
            video_stream.get("avg_frame_rate", "0/0"),
            video_stream.get("r_frame_rate", "0/0"),
        )
        # Source ranges use absolute presentation PTS; duration excludes a
        # positive origin (for example an MP4 empty edit).
        try:
            start, ticks = video_stream.get("start_pts"), video_stream.get("duration_ts")
            clock = Fraction(video_stream.get("time_base", "0/1"))
            if type(start) is int and start >= 0 and type(ticks) is int and ticks > 0 and clock > 0:
                end = float((start + ticks) * clock)
                if math.isfinite(end):
                    duration = max(duration, end)
        except (TypeError, ValueError, ZeroDivisionError, OverflowError):
            pass  # Missing or invalid clocks retain the existing duration.
    else:
        audio = next(
            (s for s in probe.get("streams", []) if s.get("codec_type") == "audio"), {}
        )
        try:
            timebase, fps = str(audio.get("time_base", "0/1")), 0.0
            parsed = Fraction(timebase)
            fps = float(parsed) if parsed > 0 else 0.0
        except (ValueError, ZeroDivisionError):
            timebase, fps = "0/1", 0.0
        vfr = False
    return SourceInfo(
        videoPath=str(path),
        contentHash=pipeline.file_sha256(path),
        fingerprint=pipeline.file_fingerprint(path),
        durationSec=duration,
        fps=fps,
        timebase=timebase,
        vfr=vfr,
    )


def build_cutspec(source: SourceInfo, segments: List[CutSegment]) -> CutSpec:
    """Build a versioned CutSpec; timeline placement is derived cumulatively."""
    cursor = 0.0
    for segment in segments:
        duration = segment.sourceRange.get("endSec", 0.0) - segment.sourceRange.get(
            "startSec", 0.0
        )
        segment.timelineRange = {"startSec": cursor, "endSec": cursor + duration}
        cursor += duration
    spec = CutSpec(
        cutSpecVersion=CUTSPEC_SCHEMA_VERSION,
        source=source,
        segments=list(segments),
        intendedDurationSec=cursor,
    )
    spec.update_hash()
    return spec


def _finite(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_cutspec(
    spec: CutSpec, current_source: Optional[SourceInfo] = None
) -> List[ValidationIssue]:
    """Deterministic validation. Rejects are never silently clamped."""
    issues: List[ValidationIssue] = []

    if not spec.source.videoPath or not spec.source.contentHash:
        issues.append(
            ValidationIssue(
                "MISSING_SOURCE",
                REJECT,
                None,
                "spec has no source path/content hash to verify against",
            )
        )

    try:
        rate = parse_timebase(spec.source.timebase)
        if f"{rate.numerator}/{rate.denominator}" not in TESTED_TIMEBASES:
            issues.append(
                ValidationIssue(
                    "UNTESTED_TIMEBASE",
                    DEGRADED,
                    None,
                    f"timebase {spec.source.timebase} is not in the verified "
                    "fixture matrix; frame accuracy is not claimed",
                )
            )
    except ValueError as exc:
        issues.append(ValidationIssue("UNSUPPORTED_TIMEBASE", REJECT, None, str(exc)))

    if spec.source.vfr:
        issues.append(
            ValidationIssue(
                "VFR_SOURCE",
                DEGRADED,
                None,
                "variable frame rate source; ranges stay in seconds and are "
                "not claimed frame-accurate",
            )
        )

    if not spec.segments:
        issues.append(
            ValidationIssue("EMPTY_SPEC", REJECT, None, "no segments to export")
        )

    duration_limit = (
        current_source.durationSec
        if current_source is not None
        else spec.source.durationSec
    )
    seen_ids: set[str] = set()
    for index, seg in enumerate(spec.segments):
        if not seg.segmentId:
            issues.append(
                ValidationIssue(
                    "MISSING_SEGMENT_ID", REJECT, None, f"segment {index} has no id"
                )
            )
        elif seg.segmentId in seen_ids:
            issues.append(
                ValidationIssue(
                    "DUPLICATE_SEGMENT_ID", REJECT, seg.segmentId, "duplicate id"
                )
            )
        seen_ids.add(seg.segmentId)

        start = seg.sourceRange.get("startSec")
        end = seg.sourceRange.get("endSec")
        if not _finite(start) or not _finite(end):
            issues.append(
                ValidationIssue(
                    "MISSING_RANGE",
                    REJECT,
                    seg.segmentId,
                    "segment lacks exact source start/end; summary-level "
                    "evidence cannot mint a clip",
                )
            )
            continue
        if end <= start:
            issues.append(
                ValidationIssue(
                    "NEGATIVE_DURATION",
                    REJECT,
                    seg.segmentId,
                    f"end {end} <= start {start}",
                )
            )
        elif start < 0 or (duration_limit > 0 and end > duration_limit + 1e-6):
            issues.append(
                ValidationIssue(
                    "OUT_OF_RANGE",
                    REJECT,
                    seg.segmentId,
                    f"range {start}-{end} exceeds source duration {duration_limit}",
                )
            )
        if not seg.evidenceRef:
            issues.append(
                ValidationIssue(
                    "MISSING_EVIDENCE",
                    REJECT,
                    seg.segmentId,
                    "no evidence identity; clip cannot be traced",
                )
            )
        if seg.selection not in SELECTION_REASONS:
            issues.append(
                ValidationIssue(
                    "BAD_SELECTION",
                    REJECT,
                    seg.segmentId,
                    f"selection {seg.selection!r} not in {sorted(SELECTION_REASONS)}",
                )
            )
        if index > 0:
            previous = spec.segments[index - 1]
            prev_end = previous.sourceRange.get("endSec")
            if _finite(prev_end) and _finite(start) and start < prev_end:
                issues.append(
                    ValidationIssue(
                        "OVERLAP",
                        DEGRADED,
                        seg.segmentId,
                        f"source range overlaps {previous.segmentId}",
                    )
                )

    if current_source is not None:
        if current_source.contentHash != spec.source.contentHash:
            issues.append(
                ValidationIssue(
                    "STALE_SOURCE",
                    REJECT,
                    None,
                    "source content hash drifted; re-confirm the spec against "
                    "the new revision (no automatic rebind)",
                )
            )
        elif current_source.fingerprint != spec.source.fingerprint:
            issues.append(
                ValidationIssue(
                    "SOURCE_METADATA_DRIFT",
                    INFO,
                    None,
                    "size/mtime changed but content hash is identical",
                )
            )
    return issues


def is_exportable(issues: List[ValidationIssue]) -> bool:
    return not any(issue.severity == REJECT for issue in issues)


def preview_cutspec(
    spec: CutSpec, current_source: Optional[SourceInfo] = None,
    *, evidence_manifest: Optional[dict] = None, job_dir: Optional[Path | str] = None,
    frame_map: Optional[dict] = None,
) -> Dict[str, Any]:
    """Read-only preview; optional measured frame mode decodes local media only."""
    issues = validate_cutspec(spec, current_source=current_source)
    evidence = _evidence_status(spec, evidence_manifest, job_dir,
                                current_source.videoPath if current_source else None)
    if evidence["status"] == "failed":
        issues.append(ValidationIssue(evidence["code"], REJECT, None, evidence["failure"]))
    frames = _frame_status(spec, frame_map, current_source.videoPath if current_source else None)
    if frames["status"] == "failed":
        issues.append(ValidationIssue(frames["code"], REJECT, None, frames["failure"]))
    per_segment: Dict[str, List[ValidationIssue]] = {}
    for issue in issues:
        if issue.segmentId:
            per_segment.setdefault(issue.segmentId, []).append(issue)

    clips = []
    for seg in spec.segments:
        seg_issues = per_segment.get(seg.segmentId, [])
        status = "ok"
        if any(i.severity == REJECT for i in seg_issues):
            status = "rejected"
        elif any(i.severity == DEGRADED for i in seg_issues):
            status = "degraded"
        clips.append(
            {
                "segmentId": seg.segmentId,
                "sourceStartSec": seg.sourceRange.get("startSec"),
                "sourceEndSec": seg.sourceRange.get("endSec"),
                "timelineStartSec": seg.timelineRange.get("startSec"),
                "timelineEndSec": seg.timelineRange.get("endSec"),
                "evidenceRef": seg.evidenceRef,
                "selection": seg.selection,
                "status": status,
                "flags": [i.code for i in seg_issues],
            }
        )

    gaps = []
    for index in range(1, len(spec.segments)):
        previous_end = spec.segments[index - 1].sourceRange.get("endSec")
        start = spec.segments[index].sourceRange.get("startSec")
        if _finite(previous_end) and _finite(start) and start > previous_end:
            gaps.append(
                {
                    "afterSegmentId": spec.segments[index - 1].segmentId,
                    "beforeSegmentId": spec.segments[index].segmentId,
                    "sourceStartSec": previous_end,
                    "sourceEndSec": start,
                    "durationSec": start - previous_end,
                    "code": "SOURCE_GAP",
                }
            )

    reject_codes = sorted({i.code for i in issues if i.severity == REJECT})
    status = "rejected" if reject_codes else (
        "degraded" if any(i.severity == DEGRADED for i in issues) else "ok"
    )
    return {
        "specHash": spec.specHash,
        "evidence": evidence,
        "frameMapping": frames,
        "cutSpecVersion": spec.cutSpecVersion,
        "source": {
            "videoPath": spec.source.videoPath,
            "contentHash": spec.source.contentHash,
            "fps": spec.source.fps,
            "timebase": spec.source.timebase,
            "vfr": spec.source.vfr,
            "durationSec": spec.source.durationSec,
        },
        "clipCount": len(spec.segments),
        "intendedDurationSec": spec.intendedDurationSec,
        "clips": clips,
        "gaps": gaps,
        "status": status,
        "rejectCodes": reject_codes,
        "issues": [
            {
                "code": i.code,
                "severity": i.severity,
                "segmentId": i.segmentId,
                "detail": i.detail,
            }
            for i in issues
        ],
    }


CSV_COLUMNS = [
    "record",
    "cutSpecVersion",
    "specHash",
    "videoPath",
    "contentHash",
    "fingerprint",
    "durationSec",
    "fps",
    "timebase",
    "vfr",
    "segmentId",
    "sourceStartSec",
    "sourceEndSec",
    "timelineStartSec",
    "timelineEndSec",
    "evidenceRef",
    "selection",
    "reason",
    "confidence",
    "note",
    "intendedDurationSec",
]


def _export_path(video_path: str, redact_paths: bool) -> str:
    if not redact_paths:
        return video_path
    name = Path(video_path).name
    return name or "redacted"


def _evidence_status(spec: CutSpec, evidence_manifest: Optional[dict] = None,
                     job_dir: Optional[Path | str] = None,
                     video_path: Optional[Path | str] = None) -> dict:
    import cut_provenance

    if evidence_manifest is None and job_dir is None and not cut_provenance.requires_evidence(spec):
        return {"status": "UNRESOLVED_EVIDENCE", "provenanceCoverage": 0.0}
    try:
        if evidence_manifest is None or job_dir is None:
            raise cut_provenance.EvidenceError("MISSING_EVIDENCE_MANIFEST", "strict evidence requires its companion and source job")
        return cut_provenance.validate_evidence(spec, evidence_manifest, job_dir, video_path)
    except (cut_provenance.EvidenceError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        return {"status": "failed", "code": getattr(exc, "code", "INVALID_EVIDENCE_MANIFEST"),
                "failure": str(exc), "provenanceCoverage": 0.0}


def _require_export_evidence(spec: CutSpec, evidence_manifest: Optional[dict],
                             job_dir: Optional[Path | str]) -> None:
    evidence = _evidence_status(spec, evidence_manifest, job_dir)
    if evidence["status"] == "failed":
        raise ValueError(f"{evidence['code']}: {evidence['failure']}")


def _frame_status(spec: CutSpec, frame_map: Optional[dict], video_path: Optional[Path | str] = None) -> dict:
    if frame_map is None:
        return {"status": "UNRESOLVED_FRAME_BOUNDARIES"}
    import cut_frame_map
    tracker = {"calls": 0}
    try:
        return {**cut_frame_map.validate_frame_map(spec, frame_map, video_path or spec.source.videoPath, _tracker=tracker),
                "localSubprocessCalls": tracker["calls"]}
    except cut_frame_map.FrameMapError as exc:
        return {"status": "failed", "code": exc.code, "failure": str(exc), "localSubprocessCalls": tracker["calls"]}


def _require_export_frames(spec: CutSpec, frame_map: Optional[dict]) -> None:
    status = _frame_status(spec, frame_map)
    if status["status"] == "failed":
        raise ValueError(f"{status['code']}: {status['failure']}")


def cutspec_to_csv(spec: CutSpec, redact_paths: bool = False, *,
                   evidence_manifest: Optional[dict] = None,
                   job_dir: Optional[Path | str] = None,
                   frame_map: Optional[dict] = None) -> str:
    """Deterministic CSV interchange: one source row + one row per segment."""
    _require_export_evidence(spec, evidence_manifest, job_dir)
    _require_export_frames(spec, frame_map)
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    source_row = {name: "" for name in CSV_COLUMNS}
    source_row.update(
        {
            "record": "source",
            "cutSpecVersion": spec.cutSpecVersion,
            "specHash": spec.specHash,
            "videoPath": _export_path(spec.source.videoPath, redact_paths),
            "contentHash": spec.source.contentHash,
            "fingerprint": spec.source.fingerprint,
            "durationSec": repr(spec.source.durationSec),
            "fps": repr(spec.source.fps),
            "timebase": spec.source.timebase,
            "vfr": "1" if spec.source.vfr else "0",
            "intendedDurationSec": repr(spec.intendedDurationSec),
        }
    )
    writer.writerow([source_row[name] for name in CSV_COLUMNS])
    for seg in spec.segments:
        row = {name: "" for name in CSV_COLUMNS}
        row.update(
            {
                "record": "segment",
                "specHash": spec.specHash,
                "segmentId": seg.segmentId,
                "sourceStartSec": repr(seg.sourceRange.get("startSec", "")),
                "sourceEndSec": repr(seg.sourceRange.get("endSec", "")),
                "timelineStartSec": repr(seg.timelineRange.get("startSec", "")),
                "timelineEndSec": repr(seg.timelineRange.get("endSec", "")),
                "evidenceRef": seg.evidenceRef,
                "selection": seg.selection,
                "reason": seg.reason,
                "confidence": repr(seg.confidence),
                "note": seg.note,
            }
        )
        writer.writerow([row[name] for name in CSV_COLUMNS])
    return buffer.getvalue()


def _opt_float(value: str) -> Optional[float]:
    return float(value) if value not in ("", None) else None


def cutspec_from_csv(text: str) -> CutSpec:
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None or len(reader.fieldnames) != len(CSV_COLUMNS) or set(reader.fieldnames) != set(CSV_COLUMNS):
        raise ValueError("cut spec CSV has unexpected columns")
    source_row: Optional[dict] = None
    segments: List[CutSegment] = []
    spec_hash = ""
    for row in reader:
        if row.get("record") == "source":
            if source_row is not None or segments:
                raise ValueError("cut spec CSV must have one leading source row")
            source_row = row
            spec_hash = row.get("specHash", "")
        elif row.get("record") == "segment":
            if source_row is None or row.get("specHash") != spec_hash:
                raise ValueError("cut spec CSV segment has no matching source identity")
            segments.append(
                CutSegment(
                    segmentId=row.get("segmentId", ""),
                    sourceRange={
                        "startSec": _opt_float(row.get("sourceStartSec", "")),
                        "endSec": _opt_float(row.get("sourceEndSec", "")),
                    },
                    timelineRange={
                        "startSec": _opt_float(row.get("timelineStartSec", "")),
                        "endSec": _opt_float(row.get("timelineEndSec", "")),
                    },
                    evidenceRef=row.get("evidenceRef", ""),
                    selection=row.get("selection", ""),
                    reason=row.get("reason", ""),
                    confidence=_opt_float(row.get("confidence", "")) or 0.0,
                    note=row.get("note", ""),
                )
            )
        else:
            raise ValueError("cut spec CSV has an unknown record")
    if source_row is None:
        raise ValueError("cut spec CSV has no source row")
    source = SourceInfo(
        videoPath=source_row.get("videoPath", ""),
        contentHash=source_row.get("contentHash", ""),
        fingerprint=source_row.get("fingerprint", ""),
        durationSec=_opt_float(source_row.get("durationSec", "")) or 0.0,
        fps=_opt_float(source_row.get("fps", "")) or 0.0,
        timebase=source_row.get("timebase", "0/1"),
        vfr=source_row.get("vfr", "") == "1",
    )
    return CutSpec(
        cutSpecVersion=int(source_row.get("cutSpecVersion") or CUTSPEC_SCHEMA_VERSION),
        specHash=spec_hash,
        source=source,
        segments=segments,
        intendedDurationSec=_opt_float(source_row.get("intendedDurationSec", "")) or 0.0,
    )


def _otio_rational_time(seconds: float, timebase: str) -> Dict[str, Any]:
    return {
        "OTIO_SCHEMA": "RationalTime.1",
        "rate": float(parse_timebase(timebase)),
        "value": seconds_to_frames(seconds, timebase),
    }


def _otio_time_range(start_sec: float, end_sec: float, timebase: str) -> Dict[str, Any]:
    return {
        "OTIO_SCHEMA": "TimeRange.1",
        "start_time": _otio_rational_time(start_sec, timebase),
        "duration": _otio_rational_time(end_sec - start_sec, timebase),
    }


def _otio_dict(spec: CutSpec, redact_paths: bool = False,
               frame_map: Optional[dict] = None) -> Dict[str, Any]:
    timebase = spec.source.timebase
    url_path = _export_path(spec.source.videoPath, redact_paths)
    embedded = spec.to_dict()
    embedded["source"]["videoPath"] = url_path
    frame_entries = {entry["segmentId"]: entry for entry in frame_map["entries"]} if frame_map else {}

    def time_range(segment: CutSegment) -> dict:
        if frame_map is None:
            return _otio_time_range(segment.sourceRange["startSec"], segment.sourceRange["endSec"], timebase)
        entry = frame_entries[segment.segmentId]
        rate = float(1 / Fraction(frame_map["timeBase"]))
        start, end = entry["startBoundary"]["pts"], entry["endBoundaryExclusive"]["pts"]
        return {"OTIO_SCHEMA": "TimeRange.1",
                "start_time": {"OTIO_SCHEMA": "RationalTime.1", "rate": rate, "value": start},
                "duration": {"OTIO_SCHEMA": "RationalTime.1", "rate": rate, "value": end - start}}

    available_range = _otio_time_range(0.0, spec.source.durationSec, timebase)
    metadata = {"cutSpec": embedded}
    if frame_map is not None:
        import cut_frame_map
        pts, rate = frame_map["boundaryPts"], float(1 / Fraction(frame_map["timeBase"]))
        available_range = {"OTIO_SCHEMA": "TimeRange.1",
                           "start_time": {"OTIO_SCHEMA": "RationalTime.1", "rate": rate, "value": pts[0]},
                           "duration": {"OTIO_SCHEMA": "RationalTime.1", "rate": rate, "value": pts[-1] - pts[0]}}
        metadata["frameMap"] = cut_frame_map.export_marker(frame_map)
    clips = []
    for seg in spec.segments:
        clips.append(
            {
                "OTIO_SCHEMA": "Clip.2",
                "name": seg.segmentId,
                "metadata": {
                    "cutspec_segment": {
                        "evidenceRef": seg.evidenceRef,
                        "selection": seg.selection,
                        "reason": seg.reason,
                        "confidence": seg.confidence,
                        "note": seg.note,
                    }
                },
                "source_range": time_range(seg),
                "media_references": {
                    "DEFAULT_MEDIA": {
                        "OTIO_SCHEMA": "ExternalReference.1",
                        "name": "",
                        "target_url": url_path,
                        "available_range": available_range,
                        "available_image_bounds": None,
                        "metadata": {},
                    }
                },
                "active_media_reference_key": "DEFAULT_MEDIA",
                "enabled": True,
                "color": None,
                "effects": [],
                "markers": [],
            }
        )
    return {
        "OTIO_SCHEMA": "Timeline.1",
        "name": f"cutspec-{spec.specHash[:12]}",
        "metadata": {"cutspec": metadata},
        "tracks": {
            "OTIO_SCHEMA": "Stack.1",
            "name": "tracks",
            "children": [
                {
                    "OTIO_SCHEMA": "Track.1",
                    "name": "V1",
                    "kind": "Video",
                    "children": clips,
                    "metadata": {},
                }
            ],
            "metadata": {},
        },
    }


def cutspec_to_otio_json(spec: CutSpec, redact_paths: bool = False, *,
                         evidence_manifest: Optional[dict] = None,
                         job_dir: Optional[Path | str] = None,
                         frame_map: Optional[dict] = None) -> str:
    """Emit an OTIO timeline deterministically (sorted keys, compact)."""
    _require_export_evidence(spec, evidence_manifest, job_dir)
    _require_export_frames(spec, frame_map)
    return json.dumps(
        _otio_dict(spec, redact_paths=redact_paths, frame_map=frame_map),
        ensure_ascii=True,
        sort_keys=True,
        indent=2,
    )


def _otio_module():
    try:
        import opentimelineio as otio  # type: ignore

        return otio
    except ImportError:
        return None


def otio_clip_ranges(otio_json: str) -> List[Dict[str, Any]]:
    """Parse an OTIO export back into per-clip ranges.

    Uses the real OpenTimelineIO parser when installed (the actual round-trip
    canary); falls back to a structural JSON walk otherwise, which the receipt
    records as an unverified parser.
    """
    data = json.loads(otio_json)
    stack = data.get("tracks", {})
    tracks = stack.get("children", [])
    if (data.get("OTIO_SCHEMA") != "Timeline.1" or stack.get("OTIO_SCHEMA") != "Stack.1"
            or len(tracks) != 1 or tracks[0].get("OTIO_SCHEMA") != "Track.1"
            or tracks[0].get("kind") != "Video"):
        raise ValueError("unsupported OTIO timeline layout")
    for container in (data, stack, tracks[0]):
        if container.get("source_range") is not None or container.get("effects", []):
            raise ValueError("OTIO container trims or effects are not verified")
    # Validate JSON numeric types before a native parser can coerce booleans,
    # and before division or delta comparisons can propagate NaN/Infinity.
    for track in tracks:
        for clip in track.get("children", []):
            if (clip.get("OTIO_SCHEMA") != "Clip.2" or clip.get("enabled", True) is not True
                    or clip.get("effects", [])):
                raise ValueError("unsupported OTIO timeline element")
            for field in ("start_time", "duration"):
                rational = clip["source_range"][field]
                if (not _finite(rational.get("value"))
                        or not _finite(rational.get("rate")) or rational["rate"] <= 0):
                    raise ValueError("OTIO clip timing must be finite numeric values at a positive rate")
    otio = _otio_module()
    ranges: List[Dict[str, Any]] = []
    if otio is not None:
        timeline = otio.adapters.read_from_string(otio_json)
        for track in timeline.tracks:
            for clip in track:
                if clip.schema_name() != "Clip":
                    continue
                source_range = clip.source_range
                timeline_range = clip.range_in_parent()
                ranges.append(
                    {
                        "segmentId": clip.name,
                        "sourceStartSec": source_range.start_time.to_seconds(),
                        "sourceEndSec": source_range.end_time_exclusive().to_seconds(),
                        "timelineStartSec": timeline_range.start_time.to_seconds(),
                        "timelineEndSec": timeline_range.end_time_exclusive().to_seconds(),
                        "evidence": dict(clip.metadata.get("cutspec_segment", {})),
                        "targetUrl": getattr(clip.media_reference, "target_url", None),
                    }
                )
        return ranges

    cursor = 0.0
    for track in data.get("tracks", {}).get("children", []):
        for clip in track.get("children", []):
            if clip.get("OTIO_SCHEMA") != "Clip.2":
                continue
            source_range = clip["source_range"]
            start = source_range["start_time"]["value"] / source_range["start_time"]["rate"]
            duration = (
                source_range["duration"]["value"] / source_range["duration"]["rate"]
            )
            ranges.append(
                {
                    "segmentId": clip.get("name", ""),
                    "sourceStartSec": start,
                    "sourceEndSec": start + duration,
                    "timelineStartSec": cursor,
                    "timelineEndSec": cursor + duration,
                    "evidence": clip.get("metadata", {}).get("cutspec_segment", {}),
                    "targetUrl": clip.get("media_references", {}).get(
                        clip.get("active_media_reference_key", "DEFAULT_MEDIA"), {}
                    ).get("target_url"),
                }
            )
            cursor += duration
    return ranges


def cutspec_from_otio_json(otio_json: str) -> CutSpec:
    """Recover the embedded CutSpec from an OTIO export's metadata."""
    otio = _otio_module()
    if otio is not None:
        timeline = otio.adapters.read_from_string(otio_json)
        payload = dict(timeline.metadata.get("cutspec", {})).get("cutSpec")
        if payload is None:
            raise ValueError("OTIO timeline carries no cutspec metadata")
        return CutSpec.from_dict(dict(payload))
    data = json.loads(otio_json)
    payload = data.get("metadata", {}).get("cutspec", {}).get("cutSpec")
    if payload is None:
        raise ValueError("OTIO timeline carries no cutspec metadata")
    return CutSpec.from_dict(payload)


def _ffprobe_interval_seconds(
    video_path: Path,
    start: float,
    end: float,
    stream_selector: str,
    nominal_frame_dur: float = 0.0,
) -> Dict[str, Any]:
    """Count frames whose pts falls inside [start, end) of the real media."""
    output = pipeline.run_external(
        [
            pipeline.require_tool("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            stream_selector,
            "-read_intervals",
            f"{start}%{end}",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
            str(video_path),
        ]
    )
    pts_values = []
    for token in output.replace(",", "\n").split():
        try:
            pts = float(token)
        except ValueError:
            continue
        if math.isfinite(pts):
            pts_values.append(pts)
    pts_values.sort()
    if not pts_values:
        return {"frames": 0}
    deltas = [b - a for a, b in zip(pts_values, pts_values[1:]) if b - a > 0]
    frame_dur = sorted(deltas)[len(deltas) // 2] if deltas else nominal_frame_dur
    return {
        "frames": len(pts_values),
        "firstPtsSec": pts_values[0],
        "lastPtsSec": pts_values[-1],
        "medianFrameDurSec": frame_dur,
    }


def _default_tolerance_sec(spec: CutSpec) -> float:
    try:
        fps = float(parse_timebase(spec.source.timebase))
    except ValueError:
        fps = 0.0
    base = 1.5 / fps if fps > 0 else 0.05
    if spec.source.vfr:
        base = max(base, 0.15)
    return base


def verify_export(
    spec: CutSpec,
    video_path: Path | str,
    export_text: str,
    export_format: str,
    tolerance_sec: Optional[float] = None,
    *, evidence_manifest: Optional[dict] = None, job_dir: Optional[Path | str] = None,
    frame_map: Optional[dict] = None,
) -> Dict[str, Any]:
    """Verify an export against the real source; emit a CutReceipt.

    Checks: source identity (STALE_SOURCE / MISSING_SOURCE, never rebinds),
    imported ranges vs intended (within tolerance), and ffprobe evidence that
    each intended source range maps to real frames. Zero network calls.
    """
    path = Path(video_path)
    tolerance = (
        tolerance_sec if tolerance_sec is not None else _default_tolerance_sec(spec)
    )
    receipt: Dict[str, Any] = {
        "schema": CUTRECEIPT_SCHEMA,
        "specHash": spec.specHash,
        "exportHash": hashlib.sha256(export_text.encode("utf-8")).hexdigest(),
        "exportFormat": export_format,
        "expectedContentHash": spec.source.contentHash,
        "observedContentHash": "",
        "toleranceSec": tolerance,
        "nleImport": "NLE_IMPORT_UNVERIFIED",
        "externalApiCalls": 0,
        "localSubprocessCalls": 0,
        "segments": [],
        "mismatches": 0,
        "stillNeedsHumanFix": False,
        "status": "pending",
        "parser": "",
        "specIssues": [
            {"code": i.code, "severity": i.severity, "segmentId": i.segmentId}
            for i in validate_cutspec(spec)
        ],
    }

    def refuse(status: str, detail: str) -> Dict[str, Any]:
        receipt.update(status=status, stillNeedsHumanFix=True, failure=detail, mismatches=1)
        receipt["receiptId"] = hashlib.sha256(
            json.dumps(receipt, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        return receipt

    if not _finite(tolerance) or tolerance < 0:
        return refuse("INVALID_TOLERANCE", "tolerance must be finite and non-negative")
    if any(issue["severity"] == REJECT for issue in receipt["specIssues"]):
        return refuse("INVALID_SPEC", "rejected spec cannot be verified")

    if spec.compute_spec_hash() != spec.specHash:
        receipt["status"] = "SPEC_HASH_MISMATCH"
        receipt["stillNeedsHumanFix"] = True
        receipt["receiptId"] = hashlib.sha256(
            json.dumps(receipt, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        return receipt

    evidence = _evidence_status(spec, evidence_manifest, job_dir, path)
    receipt["evidence"] = evidence
    if evidence["status"] == "failed":
        return refuse(evidence["code"], evidence["failure"])

    if export_format == "otio" and frame_map is None:
        try:
            if "frameMap" in json.loads(export_text).get("metadata", {}).get("cutspec", {}):
                return refuse("MISSING_FRAME_MAP", "frame-mapped OTIO requires its measured companion")
        except (TypeError, ValueError, AttributeError):
            pass  # Normal export parsing below emits INVALID_EXPORT.
    frames = _frame_status(spec, frame_map, path)
    receipt["frameMapping"] = frames
    receipt["localSubprocessCalls"] += frames.get("localSubprocessCalls", 0)
    if frames["status"] == "failed":
        return refuse(frames["code"], frames["failure"])

    if not path.is_file():
        receipt["status"] = "MISSING_SOURCE"
        receipt["stillNeedsHumanFix"] = True
        receipt["receiptId"] = hashlib.sha256(
            json.dumps(receipt, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        return receipt

    probe = pipeline.probe_video(path)
    receipt["localSubprocessCalls"] += 1
    current = source_from_video(path, probe=probe)
    receipt["observedContentHash"] = current.contentHash
    receipt["sourceFingerprint"] = current.fingerprint

    if current.contentHash != spec.source.contentHash:
        receipt["status"] = "STALE_SOURCE"
        receipt["stillNeedsHumanFix"] = True
        receipt["receiptId"] = hashlib.sha256(
            json.dumps(receipt, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        return receipt

    try:
        if export_format == "otio":
            receipt["parser"] = (
                f"opentimelineio-{_otio_module().__version__}"
                if _otio_module() is not None
                else "internal-json/1"
            )
            parsed = cutspec_from_otio_json(export_text)
            imported_rows = otio_clip_ranges(export_text)
        elif export_format == "csv":
            parsed = cutspec_from_csv(export_text)
            imported_rows = [{
                "segmentId": segment.segmentId,
                "sourceStartSec": segment.sourceRange.get("startSec"),
                "sourceEndSec": segment.sourceRange.get("endSec"),
                "timelineStartSec": segment.timelineRange.get("startSec"),
                "timelineEndSec": segment.timelineRange.get("endSec"),
            } for segment in parsed.segments]
            receipt["parser"] = "cutspec-csv/1"
        else:
            raise ValueError(f"unknown export format: {export_format}")
        receipt["adapterVersion"] = receipt["parser"]
        if not is_exportable(validate_cutspec(parsed)):
            return refuse("INVALID_EXPORT", "parsed export has a rejected spec")
        # Compare the actual parsed export, not only its claimed specHash.
        # Numeric int/float representations compare equally; source locators
        # remain excluded by the documented canonical identity contract.
        if (parsed.specHash != spec.specHash
                or parsed.canonical_payload() != spec.canonical_payload()):
            return refuse("EXPORT_SPEC_MISMATCH", "parsed export changed source or evidence identity")
        if [row["segmentId"] for row in imported_rows] != [segment.segmentId for segment in spec.segments]:
            return refuse("EXPORT_SEGMENTS_MISMATCH", "export changed clip order, count, or identity")
        if export_format == "otio":
            for segment, row in zip(spec.segments, imported_rows):
                evidence = {
                    "evidenceRef": segment.evidenceRef, "selection": segment.selection,
                    "reason": segment.reason, "confidence": segment.confidence,
                    "note": segment.note,
                }
                if row["evidence"] != evidence or row["targetUrl"] not in {
                    spec.source.videoPath, _export_path(spec.source.videoPath, True)
                }:
                    return refuse("EXPORT_PROVENANCE_MISMATCH", "clip reference differs from its source evidence")
        imported = {row["segmentId"]: row for row in imported_rows}
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError):
        return refuse("INVALID_EXPORT", "export could not be parsed with its declared contract")

    if frame_map is not None:
        import cut_frame_map
        try:
            cut_frame_map.verify_imported(frame_map, imported_rows, export_text, export_format)
        except cut_frame_map.FrameMapError as exc:
            return refuse(exc.code, str(exc))
    frame_entries = {entry["segmentId"]: entry for entry in frames.get("entries", [])}

    has_video = any(
        s.get("codec_type") == "video" for s in probe.get("streams", [])
    )
    stream_selector = "v:0" if has_video else "a:0"

    for seg in spec.segments:
        intended_start = seg.sourceRange["startSec"]
        intended_end = seg.sourceRange["endSec"]
        expected_timeline = seg.timelineRange
        mapped = frame_entries.get(seg.segmentId)
        if mapped and export_format == "otio":
            intended_start, intended_end = (mapped["normalizedSourceRange"][key] for key in ("startSec", "endSec"))
            expected_timeline = mapped["normalizedTimelineRange"]
        entry: Dict[str, Any] = {
            "segmentId": seg.segmentId,
            "intendedSourceRange": {"startSec": intended_start, "endSec": intended_end},
            "status": "ok",
            "deltaImportSec": None,
            "deltaFFmpegSec": None,
        }
        if mapped:
            entry["requestedSourceRange"] = dict(seg.sourceRange)
            entry["frameBoundaries"] = mapped
        got = imported.get(seg.segmentId)
        if got is None:
            entry["status"] = "failed"
            entry["failure"] = "segment missing from parsed export"
        elif (not all(_finite(got.get(field)) for field in (
                "sourceStartSec", "sourceEndSec", "timelineStartSec", "timelineEndSec"))
              or got["sourceStartSec"] < 0
              or got["sourceEndSec"] <= got["sourceStartSec"]
              or got["timelineStartSec"] < 0
              or got["timelineEndSec"] <= got["timelineStartSec"]):
            entry["status"] = "failed"
            entry["failure"] = "parsed export has invalid numeric ranges"
        else:
            deltas = [
                abs(got["sourceStartSec"] - intended_start),
                abs(got["sourceEndSec"] - intended_end),
            ]
            if got.get("timelineStartSec") is not None:
                deltas.append(
                    abs(got["timelineStartSec"] - expected_timeline["startSec"])
                )
            if got.get("timelineEndSec") is not None:
                deltas.append(
                    abs(got["timelineEndSec"] - expected_timeline["endSec"])
                )
            entry["importedSourceRange"] = {
                "startSec": got["sourceStartSec"],
                "endSec": got["sourceEndSec"],
            }
            entry["deltaImportSec"] = max(deltas)
            if entry["deltaImportSec"] > tolerance:
                entry["status"] = "failed"
                entry["failure"] = "imported range drifted beyond tolerance"

        if mapped:
            # The full v:0 map was actually decoded and matched above. Its
            # exact indices/terminal duration replace seek-point + nominal
            # median-duration coverage estimates in measured frame mode.
            entry["decodedFrames"] = mapped["endBoundaryExclusive"]["frameIndex"] - mapped["startBoundary"]["frameIndex"]
            entry["observedCoverageSec"] = mapped["normalizedSourceRange"]
            entry["deltaFFmpegSec"] = 0.0
            if entry["status"] == "failed":
                receipt["mismatches"] += 1
            receipt["segments"].append(entry)
            continue

        observed = _ffprobe_interval_seconds(
            path,
            intended_start,
            intended_end,
            stream_selector,
            nominal_frame_dur=1.0 / spec.source.fps if spec.source.fps > 0 else 0.0,
        )
        receipt["localSubprocessCalls"] += 1
        entry["ffprobe"] = observed
        if observed.get("frames", 0) == 0:
            entry["status"] = "failed"
            entry["failure"] = "no frames observed in intended source range"
        elif "firstPtsSec" in observed:
            # read_intervals decodes from the nearest seek point at-or-before
            # start, so coverage containment is the honest check: the intended
            # range must be covered by real frames within tolerance.
            observed_end = observed["lastPtsSec"] + observed["medianFrameDurSec"]
            entry["observedCoverageSec"] = {
                "startSec": observed["firstPtsSec"],
                "endSec": observed_end,
            }
            entry["deltaFFmpegSec"] = max(
                0.0,
                observed["firstPtsSec"] - (intended_start + tolerance),
                (intended_end - tolerance) - observed_end,
            )
            if entry["deltaFFmpegSec"] > 0 and entry["status"] == "ok":
                entry["status"] = "failed"
                entry["failure"] = "source media does not cover the intended range"
        if entry["status"] == "failed":
            receipt["mismatches"] += 1
        receipt["segments"].append(entry)

    receipt["status"] = "verified" if receipt["mismatches"] == 0 else "failed"
    if frame_map is not None and pipeline.file_sha256(path) != spec.source.contentHash:
        return refuse("STALE_SOURCE", "source changed during measured frame verification")
    receipt["stillNeedsHumanFix"] = receipt["mismatches"] > 0
    receipt["receiptId"] = hashlib.sha256(
        json.dumps(receipt, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return receipt


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8"))
    tmp.replace(path)


def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evidence-backed paper-cut / NLE handoff (issue #11 spike)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_probe = sub.add_parser("probe", help="derive SourceInfo for a video")
    p_probe.add_argument("--video", required=True)

    p_build = sub.add_parser("build", help="build a CutSpec from a segment list")
    p_build.add_argument("--video", required=True)
    p_build.add_argument(
        "--segments",
        required=True,
        help="JSON list of {sourceRange:{startSec,endSec}, evidenceRef, "
        "selection, reason, confidence, note, segmentId?}",
    )
    p_build.add_argument("--out")

    p_evidence = sub.add_parser("build-evidence", help="resolve exact spans from an existing canonical source job")
    p_evidence.add_argument("--video", required=True)
    p_evidence.add_argument("--job-dir", required=True)
    p_evidence.add_argument("--selections", required=True,
                            help="JSON list of {artifact:transcript|timeline,index,selection?,reason?,note?}; ranges are derived")
    p_evidence.add_argument("--out", required=True)
    p_evidence.add_argument("--evidence-manifest", required=True)

    p_frames = sub.add_parser("map-frames", help="freeze actual decoded presentation PTS boundaries for a CutSpec")
    p_frames.add_argument("--spec", required=True)
    p_frames.add_argument("--video", required=True)
    p_frames.add_argument("--out", required=True)

    p_preview = sub.add_parser("preview", help="read-only cut preview")
    p_preview.add_argument("--spec", required=True)
    p_preview.add_argument("--video", help="also check source revision")

    p_export = sub.add_parser("export", help="export CutSpec to csv|otio")
    p_export.add_argument("--spec", required=True)
    p_export.add_argument("--format", choices=["csv", "otio"], required=True)
    p_export.add_argument(
        "--full-paths",
        action="store_true",
        help="keep absolute source paths (default redacts to basename)",
    )
    p_export.add_argument("--out")

    p_verify = sub.add_parser("verify", help="verify an export, emit CutReceipt")
    p_verify.add_argument("--spec", required=True)
    p_verify.add_argument("--video", required=True)
    p_verify.add_argument("--export-file", required=True)
    p_verify.add_argument("--format", choices=["csv", "otio"], required=True)
    p_verify.add_argument("--receipt")

    for command in (p_preview, p_export, p_verify, p_frames):
        command.add_argument("--job-dir", help="existing canonical source job for strict evidence resolution")
        command.add_argument("--evidence-manifest", help="versioned cut-evidence/1 companion")
    for command in (p_preview, p_export, p_verify):
        command.add_argument("--frame-map", help="measured cut-frame-map/1 companion; revalidated against actual decoded media")

    args = parser.parse_args(argv)

    if args.command == "probe":
        source = source_from_video(Path(args.video))
        print(json.dumps(source.__dict__, ensure_ascii=False, indent=2))
        return 0

    if args.command == "build-evidence":
        import cut_provenance

        selections = json.loads(Path(args.selections).read_text(encoding="utf-8"))
        spec, companion = cut_provenance.build_evidence_cutspec(args.job_dir, args.video, selections)
        outputs = [Path(args.out).resolve(), Path(args.evidence_manifest).resolve()]
        _check_evidence_outputs(outputs, args.job_dir, args.video)
        if outputs[0] == outputs[1]:
            raise ValueError("CutSpec and evidence companion require distinct output files")
        _write_atomic(outputs[0], spec.to_json() + "\n")
        _write_atomic(outputs[1], json.dumps(companion, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        return 0

    if args.command == "build":
        source = source_from_video(Path(args.video))
        raw_segments = json.loads(Path(args.segments).read_text(encoding="utf-8"))
        segments = [
            CutSegment(
                segmentId=item.get("segmentId", f"seg-{index}"),
                sourceRange=dict(item["sourceRange"]),
                timelineRange={"startSec": 0.0, "endSec": 0.0},
                evidenceRef=item.get("evidenceRef", ""),
                selection=item.get("selection", "manual"),
                reason=item.get("reason", ""),
                confidence=float(item.get("confidence", 1.0)),
                note=item.get("note", ""),
            )
            for index, item in enumerate(raw_segments)
        ]
        spec = build_cutspec(source, segments)
        text = spec.to_json()
        if args.out:
            _write_atomic(Path(args.out), text + "\n")
        else:
            print(text)
        return 0

    if args.command == "preview":
        spec = CutSpec.from_json(Path(args.spec).read_text(encoding="utf-8"))
        evidence = _load_evidence_manifest(args.evidence_manifest)
        with contextlib.redirect_stdout(sys.stderr) if args.frame_map else contextlib.nullcontext():
            current = source_from_video(Path(args.video)) if args.video else None
            preview = preview_cutspec(spec, current, evidence_manifest=evidence, job_dir=args.job_dir,
                                      frame_map=_load_frame_map(args.frame_map))
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 2 if preview["status"] == "rejected" else 0

    if args.command == "export":
        spec = CutSpec.from_json(Path(args.spec).read_text(encoding="utf-8"))
        evidence = _load_evidence_manifest(args.evidence_manifest)
        issues = validate_cutspec(spec)
        if not is_exportable(issues):
            for issue in issues:
                if issue.severity == REJECT:
                    print(str(issue), file=sys.stderr)
            return 2
        with contextlib.redirect_stdout(sys.stderr) if args.frame_map else contextlib.nullcontext():
            text = (
                cutspec_to_csv(spec, redact_paths=not args.full_paths, evidence_manifest=evidence, job_dir=args.job_dir,
                              frame_map=_load_frame_map(args.frame_map))
                if args.format == "csv"
                else cutspec_to_otio_json(spec, redact_paths=not args.full_paths, evidence_manifest=evidence, job_dir=args.job_dir,
                                         frame_map=_load_frame_map(args.frame_map))
            )
        if args.out:
            if args.frame_map:
                _check_frame_outputs([Path(args.out).resolve()], args, spec.source.videoPath)
            if args.job_dir:
                _check_evidence_outputs([Path(args.out).resolve()], args.job_dir, spec.source.videoPath)
            _write_atomic(Path(args.out), text)
        else:
            print(text)
        return 0

    if args.command == "verify":
        spec = CutSpec.from_json(Path(args.spec).read_text(encoding="utf-8"))
        export_text = Path(args.export_file).read_bytes().decode("utf-8")
        evidence = _load_evidence_manifest(args.evidence_manifest)
        with contextlib.redirect_stdout(sys.stderr) if args.frame_map else contextlib.nullcontext():
            receipt = verify_export(spec, Path(args.video), export_text, args.format,
                                    evidence_manifest=evidence, job_dir=args.job_dir,
                                    frame_map=_load_frame_map(args.frame_map))
        text = json.dumps(receipt, ensure_ascii=False, indent=2)
        if args.receipt:
            if args.frame_map:
                _check_frame_outputs([Path(args.receipt).resolve()], args, args.video, spec.source.videoPath)
            if args.job_dir:
                _check_evidence_outputs([Path(args.receipt).resolve()], args.job_dir, args.video)
            _write_atomic(Path(args.receipt), text + "\n")
        else:
            print(text)
        return 0 if receipt["status"] == "verified" else 2

    if args.command == "map-frames":
        import cut_frame_map
        spec = CutSpec.from_json(Path(args.spec).read_text(encoding="utf-8"))
        _require_export_evidence(spec, _load_evidence_manifest(args.evidence_manifest), args.job_dir)
        output = Path(args.out).resolve()
        _check_frame_outputs([output], args, args.video, spec.source.videoPath)
        with contextlib.redirect_stdout(sys.stderr):
            manifest = cut_frame_map.build_frame_map(spec, args.video)
        _write_atomic(output, json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        return 0

    return 1


def _load_evidence_manifest(path: Optional[str]) -> Optional[dict]:
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        # Verification still emits a failed receipt for a missing/malformed
        # companion; preview/export reject the same unsupported shape.
        return {"schema": None}


def _load_frame_map(path: Optional[str]) -> Optional[dict]:
    if path is None:
        return None
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate frame map key")
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError("nonfinite frame map JSON")
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant)
    except (OSError, ValueError, TypeError):
        return {"schema": "invalid-frame-companion"}


def _check_frame_outputs(outputs: List[Path], args: Any, video_path: Path | str,
                         spec_source: Optional[Path | str] = None) -> None:
    protected = {Path(video_path).resolve()}
    if spec_source:
        protected.add(Path(spec_source).resolve())
    for field in ("spec", "frame_map", "evidence_manifest", "export_file"):
        value = getattr(args, field, None)
        if value:
            protected.add(Path(value).resolve())
    if any(output in protected or output.with_name(output.name + ".tmp").resolve() in protected for output in outputs):
        raise ValueError("frame handoff outputs must not replace source media, spec or input companions")
    if args.job_dir:
        _check_evidence_outputs(outputs, args.job_dir, video_path)


def _check_evidence_outputs(outputs: List[Path], job_dir: Path | str,
                             video_path: Path | str) -> None:
    job = Path(job_dir).resolve()
    protected = {Path(video_path).resolve(), *(job / name for name in (
        "manifest.json", "transcript.json", "timeline.json", "analysis/result.json"))}
    if any(output in protected or output.is_relative_to(job) or output.name.endswith(".tmp") for output in outputs):
        raise ValueError("evidence outputs must not replace canonical artifacts or source media")
    if any(output.with_name(output.name + ".tmp").resolve() in protected
           or output.with_name(output.name + ".tmp").resolve().is_relative_to(job) for output in outputs):
        raise ValueError("temporary output would replace a canonical artifact")


def main(argv: Optional[List[str]] = None) -> int:
    try:
        return _main(argv)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        print(f"{getattr(exc, 'code', 'INVALID_INPUT')}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
