import hashlib
import importlib.util
import json
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
    assert store.save(desktop.persistence.snapshot(source, True)) == digest
    assert len(list(store_path.glob('*.tar.gz'))) == 1
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


@pytest.mark.asyncio
async def test_release_ack_waits_for_restorable_fence_and_failed_storage_withholds_proof(tmp_path):
    from hermes_cli import profile_handoff as handoff
    import uuid
    source = tmp_path / 'source'
    trial = source / 'profiles' / 'handoff-trial-checkpoint'
    trial.mkdir(parents=True)
    handoff.enroll(trial)
    operation, target = uuid.uuid4().hex, uuid.uuid4().hex
    handoff.freeze(trial, operation, target)
    capsule = handoff.export(trial, operation, 'ab' * 32)
    proof = handoff.release(trial, operation, capsule['sha256'])
    store_path = tmp_path / 'store'
    store_path.mkdir()
    store = desktop.EncryptedStore(desktop.persistence.Store(str(store_path), 'fixture'), 'cd' * 32)
    barrier = desktop.HandoffCheckpointBarrier()
    request = json.dumps({'id': 1, 'method': 'profiles.handoff', 'params': {'action': 'release'}})
    # The backend can coalesce a notification and reply into one NDJSON frame.
    response = json.dumps({'method': 'event', 'params': {'type': 'idle'}}) + '\n' + json.dumps({'id': 1, 'result': {'proof': proof}})
    async def unavailable():
        raise OSError('Checkpoint storage unavailable')
    await barrier.observe(request, response=False, checkpoint=unavailable)
    delivered = []
    async def forward(checkpoint):
        await barrier.observe(response, response=True, checkpoint=checkpoint)
        delivered.append(response)
    with pytest.raises(OSError):
        await forward(unavailable)
    assert delivered == []
    async def persisted():
        store.save(desktop.persistence.snapshot(source, True))
        data, digest = store.load()
        restored = tmp_path / 'restarted'
        restored.mkdir()
        desktop.persistence.restore(restored, data, digest, True)
        with pytest.raises(handoff.HandoffError), handoff.execution(restored / 'profiles' / trial.name):
            pass
    await forward(persisted)
    assert delivered == [response]


@pytest.mark.asyncio
async def test_initial_gateway_reply_waits_for_identity_restoration_after_restart(tmp_path, monkeypatch):
    import tui_gateway.server as server

    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(server, '_hermes_home', str(home))
    monkeypatch.setattr(server, '_sessions', {})
    rpc = {'jsonrpc': '2.0', 'id': 2, 'method': 'profiles.handoff', 'params': {'action': 'gateway'}}
    reply = server.handle_request(rpc)
    identity = reply['result']['gateway']
    store_path = tmp_path / 'store'
    store_path.mkdir()
    store = desktop.EncryptedStore(desktop.persistence.Store(str(store_path), 'fixture'), 'ab' * 32)
    barrier = desktop.HandoffCheckpointBarrier()
    request = json.dumps(rpc)
    response = json.dumps(reply)
    delivered = []

    async def unavailable():
        raise OSError('Identity checkpoint unavailable')

    async def forward(checkpoint):
        await barrier.observe(response, response=True, checkpoint=checkpoint)
        delivered.append(response)

    await barrier.observe(request, response=False, checkpoint=unavailable)
    with pytest.raises(OSError):
        await forward(unavailable)
    assert delivered == []

    async def persisted():
        store.save(desktop.persistence.snapshot(home, True))
        data, digest = store.load()
        restored = tmp_path / 'restarted'
        restored.mkdir()
        desktop.persistence.restore(restored, data, digest, True)
        monkeypatch.setenv('HERMES_HOME', str(restored))
        monkeypatch.setattr(server, '_hermes_home', str(restored))
        assert server.handle_request(rpc)['result']['gateway'] == identity

    await forward(persisted)
    assert delivered == [response]
