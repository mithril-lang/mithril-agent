"""Host-authored built-in memory slot and declared mirror identities, not a grant."""

import hashlib
import json

from tools.todo_owned_target import selected_todo_target


def memory_store_routes(store):
    if store is None or not callable(getattr(store, "target_enabled", None)) or not callable(getattr(store, "_path_for", None)):
        return ()
    return tuple((target, store.target_enabled(target), str(store._path_for(target).resolve()))
                 for target in ("memory", "user"))


def capture_memory_targets(agent):
    from agent.inline_dispatch_binding import memory_provider_identity

    mirrors = []
    for provider in getattr(getattr(agent, "_memory_manager", None), "providers", ()):
        if provider.name == "builtin":
            continue
        name = provider.name
        if not isinstance(name, str) or not name.strip() or len(name) > 128 or not name.isprintable():
            raise ValueError("memory mirror name exceeds target limits")
        mirrors.append({"name": name, "identityDigest": memory_provider_identity(provider)[2]})
    if len(mirrors) > 16 or len({row["name"] for row in mirrors}) != len(mirrors):
        raise ValueError("memory mirror inventory exceeds target limits")
    return {target: {"storeDigest": hashlib.sha256(path.encode()).hexdigest(), "mirrors": mirrors}
            for target, enabled, path in memory_store_routes(getattr(agent, "_memory_store", None)) if enabled}


def selected_memory_target(args, task_id):
    from tools.terminal_tool import resolve_task_overrides

    owner = selected_todo_target(args, task_id)
    target = args.get("target", "memory")
    target = "memory" if target is None else target
    if owner is None or target not in ("memory", "user"):
        return None
    routes = resolve_task_overrides(task_id).get("_owned_memory_targets", {})
    selected = routes.get(target)
    if selected is None:
        return None
    return {"namespace": "selected-session-memory", "sessionId": owner["sessionId"],
            "ownerDigest": owner["ownerDigest"], "store": target,
            **json.loads(json.dumps(selected, allow_nan=False))}
