"""Host-authored identity of the selected agent's todo store, not its mutable data."""

import hashlib
import json
import re


def selected_todo_target(args, task_id):
    from hermes_constants import hermes_home_key
    from tools.terminal_tool import resolve_task_overrides

    overrides = resolve_task_overrides(task_id)
    owner = overrides.get("_owned_session_id")
    if (not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", owner)
            or task_id != f"tool-only:{owner}" or not overrides.get("_owned_runtime_context")):
        return None
    identity = json.dumps({"home": hermes_home_key(), "session": owner}, sort_keys=True).encode()
    return {"namespace": "selected-session-store", "sessionId": owner, "store": "todo-list",
            "ownerDigest": hashlib.sha256(identity).hexdigest()}
