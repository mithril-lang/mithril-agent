"""Bounded client for the existing Code runner; never executes repository source."""
import json
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_BYTES = 2_000_000
MODE = "local-jeV-mithril-harness"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def runner_url(value):
    if not isinstance(value, str):
        raise ValueError("runner_not_configured")
    u = urlsplit(value)
    if (not u.hostname or u.username or u.password or u.query or u.fragment
            or u.path not in ("", "/") or u.scheme not in ("http", "https")
            or (u.scheme == "http" and u.hostname not in ("127.0.0.1", "localhost", "::1"))):
        raise ValueError("invalid_runner_url")
    return value.rstrip("/")


def validate_result(value):
    if (not isinstance(value, dict) or value.get("verified") is not True
            or value.get("format") != "mithril.code-project/v1"
            or not isinstance(value.get("metrics"), dict)
            or not isinstance(value.get("logic"), dict)
            or not isinstance(value.get("files"), dict) or not value["files"]
            or len(value["files"]) > 2):
        raise ValueError("harness_verification_failed")
    allowed = {"src/todo/interaction.cljk", "src/todo/summary.cljk"}
    if set(value["files"]) != allowed or any(not isinstance(s, str) for s in value["files"].values()):
        raise ValueError("harness_verification_failed")
    return value


def call_runner(url, token, action, goal=""):
    if action not in ("status", "run"):
        return {"ok": False, "error": "invalid_action"}
    if action == "run" and (not isinstance(goal, str) or not goal.strip() or len(goal) > 2000):
        return {"ok": False, "error": "invalid_goal"}
    try:
        url = runner_url(url)
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("runner_not_configured")
    except ValueError as error:
        return {"ok": False, "error": str(error)}
    body = json.dumps({"template": "todo", "goal": goal}).encode() if action == "run" else None
    request = Request(url + ("/run" if action == "run" else "/health"), data=body,
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        # Neither redirects nor ambient proxies may forward the runner credential.
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=185 if body else 10) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("invalid_runner_response")
        value = json.loads(raw)
        if action == "status":
            if not isinstance(value, dict) or value.get("mode") != MODE or value.get("ready") is not True:
                raise ValueError("invalid_runner_response")
            return {"ok": True, "ready": True, "busy": value.get("busy") is True, "template": "todo"}
        return {"ok": True, "result": validate_result(value)}
    except HTTPError as error:
        # Do not echo upstream bodies, URLs or credentials. Never retry an uncertain POST.
        codes = {401: "runner_authorization_required", 409: "runner_busy", 503: "harness_verification_failed"}
        return {"ok": False, "error": codes.get(error.code, "runner_request_failed")}
    except (TimeoutError, socket.timeout, URLError, OSError):
        return {"ok": False, "error": "run_outcome_unknown" if body else "runner_unavailable"}
    except (ValueError, TypeError):
        return {"ok": False, "error": "invalid_runner_response"}
