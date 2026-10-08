"""Owned atomic MCP edits preserve profile isolation and the original entry on failure."""
from pathlib import Path

import pytest
import hermes_yaml as yaml


@pytest.fixture
def homes(tmp_path, monkeypatch, _isolate_hermes_home):
    from hermes_constants import get_hermes_home
    from hermes_cli import profiles
    root = get_hermes_home()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(profiles, "_get_default_hermes_home", lambda: root)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: root / "profiles")
    homes = {name: root / "profiles" / name for name in ("alpha", "beta")}
    homes["default"] = root
    for name, home in homes.items():
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(yaml.safe_dump({
            "mcp_servers": {
                "old": {"command": "node", "args": ["server.js"], "env": {"API_KEY": f"private-{name}-token"},
                        "enabled": False, "tools": {"include": []}},
                "other": {"url": "https://other.example/mcp"},
            }, "terminal": {"cwd": str(home)},
        }), encoding="utf-8")
        (home / ".env").write_text("", encoding="utf-8")
    return homes


@pytest.fixture
def client(homes):
    from starlette.testclient import TestClient
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
    c = TestClient(app)
    c.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return c


def read(home):
    return yaml.safe_load((home / "config.yaml").read_text())


def test_edit_and_rename_preserve_secrets_policy_and_other_profiles(client, homes):
    original_default = (homes["default"] / "config.yaml").read_bytes()
    for name in ("alpha", "beta", "alpha"):
        before = read(homes[name])
        rows = client.get(f"/api/mcp/servers?profile={name}").json()["servers"]
        old = next(row for row in rows if row["name"] == "old")
        response = client.put(f"/api/mcp/servers/old?profile={name}", json={
            "name": "edited", "command": "node", "args": ["edited.js"], "env": old["env"],
        })
        assert response.status_code == 200, response.text
        after = read(homes[name])
        assert "old" not in after["mcp_servers"]
        edited = after["mcp_servers"]["edited"]
        assert edited["env"] == before["mcp_servers"]["old"]["env"]
        assert edited["enabled"] is False and edited["tools"] == {"include": []}
        assert after["mcp_servers"]["other"] == before["mcp_servers"]["other"]
        assert after["terminal"] == before["terminal"]
        assert f"private-{name}-token" not in response.text
        # Restore the name through the same path, then revisit A after B.
        restored = client.put(f"/api/mcp/servers/edited?profile={name}", json={
            "name": "old", "command": "node", "args": ["server.js"], "env": old["env"],
        })
        assert restored.status_code == 200
    assert (homes["default"] / "config.yaml").read_bytes() == original_default


@pytest.mark.parametrize("failure", ["collision", "invalid", "write", "plugin", "header-origin"])
def test_failed_edit_leaves_original_config_intact(client, homes, monkeypatch, failure):
    import hermes_cli.config as config
    import tui_gateway.mcp_rpc_helpers as helpers
    if failure == "header-origin":
        cfg = read(homes["alpha"])
        cfg["mcp_servers"]["old"] = {"url": "https://original.example/mcp", "headers": {"Authorization": "Bearer ${OWNED_TOKEN}"}}
        (homes["alpha"] / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    before = {name: (home / "config.yaml").read_bytes() for name, home in homes.items()}
    body = {"name": "edited", "command": "node", "args": ["edited.js"]}
    expected = 400
    if failure == "collision":
        body["name"] = "other"
        expected = 409
    elif failure == "invalid":
        body["url"] = "https://invalid.example/mcp"
    elif failure == "header-origin":
        body = {"name": "edited", "url": "https://different.example/mcp", "auth": "header"}
    elif failure == "write":
        def refuse(*args, **kwargs):
            raise OSError("test-only write failure")
        monkeypatch.setattr(config, "atomic_config_write", refuse)
        # TestClient propagates the server exception while preserving the file.
    elif failure == "plugin":
        monkeypatch.setattr(helpers, "server_configs_with_sources", lambda entries: (entries, {"edited": "owned-plugin"}))
        expected = 409
    if failure == "write":
        with pytest.raises(OSError, match="test-only"):
            client.put("/api/mcp/servers/old?profile=alpha", json=body)
    else:
        assert client.put("/api/mcp/servers/old?profile=alpha", json=body).status_code == expected
    for name, home in homes.items():
        assert (home / "config.yaml").read_bytes() == before[name]
