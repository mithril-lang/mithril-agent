"""Owned RPC readback of model-visible schemas; never rebuilds tools or grants execution."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid


def _deferred_execution_names(agent, definitions):
    """Fence selectable deferred registrations without availability probes or publication."""
    if not any(d["function"]["name"] == "tool_call" for d in definitions):
        return ()
    from model_tools import _select_tool_names
    from tools.tool_search import is_deferrable_tool_name, load_config_readonly

    selected = _select_tool_names(getattr(agent, "enabled_toolsets", None),
                                  getattr(agent, "disabled_toolsets", None), True)
    defer = load_config_readonly().effective_defer_tools
    published = {d["function"]["name"] for d in definitions}
    names = tuple(sorted(name for name in selected - published
                         if is_deferrable_tool_name(name, defer)))
    if len(names) + len(definitions) > 4096:
        raise ValueError("owned deferred execution scope exceeds context limit")
    return names


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
    from tools.effect_manifest import runtime_effect_snapshot
    from agent.inline_dispatch_binding import InlineDispatchBinding
    from tui_gateway.server import _session_profile_runtime_scope

    try:
        with _session_profile_runtime_scope(session, hydrate_secrets=False):
            inline_bindings = tuple(InlineDispatchBinding(agent, name) for name in
                                    [d["function"]["name"] for d in definitions])
            manifests, bindings = runtime_effect_snapshot(definitions, inline_bindings=inline_bindings)
            from tools.dispatch_binding import capture_dispatch_binding
            from tools.registry import registry

            with registry._lock:
                deferred_names = _deferred_execution_names(agent, definitions)
                bindings += tuple(capture_dispatch_binding(registry, name) for name in deferred_names)
            # Registry metadata alone does not identify agent-owned stores,
            # callbacks, context engines or external memory-provider routes.
            # Keep captures private so target replacement retires old consent.
            inline_bindings += tuple(InlineDispatchBinding(agent, name) for name in deferred_names)
        manifest_encoded = json.dumps(manifests, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":"), allow_nan=False).encode("utf-8")
        manifest_identity = hashlib.sha256(manifest_encoded).hexdigest()
    except Exception:
        with _agent_tools_lock:
            session["_tool_snapshot_effect_identity"] = None
            session["_tool_snapshot_registration_bindings"] = None
            session["_tool_snapshot_inline_bindings"] = None
            session["_tool_snapshot_context"] = uuid.uuid4().hex
        raise ValueError("owned effect manifest is unavailable") from None
    with _agent_tools_lock:
        previous = session.get("_tool_snapshot_registration_bindings")
        registrations_equal = (previous is not None and len(previous) == len(bindings)
                               and all(old.same_capture(new) for old, new in zip(previous, bindings)))
        previous_inline = session.get("_tool_snapshot_inline_bindings")
        inline_equal = (previous_inline is not None and len(previous_inline) == len(inline_bindings)
                        and all(old.agent is new.agent and old.name == new.name
                                and old.home == new.home and old.identity == new.identity
                                for old, new in zip(previous_inline, inline_bindings)))
        if (session.get("_tool_snapshot_effect_identity") != manifest_identity
                or not registrations_equal or not inline_equal):
            session["_tool_snapshot_effect_identity"] = manifest_identity
            session["_tool_snapshot_context"] = uuid.uuid4().hex
        session["_tool_snapshot_registration_bindings"] = bindings
        session["_tool_snapshot_inline_bindings"] = inline_bindings
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
