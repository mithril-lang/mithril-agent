"""Host-only registration fence used by the existing parent-bound child dispatcher."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json

from hermes_constants import hermes_home_key

_CURRENT = ContextVar("tool_dispatch_registration", default=None)


def _schema_digest(entry):
    if entry is None:
        return None
    from tools.effect_manifest import copy_effect_manifest
    encoded = json.dumps({"schema": entry.schema,
                          "effects": copy_effect_manifest(entry.effect_manifest)},
                         sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > 2 * 1024 * 1024:
        raise ValueError("Registration schema exceeds dispatch limit")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class DispatchBinding:
    registry: object
    name: str
    home: str
    entry: object
    handler: object
    is_async: bool
    schema_digest: str | None

    def same_capture(self, other):
        """Compare immutable captures, never a plugin callable's custom equality."""
        return (self.registry is other.registry and self.name == other.name
                and self.home == other.home and self.entry is other.entry
                and self.handler is other.handler and self.is_async == other.is_async
                and self.schema_digest == other.schema_digest)

    def matches(self, entry):
        return (entry is self.entry and (entry is None or
                (entry.handler is self.handler and entry.is_async == self.is_async
                 and _schema_digest(entry) == self.schema_digest)))


def capture_dispatch_binding(registry, name):
    with registry._lock:
        entry = registry.get_entry(name)
        return DispatchBinding(registry, name, hermes_home_key(), entry,
            entry.handler if entry else None, entry.is_async if entry else False, _schema_digest(entry))


@contextmanager
def bind_dispatch_registration(binding):
    token = _CURRENT.set(binding)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def resolve_dispatch_handler(registry, name, entry, scope=None):
    binding = _CURRENT.get()
    if binding is None or binding.registry is not registry or binding.name != name:
        return entry.handler, entry.is_async
    # Commit selection under the existing registry lock, then invoke the captured
    # callable outside it. A later registration cannot redirect this invocation.
    with registry._lock:
        current = registry.get_entry(name, scope=scope)
        if (hermes_home_key() != binding.home
                or (scope is not None and hermes_home_key(scope) != binding.home)
                or not binding.matches(current) or binding.handler is None):
            raise RuntimeError("Tool registration changed during authorized dispatch")
        return binding.handler, binding.is_async
