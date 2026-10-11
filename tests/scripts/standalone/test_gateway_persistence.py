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
    tmp_path = tmp_path / 'restore'; tmp_path.mkdir()
    with pytest.raises(ValueError, match='digest'):
        persistence.restore(tmp_path, b'corrupt', '0' * 64)
    assert list(tmp_path.iterdir()) == []


def test_archive_traversal_and_symlinks_rejected(tmp_path):
    tmp_path = tmp_path / 'restore'; tmp_path.mkdir()
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


def test_shared_gateway_restores_native_profile_databases_without_merging(tmp_path):
    """A shared checkpoint preserves each native store even when session IDs collide."""
    from contextlib import ExitStack
    from hermes_state import SessionDB

    source = tmp_path / 'source'
    destination = tmp_path / 'restored'
    source.mkdir()
    destination.mkdir()
    homes = [Path('.'), Path('profiles/alpha'), Path('profiles/bravo')]
    session_id = 'same-durable-session'
    expected = {}
    with ExitStack() as handles:
        for index, relative in enumerate(homes):
            home = source / relative
            home.mkdir(parents=True, exist_ok=True)
            db = SessionDB(home / 'state.db')
            handles.callback(db.close)
            db.create_session(session_id, 'cli', model=f'profile-model-{index}')
            db.append_message(session_id, 'user', f'profile{index}isolatedhistory')
            db.update_token_counts(session_id, input_tokens=index + 1,
                                   output_tokens=index + 2, api_call_count=1)
            (home / 'memories').mkdir()
            (home / 'memories/MEMORY.md').write_text(f'profile {index} memory')
            (home / 'SOUL.md').write_text(f'profile {index} identity')
            (home / 'config.yaml').write_text(f'model: profile-model-{index}\n')
            (home / '.env').write_text(f'FIXTURE_SECRET=profile-{index}')
            expected[relative] = (
                db.get_messages(session_id), db.get_session(session_id),
                (home / 'memories/MEMORY.md').read_bytes(),
                (home / 'SOUL.md').read_bytes(), (home / 'config.yaml').read_bytes(),
            )

        # Take the checkpoint while original native WAL databases remain open.
        data = persistence.snapshot(source)
        persistence.restore(destination, data, hashlib.sha256(data).hexdigest())

        for index, relative in enumerate(homes):
            home = destination / relative
            with ExitStack() as restored_handles:
                db = SessionDB(home / 'state.db', read_only=True)
                restored_handles.callback(db.close)
                messages, session, memory, soul, config = expected[relative]
                assert db.get_messages(session_id) == messages
                restored = db.get_session(session_id)
                for key in ['model', 'input_tokens', 'output_tokens', 'api_call_count']:
                    assert restored[key] == session[key]
                assert db.search_messages(f'profile{index}isolatedhistory')
                for other_index in range(len(homes)):
                    if other_index != index:
                        assert not db.search_messages(f'profile{other_index}isolatedhistory')
                assert (home / 'memories/MEMORY.md').read_bytes() == memory
                assert (home / 'SOUL.md').read_bytes() == soul
                assert (home / 'config.yaml').read_bytes() == config
                assert not (home / '.env').exists()


def test_authenticated_http_checkpoint_transport(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):
            assert self.headers['Authorization'] == 'Bearer fixture'
            assert self.headers['User-Agent'] == 'Hermes-Gateway-Trial/1.0'
            body = self.rfile.read(int(self.headers['Content-Length']))
            assert self.headers['X-Checkpoint-SHA256'] == hashlib.sha256(body).hexdigest()
            received.append(body)
            self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        persistence.Store('http://127.0.0.1:' + str(server.server_port), 'fixture').request(b'checkpoint')
        assert received == [b'checkpoint']
    finally:
        server.shutdown(); server.server_close()
