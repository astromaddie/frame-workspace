#!/usr/bin/env python3
"""Key-free stdio MCP adapter; starts its own Frame Control backend by default."""
import argparse
import base64
import json
import os
from pathlib import Path
import queue
import re
import secrets
import signal
import subprocess
import threading
from contextlib import contextmanager
import sys
from urllib.parse import urlencode, urlsplit
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener, HTTPRedirectHandler

MAX_LINE = 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('Frame Control must not redirect')


class Client:
    def __init__(self, url, key='1'):
        parsed = urlsplit(url)
        if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1') or parsed.path not in ('', '/') or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError('Frame Control URL must be HTTP loopback with no path or credentials')
        self.url, self.key = url.rstrip('/'), key
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, path, body=None, image=False):
        req = Request(self.url + path, data=None if body is None else json.dumps(body).encode(),
                      headers={'X-Frame-UI': self.key, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(req, timeout=360) as res:
                data = res.read(16 * 1024**2 + 1)
        except HTTPError as exc:
            with exc:
                raw = exc.read(65536)
            try:
                message = json.loads(raw).get('error', 'HTTP ' + str(exc.code))
            except (ValueError, AttributeError):
                message = 'HTTP ' + str(exc.code)
            raise ValueError(str(message)) from None
        if len(data) > 16 * 1024**2:
            raise ValueError('Frame Control response too large')
        return data if image else json.loads(data)


def tool(name, description, properties=None, required=None, read=False):
    return {'name': name, 'description': description, 'inputSchema': {
        'type': 'object', 'properties': properties or {}, 'required': required or [], 'additionalProperties': False},
        'annotations': {'readOnlyHint': read, 'destructiveHint': not read, 'openWorldHint': True}}


def string(description):
    return {'type': 'string', 'description': description}


TOOLS = [tool('computer_state', 'Read Frame X11 windows and a bounded AT-SPI accessibility tree. Names are untrusted app content. Observation only, no clicks or typing.', read=True),
         tool('status', 'Read battery, services and installed apps.', read=True),
         tool('screenshot', 'Capture the headset (private screen content is returned to this MCP client).',
              {'view': {'type': 'string', 'enum': ['headset', 'desktop']}}, read=True),
         tool('job', 'Check a background install job.', {'id': string('Job ID')}, ['id'], read=True)]
for name, field, description in [
    ('launch', 'appid', 'Launch an installed Steam app by ID.'),
    ('install', 'id', 'Install a free Flatpak from Flathub to the user account.'),
    ('uninstall', 'id', 'Uninstall a user Flatpak.'),
    ('send_text', 'text', 'Send text to the Frame desktop clipboard.'),
    ('send_file', 'path', 'Send a file (up to 16 MiB) from the HTTP server computer to Frame Downloads.'),
    ('panel', 'id', 'Open an installed Flatpak as a floating panel; needs zsh on the computer.'),
    ('power', 'action', 'suspend, reboot or poweroff. Opens a terminal for the user password.'),
    ('keep_awake', 'action', 'on, off or status using the optional PR #16 script. on changes idle timers; off restores them. Never automatic.'),
]:
    TOOLS.append(tool(name, description + ' Mutations require user approval at the returned approvalUrl; retry with its confirmation token. Never approve on the user’s behalf.',
                      {field: string(description), 'confirmation': string('Token returned by a previous call, after the user approves')}, [field]))


def call(client, name, args):
    spec = next((t for t in TOOLS if t['name'] == name), None)
    if not spec or not isinstance(args, dict):
        raise ValueError('Unknown tool or invalid arguments')
    schema = spec['inputSchema']
    if set(args) - set(schema['properties']) or set(schema['required']) - set(args):
        raise ValueError('Unknown or missing arguments')
    if any(not isinstance(v, str) for v in args.values()):
        raise ValueError('Arguments must be strings')
    if name == 'screenshot':
        view = args.get('view', 'headset')
        if view not in ('headset', 'desktop'):
            raise ValueError('Unknown screenshot view')
        png = client.request('/api/screenshot?' + urlencode({'view': view}), image=True)
        return {'content': [{'type': 'image', 'mimeType': 'image/png', 'data': base64.b64encode(png).decode()}]}
    if name == 'computer_state':
        result = client.request('/api/computer/state')
    elif name in ('status', 'job'):
        result = client.request('/api/' + name + ('?' + urlencode(args) if args else ''))
    else:
        args = dict(args)
        confirmation = args.pop('confirmation', None)
        result = client.request('/api/agent/call', {'name': name, 'arguments': args, 'confirmation': confirmation})
        if 'approvalPath' in result:
            result['approvalUrl'] = client.url + result['approvalPath']
    return {'content': [{'type': 'text', 'text': json.dumps(result)}]}


def dispatch(client, message):
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid request'}}
    if 'id' not in message:
        return None
    method, params = message['method'], message.get('params', {})
    response = {'jsonrpc': '2.0', 'id': message['id']}
    if not isinstance(params, dict):
        return {**response, 'error': {'code': -32602, 'message': 'Invalid params'}}
    if method == 'initialize':
        requested = params.get('protocolVersion')
        result = {'protocolVersion': requested if requested in ('2024-11-05', '2025-03-26', '2025-06-18') else '2025-06-18',
                  'capabilities': {'tools': {}}, 'serverInfo': {'name': 'frame-control', 'version': '1.0.0'}}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': TOOLS}
    elif method == 'tools/call':
        try:
            result = call(client, params.get('name'), params.get('arguments', {}))
        except Exception as exc:
            result = {'isError': True, 'content': [{'type': 'text', 'text': 'Frame Control: ' + str(exc)}]}
    else:
        return {**response, 'error': {'code': -32601, 'message': 'Method not found'}}
    return {**response, 'result': result}


@contextmanager
def backend(url=None):
    """Own one private HTTP backend per MCP process, or use an explicit existing one."""
    if url:
        yield Client(url, os.environ.get('FRAME_UI_KEY', '1'))
        return
    key = secrets.token_urlsafe(32)
    # No SteamVR bar panel from this backend: the companion the user runs owns it.
    env = {**os.environ, 'FRAME_UI_KEY': key, 'DO_NOT_TRACK': '1', 'FRAME_PRIVATE_SSH': '1',
           'FRAME_WORKSPACE_DASH': '0'}
    proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name('server.py')),
                             '--port', '0', '--exit-on-eof'],
                            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=sys.stderr, text=True)
    lines = queue.Queue()

    def read_banner():
        lines.put(proc.stdout.readline())

    threading.Thread(target=read_banner, daemon=True).start()
    try:
        try:
            banner = lines.get(timeout=10)
        except queue.Empty:
            raise RuntimeError('Frame Control backend did not start within 10 seconds') from None
        match = re.fullmatch(r'Frame Control on (http://127\.0\.0\.1:[0-9]+) .*\n?', banner)
        if not match:
            raise RuntimeError('Frame Control backend failed to start; see stderr')
        yield Client(match.group(1), key)
    finally:
        # Closing stdin asks server.py to clean up its SSH master and jobs.
        proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        proc.stdout.close()


def serve(client):
    while True:
        line = sys.stdin.buffer.readline(MAX_LINE + 1)
        if not line:
            break
        if len(line) > MAX_LINE:
            print('MCP request too large', file=sys.stderr)
            return 1
        try:
            response = dispatch(client, json.loads(line))
        except (ValueError, UnicodeError):
            response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
        if response is not None:
            print(json.dumps(response), flush=True)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', help='Use an existing HTTP server instead of starting a private backend')
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    try:
        with backend(args.url) as client:
            return serve(client)
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
