"""Unconfirmed staged memory persists metadata custody without false success or replay."""

import copy
import json
from pathlib import Path

import pytest

from tests.tui_gateway.test_owned_memory_mirrors import install_mirrors
from tests.tui_gateway.owned_fixture import owned_sessions  # noqa: F401
from tests.tui_gateway.test_owned_tool_call import _call, _target_preview


@pytest.mark.parametrize("target", ["memory", "user"])
@pytest.mark.parametrize("landed", [False, True])
def test_owned_pending_failure_never_applies_or_replays_memory(owned_sessions, monkeypatch, target, landed):
    from tools import write_approval as wa

    providers = install_mirrors(owned_sessions)
    frozen = {k: copy.deepcopy((s["agent"].tools, s["history"])) for k, s in owned_sessions.items()}
    monkeypatch.setattr(wa, "evaluate_gate", lambda *a, **k: wa.GateDecision(stage=True))
    original = wa.atomic_json_write
    records = []

    def lose_confirmation(path, record):
        records.append((path, record))
        if landed:
            original(path, record)
        raise OSError("private-storage-error-canary")

    monkeypatch.setattr(wa, "atomic_json_write", lose_confirmation)
    for visit, owner in enumerate(["a", "b", "a"]):
        args = {"action": "add", "target": target, "content": f"pending only-{visit}"}
        binding = _target_preview(owned_sessions, owner, owner, "memory", args)["result"]["target_binding"]
        request = f"unconfirmed-stage-{visit}"
        count = len(records)
        result = _call(owned_sessions, owner, owner, "memory", args, request, target_digest=binding["digest"])["result"]
        output = result["output"] or {}
        assert result["terminal"] and output.get("success") is not True and not output.get("staged"), result
        assert output["pending_confirmation"] == "unknown"
        assert "private-storage-error-canary" not in json.dumps(result)
        assert len(records) == count + 1
        path, record = records[-1]
        assert path.parent.parent.parent == Path(owned_sessions[owner]["profile_home"])
        assert path.exists() == landed
        if landed:
            assert json.loads(path.read_text())["payload"] == {**args, "old_text": None}
        replay = _call(owned_sessions, owner, owner, "memory", args, request, target_digest=binding["digest"])["result"]
        assert replay["duplicate"] and replay["output"] is None and len(records) == count + 1
        for key, session in owned_sessions.items():
            assert session["agent"]._memory_store.memory_entries == session["agent"]._memory_store.user_entries == []
            assert not providers[key].path.exists()
            assert (session["agent"].tools, session["history"]) == frozen[key]
