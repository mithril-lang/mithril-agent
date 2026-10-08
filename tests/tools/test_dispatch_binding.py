"""Actual registry dispatch selects the admitted callable, with profile-owned fencing."""

from contextlib import nullcontext
import json

import pytest


@pytest.mark.parametrize("bound", [False, True])
def test_registry_invokes_selected_callable_after_entry_changes(tmp_path, monkeypatch, bound):
    from tools.registry import ToolRegistry
    import tools.registry as module

    registry = ToolRegistry()
    selected, replaced = tmp_path / "selected.txt", tmp_path / "replaced.txt"

    def first(args):
        selected.write_text(args["value"])
        return json.dumps({"selected": True})

    def second(args):
        replaced.write_text(args["value"])
        return json.dumps({"replaced": True})

    registry.register("owned_write", "owned", {"parameters": {"type": "object"}}, first)
    entry = registry.get_entry("owned_write")
    signature = module._kwargs_accepted_by

    def change_after_selection(handler, kwargs):
        accepted = signature(handler, kwargs)
        entry.handler = second
        entry.is_async = True
        return accepted

    monkeypatch.setattr(module, "_kwargs_accepted_by", change_after_selection)
    context = nullcontext()
    if bound:
        from tools.dispatch_binding import capture_dispatch_binding, bind_dispatch_registration
        context = bind_dispatch_registration(capture_dispatch_binding(registry, "owned_write"))
    with context:
        result = registry.dispatch("owned_write", {"value": "owned mutation"})
    assert selected.exists() and not replaced.exists(), {"selected": selected.exists(), "replaced": replaced.exists()}
    assert json.loads(result) == {"selected": True}
    assert selected.read_text() == "owned mutation"


def test_registration_binding_uses_actual_profile_scope_and_rejects_replacement(tmp_path, monkeypatch):
    from agent import secret_scope
    import tui_gateway.server as server
    from tools.registry import ToolRegistry
    from tools.dispatch_binding import capture_dispatch_binding, bind_dispatch_registration

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    registry = ToolRegistry()
    homes = {name: tmp_path / name for name in ["a", "b"]}

    def scope(name):
        return server._session_profile_runtime_scope({"profile_home": str(homes[name])}, hydrate_secrets=False)

    for name, home in homes.items():
        home.mkdir()

        def write(args, home=home):
            (home / "owned.txt").write_text(args["value"])
            return json.dumps({"owner": home.name})

        registry.register("owned_write", "owned", {"parameters": {"type": "object"}}, write, scope=str(home))

    with scope("a"):
        a = capture_dispatch_binding(registry, "owned_write")
    for name in ["a", "b", "a"]:
        with scope(name):
            own = capture_dispatch_binding(registry, "owned_write")
            with bind_dispatch_registration(own):
                assert json.loads(registry.dispatch("owned_write", {"value": name})) == {"owner": name}
            if name == "b":
                with bind_dispatch_registration(a):
                    result = registry.dispatch("owned_write", {"value": "foreign"})
                    assert "registration changed" in json.loads(result)["error"]
                assert (homes["b"] / "owned.txt").read_text() == "b"
    with scope("a"):
        old = registry.get_entry("owned_write")
        registry.register("owned_write", "owned", old.schema, lambda args: '{"replacement":true}', scope=str(homes["a"]))
        with bind_dispatch_registration(a):
            assert "registration changed" in json.loads(registry.dispatch("owned_write", {"value": "replace"}))["error"]
        assert (homes["a"] / "owned.txt").read_text() == "a"
