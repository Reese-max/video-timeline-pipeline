"""Offline canonical job/span resolution for versioned CutSpec companions.

Only transcript segments and timed speech timeline events are selectable.
Snapshots bind existing artifact bytes, not the truth of ASR or model output.
No summaries, inferred boundaries, secrets, network, or canonical writes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

import pipeline

SCHEMA = "cut-evidence/1"
REF_PREFIX = SCHEMA + ":"


class EvidenceError(ValueError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


def _require(condition: bool, code: str, detail: str) -> None:
    if not condition:
        raise EvidenceError(code, detail)


def _hash(value: Any) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=True, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise EvidenceError("MALFORMED_EVIDENCE", "non-canonical evidence JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


def _sha(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key}")
        result[key] = value
    return result


def _read(job: Path, relative: str) -> tuple[Any, dict]:
    path = job / relative
    _require(path.is_file(), "MISSING_EVIDENCE", f"missing canonical artifact {relative}")
    _require(path.resolve().is_relative_to(job), "MALFORMED_EVIDENCE", "artifact escapes source job")
    try:
        raw = path.read_bytes()
        data = json.loads(raw, object_pairs_hook=_unique_object,
                          parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)))
    except (OSError, UnicodeError, ValueError) as exc:
        raise EvidenceError("MALFORMED_EVIDENCE", f"invalid canonical artifact {relative}") from exc
    return data, {"file": relative, "sha256": hashlib.sha256(raw).hexdigest()}


def _snapshot(job_dir: Path | str, video_path: Path | str) -> dict:
    job = Path(job_dir).resolve()
    video = Path(video_path).resolve()
    _require(job.is_dir() and re.fullmatch(r"[0-9a-f]{12}", job.name) is not None,
             "INVALID_SOURCE_JOB", "source job must be an existing 12-hex job directory")
    _require(video.is_file(), "MISSING_SOURCE", "missing source media")
    manifest, manifest_identity = _read(job, "manifest.json")
    _require(isinstance(manifest, dict), "MALFORMED_EVIDENCE", "manifest must be an object")
    artifacts = [manifest_identity]
    actual_hash = pipeline.file_sha256(video)
    source = manifest.get("source")
    _require(isinstance(source, dict), "MALFORMED_EVIDENCE", "manifest has no source")
    if manifest.get("version") == 2:
        _require(manifest.get("status") == "ready" and manifest.get("job_id") == job.name,
                 "INVALID_SOURCE_JOB", "prepared manifest is not ready for this source job")
        result, identity = _read(job, "analysis/result.json")
        _require(isinstance(result, dict) and result.get("status") == "complete"
                 and result.get("job_id") == job.name,
                 "INVALID_ANALYSIS", "analysis is incomplete or belongs to another job")
        source_hash = result.get("source_sha256")
        native_revision = result.get("cache_signature")
        _require(_sha(native_revision), "INVALID_ANALYSIS", "analysis has no persistent cache revision")
        _require(result.get("source_fingerprint") == source.get("fingerprint")
                 and isinstance(source.get("fingerprint"), str) and source["fingerprint"],
                 "STALE_ANALYSIS", "prepared source and analysis fingerprints disagree")
        artifacts.append(identity)
        transcript, timeline = result.get("transcript"), result.get("timeline")
        locations = {"transcript": ("analysis/result.json", "/transcript/segments/"),
                     "timeline": ("analysis/result.json", "/timeline/")}
    elif manifest.get("schema_version") == 1:
        _require(manifest.get("status") in {"completed", "local-only"},
                 "INVALID_ANALYSIS", "canonical analysis job is incomplete")
        expected_job = f"jobs/{job.name}"
        _require(manifest.get("job_dir") in {expected_job, str(Path(expected_job)), str(job)},
                 "INVALID_SOURCE_JOB", "manifest belongs to another source job")
        declarations = manifest.get("artifacts")
        _require(isinstance(declarations, dict), "MALFORMED_EVIDENCE", "missing canonical artifact declarations")
        for name in ("transcript", "timeline"):
            _require(declarations.get(name) in {f"{manifest['job_dir']}/{name}.json",
                                               str(Path(manifest["job_dir"]) / f"{name}.json"), str(job / f"{name}.json")},
                     "MALFORMED_EVIDENCE", f"invalid canonical {name} declaration")
        locator = source.get("resolved_video")
        _require(isinstance(locator, str) and locator, "MISSING_SOURCE", "manifest has no resolved media")
        declared_video = Path(locator)
        if not declared_video.is_absolute():
            _require(job.parent.name == "jobs", "INVALID_SOURCE_JOB", "relative source requires canonical jobs layout")
            declared_video = job.parent.parent / declared_video
        _require(declared_video.resolve() == video, "STALE_SOURCE", "source locator belongs to different media")
        source_hash = manifest.get("source_sha256")
        native_revision = None  # exact artifact byte hashes are the CLI analysis revision
        transcript, identity = _read(job, "transcript.json")
        artifacts.append(identity)
        timeline, identity = _read(job, "timeline.json")
        artifacts.append(identity)
        locations = {"transcript": ("transcript.json", "/segments/"),
                     "timeline": ("timeline.json", "/")}
    else:
        raise EvidenceError("INVALID_SOURCE_JOB", "unsupported canonical manifest version")
    _require(_sha(source_hash), "MISSING_SOURCE_HASH", "job has no persistent source SHA256; rerun analysis explicitly")
    _require(source_hash == actual_hash, "STALE_SOURCE", "source bytes differ from the analyzed source revision")
    if manifest.get("source_sha256") is not None:
        _require(manifest["source_sha256"] == source_hash, "STALE_ANALYSIS", "manifest and analysis source hashes disagree")
    _require(isinstance(transcript, dict) and isinstance(transcript.get("segments"), list)
             and isinstance(timeline, list), "MALFORMED_EVIDENCE", "invalid transcript/timeline container")
    revision = _hash({"sourceJob": job.name, "sourceContentHash": source_hash,
                      "artifacts": artifacts, "nativeAnalysisRevision": native_revision})
    return {"sourceJob": job.name, "sourceContentHash": source_hash,
            "analysisRevision": revision, "nativeAnalysisRevision": native_revision,
            "artifacts": artifacts, "transcript": transcript, "timeline": timeline,
            "locations": locations}


def _span(snapshot: dict, artifact: str, index: int, duration: float) -> dict:
    _require(artifact in {"transcript", "timeline"} and isinstance(index, int)
             and not isinstance(index, bool) and index >= 0,
             "INVALID_EVIDENCE_LOCATOR", "select transcript/timeline by a non-negative integer index")
    rows = snapshot["transcript"]["segments"] if artifact == "transcript" else snapshot["timeline"]
    _require(index < len(rows), "MISSING_EVIDENCE_SPAN", "selected canonical span does not exist")
    row = rows[index]
    _require(isinstance(row, dict), "MALFORMED_EVIDENCE", "selected span must be an object")
    if artifact == "transcript":
        _require(snapshot["transcript"].get("status") != "skipped", "UNTIMED_EVIDENCE", "transcript was skipped")
        start, end = row.get("start"), row.get("end")
    else:
        _require(row.get("type") == "speech", "UNTIMED_EVIDENCE", "point observations and summaries have no exact cut interval")
        start, end = row.get("timestamp"), row.get("end")
    _require(all(isinstance(value, (int, float)) and not isinstance(value, bool)
                 and math.isfinite(value) for value in (start, end)),
             "UNTIMED_EVIDENCE", "span must have finite numeric start/end timestamps")
    _require(0 <= start < end <= duration, "INVALID_EVIDENCE_RANGE", "canonical span is outside the source or has no duration")
    _require(isinstance(row.get("text"), str) and row["text"].strip(),
             "MALFORMED_EVIDENCE", "speech span has no transcript text")
    file, prefix = snapshot["locations"][artifact]
    pointer = prefix + str(index)
    span_hash = _hash(row)
    reference = (f"{REF_PREFIX}{snapshot['sourceJob']}:{snapshot['analysisRevision']}:"
                 f"{file}:{pointer}:{span_hash}")
    return {"artifact": artifact, "index": index, "file": file, "pointer": pointer,
            "spanHash": span_hash, "evidenceRef": reference,
            "sourceRange": {"startSec": float(start), "endSec": float(end)}}


def manifest_hash(manifest: dict) -> str:
    payload = copy.deepcopy(manifest)
    payload.pop("manifestHash", None)
    return _hash(payload)


def build_evidence_cutspec(job_dir: Path | str, video_path: Path | str,
                           selections: list[dict]) -> tuple[Any, dict]:
    """Resolve explicit selections and derive exact ranges; never accept guessed ranges."""
    import cutspec

    _require(isinstance(selections, list) and bool(selections), "EMPTY_SPEC", "select at least one canonical span")
    snapshot = _snapshot(job_dir, video_path)
    source = cutspec.source_from_video(video_path)
    _require(source.contentHash == snapshot["sourceContentHash"], "STALE_SOURCE", "source changed during evidence resolution")
    segments, entries = [], []
    allowed = {"artifact", "index", "segmentId", "selection", "reason", "confidence", "note"}
    for index, selected in enumerate(selections):
        _require(isinstance(selected, dict) and not (set(selected) - allowed),
                 "INVALID_EVIDENCE_LOCATOR", "selections may not provide ranges, summaries, or unknown fields")
        confidence = selected.get("confidence", 1.0)
        _require(isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
                 and math.isfinite(confidence) and 0 <= confidence <= 1,
                 "INVALID_SELECTION", "selection confidence must be a finite number between zero and one")
        _require(all(isinstance(selected.get(key, ""), str) for key in ("reason", "note"))
                 and isinstance(selected.get("selection", "manual"), str)
                 and selected.get("selection", "manual") in cutspec.SELECTION_REASONS,
                 "INVALID_SELECTION", "selection metadata must use the declared string contract")
        entry = _span(snapshot, selected.get("artifact"), selected.get("index"), source.durationSec)
        segment_id = selected.get("segmentId", f"seg-{index}")
        entry["segmentId"] = segment_id
        segments.append(cutspec.CutSegment(segment_id, entry["sourceRange"].copy(), {}, entry["evidenceRef"],
                                          selected.get("selection", "manual"), selected.get("reason", "canonical evidence selection"),
                                          confidence, selected.get("note", "")))
        entries.append(entry)
    spec = cutspec.build_cutspec(source, segments)
    _require(cutspec.is_exportable(cutspec.validate_cutspec(spec)), "INVALID_SPEC", "canonical selections produced a rejected spec")
    manifest = {"schema": SCHEMA, "specHash": spec.specHash,
                **{key: snapshot[key] for key in ("sourceJob", "sourceContentHash", "analysisRevision", "nativeAnalysisRevision", "artifacts")},
                "entries": entries}
    manifest["manifestHash"] = manifest_hash(manifest)
    validate_evidence(spec, manifest, job_dir, video_path)
    return spec, manifest


def requires_evidence(spec: Any) -> bool:
    return any(isinstance(segment.evidenceRef, str) and segment.evidenceRef.startswith(REF_PREFIX)
               for segment in spec.segments)


def validate_evidence(spec: Any, manifest: dict, job_dir: Path | str,
                      video_path: Path | str | None = None) -> dict:
    """Re-resolve every entry against current canonical bytes; fail closed on drift."""
    _require(isinstance(manifest, dict) and manifest.get("schema") == SCHEMA,
             "INVALID_EVIDENCE_MANIFEST", "missing or unsupported evidence companion")
    _require(_sha(manifest.get("manifestHash")) and manifest_hash(manifest) == manifest["manifestHash"],
             "EVIDENCE_MANIFEST_HASH_MISMATCH", "evidence companion was modified")
    _require(spec.compute_spec_hash() == spec.specHash and manifest.get("specHash") == spec.specHash,
             "EVIDENCE_SPEC_MISMATCH", "evidence companion does not bind this exact CutSpec")
    snapshot = _snapshot(job_dir, video_path or spec.source.videoPath)
    _require(snapshot["sourceContentHash"] == spec.source.contentHash,
             "STALE_SOURCE", "CutSpec belongs to different source bytes")
    _require(manifest.get("sourceJob") == snapshot["sourceJob"], "INVALID_SOURCE_JOB", "evidence belongs to a different job")
    for key in ("sourceContentHash", "analysisRevision", "nativeAnalysisRevision", "artifacts"):
        _require(manifest.get(key) == snapshot[key], "STALE_ANALYSIS", "canonical analysis artifact bytes or revision changed")
    entries = manifest.get("entries")
    _require(isinstance(entries, list) and len(entries) == len(spec.segments),
             "EVIDENCE_SPEC_MISMATCH", "all cut entries require evidence")
    for segment, entry in zip(spec.segments, entries):
        _require(isinstance(entry, dict), "INVALID_EVIDENCE_MANIFEST", "evidence entry is malformed")
        resolved = _span(snapshot, entry.get("artifact"), entry.get("index"), spec.source.durationSec)
        resolved["segmentId"] = segment.segmentId
        _require(entry == resolved and entry["evidenceRef"] == segment.evidenceRef
                 and entry["sourceRange"] == segment.sourceRange,
                 "EVIDENCE_SPAN_MISMATCH", "clip identity/range differs from its actual canonical span")
    return {"status": "resolved", "manifestHash": manifest["manifestHash"],
            "sourceJob": snapshot["sourceJob"], "analysisRevision": snapshot["analysisRevision"],
            "sourceContentHash": snapshot["sourceContentHash"], "provenanceCoverage": 1.0,
            "entries": copy.deepcopy(entries)}
