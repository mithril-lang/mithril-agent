"""Run real image checks offline as the unprivileged Hermes user."""
from pathlib import Path
import json
import os
import sqlite3
import sys

root = Path('/opt/hermes')
stamp = json.loads((root / 'install-stamp.json').read_text())
assert stamp['commit'] == sys.argv[1], 'Image source identity mismatch'
assert os.getuid() != 0, 'Runtime probe must run as Hermes'
from hermes_cli.sqlite_runtime import is_sqlite_wal_reset_vulnerable
assert not is_sqlite_wal_reset_vulnerable(sqlite3.sqlite_version_info), 'Vulnerable SQLite'
with sqlite3.connect(':memory:') as db:
    db.execute("CREATE VIRTUAL TABLE docs USING fts5(content, tokenize='trigram')")
    db.execute("INSERT INTO docs VALUES ('hermes')")
    assert db.execute("SELECT count(*) FROM docs WHERE docs MATCH 'erm'").fetchone()[0] == 1
home = Path('/opt/data')
marker = home / 'standalone-restart-proof.json'
if len(sys.argv) > 2:
    assert json.loads(marker.read_text())['commit'] == sys.argv[1], 'Restart lost durable state'
else:
    marker.write_text(json.dumps({'commit': sys.argv[1]}))
print(json.dumps({'commit': stamp['commit'], 'uid': os.getuid(), 'sqlite': sqlite3.sqlite_version, 'restart': len(sys.argv) > 2}))
