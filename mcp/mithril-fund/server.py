#!/usr/bin/env python3
"""stdio MCP server for Mithril fund reads and local record checks.

Local tools do not use the network. Remote tools only call MITHRIL_API_ORIGIN
and never call billing, sandbox launch, lake ingest, research-scope writes, or
device approval. Most remote tools are GET-only. The evidence and packet tools
are the exception: they add to, read from and export the signed-in account's own
hosted evidence vault (/v1/evidence) and build report packets (/v1/packets) from it.
There is deliberately no evidence delete tool: deleting evidence is the account
owner's explicit action, not something an agent should do on its own. Nothing here
submits a report anywhere; packets are text the person files themselves.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ALLOWED_ORIGINS = {"https://api.mithril.fund", "http://127.0.0.1:8787"}
REMOTE_GETS = {
    "domain_check": "/v1/check/domain",
    "account_me": "/v1/me",
    "list_models": "/v1/models",
    "billing_status": "/v1/billing/status",
    "list_cases": "/v1/cases",
    "sandbox_status": "/v1/sandbox/session",
}
AUTHENTICATED = {"account_me", "list_models", "billing_status", "list_cases", "sandbox_status"}

MAX_EVIDENCE_FILE_BYTES = 10 * 1024 * 1024
# Local folders that hold credentials. A prompt-injected agent must not be able to copy them into the vault.
_BLOCKED_DIRS = (".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".hermes", ".config", ".mozilla", ".password-store")
_BLOCKED_NAMES = {".env", ".netrc", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "credentials", "credentials.json"}


def _records():
    candidates = []
    override = os.environ.get("MITHRIL_RECORDS_SCRIPT", "").strip()
    if override:
        candidates.append(Path(override))
    here = Path(__file__).resolve()
    candidates.append(here.parents[2] / "skills" / "security" / "security-records" / "scripts" / "records.py")
    candidates.append(Path.home() / ".hermes" / "skills" / "security" / "security-records" / "scripts" / "records.py")
    for path in candidates:
        if path.is_file():
            spec = importlib.util.spec_from_file_location("mithril_security_records", path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module
    raise RuntimeError("security-records script was not found")


RECORDS = None


def records():
    global RECORDS
    if RECORDS is None:
        RECORDS = _records()
    return RECORDS


def _origin() -> str:
    origin = os.environ.get("MITHRIL_API_ORIGIN", "https://api.mithril.fund").rstrip("/")
    if origin not in ALLOWED_ORIGINS:
        raise ValueError("MITHRIL_API_ORIGIN is not an allowed Mithril API origin")
    return origin


def _get(path: str, query: dict[str, str] | None, authenticated: bool) -> dict[str, Any]:
    url = _origin() + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    headers = {"accept": "application/json"}
    if authenticated:
        token = os.environ.get("MITHRIL_API_TOKEN", "").strip()
        if not token:
            raise ValueError("MITHRIL_API_TOKEN is not set")
        headers["authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise ValueError(f"HTTP {exc.code}: {detail}") from exc
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        return {"result": parsed}
    return parsed


def _send(method: str, path: str, query: dict[str, str] | None = None, *, json_body: Any = None,
          raw_body: bytes | None = None, content_type: str | None = None, want_text: bool = False) -> Any:
    """Authenticated request to the Mithril API (evidence vault and packets)."""
    url = _origin() + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    token = os.environ.get("MITHRIL_API_TOKEN", "").strip()
    if not token:
        raise ValueError("MITHRIL_API_TOKEN is not set")
    headers = {"accept": "application/json", "authorization": f"Bearer {token}"}
    data: bytes | None = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        headers["content-type"] = "application/json"
    elif raw_body is not None:
        data = raw_body
        headers["content-type"] = content_type or "application/octet-stream"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise ValueError(f"HTTP {exc.code}: {detail}") from exc
    if want_text:
        return body
    parsed = json.loads(body)
    return parsed if isinstance(parsed, dict) else {"result": parsed}


def _read_local_file(raw_path: str) -> tuple[bytes, str]:
    """Read one file the user named. Refuses credential folders, symlinks out of them, and oversize files."""
    path = Path(raw_path).expanduser()
    if not path.is_file():
        raise ValueError("file_path must be an existing regular file")
    resolved = path.resolve()
    parts = {part.lower() for part in resolved.parts}
    if parts & set(_BLOCKED_DIRS) or resolved.name.lower() in _BLOCKED_NAMES or resolved.name.lower().startswith(".env"):
        raise ValueError("that location holds credentials or configuration and cannot be added as evidence")
    if resolved.stat().st_size > MAX_EVIDENCE_FILE_BYTES:
        raise ValueError(f"file is larger than {MAX_EVIDENCE_FILE_BYTES} bytes")
    return resolved.read_bytes(), resolved.name


def _save_text(raw_path: str, text: str) -> str:
    """Write a packet to a new file. Never overwrites."""
    path = Path(raw_path).expanduser()
    if path.exists():
        raise ValueError("save_to already exists; choose a new file name")
    if not path.parent.is_dir():
        raise ValueError("save_to folder does not exist")
    path.write_text(text, encoding="utf-8")
    return str(path.resolve())


def _opt_str(arguments: dict[str, Any], key: str) -> str | None:
    value = arguments.get(key)
    return None if value is None or str(value).strip() == "" else str(value)


def _evidence_add(arguments: dict[str, Any]) -> Any:
    kind = str(arguments.get("kind") or "")
    title = _opt_str(arguments, "title")
    captured = arguments.get("captured_at")
    if captured is not None and (not isinstance(captured, int) or isinstance(captured, bool) or captured < 0):
        raise ValueError("captured_at must be unix seconds")
    if kind in ("note", "url"):
        text = str(arguments.get("text") or "")
        if not text.strip():
            raise ValueError("text is required for a note or a url")
        body: dict[str, Any] = {"kind": kind, "text": text}
        if title:
            body["title"] = title
        if captured is not None:
            body["capturedAt"] = captured
        return _send("POST", "/v1/evidence", json_body=body)
    if kind == "file":
        file_path = _opt_str(arguments, "file_path")
        if not file_path:
            raise ValueError("file_path is required for a file")
        data, name = _read_local_file(file_path)
        if not data:
            raise ValueError("the file is empty")
        query = {"filename": name}
        if title:
            query["title"] = title
        if captured is not None:
            query["capturedAt"] = str(captured)
        mime = _opt_str(arguments, "mime") or "application/octet-stream"
        return _send("POST", "/v1/evidence/file", query, raw_body=data, content_type=mime)
    raise ValueError("kind must be note, url or file")


def _evidence_id(arguments: dict[str, Any]) -> str:
    item_id = str(arguments.get("id") or "").strip()
    if not item_id or "/" in item_id or "?" in item_id:
        raise ValueError("id is required")
    return urllib.parse.quote(item_id, safe="")


def _packet(kind: str, arguments: dict[str, Any]) -> Any:
    body: dict[str, Any] = {}
    if kind == "scam-loss" and isinstance(arguments.get("facts"), dict):
        body["facts"] = arguments["facts"]
    if kind == "linkedin-recovery" and _opt_str(arguments, "account_note"):
        body["accountNote"] = _opt_str(arguments, "account_note")
    if isinstance(arguments.get("item_ids"), list):
        body["itemIds"] = [str(i) for i in arguments["item_ids"]]
    result = _send("POST", f"/v1/packets/{kind}", json_body=body)
    save_to = _opt_str(arguments, "save_to")
    if save_to and isinstance(result.get("markdown"), str):
        result["savedTo"] = _save_text(save_to, result["markdown"])
    return result


TOOLS = [
    {
        "name": "cmdb_check",
        "description": "Structural check of a caller-supplied asset and dependency list.",
        "inputSchema": {"type": "object", "properties": {"assets": {"type": "array"}, "dependencies": {"type": "array"}}, "required": ["assets", "dependencies"]},
    },
    {
        "name": "email_signals",
        "description": "Attachment-name signals. Does not read file bytes or message bodies.",
        "inputSchema": {"type": "object", "properties": {"messages": {"type": "array"}}, "required": ["messages"]},
    },
    {
        "name": "compliance_fit",
        "description": "Published framework-to-control mapping. Not an audit result.",
        "inputSchema": {"type": "object", "properties": {"framework": {"type": "string"}}, "required": ["framework"]},
    },
    {
        "name": "security_catalog",
        "description": "Published security-service catalog. Description only; it does not scan.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "spear_phishing_defence",
        "description": "Describes the spear-phishing defence page. It does not send mail.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "domain_check",
        "description": "GET the public phishing-domain projection for one domain name.",
        "inputSchema": {"type": "object", "properties": {"domain": {"type": "string"}}, "required": ["domain"]},
    },
    {
        "name": "account_me",
        "description": "GET /v1/me with the configured token.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_models",
        "description": "GET /v1/models with the configured token.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "billing_status",
        "description": "GET billing status. It does not start a checkout.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_cases",
        "description": "GET cases for the authenticated account.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "sandbox_status",
        "description": "GET the current sandbox session. It does not launch one.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "evidence_add",
        "description": "Add one item to the signed-in account's hosted evidence vault: a text note, a URL string (the page is not fetched), or a local file the user named. Stores the exact bytes with their SHA-256 and a server timestamp. Keep your originals; the vault does not replace them.",
        "inputSchema": {"type": "object", "properties": {
            "kind": {"type": "string", "enum": ["note", "url", "file"]},
            "text": {"type": "string", "description": "The note text, or the URL (http/https). For kind note or url."},
            "file_path": {"type": "string", "description": "Path of a file the user asked to add. For kind file. Max 10 MiB; credential folders are refused."},
            "mime": {"type": "string"},
            "title": {"type": "string"},
            "captured_at": {"type": "integer", "description": "Optional unix seconds: when the user saw or captured the item. Not verified by the server."}},
            "required": ["kind"]},
    },
    {
        "name": "evidence_list",
        "description": "List the signed-in account's own evidence items (metadata and SHA-256; not the content).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "evidence_get",
        "description": "GET one evidence item's metadata by id (own account only).",
        "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    {
        "name": "evidence_export",
        "description": "Export the account's evidence manifest (mithril-evidence-export/v1): all items with hashes and timestamps, note text and URLs inline. File bytes are not included.",
        "inputSchema": {"type": "object", "properties": {"save_to": {"type": "string", "description": "Optional new file path to save the manifest as JSON. Never overwrites."}}},
    },
    {
        "name": "packet_scam_loss",
        "description": "Build a scam-loss report packet (Markdown) from facts the user typed plus their own evidence items: a timeline, a hash table, and a checklist paraphrased from IC3, FTC pages with source URLs. The person files their own report; this tool submits nothing and is not legal advice.",
        "inputSchema": {"type": "object", "properties": {
            "facts": {"type": "object", "description": "User-entered facts: summary, howContactBegan, platforms[], domains[], phoneNumbers[], payments[{date,method,amount,currency,destination,transactionHash,note}], reportsFiled[{where,date,referenceNumber}]. Leave out anything the user did not say."},
            "item_ids": {"type": "array", "items": {"type": "string"}, "description": "Evidence item ids to include; omit for all."},
            "save_to": {"type": "string", "description": "Optional new file path to save the Markdown. Never overwrites."}}},
    },
    {
        "name": "packet_linkedin_recovery",
        "description": "Build a LinkedIn account-recovery checklist (Markdown) with the user's own evidence items, paraphrased from LinkedIn Help with its URL. The person submits LinkedIn's own form; this tool submits nothing.",
        "inputSchema": {"type": "object", "properties": {
            "account_note": {"type": "string"},
            "item_ids": {"type": "array", "items": {"type": "string"}},
            "save_to": {"type": "string", "description": "Optional new file path to save the Markdown. Never overwrites."}}},
    },
]


def call_tool(name: str, arguments: dict[str, Any]) -> Any:
    module = records()
    if name == "cmdb_check":
        return module.check_cmdb(arguments)
    if name == "email_signals":
        return module.email_signals(arguments)
    if name == "compliance_fit":
        return module.compliance_fit(str(arguments.get("framework") or ""))
    if name == "security_catalog":
        return module.service_catalog()
    if name == "spear_phishing_defence":
        return module.spear_phishing_defence()
    if name == "domain_check":
        domain = str(arguments.get("domain") or "").strip().lower().rstrip(".")
        if not domain or "." not in domain:
            raise ValueError("a domain name is required")
        return _get(REMOTE_GETS[name], {"domain": domain}, False)
    if name == "evidence_add":
        return _evidence_add(arguments)
    if name == "evidence_list":
        return _send("GET", "/v1/evidence")
    if name == "evidence_get":
        return _send("GET", f"/v1/evidence/{_evidence_id(arguments)}")
    if name == "evidence_export":
        exported = _send("GET", "/v1/evidence/export")
        save_to = _opt_str(arguments, "save_to")
        if save_to:
            exported["savedTo"] = _save_text(save_to, json.dumps(exported, ensure_ascii=False, indent=2))
        return exported
    if name == "packet_scam_loss":
        return _packet("scam-loss", arguments)
    if name == "packet_linkedin_recovery":
        return _packet("linkedin-recovery", arguments)
    if name in REMOTE_GETS:
        return _get(REMOTE_GETS[name], None, name in AUTHENTICATED)
    raise ValueError(f"unknown tool: {name}")


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    msg_id = message.get("id")
    if method == "notifications/initialized" or msg_id is None:
        return None
    try:
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mithril-fund", "version": "0.2.0"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = message.get("params") or {}
            payload = call_tool(str(params.get("name") or ""), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "isError": False}
        else:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "method not found"}}
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    except Exception as exc:  # noqa: BLE001 — tool errors are returned to the client
        if method == "tools/call":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"content": [{"type": "text", "text": json.dumps({"error": str(exc)})}], "isError": True},
            }
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32000, "message": str(exc)}}


def _read_message() -> dict[str, Any] | None:
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line == b"":
            return None
        if line in (b"\r\n", b"\n"):
            break
        decoded = line.decode("ascii", errors="replace")
        if ":" not in decoded:
            continue
        key, value = decoded.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    body = sys.stdin.buffer.read(length)
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("MCP message must be an object")
    return parsed


def _write_message(payload: dict[str, Any]) -> None:
    raw = json.dumps(payload).encode("utf-8")
    header = f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii")
    sys.stdout.buffer.write(header)
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def main() -> int:
    while True:
        message = _read_message()
        if message is None:
            return 0
        response = handle(message)
        if response is not None:
            _write_message(response)


if __name__ == "__main__":
    raise SystemExit(main())
