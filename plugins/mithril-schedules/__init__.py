"""Original occurrence execution policy and CLI adapter; preserves cron timing."""
import json
import sys
from agent.secret_scope import get_secret
from .client import LIMIT, call_custody
from .execution import execute_bound_occurrence


def register(ctx):
    def execution(**kwargs):
        return execute_bound_occurrence(ctx, **kwargs)
    ctx.register_middleware("cron_execution", execution)
    def setup(parser):
        parser.add_argument("--stdin", action="store_true", help="Read a bounded account/command envelope from stdin")

    def cli(args):
        try:
            if not args.stdin:
                raise ValueError()
            raw = sys.stdin.buffer.read(LIMIT + 1)
            envelope = json.loads(raw) if len(raw) <= LIMIT else None
            if not isinstance(envelope, dict) or set(envelope) not in ({"owner", "command"}, {"owner", "binding"}, {"owner", "prepare"}):
                raise ValueError()
        except (ValueError, TypeError):
            print(json.dumps({"ok": False, "error": "invalid_schedule_command"}))
            return
        try:
            token = get_secret("MITHRIL_API_KEY", "")
        except RuntimeError:
            print(json.dumps({"ok": False, "error": "schedule_authorization_required"}))
            return
        def call(command):
            return call_custody(ctx.get_config("api_origin") or "https://api.mithril.fund",
                                token, envelope["owner"], command)
        if 'prepare' in envelope:
            from .bindings import prepare_original_source
            try:
                result = {'ok': True, 'receipt': prepare_original_source(envelope['owner'], envelope['prepare'], call)}
            except Exception:
                result = {'ok': False, 'error': 'schedule_binding_unconfirmed'}
        elif 'binding' in envelope:
            from .bindings import bind_original_source
            try:
                result = {'ok': True, 'receipt': bind_original_source(envelope['owner'], envelope['binding'], call)}
            except Exception:
                result = {'ok': False, 'error': 'schedule_binding_unconfirmed'}
        else:
            result = call(envelope['command'])
        print(json.dumps(result, ensure_ascii=False))

    ctx.register_cli_command("mithril-schedule-custody", "Inspect or reconcile original schedule custody", setup, cli)
