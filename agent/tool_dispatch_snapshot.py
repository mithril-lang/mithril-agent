"""Pin actual agent/deferred tool definitions without rebuilding conversation context."""

import copy
import hashlib
import json


def _published_definitions(agent):
    from tools.mcp_tool_agent import _agent_tools_lock

    with _agent_tools_lock:
        definitions = copy.deepcopy(getattr(agent, "tools", None) or [])
    if not isinstance(definitions, list) or len(definitions) > 4096:
        raise ValueError("Invalid published tool definitions")
    return definitions


def _fingerprints(definitions):
    contracts = {}
    total = 0
    for definition in definitions:
        function = definition.get("function") if isinstance(definition, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        if (not isinstance(name, str) or not name or len(name) > 256 or name in contracts
                or not isinstance(function.get("parameters"), dict)):
            raise ValueError("Invalid tool definition")
        encoded = json.dumps(definition, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
        total += len(encoded)
        if total > 2 * 1024 * 1024:
            raise ValueError("Tool definitions exceed dispatch snapshot limit")
        contracts[name] = hashlib.sha256(encoded).hexdigest()
    return contracts


def _deferred_definitions(agent):
    import model_tools

    return model_tools.get_tool_definitions(
        enabled_toolsets=getattr(agent, "enabled_toolsets", None),
        disabled_toolsets=getattr(agent, "disabled_toolsets", None),
        quiet_mode=True, skip_tool_search_assembly=True,
    ) or []


class ToolDispatchSnapshot:
    def __init__(self, agent, names):
        self.agent = agent
        self.published = _fingerprints(_published_definitions(agent))
        self.deferred_names = frozenset(names) - self.published.keys()
        self.deferred = (_fingerprints(_deferred_definitions(agent))
                         if self.deferred_names else {})
        from tools.dispatch_binding import capture_dispatch_binding
        from tools.registry import registry
        self.registrations = {name: capture_dispatch_binding(registry, name) for name in names}

    def rejection(self, name):
        try:
            if name in self.published:
                current = _fingerprints(_published_definitions(self.agent)).get(name)
                expected = self.published[name]
            elif name in self.deferred_names:
                current = _fingerprints(_deferred_definitions(self.agent)).get(name)
                expected = self.deferred.get(name)
            else:
                return "The tool has no frozen schema in this parent dispatch."
            if expected is None or current != expected:
                return "The tool schema changed during this parent dispatch."
            from tools.registry import registry
            if not self.registrations[name].matches(registry.get_entry(name)):
                return "The tool registration changed during this parent dispatch."
        except (TypeError, ValueError, OverflowError):
            return "The tool schema is unavailable during this parent dispatch."
        return None

    def bind_registration(self, name):
        from tools.dispatch_binding import bind_dispatch_registration
        return bind_dispatch_registration(self.registrations[name])
