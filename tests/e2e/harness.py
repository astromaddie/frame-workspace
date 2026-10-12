"""Plumbing for the end-to-end tests against the fake Frame (tests/fakeframe).

scripts/e2e.sh runs these inside the compose `host` container, where
`ssh frame` reaches the fake Frame and FAKEFRAME_CTL is its control port.
Each test starts from `fakeframe-ctl reset` and drives the real ui/server.py
(started once, on a free port) over HTTP, then checks the fake's state.
Without FRAME_E2E=1 every test here is skipped.
"""
import atexit
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'ui'), str(ROOT / 'tests'), str(ROOT / 'tests' / 'smoke')]

# Only inside the compose `host` container (FRAME_E2E_CONTAINER from compose.yaml, plus
# the container runtime's marker file): these tests drive ui/server.py with the real
# home, ~/.ssh and cache, which is only safe in a throwaway container.
REQUESTED = os.environ.get('FRAME_E2E') == '1'
IN_CONTAINER = os.environ.get('FRAME_E2E_CONTAINER') == '1' and (
    os.path.exists('/.dockerenv') or os.path.exists('/run/.containerenv'))
ENABLED = REQUESTED and IN_CONTAINER
if ENABLED:
    # What tests/sandbox.py does, minus moving HOME: the container's real ~/.ssh is what
    # points `ssh frame` at the fake Frame, and OpenSSH reads ~/.ssh from the password
    # database, not $HOME. (These tests used to get this through test_frame_apk.)
    _data = tempfile.mkdtemp(prefix='frame-control-e2e-')
    atexit.register(shutil.rmtree, _data, ignore_errors=True)
    os.environ['FRAME_CONTROL_DATA_DIR'] = _data
    os.environ['FRAME_CONTROL_TELEMETRY'] = '0'
    os.environ['FRAME_COMPAT_DB_URL'] = 'http://127.0.0.1:9'
CTL = os.environ.get('FAKEFRAME_CTL', 'http://fakeframe:9999').rstrip('/')
FAKE_HOST = os.environ.get('FAKEFRAME_HOST', 'fakeframe')
HOME = '/home/steamos'
SERVER_LOG = os.path.join(tempfile.gettempdir(), 'fakeframe-e2e-server.log')


def require():
    """Skips (the module, at module level; the class, in setUpClass) unless this is
    scripts/e2e.sh's container with the fake Frame up."""
    if ENABLED:
        return
    if REQUESTED:
        raise unittest.SkipTest('FRAME_E2E=1 outside the e2e container: these tests use the real home, ~/.ssh '
                                'and app cache, so they only run in scripts/e2e.sh\'s throwaway container')
    raise unittest.SkipTest('needs the fake Frame: run scripts/e2e.sh (sets FRAME_E2E=1)')


# ---- the fake Frame's control port --------------------------------------------

def _ctl_request(path, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CTL + path, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def ctl(*args):
    out = _ctl_request('/ctl', {'args': list(args)})
    if 'error' in out:
        raise AssertionError(f'fakeframe-ctl {" ".join(args)}: {out["error"]}')
    return out


def state():
    return _ctl_request('/state')


def calls(tool=None):
    return _ctl_request('/calls' + (f'?{tool}' if tool else ''))


def wait_for(check, timeout=30, what='a condition', every=0.3):
    """Poll check() until it returns something truthy; returns that."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = check()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f'timed out after {timeout}s waiting for {what} (last: {last!r})')


def reset():
    ctl('reset')
    wait_for(lambda: _ctl_request('/ping')['ok'], 30, 'the fake Frame to come back after reset')


def ssh(cmd, check=True, timeout=30):
    """Run cmd on the fake Frame through the `frame` alias, as Frame Control does."""
    r = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', 'frame', cmd],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=timeout)
    if check and r.returncode != 0:
        raise AssertionError(f'ssh frame {cmd!r} exited {r.returncode}: {r.stderr.strip()}')
    return r.stdout


def exists(path):
    return ssh(f'test -e {path} && echo yes || echo no').strip() == 'yes'


# ---- the real server ----------------------------------------------------------

class Server:
    proc = None
    port = None

    @classmethod
    def start(cls):
        require()  # never a server on this computer's real data
        if cls.proc and cls.proc.poll() is None:
            return
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            cls.port = s.getsockname()[1]
        env = dict(os.environ, FRAME_CONTROL_LOCAL_LINKS='1')
        log = open(SERVER_LOG, 'ab')
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / 'ui' / 'server.py'), '--port', str(cls.port)],
                                    cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        log.close()
        atexit.register(cls.stop)
        wait_for(lambda: cls._up(), 20, 'ui/server.py to listen')

    @classmethod
    def _up(cls):
        try:
            return api('GET', '/api/host')[0] == 200
        except OSError:
            return False

    @classmethod
    def stop(cls):
        if cls.proc and cls.proc.poll() is None:
            cls.proc.terminate()
            try:
                cls.proc.wait(10)
            except subprocess.TimeoutExpired:
                cls.proc.kill()


def api(method, path, body=None, raw=None, headers=None, timeout=120):
    """One request to the server with the headers its guards want. -> (status, JSON or bytes, headers)."""
    conn = http.client.HTTPConnection('127.0.0.1', Server.port, timeout=timeout)
    try:
        hdrs = {'X-Frame-UI': '1', **(headers or {})}   # Host is 127.0.0.1:<port>, which it accepts
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None and 'Content-Type' not in hdrs:
            hdrs['Content-Type'] = 'application/json'
        conn.request(method, path, body=data, headers=hdrs)
        r = conn.getresponse()
        payload = r.read()
        if r.getheader('Content-Type', '').startswith('application/json'):
            payload = json.loads(payload)
        return r.status, payload, dict(r.getheaders())
    finally:
        conn.close()


def ok(method, path, body=None, **kw):
    status, out, _ = api(method, path, body, **kw)
    if status != 200:
        raise AssertionError(f'{method} {path} -> {status}: {out}')
    return out


def finished(started, timeout=60):
    """Wait for a background job (server.start_job's {"job": id}); returns its final state."""
    return wait_for(lambda: (lambda j: j['done'] and j)(ok('GET', f"/api/job?id={started['job']}")),
                    timeout, f"job {started['job']}")


def upload(path, mode, name=None):
    with open(path, 'rb') as f:
        data = f.read()
    return api('POST', '/api/upload', raw=data, headers={
        'X-Filename': name or os.path.basename(path), 'X-Mode': mode, 'Content-Type': 'application/octet-stream'})


def wait_title_job(token, timeout=180):
    def done():
        job = ok('GET', f'/api/titles/job?token={token}')
        return job if job['done'] else None
    return wait_for(done, timeout, f'title install job {token}', every=0.5)


def install_title(path, **options):
    """Upload (or, for a folder, inspect by path) and install; returns (plan, finished job)."""
    if os.path.isdir(path):
        staged = ok('POST', '/api/titles', {'action': 'inspect', 'path': path})
    else:
        status, staged, _ = upload(path, 'title')
        if status != 200:
            raise AssertionError(f'upload -> {status}: {staged}')
    started = ok('POST', '/api/titles', {'action': 'install', 'token': staged['token'], **options})
    return staged['plan'], wait_title_job(started['job'])


def launches(kind=None):
    return [r for r in state()['launches'] if kind is None or r['kind'] == kind]


class FrameTestCase(unittest.TestCase):
    """Starts the server once and the fake Frame afresh for every test."""

    @classmethod
    def setUpClass(cls):
        require()
        Server.start()

    def setUp(self):
        reset()
        self.tmp = tempfile.mkdtemp(prefix='fakeframe-e2e-')
        self.addCleanup(subprocess.run, ['rm', '-rf', self.tmp])

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)
