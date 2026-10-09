"""Profile-local pending decision custody across threads and processes.

One queue lock spans reread, review comparison, durable claim, effect and discard.
An interrupted claim is never automatically replayed. This coordinates Hermes
writers; it is not a store data revision or an arbitrary filesystem-writer fence.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import threading
import time

from hermes_constants import get_hermes_home
from utils import atomic_json_write

_held = ContextVar('pending_decision_locks', default=())


def _path(subsystem: str, pending_id: str) -> Path:
    from tools.write_approval import _SUBSYSTEMS
    if subsystem not in _SUBSYSTEMS or not pending_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in pending_id):
        raise ValueError('Invalid pending decision selection.')
    return get_hermes_home() / 'pending' / subsystem / f'{pending_id}.json'


@contextmanager
def pending_decision_lock(subsystem: str, pending_id: str):
    """Reentrant only for this process/thread; child contexts cannot borrow custody."""
    from tools import memory_tool
    if memory_tool.fcntl is None and memory_tool.msvcrt is None:
        raise OSError('Pending decision locking is unavailable; nothing was applied.')
    path = _path(subsystem, pending_id)
    key = (os.getpid(), threading.get_ident(), str(path.resolve()))
    if key in _held.get():
        yield path
        return
    with memory_tool.MemoryStore._file_lock(path):
        token = _held.set((*_held.get(), key))
        try:
            yield path
        finally:
            _held.reset(token)


def receipt_path(subsystem: str, pending_id: str) -> Path:
    path = _path(subsystem, pending_id)
    return path.parent / '.decisions' / path.name


def decision_receipt(subsystem: str, pending_id: str):
    """Unreadable receipts fail closed rather than silently allowing another effect."""
    path = receipt_path(subsystem, pending_id)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    except Exception:
        raise OSError('Pending decision receipt is unreadable; inspect saved data before any new proposal.') from None
    if not isinstance(data, dict) or data.get('state') not in {'applying', 'applied', 'rejecting', 'rejected', 'unknown'}:
        raise OSError('Pending decision receipt is invalid; inspect saved data before any new proposal.')
    return data


def write_receipt(subsystem: str, record: dict, decision: str, state: str):
    """Private durable claim BEFORE an effect; caller holds pending_decision_lock."""
    digest = hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                      separators=(',', ':')).encode()).hexdigest()
    atomic_json_write(receipt_path(subsystem, record['id']), {'id': record['id'], 'decision': decision,
                      'state': state, 'recordDigest': digest, 'updatedAt': time.time()}, mode=0o600, fsync_dir=True)


def clear_failed_claim(subsystem: str, pending_id: str):
    """Only an explicit no-effect failure can return a proposal to the review queue."""
    receipt_path(subsystem, pending_id).unlink()


def finish_decision(subsystem: str, record: dict, decision: str):
    """Record the terminal outcome before removing exactly the claimed record."""
    from tools import write_approval as wa
    path = _path(subsystem, record['id'])
    if wa.get_pending(subsystem, record['id']) != record:
        # A noncooperating filesystem writer replaced the queue entry. Preserve it.
        write_receipt(subsystem, record, decision, 'unknown')
        raise OSError('Pending proposal changed during the decision; inspect saved data. No decision will be repeated.')
    write_receipt(subsystem, record, decision, 'applied' if decision == 'approve' else 'rejected')
    path.unlink()
