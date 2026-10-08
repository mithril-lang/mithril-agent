"""Durable attempt pages follow exact attached session/profile authority."""

import threading

import pytest

from hermes_state import SessionDB
import tui_gateway.server as server
from tui_gateway.transport import StdioTransport


@pytest.fixture
def homes(tmp_path, monkeypatch):
    from agent import secret_scope
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    sessions = {}
    for name in ["a", "b"]:
        home = tmp_path / name
        home.mkdir()
        db = SessionDB(db_path=home / "state.db")
        db.create_session(session_id="same-durable-owner", source="test", model="test")
        for number in range(3):
            ident = f"attempt-{number}"
            assert db.begin_tool_attempt("same-durable-owner", ident, "parent", f"{name}-tool", "private-request-hash")
            if number == 0:
                assert db.dispatch_tool_attempt("same-durable-owner", ident)
                assert db.settle_tool_attempt("same-durable-owner", ident, "returned", "result-hash", 10)
        db.close()
        sessions[name] = {"agent": None, "profile_home": str(home), "session_key": "same-durable-owner",
                          "transport": StdioTransport(lambda: None, threading.Lock())}
    monkeypatch.setattr(server, "_sessions", sessions)
    return sessions


def _read(sessions, caller, target, **params):
    token = server.bind_transport(sessions[caller]["transport"])
    try:
        return server._methods["tools.attempts"]("rid", {"session_id": target, **params})
    finally:
        server.reset_transport(token)


def test_attempt_pages_are_owned_profile_scoped_and_do_not_publish_payloads(homes):
    for name in ["a", "b", "a"]:
        first = _read(homes, name, name, limit=2)["result"]
        assert first["available"] and first["next_cursor"]
        assert len(first["attempts"]) == 2
        second = _read(homes, name, name, limit=2, before_attempt_id=first["next_cursor"])["result"]
        rows = first["attempts"] + second["attempts"]
        assert len({row["attempt_id"] for row in rows}) == 3 and second["next_cursor"] is None
        assert {row["tool_name"] for row in rows} == {f"{name}-tool"}
        assert {row["state"] for row in rows} == {"pending", "returned"}
        assert all("request_digest" not in row and "session_id" not in row and "result" not in row for row in rows)
    from pydantic import ValidationError
    from tui_gateway.contracts.tool_attempts import ToolAttemptsParams
    for invalid in ({}, {"session_id": "a", "profile": "b"}, {"session_id": "a", "limit": True},
                    {"session_id": "a", "limit": 0}, {"session_id": "a", "limit": 101}):
        with pytest.raises(ValidationError):
            ToolAttemptsParams.model_validate(invalid)
    assert _read(homes, "a", "a", before_attempt_id="foreign")["error"]["code"] == 5036
    rejected = _read(homes, "a", "b")
    assert rejected["error"]["code"] == 4001
    assert server._methods["tools.attempts"]("rid", {})["error"]["code"] == 4001
    assert _read(homes, "a", "missing")["error"]["code"] == 4001


def test_attempt_readback_rejects_attachment_retirement_after_real_db_read(homes, monkeypatch):
    original = SessionDB.list_tool_attempts

    def retire(db, *args, **kwargs):
        page = original(db, *args, **kwargs)
        homes["b"] = dict(homes["b"])
        return page

    monkeypatch.setattr(SessionDB, "list_tool_attempts", retire)
    rejected = _read(homes, "b", "b")
    assert rejected["error"]["code"] == 4001 and "result" not in rejected
