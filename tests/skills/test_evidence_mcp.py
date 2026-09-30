"""Tests for the evidence_* and packet_* tools of the mithril-fund MCP server."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "mcp" / "mithril-fund" / "server.py"


@pytest.fixture()
def server():
    spec = importlib.util.spec_from_file_location("mithril_fund_server", SERVER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def calls(monkeypatch, server):
    """Replace the network. Records (method, url, headers, body) and answers {"ok": true} unless configured."""
    recorded: list[dict] = []
    answers: dict = {"status": 200, "body": {"ok": True}}

    def fake_urlopen(request, timeout=0):
        recorded.append({
            "method": request.get_method(),
            "url": request.full_url,
            "headers": {k.lower(): v for k, v in request.header_items()},
            "body": request.data,
        })
        if answers["status"] != 200:
            raise urllib.error.HTTPError(
                request.full_url,
                answers["status"],
                "err",
                {},
                io.BytesIO(b'{"error":{"code":"x"}}'),
            )
        return FakeResponse(json.dumps(answers["body"]).encode())

    monkeypatch.setattr(server.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("MITHRIL_API_TOKEN", "mf_test_token_value")
    monkeypatch.delenv("MITHRIL_API_ORIGIN", raising=False)
    recorded_answers = answers
    recorded.append  # keep reference
    return type("Calls", (), {"log": recorded, "answers": recorded_answers})


def test_tools_are_listed_and_there_is_no_delete(server) -> None:
    names = {t["name"] for t in server.TOOLS}
    assert {
        "evidence_add",
        "evidence_list",
        "evidence_get",
        "evidence_export",
        "packet_scam_loss",
        "packet_linkedin_recovery",
    } <= names
    assert not any("delete" in n or "remove" in n for n in names)


def test_add_note_and_url_post_json_with_the_bearer(server, calls) -> None:
    server.call_tool(
        "evidence_add",
        {
            "kind": "note",
            "text": "wire on 2026-09-01",
            "title": "Wire",
            "captured_at": 1790000000,
        },
    )
    server.call_tool("evidence_add", {"kind": "url", "text": "https://example.test/x"})
    first, second = calls.log
    assert (
        first["method"] == "POST"
        and first["url"] == "https://api.mithril.fund/v1/evidence"
    )
    assert first["headers"]["authorization"] == "Bearer mf_test_token_value"
    assert json.loads(first["body"]) == {
        "kind": "note",
        "text": "wire on 2026-09-01",
        "title": "Wire",
        "capturedAt": 1790000000,
    }
    assert json.loads(second["body"]) == {
        "kind": "url",
        "text": "https://example.test/x",
    }


def test_add_file_sends_raw_bytes_with_filename_and_type(
    server, calls, tmp_path
) -> None:
    f = tmp_path / "statement.pdf"
    f.write_bytes(b"%PDF-1.4 test")
    server.call_tool(
        "evidence_add",
        {
            "kind": "file",
            "file_path": str(f),
            "title": "Bank",
            "mime": "application/pdf",
        },
    )
    (req,) = calls.log
    assert req["method"] == "POST" and req["url"].startswith(
        "https://api.mithril.fund/v1/evidence/file?"
    )
    assert "filename=statement.pdf" in req["url"] and "title=Bank" in req["url"]
    assert req["body"] == b"%PDF-1.4 test"
    assert req["headers"]["content-type"] == "application/pdf"


def test_add_file_refuses_credentials_missing_and_oversize_files(
    server, calls, tmp_path
) -> None:
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    key = ssh / "id_ed25519"
    key.write_text("secret")
    with pytest.raises(ValueError, match="credentials"):
        server.call_tool("evidence_add", {"kind": "file", "file_path": str(key)})
    env = tmp_path / ".env"
    env.write_text("A=1")
    with pytest.raises(ValueError, match="credentials"):
        server.call_tool("evidence_add", {"kind": "file", "file_path": str(env)})
    with pytest.raises(ValueError, match="existing regular file"):
        server.call_tool(
            "evidence_add", {"kind": "file", "file_path": str(tmp_path / "nope.txt")}
        )
    big = tmp_path / "big.bin"
    big.write_bytes(b"x")
    server.MAX_EVIDENCE_FILE_BYTES = 0
    with pytest.raises(ValueError, match="larger than"):
        server.call_tool("evidence_add", {"kind": "file", "file_path": str(big)})
    assert calls.log == []


def test_add_validates_kind_and_text(server, calls) -> None:
    for args in (
        {"kind": "video"},
        {"kind": "note", "text": "  "},
        {"kind": "file"},
        {"kind": "note", "text": "x", "captured_at": -1},
    ):
        with pytest.raises(ValueError):
            server.call_tool("evidence_add", args)
    assert calls.log == []


def test_list_get_and_export_are_gets_and_get_quotes_the_id(
    server, calls, tmp_path
) -> None:
    server.call_tool("evidence_list", {})
    server.call_tool("evidence_get", {"id": "abc-123"})
    with pytest.raises(ValueError):
        server.call_tool("evidence_get", {"id": "../../v1/me"})
    with pytest.raises(ValueError):
        server.call_tool("evidence_get", {"id": ""})
    calls.answers["body"] = {"format": "mithril-evidence-export/v1", "items": []}
    out = tmp_path / "manifest.json"
    result = server.call_tool("evidence_export", {"save_to": str(out)})
    assert [c["method"] for c in calls.log] == ["GET", "GET", "GET"]
    assert [c["url"].removeprefix("https://api.mithril.fund") for c in calls.log] == [
        "/v1/evidence",
        "/v1/evidence/abc-123",
        "/v1/evidence/export",
    ]
    assert json.loads(out.read_text())["format"] == "mithril-evidence-export/v1"
    assert result["savedTo"] == str(out.resolve())
    with pytest.raises(ValueError, match="already exists"):
        server.call_tool("evidence_export", {"save_to": str(out)})


def test_packets_post_facts_only_what_was_given_and_can_save(
    server, calls, tmp_path
) -> None:
    calls.answers["body"] = {
        "kind": "scam-loss",
        "markdown": "# Packet\n",
        "itemCount": 0,
    }
    out = tmp_path / "packet.md"
    result = server.call_tool(
        "packet_scam_loss",
        {"facts": {"summary": "s"}, "item_ids": ["a", "b"], "save_to": str(out)},
    )
    req = calls.log[0]
    assert (
        req["method"] == "POST"
        and req["url"] == "https://api.mithril.fund/v1/packets/scam-loss"
    )
    assert json.loads(req["body"]) == {"facts": {"summary": "s"}, "itemIds": ["a", "b"]}
    assert out.read_text() == "# Packet\n" and result["savedTo"] == str(out.resolve())

    server.call_tool("packet_linkedin_recovery", {"account_note": "locked out"})
    assert calls.log[1]["url"].endswith("/v1/packets/linkedin-recovery")
    assert json.loads(calls.log[1]["body"]) == {"accountNote": "locked out"}


def test_http_errors_and_missing_token_surface_as_errors(
    server, calls, monkeypatch
) -> None:
    calls.answers["status"] = 404
    with pytest.raises(ValueError, match="HTTP 404"):
        server.call_tool("evidence_get", {"id": "x"})
    monkeypatch.delenv("MITHRIL_API_TOKEN")
    with pytest.raises(ValueError, match="MITHRIL_API_TOKEN is not set"):
        server.call_tool("evidence_list", {})


def test_disallowed_origin_is_refused_before_any_request(
    server, calls, monkeypatch
) -> None:
    monkeypatch.setenv("MITHRIL_API_ORIGIN", "https://evil.example")
    with pytest.raises(ValueError, match="not an allowed"):
        server.call_tool("evidence_add", {"kind": "note", "text": "x"})
    assert calls.log == []


def test_tool_text_has_no_investigation_or_storage_claims(server) -> None:
    text = json.dumps([
        t for t in server.TOOLS if t["name"].startswith(("evidence_", "packet_"))
    ])
    for pattern in (
        r"we (will )?investigate",
        r"on your behalf",
        r"never stored",
        r"\bZDR\b",
        r"legal advice is",
        r"guarantee",
    ):
        assert not re.search(pattern, text, re.I)
    assert "submits nothing" in text
