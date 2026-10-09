"""Run an explicit tool through the attached agent, with durable single-attempt authority."""

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()
method = _registry.method


def _tool_only_output(rid, row, *, duplicate=False, output=None, observation="metadata-only"):
    return _ok(rid, {"protocol": "hermes-owned-tool-call-v1", "attempt_id": row["attempt_id"],
        "state": row["state"], "terminal": row["terminal"], "duplicate": duplicate,
        "observation": observation, "output": output})


def _tool_only_run(rid, params, session, agent, db):
    import json
    import time
    import uuid
    from agent.code_child_attempts import _digest
    from agent.code_child_dispatch import _ParentDispatch, bind_tool_only_authority
    from agent.tool_executor import _ToolCallRef, _registered_tool_worker
    from agent.subagent_lifecycle import bind_subagent_parent
    from agent.turn_facade_lease import DurableTurnLease, LEASE_TTL_SECONDS
    from tui_gateway.tool_snapshot import session_tool_snapshot
    from tui_gateway.owned_tool_targets import prepare_owned_tool_task, owned_target_binding

    sid, name = params["session_id"], params["name"]
    home, owner = session.get("profile_home"), agent.session_id
    identity = (params["context_id"], params["revision"])
    deadline = time.monotonic() + params.get("timeout_ms", 120000) / 1000
    task = f"tool-only:{owner}"
    attempt = f"rpc:{params['request_id']}"
    parent = _ToolCallRef(name, {}, task, f"tool-only-call:{params['request_id']}", [])
    try:
        args = json.loads(json.dumps(params["arguments"], ensure_ascii=False, allow_nan=False))
        intent = {"name": name, "args": args, "task": task, "parent": parent.call_id}
        if params.get("target_digest") is not None:
            intent["target_digest"] = params["target_digest"]
        digest, size = _digest(intent)
    except (TypeError, ValueError, OverflowError):
        return _err(rid, 4000, "tool arguments must be bounded finite JSON")
    if size > 2 * 1024 * 1024:
        return _err(rid, 4000, "tool arguments exceed the owned call limit")
    prior = db.get_tool_attempt(owner, attempt)
    if prior is not None:
        if prior["request_digest"] != digest:
            return _err(rid, 4092, "request_id already belongs to a different tool request")
        return _tool_only_output(rid, prior, duplicate=True)
    holder = f"pid={os.getpid()}:tool-only={uuid.uuid4().hex}"
    if not db.try_acquire_session_turn_lease(owner, holder, ttl_seconds=LEASE_TTL_SECONDS):
        return _err(rid, 4009, "session is owned by another active process")
    lease = DurableTurnLease(agent, db, owner, holder)
    previous_turn = getattr(agent, "_current_turn_id", None)
    previous_api_request = getattr(agent, "_current_api_request_id", None)
    agent._active_session_turn_lease_holder = holder
    agent._active_session_turn_lease_ttl_seconds = LEASE_TTL_SECONDS
    agent._current_turn_id = parent.call_id
    agent._current_api_request_id = ""
    target_ready = False

    def authority():
        if (time.monotonic() >= deadline or _current_session_steer_authority(sid)[1] is not session
                or session.get("agent") is not agent or session.get("profile_home") != home
                or agent.session_id != owner or agent._session_db is not db):
            return False
        snapshot = session_tool_snapshot(session)
        if target_ready:
            try:
                binding = owned_target_binding(session, snapshot, name, args, task)
                if binding is None or binding["digest"] != params["target_digest"]:
                    return False
            except (TypeError, ValueError, OSError, RuntimeError):
                return False
        return ((snapshot["context_id"], snapshot["revision"]) == identity
                and db.refresh_session_turn_lease(owner, holder, ttl_seconds=LEASE_TTL_SECONDS))

    dispatch = None
    try:
        if not authority():
            return _err(rid, 4092, "session schema context or authority changed")
        # The private tool-only task must use this conversation's selected
        # workspace and backend overrides, not a gateway process fallback cwd.
        prepare_owned_tool_task(session, agent)
        target_ready = params.get("target_digest") is not None
        if not authority():
            return _err(rid, 4092, "session workspace changed before tool dispatch")
        lease.start()
        dispatch = _ParentDispatch(agent, parent, root_tool=name, authority=authority)
        with _registered_tool_worker(agent), bind_tool_only_authority(authority), bind_subagent_parent(agent):
            result = dispatch.dispatch(task, name, args, call_id=attempt, target_digest=params.get("target_digest"))
        row = db.get_tool_attempt(owner, attempt)
        if row is None:
            return _err(rid, 4092, "tool was not admitted by the owning agent")
        if not authority():
            return _tool_only_output(rid, row, observation="unknown")
        output = result
        if isinstance(result, str):
            try:
                output = json.loads(result)
            except json.JSONDecodeError:
                output = result
        if len(json.dumps(output, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 2 * 1024 * 1024:
            return _tool_only_output(rid, row, observation="unknown")
        observation = "handler-return" if row["state"] in {"returned", "returned-error"} else "policy-result"
        return _tool_only_output(rid, row, output=output, observation=observation)
    except Exception:
        row = db.get_tool_attempt(owner, attempt)
        if row is not None:
            return _tool_only_output(rid, row, observation="unknown")
        return _err(rid, 5036, "owned tool dispatch is unavailable")
    finally:
        if dispatch is not None:
            dispatch.active = False
        lease.stop_refresher()
        lease.join_threads()
        lease.release()
        agent._current_turn_id = previous_turn
        agent._current_api_request_id = previous_api_request


@method("tools.call")
def _(rid, params):
    from pydantic import ValidationError
    from tui_gateway.contracts.tool_call import ToolsCallParams
    from tools.approval_context import set_current_session_key, reset_current_session_key
    from hermes_constants import hermes_home_key
    from pathlib import Path

    try:
        params = ToolsCallParams.model_validate(params).model_dump()
    except ValidationError:
        return _err(rid, 4000, "invalid owned tool call parameters")
    sid = params.get("session_id")
    _, session = _current_session_steer_authority(sid) if sid else (None, None)
    if session is None:
        return _err(rid, 4001, "session is not attached to this transport")
    agent = session.get("agent")
    db = getattr(agent, "_session_db", None)
    if agent is None or db is None or not getattr(agent, "session_id", None):
        return _err(rid, 5032, "an existing built agent and durable session are required")
    with _session_turn_admission(session) as admitted:
        if not admitted or session.get("running"):
            return _err(rid, 4009, "session is busy or retiring")
        if _current_session_steer_authority(sid)[1] is not session:
            return _err(rid, 4001, "session attachment changed before admission")
        session["running"] = True
    try:
        with _session_profile_runtime_scope(session):
            if (hermes_home_key(Path(db.db_path).parent) != hermes_home_key()
                    or db.get_session(agent.session_id) is None):
                return _err(rid, 5036, "owned durable session storage is unavailable")
            token = _current_runtime_session_record.set(session)
            approval = set_current_session_key(agent.session_id)
            context = _set_session_context(agent.session_id, ui_session_id=sid)
            try:
                _wire_callbacks(sid)
                return _tool_only_run(rid, params, session, agent, db)
            finally:
                _clear_session_context(context)
                reset_current_session_key(approval)
                _current_runtime_session_record.reset(token)
    finally:
        with session["history_lock"]:
            session["running"] = False


def register(server):
    bind_module(globals(), server, skip=("_",))
