"""Real local processes exercising the remote file transport, not SSH/provider evidence."""
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest


@pytest.mark.platforms("posix")
def test_remote_kernel_reuses_process_and_namespace_without_reusing_child_claims(tmp_path):
    from pm.shell import bash
    import tools.file_tools  # noqa: F401
    from tools.code_kernel_remote import (
        _REMOTE_KERNELS, execute_in_remote_kernel, shutdown_remote_kernels_for_owner,
    )

    shell = bash()
    assert shell
    task = f"file-rpc-live-{tmp_path.name}"

    class ShellTransport:
        def get_temp_dir(self):
            return str(tmp_path)

        def execute(self, command, cwd=None, timeout=None, stdin_data=None):
            # No provider/user credentials in the execution target.
            result = subprocess.run([shell, "-c", command], cwd=cwd, timeout=timeout,
                env={"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "LANG": "C.UTF-8"},
                input=stdin_data or "", capture_output=True, text=True)
            return {"output": result.stdout, "returncode": result.returncode}

    transport = ShellTransport()
    files = [tmp_path / f"fixture-{index}.txt" for index in range(3)]
    for index, path in enumerate(files):
        path.write_text(f"owned-file-{index}", encoding="utf-8")

    def run(code):
        return execute_in_remote_kernel(code, env=transport, env_type="file-rpc-fixture",
            task_env_id=task, sandbox_tools=frozenset({"read_file"}), timeout=20,
            max_tool_calls=2, reset=False, idle_exit=30)

    kernel = None
    try:
        first = run("import json\nfrom hermes_tools import read_file\nvalue = 41\n" +
            f"print(json.dumps([read_file({str(files[0])!r}), read_file({str(files[1])!r}), " +
            f"read_file({str(files[2])!r})]))")
        assert first is not None and first["status"] == "success", first
        reads = json.loads(first["stdout"])
        assert "owned-file-0" in reads[0]["content"]
        assert "owned-file-1" in reads[1]["content"]
        assert "limit reached" in reads[2]["error"]
        assert first["tool_calls_made"] == 2 and first["kernel"]["execution_count"] == 1
        kernel = next(k for k in _REMOTE_KERNELS.values() if k.owner == task)
        pid = kernel.pid
        claims_before = set((Path(kernel.kernel_dir) / "rpc").glob("dispatch_*"))
        second = run(f"print(json.dumps([value + 1, read_file({str(files[2])!r})]))")
        assert second is not None and second["status"] == "success", second
        assert second["kernel"]["reused"] and second["kernel"]["execution_count"] == 2
        result = json.loads(second["stdout"])
        assert result[0] == 42 and "owned-file-2" in result[1]["content"]
        assert second["tool_calls_made"] == 1 and kernel.pid == pid and kernel.is_alive()
        claims_after = set((Path(kernel.kernel_dir) / "rpc").glob("dispatch_*"))
        assert claims_before < claims_after  # fresh sequence, retained old claims
    finally:
        shutdown_remote_kernels_for_owner(task)
    deadline = time.monotonic() + 5
    while kernel is not None and kernel.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert kernel is not None and not kernel.is_alive()
    assert not Path(kernel.kernel_dir).exists()


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("kind", ["local", "remote"])
@pytest.mark.parametrize("selector", ["owner", "delegated-child"])
def test_same_owner_id_keeps_profile_kernel_state_and_shutdown_separate(tmp_path, monkeypatch, kind, selector):
    import tui_gateway.server as server
    from agent import secret_scope
    from pm.shell import bash
    from tools.code_kernel import (
        execute_in_session_kernel, shutdown_kernels_for_owner, shutdown_kernels_for_delegated_child,
    )
    from tools.code_kernel_remote import execute_in_remote_kernel, shutdown_remote_kernels_for_owner

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    homes = [tmp_path / "profile-a", tmp_path / "profile-b"]
    for home in homes:
        home.mkdir()
    task = "same-public-session-id" + ("::child::shared-child" if selector == "delegated-child" else "")
    shell = bash()
    assert shell

    class ShellTransport:
        def get_temp_dir(self):
            return str(tmp_path)

        def execute(self, command, cwd=None, timeout=None, stdin_data=None):
            result = subprocess.run([shell, "-c", command], cwd=cwd, timeout=timeout,
                env={"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "LANG": "C.UTF-8"},
                input=stdin_data or "", capture_output=True, text=True)
            return {"output": result.stdout, "returncode": result.returncode}

    transport = ShellTransport()

    def scope(home):
        return server._session_profile_runtime_scope({"profile_home": str(home)}, hydrate_secrets=False)

    def run(home, code):
        with scope(home):
            if kind == "local":
                result = json.loads(execute_in_session_kernel(code, task_id=task, mode="strict",
                    child_python=sys.executable, child_cwd=str(tmp_path), sandbox_tools=frozenset(),
                    timeout=20, max_tool_calls=2, reset=False, is_interrupted=lambda: False))
                assert result["exit_code"] == 0, result
                return result["output"].strip()
            result = execute_in_remote_kernel(code, env=transport, env_type="profile-fixture",
                task_env_id=task, sandbox_tools=frozenset(), timeout=20,
                max_tool_calls=2, reset=False, idle_exit=30)
            assert result is not None and result["status"] == "success", result
            return result["stdout"].strip()

    def shutdown(home):
        with scope(home):
            if selector == "delegated-child":
                shutdown_kernels_for_delegated_child("shared-child")
            else:
                (shutdown_kernels_for_owner if kind == "local" else shutdown_remote_kernels_for_owner)(task)

    try:
        assert run(homes[0], "value = 'profile-a'; print(value)") == "profile-a"
        assert run(homes[1], "print('value' in globals()); value = 'profile-b'") == "False"
        assert run(homes[0], "print(value)") == "profile-a"
        shutdown(homes[0])
        assert run(homes[1], "print(value)") == "profile-b"
        assert run(homes[0], "print('value' in globals())") == "False"
    finally:
        for home in homes:
            shutdown(home)
