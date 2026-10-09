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
