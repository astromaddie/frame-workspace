"""My Meta library (Experimental): install Quest games you own from Meta, with your own token.

Opt-in and off until you connect. You paste your own Meta access token (docs/meta-library.md
says how to get it); Frame Control checks it with a harmless "who am I" query, then keeps it
in a private file (frame_host.data_dir('meta-account.json'), owner-only). It lists the Quest
(Android) apps your account owns, downloads one you choose (its APK, plus its OBB game data
when it has some) from Meta's download server into a per-user cache, and installs it on the
Frame through the same checks and install as Import from my Quest (frame_quest.py). Install
and test then runs the game and reads its log to say whether it runs, and records the
result through the existing compatibility reports (frame_catalog.add_report), which share
it only if you chose to share compatibility results.

This uses Meta's undocumented internal API (graph.oculus.com, securecdn.oculus.com), as the
open-source tools QAVS, DownQuest and BSManager do; it may break whenever Meta changes it and
may be against Meta's terms. Frame Control's code is its own (MIT); only the protocol facts
(endpoints, query ids, field names) come from those tools' public source (docs/meta-library.md).

Rules this module keeps:
- Only games the account owns: a download must be an app in the account's own library.
- No licence-check bypass of any kind: games are installed as Meta ships them.
- The token goes only to TOKEN_HOSTS over HTTPS, never into logs, errors, API answers,
  the page or reports (redact()); a redirect anywhere else drops it.
- Game files stay in the local install cache and are deleted after each install.

Python stdlib only. CLI: python3 ui/frame_meta.py
  connect --token-stdin | status | disconnect | list [--refresh] | download APP_ID
  install APP_ID [--test] [--allow-downgrade] | test PACKAGE [--wait SECONDS] [--no-report]
"""
import concurrent.futures
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import frame_android as android
import frame_host
import frame_quest as quest

GRAPH_URL = 'https://graph.oculus.com/graphql'
DOWNLOAD_URL = 'https://securecdn.oculus.com/binaries/download/'
# The only hosts the token is ever sent to. Downloads may redirect elsewhere (a CDN): the
# token is dropped from the request before it goes there.
TOKEN_HOSTS = frozenset({'graph.oculus.com', 'securecdn.oculus.com'})
# Stored queries ("doc_id") as the public tools use them (docs/meta-library.md).
DOC_VIEWER = '4149322231793299'        # data.viewer.user {id, alias}
DOC_ENTITLEMENTS = '4850747515044496'  # data.viewer.user.active_entitlements.nodes[].item
DOC_BINARY = '4734929166632773'        # one binary: package, version, size, OBB and asset files
QUEST_PLATFORMS = frozenset({'ANDROID_6DOF'})  # Quest apps; Rift (PC) and Go (3DOF) are left out
UA = 'FrameControl/1 (My Meta library)'
ACCOUNT_FILE = 'meta-account.json'

MAX_RESPONSE = 32 * 1024**2  # one API answer
MAX_APK = 2 * 1024**3        # as for every other APK download (apk_sources/_web.py)
MAX_DATA_FILE = 8 * 1024**3  # one OBB
MAX_TOTAL = 16 * 1024**3     # everything one install downloads
MAX_APPS = 1000              # owned apps listed
MAX_DETAILS = 200            # binary queries per library refresh (the rest when installed)
DETAIL_WORKERS = 4
MAX_REDIRECTS = 5
LIBRARY_TTL = 600
CACHE_TTL = 6 * 3600         # a download a crash left behind is swept after this
TIMEOUT = 30
TEST_WAIT = 30               # seconds a test watches the game after its instance is up
TEST_STEP = 10               # ...looking at it this often
START_TIMEOUT = 90

WARNING = ("Experimental. My Meta library uses Meta's undocumented internal API with your own Meta "
           "access token. It may stop working whenever Meta changes things, and using it may be against "
           "Meta's terms; your account is your responsibility. It only lists and downloads games your "
           "account owns, never changes them, and never removes a licence check.")
NOT_CONNECTED = 'Connect your Meta account first (My Meta library, Experimental).'
REJECTED = ("Meta didn't accept this token. It may have expired or been signed out: get a new one "
            "(docs/meta-library.md) and connect again.")
RAN_PLATFORM_SDK = ("It uses Meta's licence check; on the Frame that check can't reach Meta, so the "
                    "game may quit, hang, or run without online features. Frame Control won't remove the check.")


class MetaError(android.FrameError):
    """A readable failure. Its text never carries the token (built with redact())."""


class TokenRejected(MetaError):
    pass


# ---- Keeping the token out of everything ---------------------------------------

REDACTED = '<redacted>'
# Any token-shaped value, whoever's it is: access_token=..., "access_token": "...",
# Authorization schemes, and bare OC.../FRL... tokens (with a digit somewhere in them).
_GENERIC = [
    re.compile(r'(?i)(access[_%5f-]*token["\']?\s*(?:=|:|%3d)\s*["\']?)[^&\s"\'<>,;]+'),
    re.compile(r'(?i)\b(bearer|oauth)\s+[^\s"\'<>,;]+'),
    # Upper-case prefix and no dots, so package names (com.oculus...) are left alone; the
    # stored token itself is matched in any case by _variants.
    re.compile(r'\b(?:OC|FRL)(?=[A-Za-z0-9%|:_\-]*\d)[A-Za-z0-9%|:_\-]{16,}'),
]


def _variants(token):
    """A pattern for token as written, in any letter case, and percent-encoded (once or
    twice, in either case) character by character."""
    parts = []
    for ch in token:
        code = ord(ch)
        alts = [re.escape(ch)]
        if code < 256:
            alts += [f'%{code:02x}', f'%25{code:02x}']
        if ch == ' ':
            alts.append(r'\+')
        parts.append('(?:' + '|'.join(alts) + ')')
    return re.compile(''.join(parts), re.I)


_pattern_cache = {}


def redact(text, limit=None, token=None):
    """text without the stored token (and any other token-shaped value), then cut to limit
    characters. Redacting before cutting means a cut can't leave part of a token behind."""
    text = '' if text is None else str(text)
    for t in {t for t in (token, _stored_token_quiet()) if t}:
        pattern = _pattern_cache.get(t)
        if pattern is None:
            if len(_pattern_cache) > 4:
                _pattern_cache.clear()  # only ever the current tokens
            pattern = _pattern_cache[t] = _variants(t)
        text = pattern.sub(REDACTED, text)
    for pattern in _GENERIC:
        text = pattern.sub(lambda m: (m.group(1) + REDACTED) if m.groups() else REDACTED, text)
    if limit is not None and len(text) > limit:
        text = text[:max(0, limit - 1)] + '…'
    return text


def check_token(token):
    """The token, stripped, if it looks like a Meta user access token; else MetaError. The
    rules match what the public tools check before sending one anywhere."""
    token = (token or '').strip()
    if token.lower().startswith('bearer '):
        token = token[7:].strip()
    if not token:
        raise MetaError('Paste your Meta access token first.')
    if len(token) > 4096 or not re.fullmatch(r'[\x21-\x7e]+', token):
        raise MetaError("That doesn't look like a Meta access token (it has spaces or unusual characters).")
    if not (token.startswith('OC') or token.startswith('FRL')):
        raise MetaError("That doesn't look like a Meta access token: it should start with OC or FRL.")
    if '%' in token:
        raise MetaError('That token is URL-encoded (it has % in it); copy it from the cookie or request '
                        'payload itself, not from an address.')
    if '|' in token or ':' in token:
        raise MetaError("That's an app token, not your account's token. See docs/meta-library.md for which one to copy.")
    if re.fullmatch(r'OC\d{15}', token) or len(token) < 20:
        raise MetaError("That token is incomplete. Copy the whole value.")
    return token


# ---- The account file -------------------------------------------------------------

_lock = threading.Lock()  # the account file, the generation and the library cache
_state = {'generation': 0}
_library = {'generation': None, 'at': 0, 'apps': None}


def account_path():
    return Path(frame_host.data_dir(ACCOUNT_FILE))


def _read_account():
    try:
        with open(account_path(), encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get('token'), str) else None


def _stored_token_quiet():
    account = _read_account()
    return account['token'] if account else None


def _write_account(data):
    """Write the account file owner-only (0600), atomically: a temporary file made by mkstemp
    (already 0600) in the same folder, then renamed over the old one."""
    path = account_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.meta-account-', suffix='.tmp')
    try:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        os.replace(tmp, str(path))
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _session():
    """(token, generation) for the connected account, or MetaError."""
    with _lock:
        account = _read_account()
        if not account:
            raise MetaError(NOT_CONNECTED)
        return account['token'], _state['generation']


def _current(generation):
    with _lock:
        return _state['generation'] == generation


def status():
    """For the page: whether an account is connected and its display name. Never the token."""
    account = _read_account()
    user = (account or {}).get('user') or {}
    return {'connected': bool(account), 'experimental': True, 'warning': WARNING,
            'user': {'name': str(user.get('name') or '')[:80]} if account else None,
            'connectedAt': (account or {}).get('connected')}


def connect(token):
    """Check the token with Meta ("who am I"), then keep it. A disconnect (or another
    connect) that happens while it is being checked wins: this one is then not saved."""
    token = check_token(token)
    with _lock:
        generation = _state['generation']
    user = whoami(token)
    with _lock:
        if _state['generation'] != generation:
            raise MetaError('The Meta account was disconnected (or another token connected) while this one '
                            'was being checked, so it was not saved.')
        _write_account({'token': token, 'user': user, 'connected': time.strftime('%Y-%m-%dT%H:%M:%S')})
        _state['generation'] += 1
        _library.update(generation=None, at=0, apps=None)
    return status()


def disconnect():
    """Forget the token: delete the file and the library in memory. Downloads under way stop."""
    with _lock:
        _state['generation'] += 1
        _library.update(generation=None, at=0, apps=None)
        try:
            os.remove(str(account_path()))
        except FileNotFoundError:
            pass
    _pattern_cache.clear()
    return status()


# ---- HTTPS to Meta ----------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are followed by hand (_follow), so the token never goes along by accident."""
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _send(url, data=None, headers=None, timeout=TIMEOUT):
    """One HTTPS request, redirects not followed: (status, headers, readable body). The
    tests replace this with a fake Meta."""
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method='POST' if data is not None else 'GET')
    try:
        r = _opener.open(req, timeout=timeout)
        return r.status, r.headers, r
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e


def _addresses(host):
    return {info[4][0] for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)}


def check_url(url):
    """url if it is HTTPS on port 443, without credentials, to a host whose addresses are all
    public; else MetaError (never echoing the URL, which can hold the token)."""
    try:
        p = urllib.parse.urlsplit(url)
        port = p.port
    except (ValueError, TypeError):
        raise MetaError("Meta's server gave an invalid address") from None
    host = (p.hostname or '').lower()
    if p.scheme != 'https' or p.username or p.password or port not in (None, 443) or not host:
        raise MetaError("Meta's server sent the download somewhere that isn't plain HTTPS; not following it")
    try:
        addresses = _addresses(host)
    except OSError:
        raise MetaError(f"Couldn't look up {host}; check the internet connection") from None
    for a in addresses:
        try:
            ip = ipaddress.ip_address(a.split('%', 1)[0])
        except ValueError:
            raise MetaError(f'{host} has an address Frame Control can\'t check; not connecting') from None
        if not ip.is_global:
            raise MetaError(f'{host} points at a private or local address; not connecting')
    return url


def strip_token(url):
    """url without any access_token query parameter (any letter case)."""
    p = urllib.parse.urlsplit(url)
    if not p.query:
        return url
    kept = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
            if k.lower() != 'access_token']
    return urllib.parse.urlunsplit(p._replace(query=urllib.parse.urlencode(kept)))


def next_hop(current, location):
    """Where a redirect from current to location goes: absolute, checked, and without the
    token unless it stays on a TOKEN_HOSTS host."""
    url = urllib.parse.urljoin(current, location or '')
    url = check_url(url)
    if (urllib.parse.urlsplit(url).hostname or '').lower() not in TOKEN_HOSTS:
        url = strip_token(url)
    return url


def _follow(url, headers):
    """GET url, following up to MAX_REDIRECTS redirects through next_hop. Returns
    (status, headers, body) of the final answer."""
    check_url(url)
    for _ in range(MAX_REDIRECTS + 1):
        status, rheaders, body = _send(url, headers=headers)
        if status in (301, 302, 303, 307, 308):
            location = rheaders.get('Location') if rheaders else None
            body.close()
            if not location:
                raise MetaError("Meta's server redirected the download without saying where")
            url = next_hop(url, location)
            continue
        return status, rheaders, body
    raise MetaError("Meta's server redirected the download too many times")


def graphql(token, doc_id, variables=None):
    """One query to Meta's GraphQL API: the token travels in the POST body, never the URL.
    Returns the answer's "data"; raises TokenRejected or MetaError with readable text."""
    body = urllib.parse.urlencode({'access_token': token, 'doc_id': doc_id,
                                   'variables': json.dumps(variables or {})}).encode()
    try:
        check_url(GRAPH_URL)
        status, _, r = _send(GRAPH_URL, data=body, headers={
            'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json', 'User-Agent': UA})
        with r:
            raw = r.read(MAX_RESPONSE + 1)
    except MetaError:
        raise
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise MetaError("Couldn't reach Meta's API: " + redact(getattr(e, 'reason', e), 200, token)) from None
    if len(raw) > MAX_RESPONSE:
        raise MetaError("Meta's API answer was too large")
    if 300 <= status < 400:
        raise MetaError(f"Meta's API redirected the request (HTTP {status}); Frame Control doesn't follow that")
    try:
        answer = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, ValueError):
        answer = None
    error = answer.get('error') if isinstance(answer, dict) else None
    if status in (401, 403) or (isinstance(error, dict) and (error.get('type') == 'OAuthException'
                                                            or error.get('code') in (102, 190))):
        raise TokenRejected(REJECTED)
    if status != 200 or not isinstance(answer, dict):
        said = error.get('message') if isinstance(error, dict) else ''
        raise MetaError(f"Meta's API said HTTP {status}" + (f': {redact(said, 200, token)}' if said else ''))
    if isinstance(error, dict):
        raise MetaError("Meta's API refused the request: " + redact(error.get('message'), 200, token))
    errors = answer.get('errors')
    if errors and not answer.get('data'):
        first = errors[0] if isinstance(errors, list) and errors else {}
        raise MetaError("Meta's API refused the request: "
                        + redact(first.get('message') if isinstance(first, dict) else first, 200, token))
    data = answer.get('data')
    if not isinstance(data, dict):
        raise MetaError("Meta's API answered without data; it may have changed")
    return data


def whoami(token):
    """The account a token belongs to ({'id', 'name'}), from a harmless read-only query."""
    data = graphql(token, DOC_VIEWER)
    user = ((data.get('viewer') or {}).get('user') if isinstance(data.get('viewer'), dict) else None)
    if not isinstance(user, dict) or not user.get('id'):
        raise TokenRejected(REJECTED)
    return {'id': str(user['id'])[:40], 'name': str(user.get('alias') or user.get('display_name') or '')[:80]}


# ---- The library --------------------------------------------------------------------

def _int(value):
    try:
        n = int(str(value).strip())
        return n if n >= 0 else None
    except (TypeError, ValueError):
        return None


def _nodes(connection):
    """The items of a GraphQL list, whether it is {nodes: [...]}, {edges: [{node}]} or a list."""
    if isinstance(connection, list):
        items = connection
    elif isinstance(connection, dict):
        items = connection.get('nodes') or [e.get('node') for e in connection.get('edges') or [] if isinstance(e, dict)]
    else:
        items = []
    return [i for i in items if isinstance(i, dict)]


def _file(node, required=None):
    """An OBB or asset file: {'id', 'name', 'size', 'required', 'sha256'} or None."""
    if not isinstance(node, dict) or not str(node.get('id') or '').isdigit():
        return None
    digest = str(node.get('sha256') or node.get('sha256_hash') or '').lower()
    return {'id': str(node['id']), 'name': str(node.get('file_name') or ''),
            'size': _int(node.get('size')), 'sha256': digest if re.fullmatch(r'[0-9a-f]{64}', digest) else None,
            'required': bool(node.get('is_required')) if required is None else required}


def parse_binary(binary):
    """The download facts of one Android binary: package, version, size, OBB and asset files."""
    if not isinstance(binary, dict):
        return {}
    out = {}
    if str(binary.get('id') or '').isdigit():
        out['binary_id'] = str(binary['id'])
    if isinstance(binary.get('package_name'), str) and android.PKG_RE.match(binary['package_name']):
        out['package'] = binary['package_name']
    if binary.get('version') not in (None, ''):
        out['version'] = str(binary['version'])[:80]
    if _int(binary.get('version_code')) is not None:
        out['version_code'] = _int(binary['version_code'])
    if _int(binary.get('size')) is not None:
        out['size'] = _int(binary['size'])
    digest = str(binary.get('sha256') or '').lower()
    if re.fullmatch(r'[0-9a-f]{64}', digest):
        out['sha256'] = digest
    if 'obb_binary' in binary:
        out['obb'] = _file(binary.get('obb_binary'), required=True)
    if 'asset_files' in binary:
        out['assets'] = [f for f in (_file(n) for n in _nodes(binary.get('asset_files'))) if f]
    return out


def parse_entitlements(data, now=None):
    """The Quest apps an entitlements answer says the account owns, newest facts first.
    Expired or inactive entitlements and other platforms (Rift, Go) are left out."""
    now = now or time.time()
    user = ((data.get('viewer') or {}).get('user') or {}) if isinstance(data, dict) else {}
    apps, seen = [], set()
    for node in _nodes(user.get('active_entitlements') if isinstance(user, dict) else None):
        state = str(node.get('active_state') or 'ACTIVE').upper()
        expires = _int(node.get('expiration_time'))
        item = node.get('item')
        if state != 'ACTIVE' or (expires and expires < now) or not isinstance(item, dict):
            continue
        app_id = str(item.get('id') or '')
        platform = str(item.get('platform') or '').upper()
        if not app_id.isdigit() or app_id in seen or (platform and platform not in QUEST_PLATFORMS):
            continue
        seen.add(app_id)
        app = {'id': app_id, 'title': str(item.get('display_name') or item.get('name') or app_id)[:120],
               'package': None, 'version': None, 'version_code': None, 'size': None, 'binary_id': None}
        app.update(parse_binary(item.get('latest_supported_binary')))
        # Detailed: the answer said what game data the binary has (its OBB and asset files).
        app['detailed'] = 'obb' in app and 'assets' in app
        app.setdefault('obb', None)
        app.setdefault('assets', [])
        apps.append(app)
        if len(apps) >= MAX_APPS:
            break
    return sorted(apps, key=lambda a: a['title'].lower())


def binary_details(token, binary_id):
    """The full facts of one binary (package, version code, size, OBB, asset files)."""
    data = graphql(token, DOC_BINARY, {'binaryID': str(binary_id)})
    node = data.get('node')
    if not isinstance(node, dict):
        node = next((v for v in data.values() if isinstance(v, dict) and str(v.get('id') or '') == str(binary_id)), {})
    return parse_binary(node)


def _complete(app):
    return bool(app.get('package') and app.get('version_code') is not None and app.get('detailed'))


def _fill(token, app):
    """Add one binary query's facts to app (in place)."""
    try:
        facts = binary_details(token, app['binary_id'])
    except TokenRejected:
        raise
    except MetaError as e:
        app['problem'] = str(e)
        return app
    app.update({k: v for k, v in facts.items() if v is not None})
    app['detailed'] = True
    app.pop('problem', None)
    return app


def library(refresh=False):
    """The Quest apps the connected account owns, each with its download facts. Kept in
    memory only (LIBRARY_TTL), and dropped by a disconnect. Apps the list answer doesn't
    fully describe get one binary query each, at most MAX_DETAILS per refresh; the rest
    are filled in when installed."""
    token, generation = _session()
    with _lock:
        if (not refresh and _library['generation'] == generation and _library['apps'] is not None
                and time.time() - _library['at'] < LIBRARY_TTL):
            return [dict(a) for a in _library['apps']]
    apps = parse_entitlements(graphql(token, DOC_ENTITLEMENTS))
    missing = [a for a in apps if a.get('binary_id') and not _complete(a)][:MAX_DETAILS]
    if missing:
        with concurrent.futures.ThreadPoolExecutor(DETAIL_WORKERS) as pool:
            for future in [pool.submit(_fill, token, a) for a in missing]:
                future.result()
    with _lock:
        if _state['generation'] == generation:
            _library.update(generation=generation, at=time.time(), apps=[dict(a) for a in apps])
    return apps


def owned(app_id, refresh=False):
    """An app the connected account owns, by Meta app id; MetaError if it isn't in the library."""
    app_id = str(app_id or '')
    if not re.fullmatch(r'[0-9]{1,25}', app_id):
        raise MetaError('invalid Meta app id')
    for attempt in (refresh, True):
        app = next((a for a in library(refresh=attempt) if a['id'] == app_id), None)
        if app or attempt:
            break
    if not app:
        raise MetaError("That app isn't in your Meta library, so Frame Control won't download it.")
    if app.get('binary_id') and not _complete(app):
        token, _ = _session()
        _fill(token, app)
    if not app.get('binary_id') or not app.get('package'):
        raise MetaError(f"Meta didn't say which file to download for {app['title']}" +
                        (f" ({app['problem']})" if app.get('problem') else '') + '.')
    return app


def with_verdicts(apps):
    """apps with the community verdict for each package from the compatibility database,
    when there is one (apk-catalog/reports.py)."""
    try:
        import frame_catalog
        by = frame_catalog.reports.by_package(frame_catalog.compat_db.load())
    except Exception:  # the library still shows without verdicts
        return apps
    for app in apps:
        found = frame_catalog.reports.verdict(by.get(app.get('package'))) if app.get('package') else None
        app['verdict'] = {'verdict': found[0], 'lines': found[1]} if found else None
    return apps


# ---- Downloading -------------------------------------------------------------------

_install_lock = threading.Lock()


def cache_root():
    return Path(frame_host.cache_dir('meta-library'))


def sweep_cache(now=None):
    """Remove downloads a crash left behind (older than CACHE_TTL)."""
    root, now = cache_root(), now or time.time()
    if root.is_dir():
        for d in root.iterdir():
            try:
                if now - d.stat().st_mtime > CACHE_TTL:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass


def fetch(file_id, dest, limit, token, generation, expected=None, sha256=None, progress=None):
    """Download one file by id from Meta's download server into dest. Bounded by limit and
    by the size Meta declared; checked against its SHA-256 when Meta gives one. Stops if the
    account is disconnected meanwhile. Nothing is left behind on failure."""
    if expected is not None and expected > limit:
        raise MetaError(f'{dest.name} is {expected / 1024**3:.1f} GB, more than Frame Control downloads '
                        f'({limit / 1024**3:.0f} GB)')
    url = DOWNLOAD_URL + '?' + urllib.parse.urlencode({'id': file_id, 'access_token': token})
    part = dest.with_name(dest.name + '.part')
    try:
        try:
            status, headers, body = _follow(url, {'User-Agent': UA})
        except MetaError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise MetaError(f"Couldn't download {dest.name} from Meta: " + redact(getattr(e, 'reason', e), 200, token)) from None
        with body:
            if status in (401, 403):
                raise TokenRejected(f"Meta refused to download {dest.name} (HTTP {status}). " + REJECTED)
            if status != 200:
                raise MetaError(f"Meta's download server said HTTP {status} for {dest.name}")
            length = _int((headers or {}).get('Content-Length'))
            if length is not None and (length > limit or (expected is not None and length != expected)):
                raise MetaError(f'{dest.name} is not the size Meta said it is; download refused')
            h, size = hashlib.sha256(), 0
            with open(part, 'wb') as f:
                while True:
                    try:
                        chunk = body.read(1 << 20)
                    except (OSError, ValueError) as e:
                        raise MetaError(f'Downloading {dest.name} failed: ' + redact(e, 200, token)) from None
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit or (expected is not None and size > expected):
                        raise MetaError(f'{dest.name} is larger than Meta said; download stopped')
                    if not _current(generation):
                        raise MetaError('The Meta account was disconnected; the download was stopped and removed.')
                    h.update(chunk)
                    f.write(chunk)
                    if progress:
                        progress(size)
        if expected is not None and size != expected:
            raise MetaError(f'{dest.name} is incomplete ({size} of {expected} bytes); download removed')
        if sha256 and h.hexdigest() != sha256:
            raise MetaError(f"{dest.name} doesn't match the checksum Meta gave; download removed")
        os.replace(part, dest)
        return {'path': str(dest), 'size': size, 'sha256': h.hexdigest(), 'verified': bool(sha256)}
    finally:
        if part.exists():
            part.unlink()


def obb_name(app, file):
    """The name an OBB gets on the Frame: Meta's file name when it is a standard
    main|patch.<version>.<package>.obb name, else main.<version code>.<package>.obb for the
    app's own OBB binary. None for anything else (not copied)."""
    pattern = quest.OBB_NAME.format(re.escape(app['package']))
    if re.fullmatch(pattern, file.get('name') or ''):
        return file['name']
    if file is app.get('obb') and app.get('version_code') is not None:
        return f"main.{app['version_code']}.{app['package']}.obb"
    return None


def data_plan(app):
    """(files to download as OBBs [(file, name)], names left out with why)."""
    files = ([app['obb']] if app.get('obb') else []) + [f for f in app.get('assets') or [] if f.get('required')]
    plan, skipped, names = [], [], set()
    for f in files:
        name = obb_name(app, f)
        if not name or name in names:
            skipped.append(f.get('name') or f['id'])
            continue
        names.add(name)
        plan.append((f, name))
    return plan, skipped


def _check_space(work, need, title):
    free = shutil.disk_usage(str(work)).free
    if need > free:
        raise MetaError(f'Installing {title} needs about {need / 1024**3:.1f} GB free on this computer '
                        f'({free / 1024**3:.1f} GB available)')


def _bar(report, stage, start, span, total):
    def progress(done):
        if total:
            report(stage, start + int(span * min(done, total) / total))
    return progress


def download_apk(app, work, token, generation, report):
    report(f"Downloading {app['title']} from Meta", 5)
    got = fetch(app['binary_id'], work / 'base.apk', MAX_APK, token, generation, expected=app.get('size'),
                sha256=app.get('sha256'), progress=_bar(report, f"Downloading {app['title']} from Meta", 5, 40,
                                                        app.get('size')))
    try:
        with zipfile.ZipFile(got['path']) as z:
            if 'AndroidManifest.xml' not in z.namelist():
                raise MetaError(f"What Meta sent for {app['title']} is not an APK")
    except zipfile.BadZipFile:
        raise MetaError(f"What Meta sent for {app['title']} is not an APK") from None
    return Path(got['path'])


def download_data(app, work, token, generation, report):
    """The app's OBB files, in work/obb: ([paths], [names not copied])."""
    plan, skipped = data_plan(app)
    folder = work / 'obb'
    folder.mkdir(exist_ok=True)
    paths, total = [], sum(f.get('size') or 0 for f, _ in plan)
    if total > MAX_TOTAL:
        raise MetaError(f"{app['title']}'s game data is {total / 1024**3:.1f} GB, more than Frame Control "
                        f"downloads ({MAX_TOTAL // 1024**3} GB)")
    for i, (f, name) in enumerate(plan):
        stage = f"Downloading {app['title']}'s game data ({i + 1} of {len(plan)})"
        report(stage, 50)
        got = fetch(f['id'], folder / name, MAX_DATA_FILE, token, generation, expected=f.get('size'),
                    sha256=f.get('sha256'), progress=_bar(report, stage, 50, 15, f.get('size')))
        paths.append(got['path'])
    return paths, skipped


def download(app_id, progress=None):
    """CLI: download an owned app's APK and OBB files into the local install cache (removed
    after CACHE_TTL) and say where they are."""
    report = progress or (lambda stage, percent=None: None)
    token, generation = _session()
    app = owned(app_id)
    sweep_cache()
    work = cache_root() / f"{app['package']}-{secrets.token_hex(4)}"
    work.mkdir(parents=True)
    try:
        _check_space(work, (app.get('size') or 0) + sum(f.get('size') or 0 for f, _ in data_plan(app)[0]),
                     app['title'])
        apk = download_apk(app, work, token, generation, report)
        obb, skipped = download_data(app, work, token, generation, report)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return {'app': _public(app), 'apk': str(apk), 'obb': obb, 'skipped': skipped, 'folder': str(work)}


# ---- Installing -------------------------------------------------------------------

def _public(app):
    """An app as the page and the CLI see it (download ids are fine: they need the token)."""
    keys = ('id', 'title', 'package', 'version', 'version_code', 'size', 'problem', 'verdict')
    out = {k: app.get(k) for k in keys if k in app}
    plan, skipped = data_plan(app) if app.get('package') else ([], [])
    out['data_size'] = sum(f.get('size') or 0 for f, _ in plan) or None
    out['data_files'] = len(plan)
    out['skipped_files'] = skipped
    return out


def _wait_running(pkg, timeout=START_TIMEOUT):
    for _ in range(timeout // 3):
        if quest._running(pkg):
            return True
        quest._sleep(3)
    return quest._running(pkg)


def install(app_id, test=False, progress=None, allow_downgrade=False, wait=TEST_WAIT):
    """Download an owned app from Meta and install it on the Frame (as Import from my Quest
    does), then, with test, run it and record whether it runs.

    Game data (OBB) can only go into a running Lepton instance. Without test that is a second
    Install while the person has the game open (Frame Control then copies it in and closes the
    game). With test, the person asked Frame Control to run the game, so it starts the game
    itself to add the data, then starts it again for the test."""
    report = progress or (lambda stage, percent=None: None)
    if not _install_lock.acquire(blocking=False):
        raise MetaError('Another My Meta library install is running; wait for it to finish')
    try:
        token, generation = _session()
        report('Reading your Meta library', 1)
        app = owned(app_id)
        pkg = app['package']
        sweep_cache()
        work = cache_root() / f'{pkg}-{secrets.token_hex(4)}'
        work.mkdir(parents=True)
        try:
            result = _install(app, work, token, generation, report, test, allow_downgrade)
        finally:
            shutil.rmtree(work, ignore_errors=True)  # never kept: the files are on the Frame now
    finally:
        _install_lock.release()
    if test:
        report('Testing it on the Frame', 90)
        outcome = run_test(pkg, wait=wait, app=app)
        result['test'] = outcome
        result['message'] += ' ' + outcome['message']
    report('Done', 100)
    return result


def _install(app, work, token, generation, report, test, allow_downgrade):
    pkg = app['package']
    plan, skipped = data_plan(app)
    data_bytes = sum(f.get('size') or 0 for f, _ in plan)
    apk_bytes = app.get('size') or 0
    # The APK, a merged or patched copy of it, and the OBB files.
    _check_space(work, 2 * apk_bytes + data_bytes, app['title'])
    apk = download_apk(app, work, token, generation, report)
    report('Checking it can run on the Frame', 46)
    checked = quest.inspect(pkg, str(apk))
    if app.get('version_code') is not None and checked['version_code'] not in (None, app['version_code']):
        raise MetaError(f"Meta said {app['title']} is version code {app['version_code']}, but the APK it sent "
                        f"is {checked['version_code']}; not installing it")
    warnings = list(checked['warnings'])
    if skipped:
        warnings.append('Not copied (Frame Control only copies standard OBB files; other files are fetched '
                        "through Meta's Platform SDK, which the Frame doesn't have): " + ', '.join(skipped))
    label = checked['label']
    if quest._running(pkg):
        # The second Install: the game is open, so add its game data to exactly this install.
        quest._check_target(pkg, android.read_meta(pkg), checked)
        if not plan:
            raise MetaError(f'{label} is already installed and open on the Frame, and has no game data to add. '
                            'Close it first to install it again.')
        obb, _ = download_data(app, work, token, generation, report)
        result = quest.add_game_data(pkg, checked, obb, None, report, warnings, source="Meta's download server")
        result.update(package=pkg, source='meta-store', version_code=checked['version_code'])
        return result
    report('Installing on the Frame', 60)
    with android._install_lock:
        if quest._running(pkg):
            raise MetaError(f'{label} was started on the Frame during the install. Close it, then install again.')
        quest._check_downgrade(android.read_meta(pkg), checked, allow_downgrade)
        meta = android.install(str(apk), source='Meta Horizon Store')
    try:
        apk.unlink()  # the Frame has it now
    except OSError:
        pass
    message = f"Installed {meta['label']} from your Meta library. It's in the Steam library."
    needs_data = bool(plan)
    added = None
    if plan and test:
        obb, _ = download_data(app, work, token, generation, report)
        report(f'Starting {label} to add its game data', 68)
        android.launch(pkg)
        if not _wait_running(pkg):
            raise MetaError(f"{label} was installed, but its Lepton instance didn't start within "
                            f'{START_TIMEOUT} s, so its game data could not be added. Start it from the Steam '
                            'library, then use Install again to add the data.')
        added = quest.add_game_data(pkg, checked, obb, None, report, (), source="Meta's download server")
        message += ' ' + added['message']
        needs_data = False
    elif plan:
        message += (f" To add its OBB game data, start {meta['label']} from the Steam library, leave it on its "
                    'first screen, then choose Install again here: Frame Control downloads the data, copies it '
                    'in and closes the game.')
    note = android.layer_note(meta)
    return {'message': ' '.join([message, *warnings, *([note] if note else [])]), 'app': meta,
            'package': pkg, 'warnings': warnings, 'needs_game_data': needs_data,
            'obb': (added or {}).get('obb', []), 'source': 'meta-store', 'version_code': checked['version_code'],
            'platform_sdk': checked['platform_sdk']}


# ---- Testing whether it runs ----------------------------------------------------------
#
# Signatures for common reasons a Quest game fails on the Frame (from the compatibility
# research in docs/meta-library.md), written for Frame Control. Each is (key, pattern,
# kind, plain-English cause). kind: 'blocker' explains a quit or a stall, 'issue' a game
# that runs with something missing.

SIGNATURES = [
    ('licence', re.compile(r'(?i)ovr_Entitlement|IsUserEntitledToApplication|entitlement (?:check )?(?:failed|error)|'
                           r'not entitled|user is not entitled|failed to (?:verify|check) entitlement'),
     'blocker', "Meta's licence check failed (it needs Meta's services, which the Frame doesn't have). "
                "Frame Control won't remove the check."),
    ('platform', re.compile(r'(?i)ovr_Platform(?:Initialize)?\w*\s*(?:failed|error)|libovrplatform\w*\.so.*(?:not found|failed)|'
                            r'Failed to initialize (?:the )?(?:Oculus|Meta) Platform|Oculus Platform.*(?:failed|not initialized)'),
     'blocker', "Meta's Platform SDK couldn't start (no Meta services on the Frame); online features, "
                "and the licence check if the game enforces it, won't work."),
    ('device', re.compile(r'(?i)Failed to launch SystemActivities|com\.oculus\.systemactivities|IsRunningOnSantaCruz|'
                          r'(?:device|headset) (?:is )?not supported|unsupported (?:device|headset)|not an? (?:Oculus|Meta) (?:Quest|device)'),
     'blocker', 'The game checks that it is on a Meta Quest and stops on the Frame.'),
    ('vrapi', re.compile(r'(?i)vrapi_\w+ was called before|libvrapi\.so.*(?:not found|failed)|vrapi_Initialize\w*\s*(?:failed|error)'),
     'blocker', "The game uses Meta's legacy VrApi, which the Frame doesn't have."),
    ('extension', re.compile(r'XR_ERROR_EXTENSION_NOT_PRESENT'),
     'blocker', 'The game asked for an OpenXR feature the Frame lacks{detail}.'),
    ('xr-version', re.compile(r'XR_ERROR_API_VERSION_UNSUPPORTED'),
     'blocker', 'The game asked for an OpenXR version the Frame\'s runtime refuses.'),
    ('runtime', re.compile(r'XR_ERROR_RUNTIME_(?:UNAVAILABLE|FAILURE)|XR_ERROR_INITIALIZATION_FAILED|'
                           r'(?i:xrCreateInstance\w*\s*(?:failed|returned -))'),
     'blocker', "The game couldn't start an OpenXR session with the Frame's runtime."),
    ('gpu', re.compile(r'(?i)VK_ERROR_DEVICE_LOST|DEVICE[ _]LOST|vkCreate\w+ failed|VK_ERROR_\w+|'
                       r'libgallium_dri\.so|zink.*(?:error|fail)|GL_INVALID_\w+|GL_OUT_OF_MEMORY|'
                       r'shader (?:compil|link)\w* (?:failed|error)|failed to (?:compile|link) shader'),
     'issue', 'Graphics errors (the Frame uses the Mesa Vulkan and GLES drivers, which are stricter than '
              "the Quest's); it may draw wrongly or crash."),
    ('clipboard', re.compile(r'ClipboardManager'),
     'blocker', "The game needs Android's clipboard service, which Lepton doesn't have."),
    ('android', re.compile(r'NoSuchMethodError|NoClassDefFoundError'),
     'issue', 'The game called an Android feature Lepton (Android 11) lacks.'),
    ('library', re.compile(r'UnsatisfiedLinkError|dlopen failed'),
     'blocker', 'The game needs a library the Frame doesn\'t have{detail}.'),
]
CRASH = re.compile(r'FATAL EXCEPTION|Fatal signal \d+|\bSIG(?:SEGV|ABRT|BUS|ILL)\b|>>> [\w.]+ <<<|Abort message:')
HANG = re.compile(r'ANR in [\w.]+|Application Not Responding|Input dispatching timed out')
# Signs the game got far enough to show its VR scene.
SESSION = re.compile(r'XR_SESSION_STATE_(?:SYNCHRONIZED|VISIBLE|FOCUSED)|xrEndFrame|xrBeginSession|'
                     r'session state (?:changed )?(?:to )?(?:SYNCHRONIZED|VISIBLE|FOCUSED)', re.I)
XR_STARTED = re.compile(r'FrameXrCompat.*created instance|xrCreateInstance', re.I)
RESULTS = {'runs': 'runs', 'runs-with-issues': 'runs', 'quits': 'crashes', 'crashes': 'crashes',
           'hangs': 'crashes', 'instance-failed': 'instance_failed'}
OUTCOME_WORDS = {'runs': 'runs', 'runs-with-issues': 'runs, with issues', 'quits': 'quit',
                 'crashes': 'crashed', 'hangs': 'seems stuck', 'instance-failed': "didn't start"}


def _detail(key, line):
    if key == 'extension':
        m = re.search(r'\bXR_[A-Z0-9]+_[A-Za-z0-9_]+', line.replace('XR_ERROR_EXTENSION_NOT_PRESENT', ''))
        return f' ({m.group(0)})' if m else ''
    if key == 'library':
        m = re.search(r'"?([\w.+-]+\.so)"?', line)
        return f' ({m.group(1)})' if m else ''
    return ''


def classify(log, alive, instance_up=True, seconds=None, started=True):
    """An outcome (runs, runs-with-issues, quits, crashes, hangs, instance-failed), a short
    plain-English cause when one is recognised, and the signals behind it, from the game's
    Android log, whether its Lepton instance started at all, and whether its process and
    instance were still up at the end."""
    log = log or ''
    lines = log.splitlines()
    found, causes = [], []
    for key, pattern, kind, cause in SIGNATURES:
        line = next((l for l in lines if pattern.search(l)), None)
        if line is not None:
            found.append({'signal': key, 'kind': kind})
            causes.append((kind, cause.format(detail=_detail(key, line))))
    crash = next((l for l in lines if CRASH.search(l)), None)
    hang = bool(HANG.search(log))
    session = bool(SESSION.search(log))
    signals = {'alive': bool(alive), 'instance_up': bool(instance_up), 'crash': bool(crash), 'not_responding': hang,
               'xr_session': session, 'xr_started': session or bool(XR_STARTED.search(log)),
               'matched': [f['signal'] for f in found]}
    blockers = [c for kind, c in causes if kind == 'blocker']
    issues = [c for kind, c in causes if kind == 'issue']
    if not started:
        outcome, cause = 'instance-failed', f"Its Lepton instance didn't start within {START_TIMEOUT} s."
    elif not alive:
        outcome = 'crashes' if crash else 'quits'
        cause = (blockers or issues or [''])[0] or (
            _crash_reason(lines) if crash else 'It closed by itself without a recognisable error.' if log.strip()
            else 'It closed by itself, and its instance stopped before its log could be read.')
    elif hang:
        outcome, cause = 'hangs', (blockers or ['It stopped responding (Android reported it not responding).'])[0]
    elif blockers and not session:
        outcome, cause = 'hangs', blockers[0] + ' It stayed open but never showed its VR scene.'
    elif blockers or issues:
        outcome, cause = 'runs-with-issues', (blockers + issues)[0]
    else:
        outcome = 'runs'
        cause = ('' if session else "Its log didn't show the VR session starting, so rendering wasn't confirmed.")
    if seconds is not None:
        signals['seconds'] = seconds
    return {'outcome': outcome, 'result': RESULTS[outcome], 'cause': cause, 'signals': signals}


def _crash_reason(lines):
    for l in lines:
        if 'AndroidRuntime' in l and ('Exception' in l or 'Error' in l):
            return 'It crashed: ' + l.split('AndroidRuntime', 1)[1].lstrip(': ')[:200]
    for l in lines:
        m = re.search(r'Fatal signal.*', l)
        if m:
            return 'It crashed: ' + m.group(0)[:200]
    return 'It crashed.'


def collect_log(meta):
    """The game's Lepton instance's Android log (main, system and crash buffers), bounded."""
    ctr = f"lepton-steamlaunch-{int(meta['instance'])}"
    return android.ssh(f"podman exec {ctr} /system/bin/sh -c 'logcat -d -b main,system,crash -t 4000' "
                       "2>/dev/null | tail -c 400000 || true", timeout=60)


def run_test(pkg, wait=TEST_WAIT, app=None, report_result=True):
    """Start the game, watch it for `wait` seconds after its Lepton instance is up, then
    read its log and classify how it went. Records the result as a compatibility report
    (source meta-store, with the version code), which is shared only if the person chose to
    share compatibility results. The game is left running, as Test does, unless it hangs."""
    meta = android._meta_or_fail(pkg)
    ctr = f"lepton-steamlaunch-{int(meta['instance'])}"
    if quest._running(pkg):
        quest._close(pkg)  # a fresh start gives a clean log
    android.launch(pkg)
    started = up = _wait_running(pkg)
    alive, log, seconds = False, '', 0
    if started:
        # Look every TEST_STEP seconds, keeping the newest log: a game that quits can take
        # its instance (and its log) with it.
        steps = max(1, -(-wait // TEST_STEP))
        sh = f'podman exec {ctr} /system/bin/sh -c'
        for _ in range(steps):
            quest._sleep(wait / steps)
            seconds += wait / steps
            up = ctr in android.running_instances()
            alive = up and bool(android.ssh(f"{sh} 'pidof {pkg}' 2>/dev/null || true").strip())
            if up:
                log = collect_log(meta) or log
            if not alive:
                break
        seconds = int(round(seconds))
    verdict = classify(log, alive, up, seconds, started)
    if verdict['outcome'] == 'hangs':
        quest._close(pkg)
    code = meta.get('version_code') if meta.get('version_code') is not None else (app or {}).get('version_code')
    label = meta.get('label') or (app or {}).get('title') or pkg
    verdict.update(package=pkg, version=meta.get('version'), version_code=code, label=label, source='meta-store')
    verdict['notes'] = report_notes(verdict)
    word = OUTCOME_WORDS[verdict['outcome']]
    verdict['message'] = f'Test: {label} {word}.' + (f" {verdict['cause']}" if verdict['cause'] else '')
    if meta.get('vr_issues') and any('platform SDK' in i for i in meta['vr_issues']) and verdict['outcome'] != 'runs':
        verdict['message'] += ' ' + RAN_PLATFORM_SDK
    if report_result:
        verdict['report'] = submit(verdict)
    return verdict


def report_notes(verdict):
    """The report's notes: the outcome, the cause and the version code. Nothing from the
    person's account, and never a file."""
    parts = [f"Meta library test: {verdict['outcome']}"]
    if verdict.get('cause'):
        parts.append(verdict['cause'])
    if verdict.get('version_code') is not None:
        parts.append(f"version code {verdict['version_code']}")
    matched = verdict.get('signals', {}).get('matched')
    if matched:
        parts.append('signals: ' + ', '.join(matched))
    return redact('. '.join(p.rstrip('.') for p in parts) + '.', 1000)


def submit(verdict):
    """Record a test through the existing compatibility report path (frame_catalog.add_report):
    kept on this computer, and shared only as the Privacy settings say."""
    import frame_catalog
    import frame_telemetry
    try:
        r = frame_catalog.add_report(verdict['package'], verdict.get('version'), result=verdict['result'],
                                     notes=verdict['notes'], via='probe', label=verdict.get('label'),
                                     source='meta-store')
    except Exception as e:  # the test still happened: say why it wasn't recorded
        return {'id': None, 'shared': False, 'error': redact(e, 300)}
    shared = frame_catalog.compat_db.shared() or frame_telemetry.enabled('compat')
    return {'id': r.get('id'), 'shared': bool(shared)}


# ---- CLI ------------------------------------------------------------------------------

def _arg(args, name, default=None):
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default


def main():
    cmd, *args = sys.argv[1:] or ['help']
    say = lambda stage, percent=None: print(f'{percent:>3}% {stage}' if percent is not None else stage, file=sys.stderr)
    try:
        if cmd == 'connect':
            if '--token-stdin' not in args:
                raise MetaError('usage: connect --token-stdin (then paste the token and press Enter, or pipe it in)')
            print(WARNING, file=sys.stderr)
            r = connect(sys.stdin.readline())
        elif cmd == 'status':
            r = status()
        elif cmd == 'disconnect':
            r = disconnect()
        elif cmd == 'list':
            r = [_public(a) for a in with_verdicts(library(refresh='--refresh' in args))]
        elif cmd == 'download':
            names = [a for a in args if not a.startswith('--')]
            if len(names) != 1:
                raise MetaError('usage: download APP_ID')
            r = download(names[0], progress=say)
        elif cmd == 'install':
            names = [a for a in args if not a.startswith('--')]
            if len(names) != 1:
                raise MetaError('usage: install APP_ID [--test] [--allow-downgrade]')
            r = install(names[0], test='--test' in args, allow_downgrade='--allow-downgrade' in args, progress=say)
        elif cmd == 'test':
            names = [a for a in args if not a.startswith('--') and a != _arg(args, '--wait')]
            if len(names) != 1:
                raise MetaError('usage: test PACKAGE [--wait SECONDS] [--no-report]')
            wait = _int(_arg(args, '--wait', TEST_WAIT))
            if not wait or wait > 600:
                raise MetaError('--wait must be 1 to 600 seconds')
            r = run_test(names[0], wait=wait, report_result='--no-report' not in args)
        else:
            sys.exit(__doc__)
    except (android.FrameError, OSError) as e:
        sys.exit('error: ' + redact(e, 2000))
    print(json.dumps(r, indent=1))


if __name__ == '__main__':
    main()
