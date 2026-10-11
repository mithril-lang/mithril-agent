"""Spaces control plane: authenticated HTTP/WS and encrypted profile checkpoints."""
import argparse
import asyncio
import hashlib
import hmac
import importlib.util
import json
import os
import subprocess
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

spec = importlib.util.spec_from_file_location('persistence', Path(__file__).with_name('gateway-persistence.py'))
persistence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(persistence)
MAGIC = b'HERMES-ENCRYPTED-V1\0'


class HandoffCheckpointBarrier:
    """Gate transfer replies on durable encrypted storage, including retry replies."""
    def __init__(self):
        self.pending = set()

    async def observe(self, data, *, response, checkpoint):
        for line in data.splitlines():
            try:
                frame = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(frame, dict) or not isinstance(frame.get('id'), (str, int)):
                continue
            params = frame.get('params')
            if not response and frame.get('method') == 'profiles.handoff' and isinstance(params, dict) and params.get('action') not in {'gateway', 'status'}:
                self.pending.add(frame['id'])
            elif response and frame.get('id') in self.pending:
                await checkpoint()
                self.pending.discard(frame['id'])


class EncryptedStore:
    def __init__(self, store, key):
        self.store = store
        self.cipher = AESGCM(bytes.fromhex(key))
        self.plaintext_digest = None
        self.stored_digest = None

    def load(self):
        data, digest = self.store.load()
        if data is None:
            return None, None
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Checkpoint digest mismatch')
        if data.startswith(MAGIC):
            offset = len(MAGIC)
            data = self.cipher.decrypt(data[offset:offset + 12], data[offset + 12:], MAGIC)
            self.plaintext_digest = hashlib.sha256(data).hexdigest()
            self.stored_digest = digest
        # The first deployment migrates the previous non-secret trial archive.
        return data, hashlib.sha256(data).hexdigest()

    def save(self, data):
        digest = hashlib.sha256(data).hexdigest()
        if digest == self.plaintext_digest:
            return self.stored_digest
        nonce = os.urandom(12)
        stored = self.store.save(MAGIC + nonce + self.cipher.encrypt(nonce, data, MAGIC))
        self.plaintext_digest, self.stored_digest = digest, stored
        return stored


async def run(location):
    home = Path('/opt/data')
    api_key = os.environ['API_SERVER_KEY']
    token = os.environ['HERMES_DASHBOARD_SESSION_TOKEN']
    store = EncryptedStore(persistence.Store(location, api_key), os.environ['CHECKPOINT_ENCRYPTION_KEY'])
    data, digest = await asyncio.to_thread(store.load)
    if data is not None:
        persistence.restore(home, data, digest, include_secrets=True)
    # Only platform-injected operator credentials enter the default profile.
    # Named profiles receive credentials through the existing profile management APIs.
    provider_env = home / '.env'
    if not provider_env.exists():
        provider_env.write_text('CUSTOM_PROVIDER_MITHRIL_KEY=' + os.environ['CUSTOM_PROVIDER_MITHRIL_KEY'] + '\n')
        provider_env.chmod(0o600)
    children = []
    env = {**os.environ, 'API_SERVER_HOST': '127.0.0.1', 'API_SERVER_PORT': '7861'}
    for argv in [
        ['/opt/hermes/.venv/bin/python', '/opt/hermes/gateway-trial-start.py'],
        ['/opt/hermes/.venv/bin/hermes', 'serve', '--host', '127.0.0.1', '--port', '7862', '--no-open', '--isolated'],
    ]:
        children.append(await asyncio.create_subprocess_exec(*argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT))

    async def logs(child):
        values = [value for key, value in env.items() if ('KEY' in key or 'TOKEN' in key) and len(value) > 8]
        async for line in child.stdout:
            message = line.decode(errors='replace')
            for value in values:
                message = message.replace(value, '[redacted]')
            print(message, end='', flush=True)

    for child in children:
        asyncio.create_task(logs(child))
    lock = asyncio.Lock()

    async def checkpoint():
        async with lock:
            data = await asyncio.to_thread(persistence.snapshot, home, True)
            return await asyncio.to_thread(store.save, data)

    async with ClientSession(timeout=ClientTimeout(total=180)) as client:
        for port, path in [(7861, '/health'), (7862, '/api/profiles')]:
            for _ in range(180):
                if any(child.returncode is not None for child in children):
                    raise RuntimeError('Hosted Hermes process exited')
                try:
                    async with client.get(f'http://127.0.0.1:{port}{path}', headers={'Authorization': 'Bearer ' + token}) as response:
                        if response.status == 200:
                            break
                except Exception:
                    pass
                await asyncio.sleep(1)
            else:
                raise RuntimeError('Hosted Hermes readiness timed out')
        await checkpoint()

        async def periodic():
            while True:
                await asyncio.sleep(5)
                await checkpoint()

        async def handler(request):
            if request.path == '/' and request.method in {'GET', 'HEAD'}:
                return web.Response(text='Hermes Desktop gateway is running. Connect with the dedicated session token.', content_type='text/plain')
            if request.path == '/health':
                return web.json_response({'ready': all(child.returncode is None for child in children)}, status=200 if all(child.returncode is None for child in children) else 503)
            desktop = request.path.startswith('/api/')
            expected = token if desktop else api_key
            supplied = request.headers.get('X-Hermes-Session-Token') or request.headers.get('Authorization', '').removeprefix('Bearer ')
            if desktop and not supplied:
                supplied = request.query.get('token', '')
            if not hmac.compare_digest(supplied, expected):
                return web.Response(status=401, text='Unauthorized')
            if request.path == '/_trial/checkpoint' and request.method == 'POST':
                return web.json_response({'sha256': await checkpoint()})
            if request.path.startswith('/_trial/'):
                return web.Response(status=404)
            port = 7862 if desktop else 7861
            target = f'http://127.0.0.1:{port}' + request.rel_url.path_qs
            headers = {k: v for k, v in request.headers.items() if k.lower() not in {'host', 'connection', 'content-length', 'upgrade', 'sec-websocket-key', 'sec-websocket-version', 'sec-websocket-extensions'}}
            headers['Authorization'] = 'Bearer ' + expected
            if request.headers.get('Upgrade', '').lower() == 'websocket':
                remote = await client.ws_connect(target, headers=headers, timeout=180)
                local = web.WebSocketResponse()
                await local.prepare(request)
                handoffs = HandoffCheckpointBarrier()

                async def relay(source, destination):
                    async for message in source:
                        if message.type == WSMsgType.TEXT:
                            await handoffs.observe(message.data, response=source is remote, checkpoint=checkpoint)
                            await destination.send_str(message.data)
                        elif message.type == WSMsgType.BINARY:
                            await destination.send_bytes(message.data)
                        elif message.type in {WSMsgType.CLOSE, WSMsgType.ERROR}:
                            break

                tasks = [asyncio.create_task(relay(local, remote)), asyncio.create_task(relay(remote, local))]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    await remote.close()
                    await local.close()
                    await checkpoint()
                return local
            async with client.request(request.method, target, headers=headers, data=await request.read()) as response:
                result = await response.read()
                if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
                    await checkpoint()
                return web.Response(status=response.status, body=result, headers={k: v for k, v in response.headers.items() if k.lower() not in {'connection', 'transfer-encoding', 'content-length', 'content-encoding'}})

        app = web.Application(client_max_size=8 * 1024 * 1024)
        app.router.add_route('*', '/{path:.*}', handler)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, '0.0.0.0', 7860).start()
        periodic_task = asyncio.create_task(periodic())
        wait_tasks = [asyncio.create_task(child.wait()) for child in children]
        try:
            done, _ = await asyncio.wait([periodic_task, *wait_tasks], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            raise RuntimeError('Hosted Hermes supervisor stopped')
        finally:
            await runner.cleanup()
            periodic_task.cancel()
            for child in children:
                if child.returncode is None:
                    child.terminate()
            await asyncio.gather(*(child.wait() for child in children))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True)
    asyncio.run(run(parser.parse_args().store))
