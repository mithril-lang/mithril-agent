"""Plugin tool registration through the existing profile ownership ledger."""


def register_tool(ctx, name, toolset, schema, handler, check_fn=None, requires_env=None,
                  is_async=False, description="", emoji="", override=False, *, effect_manifest=None):
    from hermes_cli.plugins import PluginToolOverrideError, logger

    if override and not ctx._tool_override_allowed(name):
        raise PluginToolOverrideError(
            f"Plugin {ctx.manifest.name!r} cannot override built-in tool {name!r}. Set "
            f"plugins.entries.{ctx.plugin_id}.allow_tool_override: true "
            f"in config.yaml to allow this plugin to replace built-in tools."
        )
    from tools.registry import registry
    scope = ctx._manager.scope_key
    previous = registry.snapshot_registration(name, scope=scope)
    if previous is None and not override and registry.get_entry(name, scope=scope) is not None:
        logger.warning("Plugin %s tried to shadow global tool %s without override=True",
                       ctx.manifest.name, name)
        return None
    registry.register(
        name=name, toolset=toolset, schema=schema, handler=handler, check_fn=check_fn,
        requires_env=requires_env, is_async=is_async, description=description, emoji=emoji,
        override=override, scope=scope, effect_manifest=effect_manifest,
    )
    registered = registry.snapshot_registration(name, scope=scope)
    handle = None
    if registered is not None and registered is not previous and registered.handler is handler:
        ctx._manager._plugin_tool_names.add(name)
        handle = ctx._manager._track_scoped_registration(
            ctx.manifest, "tool", name, registry, registered, previous,
            finalize=lambda: ctx._manager._remove_tool_name_if_unowned(name),
        )
    logger.debug("Plugin %s registered tool: %s%s", ctx.manifest.name, name,
                 " (override)" if override else "")
    return handle
