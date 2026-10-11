import hashlib
import importlib.util
import sqlite3
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('desktop_trial', Path(__file__).parents[3] / 'scripts/standalone/gateway-desktop.py')
desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)


def test_encrypted_profile_checkpoint_restores_independent_histories_and_credentials(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    store_path = tmp_path / 'store'
    store_path.mkdir()
    for name in ['first', 'second']:
        home = source / 'profiles' / name
        home.mkdir(parents=True)
        (home / 'config.yaml').write_text('model: ' + name)
        (home / '.env').write_text('TOKEN=' + name + '-private-credential')
        with sqlite3.connect(home / 'state.db') as db:
            db.execute('CREATE TABLE messages (body TEXT)')
            db.execute('INSERT INTO messages VALUES (?)', (name + ' conversation',))
    store = desktop.EncryptedStore(desktop.persistence.Store(str(store_path), 'fixture'), 'ab' * 32)
    digest = store.save(desktop.persistence.snapshot(source, True))
    encrypted = (store_path / (digest + '.tar.gz')).read_bytes()
    assert b'private-credential' not in encrypted
    data, restored_digest = store.load()
    restored = tmp_path / 'restored'
    restored.mkdir()
    desktop.persistence.restore(restored, data, restored_digest, True)
    for name in ['first', 'second']:
        home = restored / 'profiles' / name
        assert (home / '.env').read_text() == 'TOKEN=' + name + '-private-credential'
        with sqlite3.connect(home / 'state.db') as db:
            assert db.execute('SELECT body FROM messages').fetchall() == [(name + ' conversation',)]
    assert not desktop.persistence.allowed('profiles/first/.env')
    assert not desktop.persistence.allowed('profiles/../.env', True)


def test_wrong_key_and_ciphertext_tampering_refuse_restore(tmp_path):
    store = desktop.persistence.Store(str(tmp_path), 'fixture')
    desktop.EncryptedStore(store, 'ab' * 32).save(b'checkpoint')
    with pytest.raises(Exception):
        desktop.EncryptedStore(store, 'cd' * 32).load()
    data, _ = store.load()
    # A valid outer hash cannot conceal tampering with authenticated ciphertext.
    store.save(data[:-1] + bytes([data[-1] ^ 1]))
    with pytest.raises(Exception):
        desktop.EncryptedStore(store, 'ab' * 32).load()
