"""Versioned restoration of locally bound original schedule files.

This is a native storage boundary, not a cloud-body importer: the Desktop
adapter must bind device resources and execution ownership before calling it.
It retains the original file shape and unknown metadata instead of projecting
jobs through the CLI list format. Runtime claims never come from another replica.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from cron import jobs
from utils import atomic_write_text

_LIMIT = 20 * 1024 * 1024
_IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,160}\Z")
_RUNTIME_CLAIMS = ("run_claim", "fire_claim", "pending_slot")


def _digest(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _checked(path: Path) -> None:
    for ancestor in (path, *path.parents):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("unsafe")
    if path.exists() and not path.is_file():
        raise ValueError("unsafe")


def _read(path: Path, limit: int = _LIMIT) -> bytes | None:
    _checked(path)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("unsafe")
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        current = path.lstat()
        if len(data) > limit:
            raise ValueError("oversize")
        def identity(value):
            return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if identity(info) != identity(current) or identity(info) != identity(after):
            raise ValueError("conflict")
        data.decode("utf-8")
        return data


def _rows(file):
    rows = file if isinstance(file, list) else file.get("jobs") if isinstance(file, dict) else None
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError("inventory")
    identities = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not 0 < len(row["id"]) <= 256:
            raise ValueError("inventory")
        if row["id"] in identities:
            raise ValueError("inventory")
        identities.add(row["id"])
    return rows


def _parse(data: bytes):
    def reject_constant(_):
        raise ValueError("inventory")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("inventory")
            result[key] = value
        return result
    file = json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique, parse_constant=reject_constant)
    _rows(file)
    return file


def _write(path: Path, text: str) -> None:
    _checked(path)
    atomic_write_text(path, text, mode=0o600, preserve_mode=True, fsync_dir=True)


@contextlib.contextmanager
def _restore_fences(path, receipt_path, fingerprint, expected_version, target_version, target_rows):
    """Take original fire fences BEFORE the jobs lock, never wait on delivery.

    A firing worker takes the same order; reversing it would deadlock a restore
    against its owner save. Unchanged records need no side-effect fence, but the
    whole-file CAS still catches every concurrent ticker save.
    """
    previous = _read(receipt_path, 4 * _LIMIT)
    receipt = json.loads(previous) if previous is not None else None
    if receipt is not None:
        if receipt.get("fingerprint") != fingerprint or receipt.get("state") not in ("pending", "complete"):
            raise ValueError("operation")
        if receipt["state"] == "complete":
            yield
            return
    preview = _read(path)
    if receipt is not None and _digest(preview) == target_version:
        yield
        return
    if _digest(preview) != expected_version:
        raise ValueError("conflict")
    current_rows = _rows(_parse(preview)) if preview is not None else []
    current = {row["id"]: row for row in current_rows}
    desired = {row["id"]: row for row in target_rows}
    with contextlib.ExitStack() as stack:
        for job_id in sorted(current.keys() | desired.keys()):
            if json.dumps(current.get(job_id), sort_keys=True) == json.dumps(desired.get(job_id), sort_keys=True):
                continue
            if not stack.enter_context(jobs._fire_job_lock(job_id, wait=False)):
                raise ValueError("busy")
        yield


def restore_original_store(*, owner: str, profile: str, operation_id: str,
                           expected_version: str | None, file) -> dict:
    """CAS restore in the active original Cron store; no profile-global mutation.

    The caller supplies the fixed local profile context via ``use_cron_store``.
    Complete receipts survive newer local edits, so lost-ack retries return the
    original acknowledgement without overwriting those edits. Pending receipts
    retain the exact pre-write bytes for recovery. They contain private data and
    stay in this profile's 0600 storage, never in the cloud repository.
    """
    if not all(isinstance(value, str) and _IDENTITY.fullmatch(value) for value in (owner, profile, operation_id)):
        raise ValueError("identity")
    if len(profile) > 64 or (expected_version is not None and (
            not isinstance(expected_version, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_version))):
        raise ValueError("identity")
    _rows(file)
    target = json.dumps(file, ensure_ascii=False, allow_nan=False, indent=2)
    target_bytes = target.encode("utf-8")
    if len(target_bytes) > _LIMIT:
        raise ValueError("oversize")
    file = _parse(target_bytes)
    target_version = _digest(target_bytes)
    fingerprint = hashlib.sha256(json.dumps([owner, profile, operation_id, expected_version, target_version]).encode()).hexdigest()
    path = jobs._current_cron_store().jobs_file
    _checked(path)
    _checked(jobs._jobs_lock_file())
    key = hashlib.sha256(json.dumps([owner, profile, operation_id]).encode()).hexdigest()
    receipt_path = path.parent / (".mithril-source-" + key + ".json")
    with _restore_fences(path, receipt_path, fingerprint, expected_version, target_version, _rows(file)), jobs._jobs_lock(require_cross_process=True):
        _checked(jobs._jobs_lock_file())
        receipt_bytes = _read(receipt_path, 4 * _LIMIT)
        receipt = json.loads(receipt_bytes) if receipt_bytes is not None else None
        if receipt is not None:
            if receipt.get("fingerprint") != fingerprint:
                raise ValueError("operation")
            if receipt.get("state") == "complete":
                return {"owner": owner, "profile": profile, "operationId": operation_id, "version": target_version}
            if receipt.get("state") != "pending":
                raise ValueError("operation")
        current = _read(path)
        if receipt is not None and _digest(current) == target_version:
            receipt["state"] = "complete"
            _write(receipt_path, json.dumps(receipt, ensure_ascii=False))
            return {"owner": owner, "profile": profile, "operationId": operation_id, "version": target_version}
        if _digest(current) != expected_version:
            raise ValueError("conflict")
        existing = _rows(_parse(current)) if current is not None else []
        by_id = {row["id"]: row for row in existing}
        now = jobs._hermes_now()
        for row in existing:
            if (jobs._job_running_in_this_process(row["id"]) or row.get("pending_slot") is not None
                    or jobs._claim_is_live(row.get("run_claim"), now, jobs._oneshot_run_claim_ttl_seconds())
                    or jobs._claim_is_live(row.get("fire_claim"), now, jobs.FIRE_CLAIM_TTL_SECONDS)):
                raise ValueError("busy")
        for row in _rows(file):
            original = by_id.get(row["id"], {})
            if any(row.get(field) != original.get(field) for field in _RUNTIME_CLAIMS):
                raise ValueError("runtime")
            # New records are restored as data first. The original resume/create
            # path activates them only after device bindings and ownership exist.
            if not original and (row.get("enabled") is not False or row.get("state") not in ("paused", "completed")):
                raise ValueError("ownership")
        if receipt is None:
            receipt = {"fingerprint": fingerprint, "state": "pending",
                       "before": current.decode("utf-8") if current is not None else None,
                       "after": target}
            _write(receipt_path, json.dumps(receipt, ensure_ascii=False))
        _write(path, target)
        receipt["state"] = "complete"
        _write(receipt_path, json.dumps(receipt, ensure_ascii=False))
        jobs._record_load_stamp(None)
        return {"owner": owner, "profile": profile, "operationId": operation_id, "version": target_version}
