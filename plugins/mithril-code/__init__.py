"""Mithril's Code harness through Hermes's supported plugin and CLI surfaces."""
import json
import sys
from agent.secret_scope import get_secret
from .client import call_runner, runner_url


def register(ctx):
    def configured():
        try:
            runner_url(ctx.get_config("runner_url"))
            return len(get_secret("CODE_RUNNER_TOKEN", "") or "") >= 32
        except (ValueError, TypeError):
            return False

    def invoke(action, goal=""):
        return call_runner(ctx.get_config("runner_url"), get_secret("CODE_RUNNER_TOKEN", ""), action, goal)

    def handler(args, **kwargs):
        return json.dumps(invoke(args.get("action", "status"), args.get("goal", "")), ensure_ascii=False)

    ctx.register_tool(
        name="mithril_code", toolset="mithril_code", handler=handler, check_fn=configured,
        requires_env=["CODE_RUNNER_TOKEN"], description="Verified Mithril coding", emoji="🧩",
        schema={"name": "mithril_code", "description": (
            "Run the configured Mithril/Jev System One coding harness for a To-do completion toggle "
            "and unfinished count, with fixed CLJK acceptance checks. Returns typed logic, source and "
            "measurements. UI and arbitrary repository execution are outside this proof. status checks "
            "readiness; run performs paid model inference. Never automatically retry an unknown outcome. "
            "Does not save, overwrite, commit or publish files."),
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
