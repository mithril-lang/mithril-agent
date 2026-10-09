"""Bounded provider-authored descriptions; never a target resolution or grant."""

import json

from tools.effect_manifest import copy_effect_manifest


def declared_memory_effects(provider):
    getter = getattr(provider, "get_tool_effect_manifests", None)
    declared = getter() if getter is not None else {}
    if (not isinstance(declared, dict) or len(declared) > 4096
            or any(not isinstance(name, str) or not name or len(name) > 256 for name in declared)):
        raise ValueError("Invalid memory provider effect inventory")
    encoded = json.dumps(declared, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > 128 * 1024:
        raise ValueError("Memory provider effect inventory exceeds owned limit")
    return {name: copy_effect_manifest(value) for name, value in declared.items()}
