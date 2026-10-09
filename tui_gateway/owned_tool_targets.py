"""Exact owned target identity, shared by read-only preview and call authority."""

import json

from agent.code_child_attempts import _digest


def prepare_owned_tool_task(session, agent, name=None):
    from tools.terminal_tool import register_task_env_overrides, resolve_task_overrides

    task = f"tool-only:{agent.session_id}"
    overrides = dict(resolve_task_overrides(agent.session_id))
    overrides["_owned_runtime_context"] = session["_tool_snapshot_runtime_context"]
    overrides["_owned_session_id"] = agent.session_id
    overrides.pop("_owned_memory_targets", None)
    if name == "memory":
        from tools.memory_owned_target import capture_memory_targets
        overrides["_owned_memory_targets"] = capture_memory_targets(agent)
    cwd = session.get("cwd")
    if isinstance(cwd, str) and cwd:
        overrides["cwd"] = cwd
    register_task_env_overrides(task, overrides)
    return task


def owned_target_binding(session, snapshot, name, args, task):
    if name not in {row["function"]["name"] for row in snapshot["definitions"]}:
        raise ValueError("tool is not part of this frozen context")
    encoded = json.dumps(args, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("target arguments exceed owned limit")
    args = json.loads(encoded)
    inline = next(row for row in session["_tool_snapshot_inline_bindings"] if row.name == name)
    registration = (inline if inline.owns_effects else
                    next(row for row in session["_tool_snapshot_registration_bindings"] if row.name == name))
    if registration.target_resolver is None:
        return None
    target = registration.target_resolver(json.loads(encoded), task)
    if target is None:
        return None
    encoded = json.dumps(target, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > 8192:
        raise ValueError("resolved target exceeds preview limit")
    target = json.loads(encoded)
    digest, _ = _digest({"protocol": "hermes-owned-target-preview-v1",
                         "owner": session["agent"].session_id, "task": task,
                         "context": snapshot["context_id"], "revision": snapshot["revision"],
                         "name": name, "arguments": args, "target": target})
    return {"coverage": "partial", "digest": digest, "target": target}
