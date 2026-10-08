"""tools.list / tools.show read back the SESSION's effective toolsets (#117977).

``profiles.configure`` pins ``platform_toolsets.cli`` in the profile's own config.yaml — the key the
agent build reads through ``_load_enabled_toolsets`` under the session's profile scope. The read-back
RPCs consulted only a BUILT agent; a session whose agent had not been built yet (``session.create``
with no prompt, exactly the editor's flow) read back as "everything enabled", and a session-less call
resolved against the launch home. Invariant: for a session_id, both RPCs answer with the toolsets the
session's own profile would build with; the launch profile's session still sees its own pin (A→B→A).
"""

from __future__ import annotations

import os
import threading

import hermes_yaml as yaml
import pytest

import tui_gateway.server as server
from tui_gateway.methods_profiles import _save_toolset_pin

LAUNCH_PIN = ["web", "browser", "terminal"]
WORKER_PIN = ["file", "clarify"]


def _pin(home, names):
    """Write the pin the way ``profiles.configure`` does (its writer, into that home's config.yaml)."""
    home.mkdir(parents=True, exist_ok=True)
    _save_toolset_pin({}, names, save_config=lambda cfg: (home / "config.yaml").write_text(
        yaml.safe_dump(cfg), encoding="utf-8"))


@pytest.fixture
def two_homes(tmp_path, monkeypatch):
    """Launch home pinned to LAUNCH_PIN, secondary ``profiles/work`` pinned to WORKER_PIN; one
    multiplexing backend serves an agent-less session per home."""
    root = tmp_path / "hermes_home"
    worker = root / "profiles" / "work"
    _pin(root, LAUNCH_PIN)
    _pin(worker, WORKER_PIN)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_TUI_TOOLSETS", raising=False)
    monkeypatch.setattr(server, "_hermes_home", root)
    from agent import secret_scope
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    server._cfg_cache = server._cfg_mtime = server._cfg_path = None
    sessions = {
        "launch": {"agent": None, "profile_home": None, "cwd": str(tmp_path), "source": "tui"},
        "work": {"agent": None, "profile_home": str(worker), "cwd": str(tmp_path), "source": "tui"},
    }
    from tui_gateway.transport import StdioTransport
    for session in sessions.values():
        session["transport"] = StdioTransport(lambda: None, threading.Lock())
    monkeypatch.setattr(server, "_sessions", sessions)
    return sessions


def _enabled(sid: str) -> set[str]:
    token = server.bind_transport(server._sessions[sid]["transport"])
    try:
        resp = server._methods["tools.list"]("rid", {"session_id": sid})
    finally:
        server.reset_transport(token)
    assert "error" not in resp, resp
    return {row["name"] for row in resp["result"]["toolsets"] if row["enabled"]}


def _sections(sid: str) -> set[str]:
    token = server.bind_transport(server._sessions[sid]["transport"])
    try:
        resp = server._methods["tools.show"]("rid", {"session_id": sid})
    finally:
        server.reset_transport(token)
    assert "error" not in resp, resp
    return {section["name"] for section in resp["result"]["sections"]}


def test_tools_list_reads_back_the_sessions_own_pin_a_b_a(two_homes):
    from hermes_constants import get_hermes_home_override

    work_first = _enabled("work")
    launch = _enabled("launch")
    work_again = _enabled("work")
    assert set(WORKER_PIN) <= work_first == work_again, sorted(work_first)
    assert work_first.isdisjoint({"web", "browser", "terminal"}), sorted(work_first)
    assert set(LAUNCH_PIN) <= launch and "file" not in launch, sorted(launch)
    assert get_hermes_home_override() is None  # scope released after each answer


def test_tools_show_sections_follow_the_sessions_pin(two_homes):
    """Sections are per resolved TOOL, so credential-gated toolsets (web, browser) may be absent
    from both; ``terminal`` vs ``file`` is the pair that tells the two pins apart."""
    work, launch = _sections("work"), _sections("launch")
    assert "file" in work and "terminal" not in work, sorted(work)
    assert "terminal" in launch and "file" not in launch, sorted(launch)


@pytest.mark.parametrize("method", ["tools.list", "toolsets.list", "tools.show"])
def test_tool_readback_requires_live_transport_membership(two_homes, method):
    """A public session id is not read authority; detachment/id reuse revokes it."""
    original = two_homes["work"]
    token = server.bind_transport(two_homes["launch"]["transport"])
    try:
        rejected = server._methods[method]("rid", {"session_id": "work"})
        assert rejected.get("error", {}).get("code") == 4001, rejected
    finally:
        server.reset_transport(token)
    token = server.bind_transport(original["transport"])
    generation = server._current_runtime_session_record.set(original)
    try:
        accepted = server._methods[method]("rid", {"session_id": "work"})
        assert "result" in accepted, accepted
        two_homes["work"] = dict(original)
        rejected = server._methods[method]("rid", {"session_id": "work"})
        assert rejected.get("error", {}).get("code") == 4001, rejected
        two_homes["work"] = original
    finally:
        server._current_runtime_session_record.reset(generation)
        server.reset_transport(token)
    rejected = server._methods[method]("rid", {"session_id": "missing"})
    assert rejected.get("error", {}).get("code") == 4001, rejected


def test_tool_schema_resolution_uses_each_sessions_secret_home(two_homes, monkeypatch):
    """The real definition resolver runs under home + secrets, not just config lookup."""
    import model_tools
    from agent.secret_scope import get_secret

    launch_home = server._hermes_home
    worker_home = launch_home / "profiles" / "work"
    for home, value in ((launch_home, "launch-only"), (worker_home, "worker-only")):
        (home / ".env").write_text(f"TOOL_SCOPE_FIXTURE={value}\n", encoding="utf-8")
    monkeypatch.delenv("TOOL_SCOPE_FIXTURE", raising=False)
    original = model_tools.get_tool_definitions
    observed = []

    def resolve(*args, **kwargs):
        observed.append(get_secret("TOOL_SCOPE_FIXTURE"))
        return original(*args, **kwargs)

    monkeypatch.setattr(model_tools, "get_tool_definitions", resolve)
    before = dict(os.environ)
    work, launch, work_again = _sections("work"), _sections("launch"), _sections("work")
    assert work == work_again and "file" in work and "terminal" in launch
    assert observed == ["worker-only", "launch-only", "worker-only"]
    assert dict(os.environ) == before


def test_rpc_snapshot_tracks_published_schemas_and_owned_agent_generation(two_homes):
    import copy
    from types import SimpleNamespace
    import model_tools
    from tools.mcp_tool_agent import _agent_tools_lock

    for sid, enabled in (("launch", LAUNCH_PIN), ("work", WORKER_PIN)):
        session = two_homes[sid]
        with server._session_profile_runtime_scope(session):
            definitions = model_tools.get_tool_definitions(
                enabled_toolsets=enabled, quiet_mode=True)
        session["agent"] = SimpleNamespace(tools=definitions, enabled_toolsets=enabled,
                                           disabled_toolsets=None, _tool_snapshot_generation=7)

    def snapshot(sid):
        token = server.bind_transport(two_homes[sid]["transport"])
        try:
            response = server._methods["tools.show"]("rid", {"session_id": sid})
        finally:
            server.reset_transport(token)
        assert "result" in response, response
        result = response["result"]
        assert {d["function"]["name"] for d in result["discovery_definitions"]} == {
            tool["name"] for section in result["sections"] for tool in section["tools"]}
        assert len(result["discovery_definitions"]) == result["total"]
        return response["result"]["runtime_snapshot"]

    work, launch, repeated = snapshot("work"), snapshot("launch"), snapshot("work")
    assert work == repeated and work["context_id"] != launch["context_id"]
    assert work["definitions"] == two_homes["work"]["agent"].tools
    assert work["coverage"] == "model-visible-only" and work["registry_generation"] == 7
    # Returned schemas are copies; neither a client nor a concurrent publication
    # can mutate an earlier snapshot or the conversation's frozen prefix.
    original = copy.deepcopy(work)
    work["definitions"][0]["function"]["parameters"]["description"] = "client mutation"
    assert snapshot("work") == original
    with _agent_tools_lock:
        agent = two_homes["work"]["agent"]
        agent.tools = copy.deepcopy(agent.tools)
        agent.tools[0]["function"]["parameters"]["description"] = "published schema change"
        agent._tool_snapshot_generation += 1
    changed = snapshot("work")
    assert changed["revision"] != original["revision"]
    assert changed["context_id"] == original["context_id"]
    assert changed["registry_generation"] == 8
    two_homes["work"]["agent"] = copy.deepcopy(agent)
    replaced = snapshot("work")
    assert replaced["revision"] == changed["revision"]
    assert replaced["context_id"] != changed["context_id"]
    two_homes["work"]["profile_home"] = None
    rehomed = snapshot("work")
    assert rehomed["context_id"] != replaced["context_id"]
    assert rehomed["revision"] == replaced["revision"]
    two_homes["work"]["agent"] = None
    unbuilt = snapshot("work")
    assert unbuilt["status"] == "not-built" and unbuilt["definitions"] == []
    assert unbuilt["context_id"] is None and unbuilt["revision"] is None
