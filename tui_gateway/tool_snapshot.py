"""Owned RPC readback of model-visible schemas; never rebuilds tools or grants execution."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid


def _terminal_policy_identity(session: dict) -> str:
    """Re-resolve the owner's policy even inside a scope captured before an approval wait.

    Only the digest stays host-side. Discovery binds the same complete profile
    scope as execution, including direct callers that have no ambient turn.
    """
    from tui_gateway.server import _session_profile_runtime_scope
    from tools.terminal_tool import _get_env_config, resolve_task_overrides

    with _session_profile_runtime_scope(session, hydrate_secrets=False):
        policy = {"terminal": _get_env_config(),
                  "overrides": resolve_task_overrides(getattr(session["agent"], "session_id", None))}
        encoded = json.dumps(policy, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > 2 * 1024 * 1024:
        raise ValueError("owned terminal policy exceeds context limit")
    return hashlib.sha256(encoded).hexdigest()


def session_tool_snapshot(session: dict | None) -> dict:
    """Copy the same published array a live agent uses, under its publication lock.

    Deferred schemas are deliberately not reconstructed from the current registry:
    that would present newly discovered tools as part of a frozen conversation.
    Revision is a server-local content identity, not the cross-language schema hash.
    The RPC caller must enforce transport ownership before and after this read.
    """
    if not session or session.get("agent") is None:
        return {"protocol": "hermes-session-tool-snapshot-v1", "status": "not-built",
                "coverage": "model-visible-only", "context_id": None,
                "revision": None, "registry_generation": None, "definitions": [],
                "effect_manifests": []}
    from tools.mcp_tool_agent import _agent_tools_lock

    try:
        policy = _terminal_policy_identity(session)
    except Exception:
        # An unreadable policy retires approval too: repairing it must not
        # revive the context that existed before the refusal interval.
        with _agent_tools_lock:
            session["_tool_snapshot_terminal_policy"] = None
            session["_tool_snapshot_context"] = uuid.uuid4().hex
        raise ValueError("owned terminal policy is unavailable") from None
    with _agent_tools_lock:
        agent = session["agent"]
        definitions = copy.deepcopy(getattr(agent, "tools", []))
        generation = getattr(agent, "_tool_snapshot_generation", None)
        home = session.get("profile_home")
        cwd = session.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise ValueError("invalid owned working directory context")
        # Targets and execution settings can change without rebuilding frozen
        # schemas. Any observed change retires grants, including A -> B -> A.
        runtime_changed = (session.get("_tool_snapshot_agent") is not agent
                           or session.get("_tool_snapshot_home") != home
                           or session.get("_tool_snapshot_terminal_policy") != policy)
        if runtime_changed:
            session["_tool_snapshot_runtime_context"] = uuid.uuid4().hex
        if runtime_changed or session.get("_tool_snapshot_cwd") != cwd:
            session["_tool_snapshot_agent"] = agent
            session["_tool_snapshot_home"] = home
            session["_tool_snapshot_cwd"] = cwd
            session["_tool_snapshot_terminal_policy"] = policy
            session["_tool_snapshot_context"] = uuid.uuid4().hex
        context = session["_tool_snapshot_context"]
    if not isinstance(definitions, list) or len(definitions) > 4096:
        raise ValueError("invalid agent tool snapshot")
    names = set()
    for definition in definitions:
        function = definition.get("function") if isinstance(definition, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if (not isinstance(name, str) or not name or len(name) > 256 or name in names
                or not isinstance(function.get("parameters"), dict)):
            raise ValueError("invalid agent tool definition")
        names.add(name)
    from tools.effect_manifest import runtime_effect_manifests
    from tui_gateway.server import _session_profile_runtime_scope

    try:
        with _session_profile_runtime_scope(session, hydrate_secrets=False):
            manifests = runtime_effect_manifests(definitions)
        manifest_encoded = json.dumps(manifests, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":"), allow_nan=False).encode("utf-8")
        manifest_identity = hashlib.sha256(manifest_encoded).hexdigest()
    except Exception:
        with _agent_tools_lock:
            session["_tool_snapshot_effect_identity"] = None
            session["_tool_snapshot_context"] = uuid.uuid4().hex
        raise ValueError("owned effect manifest is unavailable") from None
    with _agent_tools_lock:
        if session.get("_tool_snapshot_effect_identity") != manifest_identity:
            session["_tool_snapshot_effect_identity"] = manifest_identity
            session["_tool_snapshot_context"] = uuid.uuid4().hex
        context = session["_tool_snapshot_context"]
    encoded = json.dumps({"definitions": definitions, "effect_manifests": manifests},
                         ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > 2 * 1024 * 1024:
        raise ValueError("agent tool snapshot exceeds readback limit")
    return {"protocol": "hermes-session-tool-snapshot-v1", "status": "built",
            "coverage": "model-visible-only", "context_id": context,
            "revision": hashlib.sha256(encoded).hexdigest(),
            "registry_generation": generation if type(generation) is int else None,
            "definitions": definitions, "effect_manifests": manifests}
