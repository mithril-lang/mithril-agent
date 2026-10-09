"""Bind the local file tools' actual path resolver without starting a runtime."""


def local_file_target(args, task_id):
    from agent.file_safety import get_nt_namespace_error
    from tools.file_tools_paths import _resolve_path_for_task, _terminal_env_type_for_task

    # A host path is not an SSH/container identity. Those namespaces need their
    # own resolver; preserve existing calls without inventing a host grant.
    if _terminal_env_type_for_task(task_id) != "local":
        return None
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return None
    # Preserve the file handlers' raw-path guard before resolving Windows namespaces.
    if get_nt_namespace_error(path):
        return None
    return {"namespace": "selected-local-terminal", "path": str(_resolve_path_for_task(path, task_id))}


def local_search_target(args, task_id):
    """Bind the search root using the handler's documented default semantics.

    This describes the root only; files and links below it are not frozen.
    Remote terminal namespaces retain their existing unbound behavior.
    """
    path = args.get("path", ".")
    if path is None or (isinstance(path, str) and not path.strip()):
        path = "."
    return local_file_target({"path": path}, task_id)


def local_patch_target(args, task_id):
    """Describe every local checked patch path using the handler's resolution.

    Content edits dereference the final link; Delete and Move act on entries.
    This is a bounded partial identity, not atomic multi-file authority.
    """
    if args.get("mode", "replace") == "replace":
        return local_file_target(args, task_id)
    from agent.file_safety import get_nt_namespace_error
    from tools.file_tools import _collect_v4a_header_paths
    from tools.file_tools_paths import _resolve_entry_for_task, _resolve_path_for_task, _terminal_env_type_for_task

    if args.get("mode") != "patch" or _terminal_env_type_for_task(task_id) != "local":
        return None
    patch = args.get("patch")
    if not isinstance(patch, str) or not patch:
        return None
    collected = _collect_v4a_header_paths(patch)
    if isinstance(collected, str):
        raise ValueError("Invalid patch target paths")
    content_paths, entry_paths = list(collected[1]), collected[2]
    if args.get("path"):
        content_paths.append(args["path"])
    targets = set()
    for paths, resolution, resolver in [
        (content_paths, "content", _resolve_path_for_task), (entry_paths, "entry", _resolve_entry_for_task),
    ]:
        for path in paths:
            if not isinstance(path, str) or not path or get_nt_namespace_error(path):
                raise ValueError("Invalid patch target path")
            targets.add((str(resolver(path, task_id)), resolution))
    if not targets:
        return None
    return {"namespace": "selected-local-terminal", "paths": [
        {"path": path, "resolution": resolution} for path, resolution in sorted(targets)]}
