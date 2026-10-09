"""Host-owned selection of agent inline routes for a single parent dispatch."""

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json

from hermes_constants import hermes_home_key


_CURRENT = ContextVar("agent_inline_dispatch", default=None)
_TARGETS = ContextVar("agent_inline_targets", default=None)

# Mutable data revisions stay live; these are execution owners, not data snapshots.
_TARGET_ATTRIBUTES = {
    "todo_list": ("_todo_store",),
    "memory": ("_memory_store", "_memory_store_routes", "_memory_notify", "_memory_write_routes", "_build_memory_write_metadata"),
    "session_search": ("_get_session_db_for_recall",),
    "clarify": ("clarify_callback",),
    "read_terminal": ("read_terminal_callback",),
    "desktop_preview": ("read_preview_callback",),
    "drive_preview": ("drive_preview_callback",),
    "annotate_preview": ("drive_preview_callback",),
    "read_window_below": ("read_window_below_callback",),
    "gui_tour": ("tour_callback",),
    "manage_connections": ("connection_callback",),
    "manage_catalog": ("connection_callback",),
    "setup_mcp": ("connection_callback",),
    "delegate_task": ("_dispatch_delegate_task",),
}


def _read_target(agent, attribute):
    if attribute == "_memory_store_routes":
        from tools.memory_owned_target import memory_store_routes
        return memory_store_routes(getattr(agent, "_memory_store", None))
    if attribute == "_memory_notify":
        manager = getattr(agent, "_memory_manager", None)
        return manager.notify_memory_tool_write if manager else None
    if attribute == "_memory_write_routes":
        manager = getattr(agent, "_memory_manager", None)
        routes = []
        for provider in getattr(manager, "providers", ()):
            routes.append((*memory_provider_identity(provider), callable_identity(provider.on_memory_write)))
        return tuple(routes)
    return getattr(agent, attribute, None)


def memory_provider_identity(provider, *, effects=None):
    """One private, bounded declared identity for mirror and provider-owned routes."""
    signature = getattr(provider, "identity_signature", None)
    from agent.memory_provider_effects import declared_memory_effects

    encoded = json.dumps({"identity": signature() if signature else {},
                          "effects": declared_memory_effects(provider) if effects is None else effects},
                         sort_keys=True, allow_nan=False).encode()
    if len(encoded) > 128 * 1024:
        raise ValueError("memory provider identity exceeds owned limit")
    return (id(provider), provider.name, hashlib.sha256(encoded).hexdigest())


def inline_target(agent, attribute):
    selected = _TARGETS.get()
    if selected is not None and selected[0] is agent and attribute in selected[1]:
        return selected[1][attribute]
    return _read_target(agent, attribute)


def select_inline_targets(agent, name, executor):
    targets = {attr: _read_target(agent, attr) for attr in _TARGET_ATTRIBUTES.get(name, ())}
    identities = tuple((attr, value if attr in {"_memory_write_routes", "_memory_store_routes"} else
                        callable_identity(value) if callable(value) else id(value))
                       for attr, value in targets.items())

    def execute(agent, args, ctx):
        token = _TARGETS.set((agent, targets))
        try:
            return executor(agent, args, ctx)
        finally:
            _TARGETS.reset(token)

    return execute, ("inline", callable_identity(executor), identities)


def callable_identity(handler):
    return (id(getattr(handler, "__self__", None)), id(getattr(handler, "__func__", handler)))


class InlineDispatchBinding:
    def __init__(self, agent, name):
        from agent.inline_tool_executors import select_invoke_tool_executor

        self.agent, self.name, self.home = agent, name, hermes_home_key()
        self.executor, self.identity = select_invoke_tool_executor(agent, name)
        # Inline-owned names must not borrow a same-name registry handler's
        # effects or target resolver. Built-in inline tools retain registry metadata.
        self.owns_effects = self.identity[0] in {"memory", "memory-manager", "context"}
        self.effect_manifest = None
        if self.identity[0] == "memory":
            from agent.memory_provider_effects import declared_memory_effects

            provider, _, _ = agent._memory_manager.resolve_tool_dispatch(name)
            effects = declared_memory_effects(provider)
            if memory_provider_identity(provider, effects=effects) != self.identity[2]:
                raise ValueError("Memory provider changed during effect capture")
            self.effect_manifest = effects.get(name)

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
