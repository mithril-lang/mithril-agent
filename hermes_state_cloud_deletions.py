"""Original session deletion receipts consumed by Mithril Desktop cloud sync.

Desktop establishes owner/profile provenance while the source exists. Agent
writers may enqueue only that provenance, inside their original transaction;
they never create an account binding or call the cloud during deletion.
"""

import re
import sqlite3
import uuid
from typing import Iterable

from hermes_state_common import _id_chunks, _placeholders

_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")


def record_cloud_session_deletions(conn: sqlite3.Connection, source_ids: Iterable[str]) -> None:
    """Retain exact mapped deletions atomically; unconnected stores stay unchanged."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mithril_history_source_links'"
    ).fetchone():
        return
    if not conn.in_transaction:
        raise RuntimeError("Cloud deletion requires the original SQLite transaction")
    for chunk in _id_chunks(set(source_ids)):
        rows = conn.execute(
            "SELECT l.source_id,l.owner,l.profile,l.session_id "
            "FROM mithril_history_source_links l JOIN sessions s ON s.id=l.source_id "
            f"WHERE l.source_id IN ({_placeholders(chunk)})", chunk,
        ).fetchall()
        for row in rows:
            source, owner, profile, session = tuple(row)
            if (
                not isinstance(source, str) or not 1 <= len(source) <= 512
                or not isinstance(owner, str) or not _ID.fullmatch(owner)
                or not isinstance(session, str) or not _ID.fullmatch(session)
                or not isinstance(profile, str) or not 1 <= len(profile) <= 256
            ):
                raise ValueError("Invalid cloud history deletion provenance")
            conn.execute(
                "INSERT INTO mithril_history_source_deletions"
                "(operation_id,source_id,owner,profile,session_id) VALUES(?,?,?,?,?) "
                "ON CONFLICT(source_id,owner,profile) DO UPDATE SET "
                "operation_id=excluded.operation_id,base_revision=NULL,acknowledged=0,receipt_json=NULL "
                "WHERE acknowledged=1",
                (str(uuid.uuid4()), source, owner, profile, session),
            )
