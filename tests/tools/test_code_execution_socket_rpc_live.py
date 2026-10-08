"""Real socket/result loss must not replay a child file mutation."""

import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading

import pytest


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("failure", ["response-loss", "stale-before-send"])
@pytest.mark.parametrize("transport", ["uds", "tcp"])
def test_persistent_socket_reconnects_only_before_dispatch(tmp_path, failure, transport):
    import tools.file_tools  # noqa: F401
    from tools.code_execution_tool import generate_hermes_tools_module
    from tools.code_execution_rpc import _default_dispatch, _rpc_server_loop

    target = tmp_path / "written.txt"
    ready = tmp_path / "stale-closed"
    module = generate_hermes_tools_module(["write_file"])
    stop = threading.Event()
    count, log = [0], []
    dispatch = _default_dispatch(f"uds-live-{tmp_path.name}")

    class DropResponse:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def sendall(self, data):
            raise OSError("fixture: response transfer failed after dispatch")

    with tempfile.TemporaryDirectory(prefix="uds-replay-") as directory:
        if transport == "uds":
            endpoint = str(Path(directory) / "rpc.sock")
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(endpoint)
        else:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            endpoint = f"tcp://127.0.0.1:{listener.getsockname()[1]}"
        listener.listen(2)

        class Listener:
            accepted = 0

            def settimeout(self, value):
                listener.settimeout(value)

            def accept(self):
                conn, peer = listener.accept()
                self.accepted += 1
                if failure == "response-loss" and self.accepted == 1:
                    conn = DropResponse(conn)
                return conn, peer

        server = Listener()

        def serve():
            if failure == "stale-before-send":
                conn, _ = listener.accept()
                conn.close()
                ready.write_text("closed")
            while not stop.is_set():
                _rpc_server_loop(server, "fixture", log, count, 5,
                    frozenset({"write_file"}), stop, "fixture-token", dispatch=dispatch)

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        setup = ""
        if failure == "stale-before-send":
            setup = ("_sock = _connect()\n" +
                f"deadline = time.monotonic() + 5\nwhile not os.path.exists({str(ready)!r}):\n" +
                "    assert time.monotonic() < deadline\n    time.sleep(0.01)\n")
        script = module + "\n" + setup + ("try:\n" +
            f"    result = write_file({str(target)!r}, 'owned mutation')\n" +
            "    print(json.dumps({'result': result}))\n" +
            "except RuntimeError as exc:\n    print(json.dumps({'unknown': str(exc)}))\n")
        try:
            child = subprocess.run([sys.executable, "-c", script], capture_output=True,
                text=True, timeout=15, env={"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin",
                "LANG": "C.UTF-8", "HERMES_RPC_SOCKET": endpoint,
                "HERMES_RPC_TOKEN": "fixture-token", "HERMES_RPC_PERSISTENT": "1"})
            assert child.returncode == 0, child.stderr
            output = json.loads(child.stdout)
            assert target.read_text() == "owned mutation"
            assert count[0] == 1 and len(log) == 1, (count, output)
            if failure == "response-loss":
                assert "unknown" in output and "unknown" in output["unknown"].lower(), output
            else:
                assert "result" in output and "error" not in output["result"], output
        finally:
            stop.set()
            worker.join(timeout=5)
            listener.close()
        assert not worker.is_alive()
