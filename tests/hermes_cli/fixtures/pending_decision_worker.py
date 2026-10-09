"""Real shared command worker; boundary hooks coordinate competing processes."""
import json
from pathlib import Path
import sys
import time

from hermes_cli import write_approval_commands as commands
from tools.memory_tool import load_on_disk_store

config = json.loads(Path(sys.argv[1]).read_text())
root = Path(config['coordination'])
name = config['name']
original = commands._apply_one


def apply(*args):
    (root / (name + '.entered')).write_text('entered')
    if config.get('pause') == 'before':
        wait()
    result = original(*args)
    if config.get('pause') == 'after':
        (root / (name + '.committed')).write_text('committed')
        wait()
    return result


def wait():
    deadline = time.monotonic() + 25
    while not (root / 'release').exists():
        if time.monotonic() > deadline:
            raise TimeoutError('qualification release missing')
        time.sleep(0.01)


commands._apply_one = apply
store = load_on_disk_store()
frozen = store._system_prompt_snapshot.copy()
(root / (name + '.ready')).write_text('ready')
output = commands.handle_pending_subcommand(config.get('subsystem', 'memory'), config['args'], memory_store=store)
assert store._system_prompt_snapshot == frozen
(root / (name + '.result')).write_text(json.dumps({'output': output, 'frozenUnchanged': True}))
