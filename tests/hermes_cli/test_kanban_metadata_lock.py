"""Real original metadata writers serialize without losing fields or native scope."""
import json
import sqlite3
import subprocess
import sys
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_metadata_lock import board_metadata_lock


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / 'boards'
    root.mkdir()
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(root))
    for name in ('HERMES_KANBAN_BOARD', 'HERMES_KANBAN_DB'):
        monkeypatch.delenv(name, raising=False)
    return root


def test_original_writer_retains_fields_and_scope(home, tmp_path, monkeypatch):
    kb.write_board_metadata('default', name='Original', default_workdir='/private/work', project_id='project')
    first = kb.read_board_metadata('default')
    other = tmp_path / 'other'
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(other))
    kb.write_board_metadata('default', name='Other')
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))
    result = kb.write_board_metadata('default', description='Description', archived=True)
    assert result['name'] == 'Original'
    assert result['default_workdir'] == '/private/work'
    assert result['project_id'] == 'project'
    assert result['created_at'] == first['created_at']
    assert result['description'] == 'Description' and result['archived'] is True
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(other))
    assert kb.read_board_metadata('default')['name'] == 'Other'
    assert kb.read_board_metadata('default')['description'] == ''
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(home))

    kb.write_board_metadata('default', name='Original')
    path = kb.board_metadata_path('default')
    original = path.read_bytes()
    conn = sqlite3.connect(kb.kanban_db_path('default'))
    conn.execute('CREATE TABLE mithril_board_pending(operation_id TEXT)')
    conn.execute("INSERT INTO mithril_board_pending VALUES('prepared')")
    conn.commit()
    with pytest.raises(RuntimeError, match='requires recovery'):
        kb.write_board_metadata('default', name='Must not overwrite')
    assert path.read_bytes() == original
    conn.execute('DELETE FROM mithril_board_pending')
    conn.commit()
    conn.close()
    path.write_text('{broken', encoding='utf-8')
    with pytest.raises(json.JSONDecodeError):
        kb.write_board_metadata('default', name='Must not reset corrupt data')
    assert path.read_text() == '{broken'




def test_two_original_writer_processes_keep_disjoint_changes(home, tmp_path):
    kb.write_board_metadata('default', name='Initial', default_workdir='/private/work')
    ready, release, finished, attempting = (tmp_path / value for value in ('ready', 'release', 'finished', 'attempting'))
    first_script = '''
from hermes_cli import kanban_db as kb
from pathlib import Path
import time,sys
original=kb.read_board_metadata
def paused(board):
 value=original(board)
 Path(sys.argv[1]).touch()
 deadline=time.monotonic()+30
 while not Path(sys.argv[2]).exists():
  if time.monotonic()>deadline:raise TimeoutError('test release missing')
  time.sleep(.01)
 return value
kb.read_board_metadata=paused
kb.write_board_metadata('default',name='First writer')
'''
    first = subprocess.Popen([sys.executable, '-c', first_script, str(ready), str(release)])
    second = None
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            assert first.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.01)
        second = subprocess.Popen([sys.executable, '-c', "from hermes_cli import kanban_db as kb; from pathlib import Path; import sys; Path(sys.argv[2]).touch(); kb.write_board_metadata('default',description='Second writer'); Path(sys.argv[1]).touch()", str(finished), str(attempting)])
        deadline = time.monotonic() + 30
        while not attempting.exists():
            assert second.poll() is None and time.monotonic() < deadline
            time.sleep(.01)
        with pytest.raises(subprocess.TimeoutExpired):
            second.wait(timeout=2)
        assert not finished.exists()
        release.touch()
        assert first.wait(timeout=8) == 0
        assert second.wait(timeout=8) == 0
        result = kb.read_board_metadata('default')
        assert result['name'] == 'First writer' and result['description'] == 'Second writer'
        assert result['default_workdir'] == '/private/work'
    finally:
        release.touch()
        for child in (first, second):
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=5)


    path = kb.board_metadata_path('default')
    with board_metadata_lock(path, kb.kanban_db_path('default')):
        info = path.with_name('.board-metadata.lock').stat()
        before = path.read_bytes()
        child = subprocess.run([sys.executable, '-c', "from pathlib import Path; from hermes_cli.kanban_metadata_lock import board_metadata_lock; import sys;\nwith board_metadata_lock(Path(sys.argv[1]),Path(sys.argv[2]),timeout=.05):raise RuntimeError('wrote unlocked')", str(path), str(kb.kanban_db_path('default'))], capture_output=True, text=True, timeout=8)
        assert child.returncode != 0 and 'TimeoutError' in child.stderr
        assert path.read_bytes() == before
    kb.write_board_metadata('default', name='After release')
    assert path.with_name('.board-metadata.lock').stat().st_ino == info.st_ino

