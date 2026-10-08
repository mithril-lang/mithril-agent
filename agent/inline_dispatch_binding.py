"""Host-owned selection of agent inline routes for a single parent dispatch."""

from contextlib import contextmanager
from contextvars import ContextVar

from hermes_constants import hermes_home_key


_CURRENT = ContextVar("agent_inline_dispatch", default=None)


def callable_identity(handler):
    return (id(getattr(handler, "__self__", None)), id(getattr(handler, "__func__", handler)))


class InlineDispatchBinding:
    def __init__(self, agent, name):
        from agent.inline_tool_executors import select_invoke_tool_executor

        self.agent, self.name, self.home = agent, name, hermes_home_key()
        self.executor, self.identity = select_invoke_tool_executor(agent, name)

    def matches(self):
        from agent.inline_tool_executors import select_invoke_tool_executor

        return (hermes_home_key() == self.home
                and select_invoke_tool_executor(self.agent, self.name)[1] == self.identity)


@contextmanager
def bind_inline_dispatch(binding):
    token = _CURRENT.set(binding)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def resolve_bound_inline_executor(agent, name, selected):
    binding = _CURRENT.get()
    if binding is None or binding.agent is not agent or binding.name != name:
        return selected
    if not binding.matches():
        from tools.registry import tool_error

        return lambda agent, args, ctx: tool_error("The inline tool execution target changed during this parent dispatch.")
    return binding.executor
