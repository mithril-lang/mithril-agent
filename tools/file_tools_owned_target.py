"""Bind the local file writer's actual path resolver without starting a runtime."""


def local_write_target(args, task_id):
    from tools.file_tools_paths import _resolve_path_for_task, _terminal_env_type_for_task

    # A host path is not an SSH/container identity. Those namespaces need their
    # own resolver; preserve existing calls without inventing a host grant.
    if _terminal_env_type_for_task(task_id) != "local":
        return None
    path = args.get("path")
    if not isinstance(path, str) or not path:
        return None
    return {"namespace": "selected-local-terminal", "path": str(_resolve_path_for_task(path, task_id))}
