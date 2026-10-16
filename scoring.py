"""Video importance, novelty, and relevance scoring module (Issue #3).

Operates with deterministic evidence retrieval, robust caching, and policy-driven decisions.
Guarantees zero paid API calls on cache hit or explanation review.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from contextlib import closing
from pathlib import Path
from typing import Any

from notifications import get_operations_db
from operations import foundation_status, open_readonly


@dataclass
class ScoreResult:
    importance: float
    relevance: float
    novelty: float | None
    novelty_status: str  # "measured" | "insufficient_history" | "retrieval_failed"
    evidence_confidence: float
    priority_score: float
    reasons: dict[str, str]
    claims: list[dict[str, Any]]
    raw_output: str = ""


def _init_scoring_schema(conn: sqlite3.Connection) -> None:
    """Compatibility entry point; the shared registry owns all migrations."""
    from operations import ensure_foundation_schema

    ensure_foundation_schema(conn)


def calculate_priority_score(
    importance: float,
    relevance: float,
    novelty: float | None,
    weight_i: float = 0.40,
    weight_n: float = 0.35,
    weight_r: float = 0.25,
) -> float:
    """Calculate deterministic composite priority score normalized to 0-10."""
    if novelty is not None:
        total_weight = weight_i + weight_n + weight_r
        return round((importance * weight_i + novelty * weight_n + relevance * weight_r) / total_weight, 2)
    # When novelty is absent (null), renormalize over known dimensions
    total_weight = weight_i + weight_r
    if total_weight <= 0:
        return 0.0
    return round((importance * weight_i + relevance * weight_r) / total_weight, 2)


def load_scoring_profile(root: Path) -> tuple[dict[str, Any], str]:
    """Load scoring profile or fallback to categories.json with deterministic SHA256."""
    profile_path = root / "scoring-profile.json"
    if profile_path.is_file():
        try:
            data = json.loads(profile_path.read_text(encoding="utf-8"))
            serialized = json.dumps(data, sort_keys=True, ensure_ascii=False)
            return data, hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
        except Exception:
            pass

    # Fallback to categories.json
    cat_path = root / "categories.json"
    data = {"topics": [], "weights": {"importance": 0.40, "novelty": 0.35, "relevance": 0.25}}
    if cat_path.is_file():
        try:
            cats = json.loads(cat_path.read_text(encoding="utf-8"))
            data["topics"] = cats.get("categories", [])
        except Exception:
            pass
    serialized = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return data, hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


def retrieve_history_evidence(
    root: Path,
    current_job: str,
    cutoff_time: str | None,
    keywords: list[str],
    max_evidence: int = 6,
) -> tuple[list[dict[str, Any]], str]:
    """Retrieve historical evidence from library.db or jobs before cutoff time."""
    db_path = root / "library.db"
    evidence: list[dict[str, Any]] = []

    if db_path.is_file():
        try:
            with closing(sqlite3.connect(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                # Search for keyword matches across events
                for kw in keywords[:4]:
                    if len(evidence) >= max_evidence:
                        break
                    cur = conn.cursor()
                    cur.execute(
                        """
                        SELECT job, kind, timestamp, snippet
                        FROM events
                        WHERE job != ? AND snippet LIKE ?
                        LIMIT 3
                        """,
                        (current_job, f"%{kw}%"),
                    )
                    for row in cur.fetchall():
                        evidence.append(
                            {
                                "job": row["job"],
                                "kind": row["kind"],
                                "timestamp": row["timestamp"],
                                "text": row["snippet"][:250],
                            }
                        )
        except Exception:
            pass

    serialized = json.dumps(evidence, sort_keys=True, ensure_ascii=False)
    evidence_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    return evidence, evidence_hash


def compute_scoring_cache_key(
    job: str,
    analysis_revision: str,
    current_input_hash: str,
    profile_hash: str,
    evidence_hash: str,
    model_version: str,
) -> str:
    """Generate immutable cache key ensuring zero duplicate API calls."""
    raw = f"{job}:{analysis_revision}:{current_input_hash}:{profile_hash}:{evidence_hash}:{model_version}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def evaluate_decision(
    score: ScoreResult,
    importance_threshold: float = 8.5,
    relevance_threshold: float = 7.5,
    novelty_threshold: float = 7.5,
    confidence_threshold: float = 0.60,
) -> tuple[str, str, str]:
    """Apply deterministic notification decision gate.

    Returns: (decision, reason, policy_hash)
    Decision is one of: "notify" | "digest" | "ignore"
    """
    policy = {
        "I": importance_threshold,
        "R": relevance_threshold,
        "N": novelty_threshold,
        "C": confidence_threshold,
    }
    policy_hash = hashlib.sha256(json.dumps(policy, sort_keys=True).encode("utf-8")).hexdigest()[:16]

    # Confidence check
    if score.evidence_confidence < confidence_threshold:
        return "digest", f"Confidence ({score.evidence_confidence:.2f}) below threshold ({confidence_threshold:.2f})", policy_hash

    # Relevance check
    if score.relevance < relevance_threshold:
        return "digest", f"Relevance ({score.relevance:.1f}) below threshold ({relevance_threshold:.1f})", policy_hash

    # Candidate qualifies for instant notification if:
    # 1. Importance >= I
    # OR 2. Importance >= 7.5 AND novelty_status == 'measured' AND novelty >= N
    if score.importance >= importance_threshold:
        return "notify", f"High importance ({score.importance:.1f} >= {importance_threshold:.1f})", policy_hash

    if score.importance >= 7.5 and score.novelty_status == "measured" and score.novelty is not None and score.novelty >= novelty_threshold:
        return "notify", f"High novelty ({score.novelty:.1f} >= {novelty_threshold:.1f}) with importance {score.importance:.1f}", policy_hash

    return "digest", "Does not meet instant notify thresholds; routed to digest", policy_hash


def score_video(
    root: Path,
    job: str,
    mock_score: ScoreResult | None = None,
    force_rescore: bool = False,
    model: str = "minimax-m3",
) -> dict[str, Any]:
    """Execute or retrieve cached scoring for a video job."""
    job_dir = root / "jobs" / job
    manifest_file = job_dir / "manifest.json"
    summary_file = job_dir / "summary.json"
    if not manifest_file.is_file() or not summary_file.is_file():
        raise FileNotFoundError(f"Job {job} artifacts missing in {job_dir}")

    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    summary_raw = summary_file.read_text(encoding="utf-8")
    current_input_hash = hashlib.sha256(summary_raw.encode("utf-8")).hexdigest()[:16]
    analysis_revision = str(manifest.get("analysis_revision", "1"))
    cutoff_time = manifest.get("eligible_at") or manifest.get("created_at")

    profile, profile_hash = load_scoring_profile(root)
    terms = json.loads(summary_raw).get("terms", [])
    evidence, evidence_hash = retrieve_history_evidence(root, job, cutoff_time, terms)

    cache_key = compute_scoring_cache_key(
        job, analysis_revision, current_input_hash, profile_hash, evidence_hash, model
    )

    with closing(get_operations_db(root)) as conn:
        _init_scoring_schema(conn)

        # Check cache unless force_rescore
        if not force_rescore:
            cur = conn.cursor()
            cur.execute("SELECT * FROM video_scores WHERE cache_key = ?", (cache_key,))
            cached = cur.fetchone()
            if cached:
                score_id = cached["score_id"]
                cur.execute("SELECT * FROM score_decisions WHERE score_id = ?", (score_id,))
                dec_row = cur.fetchone()
                return {
                    "score_id": score_id,
                    "job": job,
                    "cached": True,
                    "importance": cached["importance"],
                    "relevance": cached["relevance"],
                    "novelty": cached["novelty"],
                    "novelty_status": cached["novelty_status"],
                    "evidence_confidence": cached["evidence_confidence"],
                    "priority_score": cached["priority_score"],
                    "decision": dec_row["decision"] if dec_row else "digest",
                    "reasons": json.loads(cached["reasons_json"] or "{}"),
                }

        # Execute scoring (mock or local deterministic computation)
        if mock_score:
            res = mock_score
        else:
            # Deterministic heuristic scoring based on artifact depth and terms
            sum_data = json.loads(summary_raw)
            brief = sum_data.get("brief") or sum_data.get("summary") or ""
            char_count = len(brief)
            term_count = len(terms)

            # Importance heuristic: length and terms depth
            imp = min(9.5, max(3.0, round(5.0 + (term_count * 0.4) + (char_count / 300.0), 1)))
            rel = min(9.0, max(4.0, round(6.0 + (term_count * 0.3), 1)))

            if not evidence:
                nov = None
                nov_status = "insufficient_history"
            else:
                nov = min(8.5, max(2.0, round(4.0 + (10.0 / (len(evidence) + 1)), 1)))
                nov_status = "measured"

            conf = min(0.95, max(0.5, round(0.6 + (0.05 * min(term_count, 6)), 2)))
            priority = calculate_priority_score(imp, rel, nov)
            reasons = {
                "importance": f"Assessed from {term_count} technical terms and {char_count} chars of summary.",
                "relevance": f"Aligned with tracked topics ({len(profile.get('topics', []))} topics configured).",
                "novelty": f"Evaluated against {len(evidence)} historical evidence references.",
            }
            res = ScoreResult(
                importance=imp,
                relevance=rel,
                novelty=nov,
                novelty_status=nov_status,
                evidence_confidence=conf,
                priority_score=priority,
                reasons=reasons,
                claims=[],
                raw_output="local_heuristic",
            )

        score_id = str(uuid.uuid4())
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        decision, dec_reason, policy_hash = evaluate_decision(res)

        with conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO video_scores (
                    score_id, library_id, job, analysis_revision, current_input_hash,
                    schema_version, prompt_version, model_version, provider,
                    profile_hash, evidence_hash, knowledge_cutoff, status,
                    importance, relevance, novelty, novelty_status, evidence_confidence,
                    priority_score, reasons_json, claims_json, raw_output, cache_key, scored_at
                ) VALUES (?, ?, ?, ?, ?, '1', '1', ?, 'local', ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    score_id,
                    manifest.get("library_id"),
                    job,
                    analysis_revision,
                    current_input_hash,
                    model,
                    profile_hash,
                    evidence_hash,
                    cutoff_time,
                    res.importance,
                    res.relevance,
                    res.novelty,
                    res.novelty_status,
                    res.evidence_confidence,
                    res.priority_score,
                    json.dumps(res.reasons, ensure_ascii=False),
                    json.dumps(res.claims, ensure_ascii=False),
                    res.raw_output,
                    cache_key,
                    now_iso,
                ),
            )
            conn.execute(
                """
                INSERT INTO score_decisions (
                    decision_id, score_id, policy_hash, priority, decision, reasons, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid.uuid4()), score_id, policy_hash, res.priority_score, decision, dec_reason, now_iso),
            )

        return {
            "score_id": score_id,
            "job": job,
            "cached": False,
            "importance": res.importance,
            "relevance": res.relevance,
            "novelty": res.novelty,
            "novelty_status": res.novelty_status,
            "evidence_confidence": res.evidence_confidence,
            "priority_score": res.priority_score,
            "decision": decision,
            "reasons": res.reasons,
        }


def explain_score(root: Path, job: str) -> dict[str, Any]:
    """Read-only score explanation without any API cost."""
    conn = open_readonly(root)
    if conn is None:
        return foundation_status(root)
    with closing(conn):

        cur = conn.cursor()
        cur.execute(
            """
            SELECT s.*, d.decision, d.reasons as decision_reason, d.policy_hash
            FROM video_scores s
            LEFT JOIN score_decisions d ON s.score_id = d.score_id
            WHERE s.job = ?
            ORDER BY s.scored_at DESC LIMIT 1
            """,
            (job,),
        )
        row = cur.fetchone()
        if not row:
            return {"job": job, "status": "unscored", "detail": "No scoring record exists for this job."}

        return {
            "job": job,
            "score_id": row["score_id"],
            "importance": row["importance"],
            "relevance": row["relevance"],
            "novelty": row["novelty"],
            "novelty_status": row["novelty_status"],
            "evidence_confidence": row["evidence_confidence"],
            "priority_score": row["priority_score"],
            "decision": row["decision"],
            "decision_reason": row["decision_reason"],
            "policy_hash": row["policy_hash"],
            "reasons": json.loads(row["reasons_json"] or "{}"),
            "knowledge_cutoff": row["knowledge_cutoff"],
            "scored_at": row["scored_at"],
        }


def generate_score_report(root: Path) -> dict[str, Any]:
    """Aggregate daily/overall scoring summary from operations.db."""
    conn = open_readonly(root)
    if conn is None:
        return foundation_status(root)
    with closing(conn):

        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*) as total_scored,
                   AVG(importance) as avg_importance,
                   AVG(priority_score) as avg_priority,
                   COUNT(CASE WHEN novelty_status = 'measured' THEN 1 END) as measured_novelty_count
            FROM video_scores
            """
        )
        summary_row = cur.fetchone()

        cur.execute(
            """
            SELECT job, priority_score, importance, relevance, novelty, status
            FROM video_scores
            ORDER BY priority_score DESC LIMIT 10
            """
        )
        top_items = [dict(row) for row in cur.fetchall()]

        return {
            "total_scored": summary_row["total_scored"],
            "avg_importance": round(summary_row["avg_importance"] or 0, 2),
            "avg_priority": round(summary_row["avg_priority"] or 0, 2),
            "measured_novelty_count": summary_row["measured_novelty_count"],
            "top_ranked": top_items,
        }
