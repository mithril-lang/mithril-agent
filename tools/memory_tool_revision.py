"""MemoryStore write-ahead revisions and review-state comparison under its lock.

The epoch survives process restarts and prevents missing-marker reset from
recreating old review state. Every MemoryStore write advances the revision before
persisting data. This covers cooperating built-in writers, not filesystem rollback
or external providers. Uncertain writes conservatively invalidate older reviews.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid

from utils import atomic_json_write

_expected = ContextVar('reviewed_memory_store_state', default=None)


def _marker_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + '.revision.json')


def _revision(path: Path):
    marker = _marker_path(path)
    try:
        value = json.loads(marker.read_text(encoding='utf-8'))
    except FileNotFoundError:
        value = {'epoch': uuid.uuid4().hex, 'revision': 0}
        atomic_json_write(marker, value, mode=0o600, fsync_dir=True)
    except Exception:
        raise OSError('Memory revision could not be read; nothing was applied.') from None
    if (not isinstance(value, dict) or not isinstance(value.get('epoch'), str) or
        len(value['epoch']) != 32 or any(c not in '0123456789abcdef' for c in value['epoch']) or
        type(value.get('revision')) is not int or value['revision'] < 0):
        raise OSError('Memory revision is invalid; nothing was applied.')
    return value


def state(path: Path, raw: str):
    """Caller holds the existing target-file lock and supplies its checked snapshot."""
    revision = _revision(path)
    return {**revision, 'routeDigest': hashlib.sha256(str(path.resolve()).encode()).hexdigest(),
            'contentDigest': hashlib.sha256(raw.encode()).hexdigest()}


def advance(path: Path):
    """Persist a new version BEFORE data changes; a failed write never revives review."""
    revision = _revision(path)
    atomic_json_write(_marker_path(path), {**revision, 'revision': revision['revision'] + 1}, mode=0o600, fsync_dir=True)


@contextmanager
def reviewed_state(expected):
    token = _expected.set(None if expected is None else (os.getpid(), threading.get_ident(), expected))
    try:
        yield
    finally:
        _expected.reset(token)


def matches_review(path: Path, raw: str):
    expected = _expected.get()
    if expected is None or expected[:2] != (os.getpid(), threading.get_ident()):
        return True
    return expected[2] == state(path, raw)
