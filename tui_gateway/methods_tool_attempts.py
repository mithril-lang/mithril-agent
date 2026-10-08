"""Live attached session authority for private durable attempt metadata."""

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method

_ATTEMPT_WIRE_KEYS = frozenset({"attempt_id", "parent_call_id", "tool_name", "state", "terminal",
    "created_at", "dispatched_at", "settled_at", "result_digest", "result_bytes"})


def _attempt_owner(session):
    agent = session.get("agent")
    return getattr(agent, "session_id", None) or session.get("session_key")


@method("tools.attempts")
def _(rid, params):
    sid = params.get("session_id")
    _, session = _current_session_steer_authority(sid) if sid else (None, None)
    if session is None:
        return _err(rid, 4001, "session is not attached to this transport")
    owner = _attempt_owner(session)
    home = session.get("profile_home")
    error = None
    payload = {"protocol": "hermes-tool-attempts-v1", "coverage": "exact-session-metadata-only",
               "available": False, "attempts": [], "next_cursor": None}
    try:
        with _session_profile_runtime_scope(session):
            with _session_db(session) as db:
                if db is not None and owner and db.get_session(owner) is not None:
                    page = db.list_tool_attempts(owner, limit=params.get("limit", 50),
                                                before_attempt_id=params.get("before_attempt_id"))
                    payload.update(available=True,
                        attempts=[{key: row[key] for key in _ATTEMPT_WIRE_KEYS} for row in page["attempts"]],
                        next_cursor=page["next_cursor"])
    except (ValueError, RuntimeError) as exc:
        error = _err(rid, 5036, str(exc))
    if (_current_session_steer_authority(sid)[1] is not session or _attempt_owner(session) != owner
            or session.get("profile_home") != home):
        return _err(rid, 4001, "session attachment changed during attempt readback")
    return error or _ok(rid, payload)


def register(server):
    bind_module(globals(), server, skip=("_",))
