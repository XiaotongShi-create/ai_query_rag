"""Audit trail and user feedback, written to S3 as one small JSON object per event.

Why one object per event instead of appending to a shared file:
  * concurrent users can't overwrite each other (no read-modify-write),
  * events are immutable -- feedback is a *new* event that points at the answer's
    event_id, so the original record is never edited,
  * keys are partitioned (audit/dt=YYYY-MM-DD/...) so Athena or Redshift Spectrum can
    query them in place later for accuracy / usage monitoring.

Logging must never break an answer, so writes return an error string instead of raising;
the UI shows it, because an audit trail that fails silently is itself a governance gap.
"""
import json
import os
import uuid
from datetime import datetime, timezone

import boto3

AUDIT_BUCKET = os.environ.get("AUDIT_BUCKET", "simplesql-logs-rag")
AUDIT_PREFIX = "audit"
FEEDBACK_PREFIX = "feedback"


def new_event_id():
    return uuid.uuid4().hex


def reliability_signal(query_log):
    """A cheap, honest proxy for how much to trust an answer -- NOT a calibrated confidence score.

    It only reflects how the agent got to its answer; it says nothing about whether the SQL
    meant what the user meant. Real confidence scoring would need e.g. self-consistency
    (run twice, compare results) calibrated against the eval set.
    """
    ok = [q for q in query_log if q["status"] == "ok"]
    failed = len(query_log) - len(ok)
    if ok and failed == 0:
        return "first_attempt"
    if ok:
        return "after_retry"
    if query_log:
        return "all_attempts_failed"
    return "no_query"  # a clarifying question or a refusal


def _put(prefix, event_id, record):
    now = datetime.now(timezone.utc)
    key = f"{prefix}/dt={now:%Y-%m-%d}/{event_id}.json"
    boto3.client("s3").put_object(
        Bucket=AUDIT_BUCKET,
        Key=key,
        Body=json.dumps(record, default=str).encode("utf-8"),
        ContentType="application/json",
    )
    return key


def log_answer(event_id, *, session_id, schema_type, model, question, answer, query_log, latency_s):
    """Record one agent turn. Returns None on success, or an error string."""
    record = {
        "event_id": event_id,
        "event_type": "answer",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": session_id,
        "schema_type": schema_type,
        "model": model,
        "question": question,
        "answer": answer,
        "queries": query_log,  # every attempt: sql, status (ok/rejected/error), error, rows
        "n_queries": len(query_log),
        "n_failed_queries": sum(1 for q in query_log if q["status"] != "ok"),
        "reliability_signal": reliability_signal(query_log),
        "latency_s": round(latency_s, 2),
    }
    try:
        _put(AUDIT_PREFIX, event_id, record)
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"


def log_feedback(answer_event_id, session_id, rating, comment=None):
    """Record a thumbs up/down against an earlier answer. rating is 'up' or 'down'."""
    record = {
        "event_id": new_event_id(),
        "event_type": "feedback",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "answer_event_id": answer_event_id,
        "session_id": session_id,
        "rating": rating,
        "comment": comment,
    }
    try:
        _put(FEEDBACK_PREFIX, record["event_id"], record)
        return None
    except Exception as e:
        return f"{type(e).__name__}: {e}"
