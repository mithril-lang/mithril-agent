"""Canonical board metadata writer lock, shared with Desktop's storage adapter.

The lock inode is permanent. Timeout is an error: writing unlocked would lose
another process's read/modify/write update. A prepared cloud transaction must
recover before a later native metadata edit can proceed.
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
import time
from pathlib import Path


@contextlib.contextmanager
def board_metadata_lock(path: Path, db_path: Path, *, timeout: float = 10.0):
    from hermes_cli.kanban_db_connect import _try_lock_nb, _unlock

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(".board-metadata.lock")
    if lock_path.is_symlink():
        raise RuntimeError("Unsafe board metadata lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    handle = os.fdopen(fd, "a+b")
    acquired = False
    try:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("Unsafe board metadata lock")
        if info.st_size == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout
        while not acquired:
            try:
                acquired = _try_lock_nb(handle)
            except OSError:
                acquired = False
            if not acquired:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Board metadata is busy; retry after synchronization")
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        current = lock_path.lstat()
        if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise RuntimeError("Board metadata lock changed")
        if db_path.exists():
            conn = sqlite3.connect(db_path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2)
            try:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='mithril_board_pending'").fetchone() and conn.execute("SELECT 1 FROM mithril_board_pending LIMIT 1").fetchone():
                    raise RuntimeError("Board metadata synchronization requires recovery")
            finally:
                conn.close()
        yield
    finally:
        try:
            if acquired:
                _unlock(handle)
        finally:
            handle.close()
