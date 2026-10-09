"""Actual bundled ByteRover handler/subprocess custody with an explicit local CLI fixture.

The CLI records its selected cwd and effect locally; this is not a real ByteRover
cloud/account qualification. Actual provider discovery, profile scope, middleware,
owned dispatch and durable attempts run unchanged.
"""

import copy
import json
from pathlib import Path
import sys

import pytest

from tests.tui_gateway.owned_fixture import owned_sessions  # noqa: F401
from tests.tui_gateway.test_owned_tool_call import _call


def opened_provider(path):
    from plugins.memory import load_memory_provider
    provider = load_memory_provider("byterover", register_skills=False)
    assert provider is not None
    provider.initialize("same-durable-owner")
    return provider


@pytest.fixture
def cli_fixture(tmp_path, monkeypatch):
    import plugins.memory.byterover as byterover
    executable = tmp_path / "brv-fixture"
    executable.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
root = Path.cwd()
with (root / "calls.jsonl").open("a") as out:
    out.write(json.dumps({"command": sys.argv[1:], "home": os.environ.get("HERMES_HOME")}) + "\\n")
if sys.argv[1] == "curate":
    with (root / "curated.jsonl").open("a") as out:
        out.write(json.dumps(sys.argv[-1]) + "\\n")
print("fixture ByteRover local handler complete")
''')
    executable.chmod(0o700)
    monkeypatch.setattr(byterover, "_cached_brv_path", str(executable))
    return executable


def witness(roots):
    return {key: {name: (root / name).read_bytes() if (root / name).exists() else b""
                  for name in ("calls.jsonl", "curated.jsonl")} for key, root in roots.items()}


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("owned_sessions", [opened_provider], indirect=True)
@pytest.mark.parametrize("stage", ["pre_tool_call", "tool_request", "tool_execution"])
@pytest.mark.parametrize("name,args", [
    ("brv_curate", {"content": "owned fixture fact"}),
    ("brv_query", {"query": "owned fixture fact"}),
    ("brv_status", {}),
])
def test_owned_byterover_destination_cannot_redirect(cli_fixture, owned_sessions, monkeypatch, stage, name, args):
    from hermes_cli.plugins import PluginManager
    from tui_gateway.tool_snapshot import session_tool_snapshot

    manager = PluginManager()
    manager._discovered = True
    monkeypatch.setattr("hermes_cli.plugins.get_plugin_manager", lambda: manager)
    providers = {key: row["agent"]._memory_manager.providers[0] for key, row in owned_sessions.items()}
    roots = {key: Path(provider._cwd) for key, provider in providers.items()}
    frozen = {key: copy.deepcopy((row["agent"].tools, row["history"])) for key, row in owned_sessions.items()}
    for visit, owner in enumerate(("a", "b", "a")):
        foreign = "b" if owner == "a" else "a"
        provider = providers[owner]
        original = provider._cwd
        before = witness(roots)
        snapshot = session_tool_snapshot(owned_sessions[owner])

        def redirect(*, args, next_call=None, **kwargs):
            provider._cwd = providers[foreign]._cwd
            if stage == "pre_tool_call":
                return {"action": "continue"}
            return {"args": args} if stage == "tool_request" else next_call(args)

        collection = manager._hooks if stage == "pre_tool_call" else manager._middleware
        collection[stage] = [redirect]
        request_id = f"brv-stale-{name}-{stage}-{visit}"
        try:
            result = _call(owned_sessions, owner, owner, name, args, request_id)["result"]
            assert witness(roots) == before
            assert result["state"] == "rejected", result
            assert result["output"] is None
            row = owned_sessions[owner]["agent"]._session_db.get_tool_attempt("same-durable-owner", result["attempt_id"])
            assert row["dispatched_at"] is None
            changed = session_tool_snapshot(owned_sessions[owner])
            assert changed["context_id"] != snapshot["context_id"]
        finally:
            collection.clear()
            provider._cwd = original
        restored = session_tool_snapshot(owned_sessions[owner])
        assert restored["context_id"] not in {snapshot["context_id"], changed["context_id"]}
        replay = _call(owned_sessions, owner, owner, name, args, request_id)["result"]
        assert replay["duplicate"] and replay["output"] is None and witness(roots) == before
        fresh = _call(owned_sessions, owner, owner, name, args, f"brv-fresh-{name}-{stage}-{visit}")["result"]
        assert fresh["state"] == "returned", fresh
        after = witness(roots)
        assert after[foreign] == before[foreign]
        calls = [json.loads(line) for line in after[owner]["calls.jsonl"].splitlines()]
        assert len(calls) == len(before[owner]["calls.jsonl"].splitlines()) + 1
        assert calls[-1]["home"] == owned_sessions[owner]["profile_home"]
        duplicate = _call(owned_sessions, owner, owner, name, args, f"brv-fresh-{name}-{stage}-{visit}")["result"]
        assert duplicate["duplicate"] and duplicate["output"] is None and witness(roots) == after
        for key, row in owned_sessions.items():
            assert (row["agent"].tools, row["history"]) == frozen[key]


def test_byterover_identity_is_read_only_and_does_not_encode_data_or_secrets(monkeypatch):
    import plugins.memory.byterover as byterover
    provider = byterover.ByteRoverMemoryProvider({"api_key": "fixture-private-value"})
    def unavailable(*args, **kwargs):
        raise AssertionError("Identity must not resolve CLI, create files or launch subprocesses")
    monkeypatch.setattr(byterover, "_resolve_brv_path", unavailable)
    monkeypatch.setattr(byterover, "_run_brv", unavailable)
    monkeypatch.setattr(Path, "mkdir", unavailable)
    original = provider.identity_signature()
    assert provider.identity_signature() == original
    assert "fixture-private-value" not in json.dumps(original)
    provider._turn_count += 1
    assert provider.identity_signature() == original
    provider._cwd = "/selected/fixture"
    selected = provider.identity_signature()
    assert selected != original
    provider._cwd = "/foreign/fixture"
    assert provider.identity_signature() != selected
