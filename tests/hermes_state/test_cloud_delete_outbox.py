"""Agent deletion and Desktop cloud receipt interoperability on real SQLite stores."""
import os
import sqlite3

import pytest

from hermes_state import SessionDB
from hermes_state_cloud_deletions import record_cloud_session_deletions
from hermes_state_errors import SessionActiveWriteGuardError


def connect(db, owner, profile, ids):
    def bind(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS mithril_history_source_links(source_id TEXT PRIMARY KEY,owner TEXT NOT NULL,profile TEXT NOT NULL,session_id TEXT NOT NULL,UNIQUE(owner,profile,session_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS mithril_history_source_deletions(operation_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,owner TEXT NOT NULL,profile TEXT NOT NULL,session_id TEXT NOT NULL,base_revision INTEGER,acknowledged INTEGER NOT NULL DEFAULT 0,receipt_json TEXT,UNIQUE(source_id,owner,profile))")
        conn.executemany("INSERT INTO mithril_history_source_links VALUES(?,?,?,?)", [(sid, owner, profile, f"cloud-{sid}") for sid in ids])
    db._execute_write(bind)


def outbox(path):
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return {row["source_id"]: dict(row) for row in conn.execute("SELECT * FROM mithril_history_source_deletions")}


def test_agent_cascade_retains_owner_scoped_intent_and_original_branch(tmp_path):
    path = tmp_path / "state.db"
    with SessionDB(path) as db:
        db.create_session("root", source="test")
        db.create_session("delegate", source="test", parent_session_id="root", model_config={"_delegate_from": "root"})
        db.create_session("branch", source="test", parent_session_id="root")
        connect(db, "alice", "default", ["root", "delegate", "branch"])
        assert db.delete_session("root")
        rows = outbox(path)
        assert set(rows) == {"root", "delegate"}
        assert all(row["owner"] == "alice" and row["profile"] == "default" for row in rows.values())
        assert all(row["session_id"] == f"cloud-{sid}" and row["base_revision"] is None and row["acknowledged"] == 0 for sid, row in rows.items())
        assert db.get_session("branch")["parent_session_id"] is None
        assert not db.delete_session("root")
        assert outbox(path) == rows


def test_bulk_only_enqueues_rows_that_pass_original_active_guards(tmp_path):
    path = tmp_path / "state.db"
    with SessionDB(path) as db:
        for sid in ["busy", "idle", "unmapped"]:
            db.create_session(sid, source="test")
        connect(db, "alice", "default", ["busy", "idle"])
        holder = f"pid={os.getpid()}:turn=cloud-delete"
        assert db.try_acquire_session_turn_lease("busy", holder, ttl_seconds=300)
        with pytest.raises(SessionActiveWriteGuardError):
            db.delete_session("busy", exclude_active_write_guards=True)
        assert not outbox(path)
        skipped = []
        assert db.delete_sessions(["busy", "idle", "unmapped"], exclude_active_write_guards=True, skipped_ids=skipped) == 2
        assert skipped == ["busy"]
        assert set(outbox(path)) == {"idle"}
        assert db.get_session("busy")
        db.release_session_turn_lease("busy", holder)


def test_failed_source_delete_rolls_back_intent_and_delegate_edits(tmp_path):
    path = tmp_path / "state.db"
    with SessionDB(path) as db:
        db.create_session("root", source="test")
        db.create_session("child", source="test", parent_session_id="root", model_config={"_delegate_from": "root"})
        connect(db, "alice", "default", ["root", "child"])
        db._execute_write(lambda conn: conn.execute("CREATE TRIGGER refuse_delete BEFORE DELETE ON sessions WHEN OLD.id='root' BEGIN SELECT RAISE(ABORT,'retained');END"))
        with pytest.raises(sqlite3.IntegrityError, match="retained"):
            db.delete_session("root")
        assert outbox(path) == {}
        assert db.get_session("root") and db.get_session("child")


def test_pending_receipt_identity_survives_recreated_source_and_acknowledged_receipt_is_renewed(tmp_path):
    path = tmp_path / "state.db"
    with SessionDB(path) as db:
        db.create_session("root", source="test")
        connect(db, "alice", "default", ["root"])
        assert db.delete_session("root")
        db._execute_write(lambda conn: conn.execute("UPDATE mithril_history_source_deletions SET base_revision=4"))
        pending = outbox(path)
        db.create_session("root", source="test")
        assert db.delete_session("root")
        assert outbox(path) == pending
        db._execute_write(lambda conn: conn.execute("UPDATE mithril_history_source_deletions SET acknowledged=1,receipt_json='accepted'"))
        db.create_session("root", source="test")
        assert db.delete_session("root")
        renewed = outbox(path)["root"]
        assert renewed["operation_id"] != pending["root"]["operation_id"]
        assert renewed["base_revision"] is None and renewed["receipt_json"] is None and renewed["acknowledged"] == 0


def test_unconnected_store_never_invents_cloud_binding(tmp_path):
    path = tmp_path / "state.db"
    with SessionDB(path) as db:
        db.create_session("root", source="test")
        assert db.delete_session("root")
    with sqlite3.connect(path) as conn:
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'mithril_history_source_%'").fetchall()


def test_original_profile_stores_stay_isolated_a_b_a(tmp_path):
    paths = [tmp_path / "a.db", tmp_path / "b.db"]
    for path, owner, profile in zip(paths, ["alice", "bob"], ["default", "work"]):
        with SessionDB(path) as db:
            db.create_session("same", source="test")
            connect(db, owner, profile, ["same"])
    for index in [0, 1, 0]:
        with SessionDB(paths[index]) as db:
            db.delete_session("same")
    assert outbox(paths[0])["same"]["owner"] == "alice"
    assert outbox(paths[1])["same"]["owner"] == "bob"
    assert outbox(paths[1])["same"]["profile"] == "work"


def test_invalid_provenance_refuses_original_delete_and_no_transaction_is_refused(tmp_path):
    path = tmp_path / "state.db"
    with SessionDB(path) as db:
        db.create_session("root", source="test")
        connect(db, "bad owner", "default", ["root"])
        with pytest.raises(ValueError, match="provenance"):
            db.delete_session("root")
        assert db.get_session("root") and not outbox(path)
    with sqlite3.connect(path) as conn:
        with pytest.raises(RuntimeError, match="transaction"):
            record_cloud_session_deletions(conn, ["root"])


@pytest.mark.parametrize("review", [
    {"expected_delete_ids": []},
    {"expected_display_messages": {"root": [{"role": "user", "content": "changed"}]}},
])
def test_stale_review_cannot_stage_cloud_delete(tmp_path, review):
    path = tmp_path / "state.db"
    with SessionDB(path) as db:
        db.create_session("root", source="test")
        connect(db, "alice", "default", ["root"])
        assert not db.delete_session("root", **review)
        assert db.get_session("root") and not outbox(path)


def test_bulk_failure_retains_every_source_and_rolls_back_every_intent(tmp_path):
    path = tmp_path / "state.db"
    with SessionDB(path) as db:
        for sid in ["first", "refused"]:
            db.create_session(sid, source="test")
        connect(db, "alice", "default", ["first", "refused"])
        db._execute_write(lambda conn: conn.execute("CREATE TRIGGER refuse_delete BEFORE DELETE ON sessions WHEN OLD.id='refused' BEGIN SELECT RAISE(ABORT,'retained');END"))
        with pytest.raises(sqlite3.IntegrityError, match="retained"):
            db.delete_sessions(["first", "refused"])
        assert outbox(path) == {}
        assert db.get_session("first") and db.get_session("refused")
