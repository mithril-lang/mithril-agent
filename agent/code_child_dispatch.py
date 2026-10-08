"""Bind existing code RPC consumers to the live parent agent's tool policy."""

from contextlib import contextmanager
from contextvars import ContextVar
import threading
import uuid

from hermes_constants import hermes_home_key


_CURRENT = ContextVar("agent_code_child_dispatch", default=None)


class _ParentDispatch:
    def __init__(self, agent, parent):
        from agent.tool_executor import _tool_search_scoped_names

        self.agent = agent
        self.parent = parent
        self.home = hermes_home_key()
        self.turn = getattr(agent, "_current_turn_id", None)
        self.session = getattr(agent, "session_id", None)
        self.names = frozenset(agent.valid_tool_names or ()) | _tool_search_scoped_names(agent)
        self.active = True
        self.thread = threading.get_ident()
        from agent.code_child_attempts import CodeChildAttempts
        self.attempts = CodeChildAttempts(agent, self.home, self.session)

    def rejection(self, task_id, name):
        from agent.tool_executor import _tool_search_scoped_names
        from tools.interrupt import is_thread_interrupted

        if (not self.active or self.agent._interrupt_requested or is_thread_interrupted(self.thread)
                or self.turn != getattr(self.agent, "_current_turn_id", None)
                or self.session != getattr(self.agent, "session_id", None)):
            return "The parent execute_code call is no longer active."
        if task_id != self.parent.task_id or hermes_home_key() != self.home:
            return "The child tool call does not belong to this parent task/profile."
        if getattr(self.agent, "_session_db", None) is not self.attempts.db:
            return "The parent attempt database has been replaced."
        allowed = set(self.agent.valid_tool_names or ()) | _tool_search_scoped_names(self.agent)
        if name not in self.names or name not in allowed or name == "execute_code":
            return f"Tool '{name}' is not available to this parent agent."
        return None

    def dispatch(self, task_id, name, args):
        from agent.tool_executor import (
            _ToolCallRef, _detect_tool_failure, _emit_tool_complete_and_risk,
            _run_agent_tool_execution_middleware,
        )
        from tools.registry import tool_error

        if not isinstance(args, dict):
            return tool_error("Child tool arguments must be a JSON object.")
        rejected = self.rejection(task_id, name)
        if rejected:
            return tool_error(rejected)
        ref = _ToolCallRef(name, args, task_id, uuid.uuid4().hex, [])
        self.attempts.begin(ref, self.parent)
        dispatched = rejected_before_dispatch = False

        def execute(final_args):
            nonlocal dispatched, rejected_before_dispatch
            # Recheck after middleware/plugin/approval waits, immediately before effect.
            rejected = self.rejection(task_id, name)
            if rejected:
                rejected_before_dispatch = True
                return tool_error(rejected)
            self.attempts.dispatch(ref)
            dispatched = True
            return self.agent._invoke_tool(name, final_args, task_id, ref.call_id,
                pre_tool_block_checked=True, skip_tool_request_middleware=True,
                skip_tool_execution_middleware=True, tool_request_middleware_trace=list(ref.trace))

        managed = _run_agent_tool_execution_middleware(self.agent,
            **ref.middleware_kwargs(), execute=execute)
        ref.args = managed.args
        result = managed.result
        failed, _ = _detect_tool_failure(name, result)
        if dispatched:
            state = "returned-error" if failed else "returned"
        else:
            state = "blocked" if managed.blocked else "rejected" if rejected_before_dispatch else "not-dispatched"
        self.attempts.settle(ref, state, result)
        if not managed.blocked:
            # Child code consumes structured data, not a model transcript. Observe
            # policy without substituting transcript-only repeat stubs/guidance.
            decision = self.agent._tool_guardrails.after_call(name, ref.args, result, failed=failed)
            if decision.should_halt:
                self.agent._set_tool_guardrail_halt(decision)
            self.agent._record_file_mutation_result(name, ref.args, result, failed, task_id=task_id)
        _emit_tool_complete_and_risk(self.agent, ref, result, None, managed.blocked)
        return result


@contextmanager
def bind_code_child_dispatch(agent, parent):
    dispatch = _ParentDispatch(agent, parent)
    token = _CURRENT.set(dispatch)
    try:
        yield
    finally:
        dispatch.active = False
        _CURRENT.reset(token)


def resolve_code_child_dispatch(task_id):
    dispatch = _CURRENT.get()
    if dispatch is not None:
        return lambda name, args: dispatch.dispatch(task_id, name, args)
    # Standalone code-tool callers retain their existing registry path.
    from model_tools import handle_function_call
    return lambda name, args: handle_function_call(name, args, task_id=task_id)
