"""Durable, single-writer handoffs for explicitly enrolled named API trial profiles.

No automatic rollback: an uncertain release must be retried with the same operation.
The release secret is withheld until the old owner has durably relinquished execution.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import gzip
import io
import json
import os
import re
from pathlib import Path
import secrets
import sqlite3
import tarfile
import tempfile
import uuid
from functools import wraps

STATE = ".execution-handoff.json"
LIMIT = 32 * 1024 * 1024
FILES = {"config.yaml", "profile.yaml", "SOUL.md", "state.db", ".env", "auth.json", "honcho.json", "desktop.json"}
DIRS = {"sessions", "memories", "skills", "assets"}


class HandoffError(RuntimeError):
    pass


def _read(home: Path) -> dict | None:
    path = home / STATE
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get("phase") not in {"active", "frozen", "moved", "staged"}:
        raise HandoffError("Invalid execution ownership record")
    return value


def _write(home: Path, value: dict) -> None:
    from utils import atomic_json_write
    atomic_json_write(home / STATE, value, mode=0o600, fsync_dir=True)
    with (home / STATE).open("rb") as handle:
        os.fsync(handle.fileno())
    descriptor = os.open(home, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def _lock(home: Path, *, exclusive: bool = False):
    """Stable sibling lock survives profile replacement; never waits on an active turn."""
    try:
        import fcntl
    except ImportError:
        raise HandoffError("Profile handoff requires a POSIX trial gateway") from None
    lock_root = home.parent / ".execution-handoff-locks"
    lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (lock_root / (home.name + ".lock")).open("a+b") as handle:
        try:
            fcntl.flock(handle, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HandoffError("Profile is busy; finish the active operation before moving it") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextlib.contextmanager
def execution(home: Path):
    """Only enrolled profiles change behavior; legacy bots retain their existing lifecycle."""
    home = Path(home)
    if not home.name.startswith("handoff-trial-") and not (home / STATE).exists():
        yield
        return
    with _lock(home):
        current = _read(home)
        if current is not None and current["phase"] != "active":
            raise HandoffError("Profile execution is disabled on this gateway after a handoff")
        yield


def execution_turn(fn):
    """Keep the source lease until the entire turn and its finalizers finish."""
    @wraps(fn)
    def guarded(agent, *args, **kwargs):
        from hermes_constants import get_hermes_home
        home = Path(agent.logs_dir).parent if getattr(agent, "logs_dir", None) else get_hermes_home()
        with execution(home):
            record = _read(home)
            owner = (record["identity"], record["generation"]) if record else None
            if fn.__name__ != "init_agent" and getattr(agent, "_execution_handoff_owner", None) != owner:
                raise HandoffError("Cached agent belongs to an earlier profile generation; reopen the conversation")
            result = fn(agent, *args, **kwargs)
            if fn.__name__ == "init_agent":
                agent._execution_handoff_owner = owner
            return result
    return guarded


def _trial_only(home: Path) -> None:
    from hermes_cli.config_defaults import OPTIONAL_ENV_VARS
    from dotenv import dotenv_values
    values = dotenv_values(home / ".env")
    messaging = [key for key, spec in OPTIONAL_ENV_VARS.items() if spec.get("category") == "messaging"]
    if any(values.get(key) for key in messaging):
        raise HandoffError("Messaging profiles are not eligible for the API trial handoff")
    cron = home / "cron"
    scheduled = False
    if cron.exists():
        for path in cron.rglob("*"):
            if not path.is_file():
                continue
            name = path.relative_to(cron).as_posix()
            if name in {".jobs.lock", ".tick.lock", "ticker_heartbeat", "ticker_last_success"}:
                continue
            if name in {"executions.db", "executions.db-wal", "executions.db-shm"}:
                try:
                    with sqlite3.connect(f"file:{cron / 'executions.db'}?mode=ro", uri=True) as db:
                        if db.execute("SELECT COUNT(*) FROM executions").fetchone() == (0,):
                            continue
                except sqlite3.Error:
                    pass
            if name == "jobs.json":
                try:
                    data = json.loads(path.read_text())
                    if isinstance(data, dict) and data.get("jobs") == []:
                        continue
                except (ValueError, OSError):
                    pass
            scheduled = True
    if (home / "gateway.pid").exists() or scheduled:
        raise HandoffError("Gateway and scheduled profiles are not eligible for the API trial handoff")


def _id(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise HandoffError("Invalid handoff identifier")


def enroll(home: Path) -> dict:
    """Enrollment is separate from freezing so old unguarded runtimes cannot be adopted."""
    if not home.name.startswith("handoff-trial-"):
        raise HandoffError("Only handoff-trial-* profiles can enroll in this trial")
    with _lock(home, exclusive=True):
        current = _read(home)
        if current:
            return status(home)
        _trial_only(home)
        _write(home, {"phase": "active", "identity": uuid.uuid4().hex, "generation": 0})
    return status(home)


def status(home: Path) -> dict:
    current = _read(home)
    if current is None:
        return {"phase": "unmanaged", "identity": None, "generation": 0, "operation": None}
    return {key: current.get(key) for key in ("phase", "identity", "generation", "operation", "sha256", "target")}


def _allowed(name: str) -> bool:
    parts = Path(name).parts
    return bool(parts) and not Path(name).is_absolute() and ".." not in parts and (
        name in FILES or parts[0] in DIRS
    ) and not any(part.endswith(("-wal", "-shm", ".lock")) for part in parts)


def _snapshot(home: Path) -> bytes:
    output = io.BytesIO()
    total = 0
    with tempfile.TemporaryDirectory() as scratch, gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed, tarfile.open(fileobj=compressed, mode="w") as archive:
        for source in sorted(home.rglob("*")):
            name = source.relative_to(home).as_posix()
            if not _allowed(name):
                continue
            if source.is_symlink() or any(parent.is_symlink() for parent in source.parents):
                raise HandoffError("Linked profile data must be materialized before moving")
            if not source.is_file():
                continue
            target = source
            if source.suffix in {".db", ".sqlite"}:
                target = Path(scratch) / uuid.uuid4().hex
                with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as db, sqlite3.connect(target) as copy:
                    db.backup(copy)
                    if copy.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                        raise HandoffError("Profile database is corrupt")
            total += target.stat().st_size
            if total > LIMIT:
                raise HandoffError("Profile exceeds the trial transfer limit")
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = target.stat().st_size, 0o600
            with target.open("rb") as handle:
                archive.addfile(entry, handle)
    return output.getvalue()


def freeze(home: Path, operation: str, target: str, *, quiesce=None) -> dict:
    _id(operation)
    _id(target)
    with _lock(home, exclusive=True):
        current = _read(home)
        if current is None:
            raise HandoffError("Enroll an idle API trial profile before moving it")
        if current["phase"] != "active":
            if current.get("operation") == operation and current.get("target") == target:
                if current["phase"] == "frozen" and quiesce is not None:
                    quiesce()
                    current["quiesced"] = True
                    _write(home, current)
                return status(home)
            raise HandoffError("Another handoff already owns this profile")
        _trial_only(home)
        current.pop("sha256", None)
        current.pop("release_hash", None)
        current.update(phase="frozen", operation=operation, target=target, release=secrets.token_hex(32), quiesced=quiesce is None)
        _write(home, current)
        if quiesce is not None:
            quiesce()
            current["quiesced"] = True
            _write(home, current)
    return status(home)


def _cipher(key: str):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
        raise HandoffError("A 256-bit transfer encryption key is required")
    return AESGCM(bytes.fromhex(key))


def _aad(capsule: dict) -> bytes:
    return json.dumps({key: capsule[key] for key in ("profile_name", "identity", "generation", "operation", "target", "sha256", "release_hash")}, sort_keys=True).encode()


def export(home: Path, operation: str, encryption_key: str) -> dict:
    with _lock(home, exclusive=True):
        current = _read(home)
        if not current or current["phase"] != "frozen" or current.get("operation") != operation or not current.get("quiesced"):
            raise HandoffError("Only the matching frozen operation can export")
        payload = _snapshot(home)
        digest = hashlib.sha256(payload).hexdigest()
        previous = current.get("sha256")
        if previous and previous != digest:
            raise HandoffError("Frozen profile changed; refuse an inconsistent transfer")
        current["sha256"] = digest
        _write(home, current)
        capsule = {"profile_name": home.name, "identity": current["identity"], "generation": current["generation"] + 1,
                "operation": operation, "target": current["target"], "sha256": digest,
                "release_hash": hashlib.sha256(current["release"].encode()).hexdigest()}
        # Each operation has immutable plaintext and a unique id. A retry uses
        # the same nonce only for the same plaintext; a changed snapshot refuses.
        nonce = hashlib.sha256(operation.encode()).digest()[:12]
        capsule["archive"] = base64.b64encode(_cipher(encryption_key).encrypt(nonce, payload, _aad(capsule))).decode()
        return capsule


def release(home: Path, operation: str, digest: str) -> str:
    with _lock(home, exclusive=True):
        current = _read(home)
        if not current or current["phase"] not in {"frozen", "moved"} or current.get("operation") != operation or current.get("sha256") != digest:
            raise HandoffError("Release does not match the verified transfer")
        current["phase"] = "moved"
        _write(home, current)
        return current["release"]


def stage(home: Path, capsule: dict, target: str, encryption_key: str) -> dict:
    if home.name != capsule["profile_name"]:
        raise HandoffError("Transfer belongs to another profile name")
    for key in ("operation", "target", "identity"):
        _id(capsule[key])
    if not isinstance(capsule["generation"], int) or isinstance(capsule["generation"], bool) or capsule["generation"] < 1:
        raise HandoffError("Invalid handoff generation")
    if capsule["target"] != target:
        raise HandoffError("Transfer belongs to another gateway")
    if len(capsule["archive"]) > LIMIT * 2:
        raise HandoffError("Transfer exceeds the trial limit")
    ciphertext = base64.b64decode(capsule["archive"], validate=True)
    nonce = hashlib.sha256(capsule["operation"].encode()).digest()[:12]
    try:
        payload = _cipher(encryption_key).decrypt(nonce, ciphertext, _aad(capsule))
    except Exception:
        raise HandoffError("Transfer authentication failed") from None
    if len(payload) > LIMIT or hashlib.sha256(payload).hexdigest() != capsule["sha256"]:
        raise HandoffError("Transfer digest mismatch")
    with _lock(home, exclusive=True):
        current = _read(home) if home.exists() else None
        if home.exists():
            if current and current.get("operation") == capsule["operation"] and current["phase"] in {"staged", "active"}:
                if current.get("sha256") != capsule["sha256"]:
                    raise HandoffError("Conflicting retry payload")
                return status(home)
            if not current or current["phase"] != "moved" or current["identity"] != capsule["identity"] or current["generation"] + 2 != capsule["generation"]:
                raise HandoffError("Destination profile already exists or has divergent ownership")
        with tempfile.TemporaryDirectory(prefix=".handoff-", dir=home.parent) as scratch:
            staged = Path(scratch) / "profile"
            staged.mkdir(mode=0o700)
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
                members = archive.getmembers()
                if sum(member.size for member in members) > LIMIT or any(not member.isfile() or not _allowed(member.name) for member in members):
                    raise HandoffError("Unsafe transfer archive")
                for member in members:
                    path = staged / member.name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open('wb') as handle:
                        handle.write(archive.extractfile(member).read())
                        handle.flush()
                        os.fsync(handle.fileno())
                    path.chmod(0o600)
            _trial_only(staged)
            state = {key: capsule[key] for key in ("identity", "generation", "operation", "sha256", "release_hash")}
            state["phase"] = "staged"
            _write(staged, state)
            # Retain the old frozen copy, including its owner journal, for recovery.
            if home.exists():
                retained = home.parent / (".handoff-retained-" + capsule["operation"])
                if retained.exists():
                    raise HandoffError("A retained copy already exists")
                home.rename(retained)
            staged.rename(home)
    return status(home)


def activate(home: Path, operation: str, proof: str) -> dict:
    with _lock(home, exclusive=True):
        current = _read(home)
        if not current or current["phase"] not in {"staged", "active"} or current.get("operation") != operation or not secrets.compare_digest(
            current.get("release_hash", ""), hashlib.sha256(proof.encode()).hexdigest()
        ):
            raise HandoffError("Activation requires the matching source release proof")
        current["phase"] = "active"
        _write(home, current)
    return status(home)
