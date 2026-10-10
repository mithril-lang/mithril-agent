"""Exercise authenticated gateway routing without inference or external network."""
import json
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

key = sys.argv[1]
url = 'http://127.0.0.1:7860/v1/capabilities'
for attempt in range(60):
    try:
        with urlopen(Request(url, headers={'Authorization': f'Bearer {key}'}), timeout=2) as r:
            assert r.status == 200
            assert isinstance(json.load(r), dict)
        break
    except (URLError, HTTPError):
        if attempt == 59:
            raise
        time.sleep(1)
try:
    urlopen(url, timeout=2)
except HTTPError as e:
    assert e.code == 401, f'Unexpected unauthorized status: {e.code}'
else:
    raise AssertionError('Gateway accepted an unauthenticated capabilities request')
print(json.dumps({'authenticated': 200, 'unauthenticated': 401}))
