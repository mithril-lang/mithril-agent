import hashlib
import importlib.util
import io
import sqlite3
import tarfile
from pathlib import Path
import pytest

spec = importlib.util.spec_from_file_location('trial_persistence', Path(__file__).parents[3] / 'scripts/standalone/gateway-persistence.py')
persistence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(persistence)


def test_wal_database_routing_and_bot_state_restore_to_new_home(tmp_path):
    source = tmp_path / 'source'; source.mkdir()
    destination = tmp_path / 'destination'; destination.mkdir()
    store_path = tmp_path / 'store'; store_path.mkdir()
    db = sqlite3.connect(source / 'state.db')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE messages (body TEXT)')
    db.execute('INSERT INTO messages VALUES (?)', ('persisted user turn',)); db.commit()
    (source / 'sessions').mkdir()
    (source / 'sessions/sessions.json').write_text('{"routing":"same-session"}')
    (source / 'bot-state.json').write_text('{"cursor":7,"enabled":true}')
    (source / '.env').write_text('SECRET=must-not-copy')
    store = persistence.Store(str(store_path), 'fixture')
    digest = store.save(persistence.snapshot(source)); data, stored_digest = store.load()
    assert digest == stored_digest
    persistence.restore(destination, data, stored_digest)
    with sqlite3.connect(destination / 'state.db') as restored:
        assert restored.execute('SELECT body FROM messages').fetchall() == [('persisted user turn',)]
    assert (destination / 'sessions/sessions.json').read_bytes() == (source / 'sessions/sessions.json').read_bytes()
    assert (destination / 'bot-state.json').read_bytes() == (source / 'bot-state.json').read_bytes()
    assert not (destination / '.env').exists()
    db.close()


def test_corrupt_checkpoint_fails_before_writing(tmp_path):
    with pytest.raises(ValueError, match='digest'):
        persistence.restore(tmp_path, b'corrupt', '0' * 64)
    assert list(tmp_path.iterdir()) == []


def test_archive_traversal_and_symlinks_rejected(tmp_path):
    for name, kind in [('../config.yaml', tarfile.REGTYPE), ('sessions/link', tarfile.SYMTYPE)]:
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w:gz') as archive:
            member = tarfile.TarInfo(name); member.type = kind
            archive.addfile(member)
        body = data.getvalue()
        with pytest.raises(ValueError, match='Unsafe'):
            persistence.restore(tmp_path, body, hashlib.sha256(body).hexdigest())
    assert list(tmp_path.iterdir()) == []


def test_missing_volume_fails_closed(tmp_path):
    with pytest.raises(ValueError, match='missing'):
        persistence.Store(str(tmp_path / 'missing'), 'fixture').load()
