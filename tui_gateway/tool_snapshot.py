"""Owned RPC readback of model-visible schemas; never rebuilds tools or grants execution."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid


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
                "revision": None, "registry_generation": None, "definitions": []}
    from tools.mcp_tool_agent import _agent_tools_lock

    with _agent_tools_lock:
        agent = session["agent"]
        definitions = copy.deepcopy(getattr(agent, "tools", []))
        generation = getattr(agent, "_tool_snapshot_generation", None)
        home = session.get("profile_home")
        if (session.get("_tool_snapshot_agent") is not agent
                or session.get("_tool_snapshot_home") != home):
            session["_tool_snapshot_agent"] = agent
            session["_tool_snapshot_home"] = home
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
    encoded = json.dumps(definitions, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > 2 * 1024 * 1024:
        raise ValueError("agent tool snapshot exceeds readback limit")
    return {"protocol": "hermes-session-tool-snapshot-v1", "status": "built",
            "coverage": "model-visible-only", "context_id": context,
            "revision": hashlib.sha256(encoded).hexdigest(),
            "registry_generation": generation if type(generation) is int else None,
            "definitions": definitions}
