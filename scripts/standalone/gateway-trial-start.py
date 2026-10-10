"""Start the isolated API-only trial; the hosting platform owns supervision."""
import json
import os
from pathlib import Path

assert os.getuid() == 1000, 'The cloud trial must run as UID 1000'
home = Path('/opt/data')
config = home / 'config.yaml'
if not config.exists():
    # JSON is also valid YAML. No local profiles, bot tokens or schedules are copied.
    config.write_text(json.dumps({
        'providers': {
            'mithril-inference': {
                'name': 'Mithril Inference',
                'base_url': 'https://api.mithril.fund/v1',
                'key_env': 'CUSTOM_PROVIDER_MITHRIL_KEY',
                'transport': 'chat_completions',
            },
        },
        'model': {
            'api_mode': 'chat_completions',
            'base_url': 'https://api.mithril.fund/v1',
            'default': 'qwen/qwen3.8-27b',
            'key_env': 'CUSTOM_PROVIDER_MITHRIL_KEY',
            'provider': 'mithril-inference',
        },
    }, indent=2) + '\n')
    config.chmod(0o600)
os.execv('/opt/hermes/.venv/bin/hermes', [
    'hermes', 'gateway', 'run', '--external-supervisor',
])
