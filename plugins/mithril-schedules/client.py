"""Original schedule custody protocol. Never executes a script, tool or model."""
import json
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

LIMIT = 8192
OCCURRENCE = {"profile", "jobId", "scheduledInstant", "operationId", "authorityRevision", "sourceRevision", "sourceDigest"}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def origin(value):
    if value == "https://api.mithril.fund":
        return value
    if not isinstance(value, str):
        raise ValueError()
    u = urlsplit(value)
    if (u.scheme != "http" or u.hostname not in ("127.0.0.1", "localhost", "::1")
            or u.username or u.password or u.query or u.fragment or u.path):
        raise ValueError()
    # Force port validation before opening any connection.
    _ = u.port
    return value


def integer(value, minimum=0):
    return type(value) is int and minimum <= value < 2**53


def validate_command(command):
    if not isinstance(command, dict) or not isinstance(command.get("profile"), str):
        raise ValueError()
    action = command.get("action")
    fields = {"status": {"action", "profile"}, "select": {"action", "profile", "expectedRevision"},
              "claim": OCCURRENCE | {"action"}, "transition": OCCURRENCE | {"action", "from", "to"}}
    if action not in fields or set(command) != fields[action]:
        raise ValueError()
    if action == "select" and not integer(command["expectedRevision"]):
        raise ValueError()
    if action in ("claim", "transition"):
        if not all(isinstance(command[key], str) for key in ("jobId", "scheduledInstant", "operationId", "sourceDigest")):
            raise ValueError()
        if not all(integer(command[key], 1) for key in ("authorityRevision", "sourceRevision")):
            raise ValueError()
        if not re.fullmatch(r"[a-f0-9]{64}", command["sourceDigest"]):
            raise ValueError()
    if action == "transition" and (command["from"], command["to"]) not in (
            ("admitted", "running"), ("admitted", "unknown"), ("running", "completed"), ("running", "unknown")):
        raise ValueError()


def receipt(value, owner, command):
    if not isinstance(value, dict) or value.get("userId") != owner or value.get("profile") != command["profile"]:
        raise ValueError()
    action = command["action"]
    if action in ("status", "select"):
        if set(value) != {"userId", "profile", "selected", "revision"} or type(value["selected"]) is not bool or not integer(value["revision"]):
            raise ValueError()
        if action == "select" and (not value["selected"] or value["revision"] != command["expectedRevision"] + 1):
            raise ValueError()
    else:
        if value.get("operationId") != command["operationId"]:
            raise ValueError()
        if action == "claim":
            if (set(value) != {"userId", "profile", "operationId", "status", "fresh"}
                    or value["status"] not in ("admitted", "running", "completed", "unknown")
                    or type(value["fresh"]) is not bool or (value["fresh"] and value["status"] != "admitted")):
                raise ValueError()
        elif set(value) != {"userId", "profile", "operationId", "changed"} or type(value["changed"]) is not bool:
            raise ValueError()
    return value


def call_custody(api_origin, token, owner, command):
    try:
        base = origin(api_origin)
        if not isinstance(token, str) or not re.fullmatch(r"mf_[A-Za-z0-9_-]{16,200}", token):
            raise ValueError()
        if not isinstance(owner, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", owner):
            raise ValueError()
        validate_command(command)
        body = json.dumps(command, ensure_ascii=False, allow_nan=False).encode()
        if len(body) > LIMIT:
            raise ValueError()
    except (ValueError, TypeError):
        return {"ok": False, "error": "invalid_schedule_command"}
    request = Request(base + "/v1/schedules/original/execution", data=body,
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        # Never forward the profile secret via redirects/ambient proxies, and
        # never retry a mutation whose acknowledgement was lost.
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=15) as response:
            raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError()
        result = receipt(json.loads(raw), owner, command)
        return {"ok": True, "receipt": result}
    except HTTPError as error:
        error.close()
        return {"ok": False, "error": {401: "schedule_sign_in_required", 403: "schedule_authorization_required",
                409: "schedule_conflict", 503: "schedule_unavailable"}.get(error.code, "schedule_request_failed")}
    except (TimeoutError, socket.timeout, URLError, OSError):
        return {"ok": False, "error": "schedule_outcome_unknown"}
    except (ValueError, TypeError):
        return {"ok": False, "error": "schedule_receipt_unconfirmed"}
