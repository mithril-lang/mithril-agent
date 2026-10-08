"""Attempt ownership/CAS survive a real DB reopen without authorizing replay."""

from concurrent.futures import ThreadPoolExecutor
import threading

from hermes_state import SessionDB


def test_tool_attempt_claim_is_atomic_owned_and_terminal_is_immutable(tmp_path):
    homes = [tmp_path / "a", tmp_path / "b"]
    dbs = []
    for home in homes:
        home.mkdir()
        db = SessionDB(db_path=home / "state.db")
        db.create_session(session_id="same-session", source="test", model="test")
        dbs.append(db)
    peer = SessionDB(db_path=homes[0] / "state.db")
    gate = threading.Barrier(2)

    def claim(db):
        gate.wait(timeout=5)
        return db.begin_tool_attempt("same-session", "attempt", "parent", "write_file", "request-digest")

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, [dbs[0], peer]))
        assert sorted(results) == [False, True]
        assert dbs[1].get_tool_attempt("same-session", "attempt") is None
        assert dbs[1].begin_tool_attempt("same-session", "attempt", "parent", "write_file", "other-digest")
        assert not peer.dispatch_tool_attempt("foreign-session", "attempt")
        assert dbs[0].dispatch_tool_attempt("same-session", "attempt")
        assert not peer.dispatch_tool_attempt("same-session", "attempt")
        assert not peer.settle_tool_attempt("same-session", "attempt", "blocked")
        assert peer.settle_tool_attempt("same-session", "attempt", "returned", "result-digest", 10)
        assert not dbs[0].settle_tool_attempt("same-session", "attempt", "returned-error", "changed", 20)
        reopened = SessionDB(db_path=homes[0] / "state.db")
        try:
            receipt = reopened.get_tool_attempt("same-session", "attempt")
            assert receipt["terminal"] and receipt["state"] == "returned"
            assert receipt["result_digest"] == "result-digest" and receipt["result_bytes"] == 10
            assert reopened.get_tool_attempt("foreign-session", "attempt") is None
            assert not reopened.begin_tool_attempt("same-session", "attempt", "other-parent", "terminal", "changed")
        finally:
            reopened.close()
        pending = dbs[1].get_tool_attempt("same-session", "attempt")
        assert pending["state"] == "pending" and not pending["terminal"]
    finally:
        peer.close()
        for db in dbs:
            db.close()
