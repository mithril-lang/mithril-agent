"""Retained manual requests using the original claim, runner and private ledger.

An uncertain attempt is never automatically repeated. This local edge does not
grant execution ownership: the original runner's execution policy still applies.
"""
import hashlib
import json
import re
import sqlite3

from cron.executions import _transaction
from cron.source_restore import OriginalSourceVersionMismatch, _IDENTITY


def _request_identity(request: dict) -> tuple[str, str]:
    keys = {"owner", "profile", "operationId", "jobId", "expectedVersion"}
    if not isinstance(request, dict) or set(request) != keys:
        raise ValueError("operation")
    if (any(not isinstance(request[key], str) or not _IDENTITY.fullmatch(request[key])
            for key in keys - {"expectedVersion"}) or len(request["profile"]) > 64):
        raise ValueError("identity")
    if (not isinstance(request["expectedVersion"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", request["expectedVersion"])):
        raise ValueError("operation")
    fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True,
        separators=(",", ":")).encode("utf-8")).hexdigest()
    return request["operationId"], fingerprint


def inspect_original_request(request: dict) -> dict:
    """Read a retained result without creating a marker, claim or execution."""
    operation, fingerprint = _request_identity(request)
    from cron import executions
    from hermes_constants import get_hermes_home
    path = executions.EXECUTIONS_FILE or (get_hermes_home().resolve() / "cron" / "executions.db")
    if not path.exists():
        return {**request, "status": "absent"}
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='original_run_requests'").fetchone() is None:
            return {**request, "status": "absent"}
        row = conn.execute("SELECT fingerprint,status FROM original_run_requests WHERE operation_id=?", (operation,)).fetchone()
        if row is None:
            return {**request, "status": "absent"}
        if row[0] != fingerprint or row[1] not in {"unknown", "rejected", "completed"}:
            raise ValueError("operation")
        return {**request, "status": row[1]}
    finally:
        conn.close()


def run_original_request(request: dict) -> dict:
    operation, fingerprint = _request_identity(request)
    with _transaction() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS original_run_requests (
            operation_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('unknown','rejected','completed'))
        )""")
        row = conn.execute("SELECT fingerprint,status FROM original_run_requests WHERE operation_id=?",
                           (operation,)).fetchone()
        if row is not None:
            if row["fingerprint"] != fingerprint:
                raise ValueError("operation")
            return {**request, "status": row["status"]}
        # Commit BEFORE the claim or any effect. A crash/replayed request returns
        # unknown rather than causing a second invocation, even after claim TTL.
        conn.execute("INSERT INTO original_run_requests VALUES (?,?,'unknown')",
                     (operation, fingerprint))
    status = "unknown"
    try:
        from cron.jobs import claim_job_for_fire
        claimed = claim_job_for_fire(request["jobId"], manual=True, return_job=True,
                                     expected_version=request["expectedVersion"])
    except OriginalSourceVersionMismatch:
        status = "rejected"
    else:
        if not isinstance(claimed, dict):
            status = "rejected"
        else:
            from tools.cronjob_tools import _run_claimed_job
            result = _run_claimed_job(claimed)
            if result.get("success") is True:
                status = "completed"
    if status != "unknown":
        with _transaction() as conn:
            conn.execute("UPDATE original_run_requests SET status=? WHERE operation_id=? AND fingerprint=?",
                         (status, operation, fingerprint))
    return {**request, "status": status}
