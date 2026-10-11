"""Single-writer trial proxy: restore before readiness, checkpoint before acknowledgement."""
import argparse, hashlib, io, json, os, sqlite3, subprocess, tarfile, tempfile
import threading, time, urllib.error, urllib.request, uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT_FILES = {'config.yaml', 'state.db', 'bot-state.json', 'standalone-restart-proof.sqlite', 'standalone-restart-proof.json', 'runs_idempotency.db'}
ROOT_DIRS = {'sessions', 'cron', 'memories', 'skills'}
LIMIT = 32 * 1024 * 1024


def allowed(name):
    p = Path(name)
    return not p.is_absolute() and '..' not in p.parts and (name in ROOT_FILES or (p.parts and p.parts[0] in ROOT_DIRS)) and not any(x.endswith(('-wal', '-shm', '.lock')) or x == '.env' for x in p.parts)


def snapshot(home):
    out = io.BytesIO()
    with tempfile.TemporaryDirectory() as temporary, tarfile.open(fileobj=out, mode='w:gz') as archive:
        for source in sorted(home.rglob('*')):
            name = str(source.relative_to(home))
            if not allowed(name) or source.is_symlink() or not source.is_file():
                continue
            if source.suffix in {'.db', '.sqlite'}:
                target = Path(temporary) / uuid.uuid4().hex
                with sqlite3.connect(f'file:{source}?mode=ro', uri=True, timeout=10) as db, sqlite3.connect(target) as copy:
                    db.backup(copy)
                    if copy.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                        raise ValueError('Invalid SQLite checkpoint')
                archive.add(target, arcname=name, recursive=False)
            else:
                archive.add(source, arcname=name, recursive=False)
    data = out.getvalue()
    if len(data) > LIMIT:
        raise ValueError('Trial checkpoint exceeds limit')
    return data


def restore(home, data, digest):
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError('Checkpoint digest mismatch')
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        members = archive.getmembers()
        if sum(x.size for x in members) > LIMIT * 4 or any(not x.isfile() or not allowed(x.name) for x in members):
            raise ValueError('Unsafe checkpoint member')
        for member in members:
            target = home / member.name
            if any(p.is_symlink() for p in [target, *target.parents]):
                raise ValueError('Unsafe restore path')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
            target.chmod(0o600)


class Store:
    def __init__(self, location, key):
        self.location, self.key = location, key

    def request(self, data=None):
        headers = {'Authorization': 'Bearer ' + self.key}
        if data is not None:
            headers['X-Checkpoint-SHA256'] = hashlib.sha256(data).hexdigest()
        req = urllib.request.Request(self.location, headers=headers, data=data, method='GET' if data is None else 'PUT')
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                body = response.read(LIMIT + 1)
                if len(body) > LIMIT:
                    raise ValueError('Oversized checkpoint')
                return body, response.headers.get('X-Checkpoint-SHA256')
        except urllib.error.HTTPError as error:
            if data is None and error.code == 404:
                return None, None
            raise RuntimeError('Checkpoint store HTTP ' + str(error.code)) from None

    def load(self):
        if self.location.startswith('https://'):
            return self.request()
        root = Path(self.location)
        # Never create a missing mount and silently use ephemeral storage.
        if not root.is_dir():
            raise ValueError('Checkpoint volume is missing')
        latest = root / 'latest.json'
        if not latest.exists():
            return None, None
        meta = json.loads(latest.read_text())
        if len(meta['sha256']) != 64 or any(c not in '0123456789abcdef' for c in meta['sha256']):
            raise ValueError('Invalid checkpoint pointer')
        return (root / (meta['sha256'] + '.tar.gz')).read_bytes(), meta['sha256']

    def save(self, data):
        digest = hashlib.sha256(data).hexdigest()
        if self.location.startswith('https://'):
            self.request(data)
        else:
            root = Path(self.location)
            target = root / (digest + '.tar.gz')
            with target.open('wb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError('Checkpoint volume readback mismatch')
            # A complete immutable object precedes atomic pointer replacement.
            temporary = root / ('latest-' + uuid.uuid4().hex + '.json')
            temporary.write_text(json.dumps({'sha256': digest}))
            temporary.replace(root / 'latest.json')
        return digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True)
    args = parser.parse_args()
    key = os.environ['API_SERVER_KEY']
    home = Path('/opt/data')
    store = Store(args.store, key)
    data, digest = store.load()
    if data is not None:
        restore(home, data, digest)
    child_env = {**os.environ, 'API_SERVER_PORT': '7861', 'API_SERVER_HOST': '127.0.0.1'}
    child = subprocess.Popen(['/opt/hermes/.venv/bin/python', '/opt/hermes/gateway-trial-start.py'], env=child_env)
    lock = threading.Lock()
    last_digest = [digest]
    for _ in range(180):
        if child.poll() is not None:
            raise RuntimeError('Gateway exited during restore')
        try:
            with urllib.request.urlopen('http://127.0.0.1:7861/health', timeout=1) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(1)
    else:
        raise RuntimeError('Gateway readiness timed out')

    def checkpoint():
        last_digest[0] = store.save(snapshot(home))
        return last_digest[0]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, data, headers=None):
            self.send_response(status)
            for k, v in (headers or {}).items():
                if k.lower() not in {'connection', 'transfer-encoding', 'content-length', 'server', 'date'}:
                    self.send_header(k, v)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def handle_request(self):
            if self.path == '/health':
                return self.respond(200 if child.poll() is None else 503, b'{}')
            if self.headers.get('Authorization') != 'Bearer ' + key:
                return self.respond(401, b'Unauthorized')
            size = int(self.headers.get('Content-Length', '0'))
            if size > 1024 * 1024:
                return self.respond(413, b'Request too large')
            body = self.rfile.read(size)
            with lock:
                try:
                    if self.path == '/_trial/state':
                        target = home / 'bot-state.json'
                        if self.command == 'PUT':
                            state = json.loads(body)
                            if not isinstance(state, dict):
                                raise ValueError('State must be an object')
                            target.write_text(json.dumps(state))
                            digest = checkpoint()
                        else:
                            digest = last_digest[0]
                        return self.respond(200, json.dumps({'state': json.loads(target.read_text()) if target.exists() else {}, 'checkpoint': digest}).encode())
                    if self.path == '/_trial/checkpoint' and self.command == 'POST':
                        return self.respond(200, json.dumps({'sha256': checkpoint()}).encode())
                    if self.path.startswith('/_trial/'):
                        return self.respond(404, b'Not found')
                    if body and json.loads(body).get('stream'):
                        return self.respond(400, b'Trial persistence requires non-streaming requests')
                    headers = {k: v for k, v in self.headers.items() if k.lower() not in {'host', 'content-length', 'connection'}}
                    request = urllib.request.Request('http://127.0.0.1:7861' + self.path, method=self.command, data=body if body else None, headers=headers)
                    try:
                        response = urllib.request.urlopen(request, timeout=180)
                    except urllib.error.HTTPError as error:
                        response = error
                    with response:
                        result, status, returned_headers = response.read(), response.status, dict(response.headers)
                    if self.command not in {'GET', 'HEAD'}:
                        checkpoint()
                    self.respond(status, result, returned_headers)
                except Exception:
                    # Do not acknowledge writes without durable confirmation or leak keys.
                    self.respond(503, b'Trial persistence is unavailable')

        do_GET = do_POST = do_PUT = do_DELETE = handle_request

    def periodic():
        while child.poll() is None:
            time.sleep(60)
            try:
                with lock:
                    checkpoint()
            except Exception:
                print('Trial periodic checkpoint failed', flush=True)

    checkpoint()
    threading.Thread(target=periodic, daemon=True).start()
    ThreadingHTTPServer(('0.0.0.0', 7860), Handler).serve_forever()


if __name__ == '__main__':
    main()
