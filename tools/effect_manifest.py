"""Explicit host-authored effect descriptions; never infer authority from schemas."""

import json


def copy_effect_manifest(value):
    """Bound and validate registration metadata, independent of agent/model imports.

    Partial declarations describe known effects, not an exhaustive authorization
    policy. Missing metadata stays unknown, including MCP annotation-only tools.
    """
    if value is None:
        return {"coverage": "unknown", "effects": [], "targets": []}
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > 8192:
        raise ValueError("Tool effect manifest exceeds registration limit")
    result = json.loads(encoded)
    if (not isinstance(result, dict) or set(result) != {"coverage", "effects", "targets"}
            or result["coverage"] != "partial"):
        raise ValueError("Tool effect manifest must explicitly declare partial coverage")
    effects, targets = result["effects"], result["targets"]
    if (not isinstance(effects, list) or not 1 <= len(effects) <= 32
            or any(not isinstance(x, str) or not x or len(x) > 128 for x in effects)
            or len(set(effects)) != len(effects)):
        raise ValueError("Invalid declared tool effects")
    if not isinstance(targets, list) or len(targets) > 32:
        raise ValueError("Invalid declared tool targets")
    for target in targets:
        if (not isinstance(target, dict) or set(target) != {"kind", "argument", "resolution"}
                or any(not isinstance(x, str) or not x or len(x) > 128 for x in target.values())
                or not target["argument"].startswith("/")):
            raise ValueError("Invalid declared tool target descriptor")
    return result


def runtime_effect_manifests(definitions):
    """Copy descriptors for the frozen model-visible names in the current profile."""
    from tools.registry import registry

    with registry._lock:
        result = []
        for definition in definitions:
            name = definition["function"]["name"]
            entry = registry.get_entry(name)
            result.append({"name": name, **copy_effect_manifest(
                getattr(entry, "effect_manifest", None))})
        return result
