"""Measured presentation-PTS boundaries for optional offline CutSpec handoffs.

Requested evidence spans stay immutable. This companion defines their explicit
nearest-boundary normalization and never estimates decoded frames from FPS.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
from fractions import Fraction
from pathlib import Path
from typing import Any

import pipeline

SCHEMA = "cut-frame-map/1"
NORMALIZATION = "nearest-decoded-pts-boundary-ties-later"
MAX_FRAMES = 1_000_000
MAX_EXACT_INTEGER = 2**53 - 1


class FrameMapError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


def _require(condition: bool, code: str, detail: str) -> None:
    if not condition:
        raise FrameMapError(code, detail)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and abs(value) <= MAX_EXACT_INTEGER


def _hash(value: Any) -> str:
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()
    except (ValueError, TypeError) as exc:
        raise FrameMapError("INVALID_FRAME_MAP", "frame map must contain finite canonical JSON") from exc
    return hashlib.sha256(raw).hexdigest()


def manifest_hash(manifest: dict) -> str:
    return _hash({k: v for k, v in manifest.items() if k != "manifestHash"})


def decoded_map(video_path: Path | str, *, _tracker: dict | None = None) -> dict:
    """Read actual v:0 presentation PTS ticks and the measured final duration.

    Decode order must already be strictly increasing presentation order. Gaps,
    overlaps, missing durations and negative timestamps are unsupported.
    """
    try:
        executable = pipeline.require_tool("ffprobe")
        if _tracker is not None:
            _tracker["calls"] += 1
        output = pipeline.run_external([
            executable, "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=index,time_base:frame=pts,pkt_duration,duration",
            "-of", "json", str(video_path),
        ])
        data = json.loads(output)
        streams, frames = data["streams"], data["frames"]
        _require(len(streams) == 1 and _integer(streams[0]["index"]), "UNSUPPORTED_FRAME_MAP", "one selected video stream is required")
        tick = Fraction(streams[0]["time_base"])
        _require(tick > 0 and tick.numerator == 1 and tick.denominator <= MAX_EXACT_INTEGER,
                 "UNSUPPORTED_FRAME_MAP", "frame handoff requires a representable integer ticks-per-second clock")
        _require(isinstance(frames, list) and 0 < len(frames) <= MAX_FRAMES,
                 "UNSUPPORTED_FRAME_MAP", "missing or oversized decoded frame map")
        pts, durations = [], []
        for frame in frames:
            p = frame.get("pts")
            d = frame.get("duration", frame.get("pkt_duration"))
            _require(_integer(p) and p >= 0 and _integer(d) and d > 0,
                     "UNSUPPORTED_FRAME_MAP", "actual non-negative PTS and positive duration are required for every decoded frame")
            pts.append(p)
            durations.append(d)
        _require(all(a < b and a + d == b for a, b, d in zip(pts, pts[1:], durations)),
                 "UNSUPPORTED_FRAME_MAP", "decoded PTS must be increasing and measured frame durations contiguous")
        terminal = pts[-1] + durations[-1]
        _require(_integer(terminal), "UNSUPPORTED_FRAME_MAP", "terminal boundary cannot be represented exactly")
        return {"streamIndex": streams[0]["index"], "timeBase": str(tick),
                "boundaryPts": pts + [terminal], "decodedFrames": len(pts)}
    except FrameMapError:
        raise
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OSError, RuntimeError) as exc:
        raise FrameMapError("UNSUPPORTED_FRAME_MAP", "could not read an actual decoded PTS map") from exc


def boundary_index(mapping: dict, seconds: float, *, parser_roundoff: bool = False) -> int:
    _require(isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and math.isfinite(seconds),
             "INVALID_FRAME_RANGE", "requested range must contain finite numeric seconds")
    ticks = Fraction(str(seconds)) / Fraction(mapping["timeBase"])
    pts = mapping["boundaryPts"]
    if parser_roundoff:
        # Native OTIO seconds use binary floats. Compensate only a millionth
        # of one measured tick at the two domain endpoints; raw OTIO integer
        # tick values are independently checked without this allowance.
        epsilon = Fraction(1, 1_000_000)
        if pts[0] - epsilon <= ticks < pts[0]:
            ticks = Fraction(pts[0])
        if pts[-1] < ticks <= pts[-1] + epsilon:
            ticks = Fraction(pts[-1])
    _require(pts[0] <= ticks <= pts[-1], "FRAME_RANGE_OUTSIDE_DECODED_SOURCE", "range lies outside actual decoded presentation boundaries")
    upper = bisect.bisect_left(pts, ticks)
    if upper == 0 or pts[upper] == ticks:
        return upper
    # Ties deliberately select the later presentation boundary, independently
    # for start and exclusive end. No nominal-frame-rate rounding or clamping.
    return upper if ticks - pts[upper - 1] >= pts[upper] - ticks else upper - 1


def _entries(spec: Any, mapping: dict) -> list[dict]:
    tick, pts, cursor = Fraction(mapping["timeBase"]), mapping["boundaryPts"], 0
    result = []
    for segment in spec.segments:
        start = boundary_index(mapping, segment.sourceRange["startSec"])
        end = boundary_index(mapping, segment.sourceRange["endSec"])
        _require(start < end, "EMPTY_NORMALIZED_FRAME_RANGE", "nearest-boundary normalization must retain at least one decoded frame")
        duration = pts[end] - pts[start]
        _require(cursor + duration <= MAX_EXACT_INTEGER, "UNSUPPORTED_FRAME_MAP", "cumulative frame timeline cannot be represented exactly")
        normalized_start, normalized_end = float(pts[start] * tick), float(pts[end] * tick)
        result.append({"segmentId": segment.segmentId, "requestedSourceRange": dict(segment.sourceRange),
                       "startBoundary": {"frameIndex": start, "pts": pts[start]},
                       "endBoundaryExclusive": {"frameIndex": end, "pts": pts[end]},
                       "normalizedSourceRange": {"startSec": normalized_start, "endSec": normalized_end},
                       "normalizationDeltaSec": {"startSec": normalized_start - segment.sourceRange["startSec"],
                                                  "endSec": normalized_end - segment.sourceRange["endSec"]},
                       "normalizedTimelineRange": {"startSec": float(cursor * tick), "endSec": float((cursor + duration) * tick)}})
        cursor += duration
    return result


def build_frame_map(spec: Any, video_path: Path | str, *, _tracker: dict | None = None) -> dict:
    import cutspec

    _require(spec.compute_spec_hash() == spec.specHash and cutspec.is_exportable(cutspec.validate_cutspec(spec)),
             "INVALID_SPEC", "frame map requires an unchanged exportable CutSpec")
    path = Path(video_path)
    _require(path.is_file(), "MISSING_SOURCE", "source media does not exist")
    source_hash = pipeline.file_sha256(path)
    _require(source_hash == spec.source.contentHash, "STALE_SOURCE", "frame map source differs from the CutSpec content revision")
    mapping = decoded_map(path, _tracker=_tracker)
    _require(pipeline.file_sha256(path) == source_hash, "STALE_SOURCE", "source changed while decoding its frame map")
    manifest = {"schema": SCHEMA, "specHash": spec.specHash, "sourceContentHash": source_hash,
                "normalization": NORMALIZATION, **mapping, "entries": _entries(spec, mapping)}
    manifest["manifestHash"] = manifest_hash(manifest)
    return manifest


def validate_frame_map(spec: Any, manifest: dict, video_path: Path | str, *, _tracker: dict | None = None) -> dict:
    _require(isinstance(manifest, dict) and manifest.get("schema") == SCHEMA,
             "INVALID_FRAME_MAP", "missing or unsupported measured frame companion")
    _require(manifest.get("manifestHash") == manifest_hash(manifest), "FRAME_MAP_HASH_MISMATCH", "frame companion changed")
    _require(manifest.get("specHash") == spec.specHash, "FRAME_MAP_SPEC_MISMATCH", "frame companion belongs to another CutSpec")
    measured = build_frame_map(spec, video_path, _tracker=_tracker)
    _require(manifest == measured, "FRAME_MAP_SOURCE_MISMATCH", "frame companion differs from actual decoded media or normalization")
    return {"status": "resolved", "manifestHash": manifest["manifestHash"], "normalization": NORMALIZATION,
            "timeBase": manifest["timeBase"], "decodedFrames": manifest["decodedFrames"], "entries": manifest["entries"]}


def export_marker(manifest: dict) -> dict:
    return {"schema": SCHEMA, "manifestHash": manifest["manifestHash"], "normalization": NORMALIZATION}


def verify_imported(manifest: dict, rows: list[dict], export_text: str, export_format: str) -> None:
    """Require actual imported ranges to identify the measured selected frames."""
    _require(len(rows) == len(manifest["entries"]), "FRAME_EXPORT_BOUNDARY_MISMATCH", "frame-mapped clip count changed")
    if export_format == "otio":
        data = json.loads(export_text)
        _require(data.get("metadata", {}).get("cutspec", {}).get("frameMap") == export_marker(manifest),
                 "FRAME_EXPORT_BINDING_MISMATCH", "OTIO does not bind this measured frame companion")
        clips = data["tracks"]["children"][0]["children"]
        rate = float(1 / Fraction(manifest["timeBase"]))
        for clip, entry in zip(clips, manifest["entries"]):
            expected = {"start_time": entry["startBoundary"]["pts"],
                        "duration": entry["endBoundaryExclusive"]["pts"] - entry["startBoundary"]["pts"]}
            for name, value in expected.items():
                actual = clip["source_range"][name]
                _require(actual["value"] == value and actual["rate"] == rate,
                         "FRAME_EXPORT_BOUNDARY_MISMATCH", "OTIO tick values differ from measured presentation boundaries")
            reference = clip["media_references"][clip["active_media_reference_key"]]
            _require(reference.get("OTIO_SCHEMA") == "ExternalReference.1", "FRAME_EXPORT_BOUNDARY_MISMATCH", "unsupported frame-mapped media reference")
            available = reference.get("available_range", {})
            for name, value in {"start_time": manifest["boundaryPts"][0],
                                "duration": manifest["boundaryPts"][-1] - manifest["boundaryPts"][0]}.items():
                actual = available.get(name, {})
                _require(actual.get("value") == value and actual.get("rate") == rate,
                         "FRAME_EXPORT_BOUNDARY_MISMATCH", "OTIO available media range differs from the measured stream")
    for row, entry in zip(rows, manifest["entries"]):
        _require(row["segmentId"] == entry["segmentId"], "FRAME_EXPORT_BOUNDARY_MISMATCH", "frame-mapped clip identity changed")
        start = boundary_index(manifest, row["sourceStartSec"], parser_roundoff=export_format == "otio")
        end = boundary_index(manifest, row["sourceEndSec"], parser_roundoff=export_format == "otio")
        _require(start == entry["startBoundary"]["frameIndex"] and end == entry["endBoundaryExclusive"]["frameIndex"],
                 "FRAME_EXPORT_BOUNDARY_MISMATCH", "imported range selects different decoded frames")
        if export_format == "otio":
            expected = entry["normalizedTimelineRange"]
            _require(abs(row["timelineStartSec"] - expected["startSec"]) <= 1e-9
                     and abs(row["timelineEndSec"] - expected["endSec"]) <= 1e-9,
                     "FRAME_EXPORT_BOUNDARY_MISMATCH", "imported timeline differs from measured cumulative durations")
