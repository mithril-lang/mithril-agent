"""Owned read-only target preview. No handler, runtime startup or permission grant."""

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method


@method("tools.target_preview")
def _(rid, params):
    from tui_gateway.owned_tool_targets import prepare_owned_tool_task, owned_target_binding
    from tui_gateway.tool_snapshot import session_tool_snapshot

    sid = params["session_id"]
    _, session = _current_session_steer_authority(sid)
    if session is None:
        return _err(rid, 4001, "session is not attached to this transport")
    agent = session.get("agent")
    if agent is None or not getattr(agent, "session_id", None):
        return _err(rid, 5032, "an existing built agent is required")
    with _session_turn_admission(session) as admitted:
        if not admitted or session.get("running"):
            return _err(rid, 4009, "session is busy or retiring")
        try:
            with _session_profile_runtime_scope(session, hydrate_secrets=False):
                token = _current_runtime_session_record.set(session)
                try:
                    snapshot = session_tool_snapshot(session)
                    if (snapshot["context_id"], snapshot["revision"]) != (params["context_id"], params["revision"]):
                        return _err(rid, 4092, "target preview context changed")
                    task = prepare_owned_tool_task(session, agent)
                    binding = owned_target_binding(session, snapshot, params["name"], params["arguments"], task)
                    latest = session_tool_snapshot(session)
                    if (_current_session_steer_authority(sid)[1] is not session
                            or (latest["context_id"], latest["revision"]) != (snapshot["context_id"], snapshot["revision"])):
                        return _err(rid, 4092, "target preview authority changed")
                    return _ok(rid, {"protocol": "hermes-owned-target-preview-v1", "target_binding": binding})
                finally:
                    _current_runtime_session_record.reset(token)
        except (TypeError, ValueError, OSError, RuntimeError):
            return _err(rid, 4092, "owned target cannot be resolved")


def register(server):
    bind_module(globals(), server, skip=("_",))
