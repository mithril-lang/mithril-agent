"""Mithril's Code harness through Hermes's supported plugin and CLI surfaces."""
import json
import sys
from agent.secret_scope import get_secret
from .client import call_mithril, runner_url


def register(ctx):
    def configured():
        try:
            runner_url(ctx.get_config("code_service_url") or "https://code.mithril.fund")
            return len(get_secret("MITHRIL_API_KEY", "") or "") > 0
        except (ValueError, TypeError):
            return False

    def invoke(action, goal=""):
        return call_mithril(ctx.get_config("code_service_url") or "https://code.mithril.fund", get_secret("MITHRIL_API_KEY", ""), action, goal)

    def handler(args, **kwargs):
        return json.dumps(invoke(args.get("action", "status"), args.get("goal", "")), ensure_ascii=False)

    ctx.register_tool(
        name="mithril_code", toolset="mithril_code", handler=handler, check_fn=configured,
        requires_env=["MITHRIL_API_KEY"], description="Verified Mithril coding", emoji="🧩",
        schema={"name": "mithril_code", "description": (
            "System One coding in the Mithril language using api.mithril.fund. Generates an inert "
            "application.mith for a bounded static dashboard, report or directory; the actual Mithril App "
            "compiler executes OWL, SPARQL, SHACL and conformance checks. Returns source, artifact, HTML "
            "and receipts. Arbitrary runtime logic and repository execution are outside this contract. "
            "status checks readiness; run uses Mithril inference and Code allowances. Never retry an "
            "unknown outcome. Does not save, overwrite, commit or publish files."),
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["status", "run"]},
                "goal": {"type": "string", "maxLength": 2000}}, "required": ["action"], "additionalProperties": False}})

    def setup(parser):
        parser.add_argument("action", choices=["status", "run"])
        parser.add_argument("--stdin", action="store_true", help="Read the project brief as bounded JSON from stdin")

    def cli(args):
        goal = ""
        if args.action == "run":
            try:
                if not args.stdin:
                    raise ValueError()
                raw = sys.stdin.buffer.read(8193)
                if len(raw) > 8192:
                    raise ValueError()
                goal = json.loads(raw).get("goal", "")
            except (ValueError, AttributeError):
                print(json.dumps({"ok": False, "error": "invalid_goal"}))
                return
        print(json.dumps(invoke(args.action, goal), ensure_ascii=False))

    ctx.register_cli_command("mithril-code", "Run or inspect the verified Mithril Code harness", setup, cli)
