"""Owned staging and later builtin approval keep route custody and metadata-only replay."""

import copy
import json
from pathlib import Path

import pytest

from tests.tui_gateway.test_owned_memory_mirrors import install_mirrors
from tests.tui_gateway.owned_fixture import owned_sessions  # noqa: F401
from tests.tui_gateway.test_owned_tool_call import _call, _target_preview


@pytest.mark.parametrize("target", ["memory", "user"])
@pytest.mark.platforms("posix")
def test_owned_staged_memory_keeps_reviewed_route_until_explicit_approval(owned_sessions, monkeypatch, target):
    from tools import write_approval as wa
    from tools.memory_tool import load_on_disk_store
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tui_gateway.server import _session_profile_runtime_scope

    providers = install_mirrors(owned_sessions)
    frozen = {k: copy.deepcopy((s["agent"].tools, s["history"])) for k, s in owned_sessions.items()}
    monkeypatch.setattr(wa, "evaluate_gate", lambda *a, **k: wa.GateDecision(stage=True))
    for session in owned_sessions.values():
        home = Path(session["profile_home"])
        original, replacement = home / "original", home / "replacement"
        original.mkdir(); replacement.mkdir()
        memories = home / "memories"
        if memories.exists():
            memories.rename(home / "initial-memory-directory")
        memories.symlink_to(original, target_is_directory=True)
    expected = {"a": [], "b": []}
    for visit, owner in enumerate(("a", "b", "a")):
        session, args = owned_sessions[owner], {"action": "add", "target": target, "content": f"pending-{owner}-{visit}"}
        home = Path(session["profile_home"])
        binding = _target_preview(owned_sessions, owner, owner, "memory", args)["result"]["target_binding"]
        request = f"route-stage-{visit}"
        result = _call(owned_sessions, owner, owner, "memory", args, request, target_digest=binding["digest"])["result"]
        assert result["terminal"] and result["output"]["success"] and result["output"]["staged"], result
        pending_id = result["output"]["pending_id"]
        (home / "memories").unlink()
        (home / "memories").symlink_to(home / "replacement", target_is_directory=True)
        with _session_profile_runtime_scope(session, hydrate_secrets=False):
            record = wa.get_pending(wa.MEMORY, pending_id)
            text = handle_pending_subcommand(wa.MEMORY, ["approve", pending_id], memory_store=load_on_disk_store())
            assert "Approved 0" in text and "store changed" in text, text
            assert wa.get_pending(wa.MEMORY, pending_id) == record
        filename = "USER.md" if target == "user" else "MEMORY.md"
        assert not (home / "replacement" / filename).exists()
        (home / "memories").unlink()
        (home / "memories").symlink_to(home / "original", target_is_directory=True)
        with _session_profile_runtime_scope(session, hydrate_secrets=False):
            text = handle_pending_subcommand(wa.MEMORY, ["approve", pending_id], memory_store=load_on_disk_store())
            assert "Approved 1" in text and wa.get_pending(wa.MEMORY, pending_id) is None
        expected[owner].append(args["content"])
        replay = _call(owned_sessions, owner, owner, "memory", args, request, target_digest=binding["digest"])["result"]
        assert replay["duplicate"] and replay["output"] is None
        for key, other in owned_sessions.items():
            with _session_profile_runtime_scope(other, hydrate_secrets=False):
                store = load_on_disk_store()
                assert store._entries_for(target) == expected[key]
            assert (other["agent"].tools, other["history"]) == frozen[key]
            assert not providers[key].path.exists()
