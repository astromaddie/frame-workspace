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
  the page or reports (redact()); a redirect elsewhere carrying it is refused.
- Game files stay in the local install cache and are deleted after each install.

Python stdlib only. CLI: python3 ui/frame_meta.py
  connect --token-stdin | status | disconnect | list [--refresh] | download APP_ID
  install APP_ID [--test] [--allow-downgrade] | test PACKAGE [--wait SECONDS] [--no-report]
"""
import concurrent.futures
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import sys
import tempfile
import threading
import time
import urllib.parse
import zipfile

import frame_android as android
import frame_host
import frame_quest as quest

GRAPH_URL = 'https://graph.oculus.com/graphql'
DOWNLOAD_URL = 'https://securecdn.oculus.com/binaries/download/'
# The only hosts the token is ever sent to. Downloads may redirect elsewhere (a CDN): the
# token never goes there: a redirect carrying it in any form is refused (next_hop).
TOKEN_HOSTS = frozenset({'graph.oculus.com', 'securecdn.oculus.com'})
# Stored queries ("doc_id") as the public tools use them (docs/meta-library.md).
DOC_VIEWER = '4149322231793299'        # data.viewer.user {id, alias}
DOC_ENTITLEMENTS = '4850747515044496'  # data.viewer.user.active_entitlements.nodes[].item
DOC_BINARY = '4734929166632773'        # one binary: package, version, size, OBB and asset files
QUEST_PLATFORMS = frozenset({'ANDROID_6DOF'})  # Quest apps; Rift (PC) and Go (3DOF) are left out
# Entitlement states that mean the account owns the app. A real account's purchases all
# came back PERMANENT (2026-10-14); ACTIVE, which this was first written for, is kept too.
OWNED_STATES = frozenset({'ACTIVE', 'PERMANENT'})
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


class SignInCancelled(MetaError):
    """The desktop app's Sign in with Meta window gave up on this attempt (closed, timed
    out, signed out), so a connect it had started is not saved."""


# ---- Keeping the token out of everything ---------------------------------------

REDACTED = '<redacted>'
# What a Meta user token may be made of, used both to accept one (check_token) and to find
# token-shaped values to redact (_GENERIC), so anything accepted is always redacted. The
# public tools only check the OC/FRL prefix and refuse % | : (docs/meta-library.md); the
# tokens they describe are letters and digits, so this allows those plus the base64 and
# URL-safe base64 characters (+ / = - _) and nothing else. Widen it here, in one place, if a
# real token needs more.
TOKEN_CHARS = r'A-Za-z0-9+/=_\-'  # a regex character-class body
TOKEN_MIN = 20  # characters, prefix included
# Any token-shaped value, whoever's it is, in this order: bare OC.../FRL... tokens of the
# shape check_token accepts first (so a token that happens to contain "access_token=" goes
# whole, prefix and all), then access_token=... / "access_token": "..." parameters and
# Authorization schemes.
#
# The token shape: the OC or FRL prefix check_token requires and its minimum length, over
# the characters tokens are made of (TOKEN_CHARS, plus % | : of encoded and app tokens in
# messages), digits or not. No word boundary or lookbehind: a token is found whatever comes
# before it (_OC..., /OC..., libOC...); the run then takes every token character after the
# prefix, so an accepted token is always inside one match. Upper-case prefix, so package
# names (com.oculus...) are left alone. One run per match, no lookahead: linear. The stored
# token itself is matched in any case by _clean.
_TOKEN_SHAPE = re.compile(r'(?:OC[%s%%|:]{%d,}|FRL[%s%%|:]{%d,})'
                          % (TOKEN_CHARS, TOKEN_MIN - 2, TOKEN_CHARS, TOKEN_MIN - 3))
_GENERIC = [
    _TOKEN_SHAPE,
    re.compile(r'(?i)(access[_%5f-]*token["\']?\s*(?:=|:|%3d)\s*["\']?)[^&\s"\'<>,;]+'),
    re.compile(r'(?i)\b(bearer|oauth)\s+[^\s"\'<>,;]+'),
]
# Upper-case constants separated by underscores (OCULUS_PLATFORM_SDK_VERSION) are names in
# logs and are kept. check_token refuses any value without a lower-case letter, so a run
# holding an accepted token (which has one) can never look like this.
_CONSTANT = re.compile(r'[A-Z0-9]+(?:_[A-Z0-9]+)+')


def _clean_spans(message, token):
    """Where in message the token is, or any start of it (8+ characters, as truncation leaves), in
    any letter case, with any of its characters written literally, percent-encoded (%4B or
    %4b) or percent-encoded twice (%254B, %254b). Mirrors _itch_account._clean on
    feat/itch-purchases (plus the double encoding), so the two can be unified."""
    if not token:
        return []
    # Each character: itself, or the (once or twice) percent-encoding of either of its letter
    # cases; hex digits in any case (re.I).
    tokens = ['(?:%s)' % '|'.join([re.escape(c)] + [f % ord(v) for v in sorted({c.lower(), c.upper()})
                                                     for f in ('%%%02x', '%%25%02x')])
              for c in token]
    start, rest = re.compile(''.join(tokens[:8]), re.I), [re.compile(t, re.I) for t in tokens[8:]]
    spans, pos = [], 0
    while True:
        found = start.search(message, pos)
        if not found:
            break
        end = found.end()
        for t in rest:  # as much more of the token as follows
            more = t.match(message, end)
            if not more:
                break
            end = more.end()
        spans.append((found.start(), end))
        # Look again from the next character, not the end: a partial match just before the
        # real token can overlap its start. Overlapping spans merge in _replace.
        pos = found.start() + 1
    return spans


def _clean(message, token):
    """message without the token in any of the forms _clean_spans finds."""
    return _replace(message, _clean_spans(message, token))


def _replace(text, spans):
    """text with every span replaced by REDACTED; overlapping or touching spans merge first,
    so no part of a value found by one rule survives because another rule found the rest."""
    out, pos = [], 0
    for a, b in sorted(spans):
        if out and a <= pos:  # overlaps or touches the previous span
            pos = max(pos, b)
            continue
        out += [text[pos:a], REDACTED]
        pos = b
    return ''.join(out) + text[pos:]


def _generic_spans(text):
    spans = []
    for pattern in _GENERIC:
        for m in pattern.finditer(text):
            if pattern is _TOKEN_SHAPE:
                if not _CONSTANT.fullmatch(m.group(0)):
                    spans.append(m.span())
            else:  # keep the parameter or scheme name, redact its value
                spans.append((m.end(1), m.end()) if m.groups() else m.span())
    return spans


def carries_token(url, token):
    """True if url holds the token (or 8+ characters of its start), however cased or encoded,
    anywhere (host, path, query or fragment), or an access_token parameter of any value.
    Mirrors _itch_account._carries_key on feat/itch-purchases."""
    for form in (url, urllib.parse.unquote(url), urllib.parse.unquote_plus(url)):
        if _clean(form, token) != form:
            return True
    names = {name.lower() for name, _ in urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query,
                                                                  keep_blank_values=True)}
    return 'access_token' in names


def redact(text, limit=None, token=None):
    """text without the stored token (and any other token-shaped value), then cut to limit
    characters. Redacting before cutting means a cut can't leave part of a token behind."""
    text = '' if text is None else str(text)
    # Every rule finds its spans in the original text and overlapping spans merge, so one
    # rule removing part of a value can never stop another from recognising the rest.
    spans = _generic_spans(text)
    for t in {t for t in (token, _stored_token_quiet()) if t}:
        spans += _clean_spans(text, t)
    text = _replace(text, spans)
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
    if re.fullmatch(r'OC\d{15}', token) or len(token) < TOKEN_MIN:
        raise MetaError("That token is incomplete. Copy the whole value.")
    if not re.fullmatch('[%s]+' % TOKEN_CHARS, token):
        raise MetaError("That doesn't look like a Meta token: it has characters a Meta token doesn't (only "
                        'letters, digits and + / = - _). Copy just the token value.')
    if not re.search('[a-z]', token):  # real tokens are mixed case; this is a constant or a name
        raise MetaError("That doesn't look like a Meta token: it has no lower-case letters. Copy just the "
                        'token value.')
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


# Sign in with Meta (app/meta-signin.js) names each of its attempts. While a connect from an
# attempt is being checked it is listed in _attempts (only then, so this never grows); a
# cancel marks it, and it is then not saved. _saved_by is the attempt whose token is the
# saved account (None for a pasted token), set together with the save, so a cancel that
# arrives just after the save still removes that token, and never a later one.
SIGNIN_ATTEMPT = re.compile(r'[A-Za-z0-9-]{8,64}')
SIGNIN_CANCELLED = 'Sign in with Meta was cancelled, so that sign-in was not saved.'
_attempts = {}     # in-flight attempt id -> {'cancelled': bool, 'running': connects under way}
_saved_by = [None]


def _check_attempt(attempt):
    if attempt is not None and not (isinstance(attempt, str) and SIGNIN_ATTEMPT.fullmatch(attempt)):
        raise MetaError('Invalid sign-in attempt id.')
    return attempt


def cancel_signin(attempt):
    """Never keep a token from this Sign in with Meta attempt: one still being checked isn't
    saved, and one already saved is deleted. Unknown attempts change nothing."""
    if attempt is None:
        raise MetaError('Invalid sign-in attempt id.')
    _check_attempt(attempt)
    with _lock:
        # Both, independently: a connect that has just saved may still be listed until
        # its cleanup runs, and its token must go all the same.
        if attempt in _attempts:
            _attempts[attempt]['cancelled'] = True
        if _saved_by[0] == attempt:
            _forget_locked()
    return status()


def connect(token, attempt=None):
    """Check the token with Meta ("who am I"), then keep it. A disconnect (or another
    connect) that happens while it is being checked wins: this one is then not saved. So
    does cancelling the Sign in with Meta attempt it came from (cancel_signin)."""
    _check_attempt(attempt)
    token = check_token(token)
    with _lock:
        generation = _state['generation']
        if attempt is not None:
            entry = _attempts.setdefault(attempt, {'cancelled': False, 'running': 0})
            entry['running'] += 1
    try:
        user = whoami(token)
        with _lock:
            if attempt is not None and _attempts[attempt]['cancelled']:
                raise SignInCancelled(SIGNIN_CANCELLED)
            if _state['generation'] != generation:
                raise MetaError('The Meta account was disconnected (or another token connected) while this one '
                                'was being checked, so it was not saved.')
            _write_account({'token': token, 'user': user, 'connected': time.strftime('%Y-%m-%dT%H:%M:%S')})
            _saved_by[0] = attempt
            _state['generation'] += 1
            _library.update(generation=None, at=0, apps=None)
    finally:
        if attempt is not None:
            with _lock:
                entry = _attempts[attempt]
                entry['running'] -= 1
                if not entry['running']:
                    del _attempts[attempt]
    return status()


def _forget_locked():
    _state['generation'] += 1
    _saved_by[0] = None
    _library.update(generation=None, at=0, apps=None)
    try:
        os.remove(str(account_path()))
    except FileNotFoundError:
        pass


def disconnect():
    """Forget the token: delete the file and the library in memory. Downloads under way stop."""
    with _lock:
        _forget_locked()
    return status()


# ---- HTTPS to Meta ----------------------------------------------------------------

# Connections: every address a host resolves to must be public (public_address), and the
# connection is made to exactly one of those checked addresses, with TLS verified for the
# host name, so a second DNS answer can't swap in another (no urllib, so no environment
# proxies either). This mirrors _web.public_address / resolve_public / _public_connection
# on feat/itch-purchases, so the two can be unified when both merge.

# IPv6 is an allow-list: only global unicast (2000::/3), which rules out everything else at
# once (loopback, unspecified, mapped/compatible IPv4, NAT64 64:ff9b::, site-local fec0::,
# unique-local fc00::, link-local, multicast, reserved). Inside it, these special blocks are
# refused too: 6to4 and Teredo carry IPv4 that may be private; the rest aren't the internet.
_GLOBAL6 = ipaddress.ip_network('2000::/3')
_SPECIAL6 = tuple(ipaddress.ip_network(n) for n in (
    '2001::/32',      # Teredo
    '2001:2::/48',    # benchmarking
    '2001:10::/28',   # ORCHID
    '2001:20::/28',   # ORCHIDv2
    '2001:db8::/32',  # documentation
    '2002::/16',      # 6to4
    '3fff::/20'))     # documentation (2024)
# IPv4 blocks that are never the public internet, listed in full rather than trusting
# is_global (Python 3.9 calls 192.0.0.8, .11 and .255 global).
_SPECIAL4 = tuple(ipaddress.ip_network(n) for n in (
    '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8', '169.254.0.0/16', '172.16.0.0/12',
    '192.0.0.0/24', '192.0.2.0/24', '192.88.99.0/24', '192.168.0.0/16', '198.18.0.0/15',
    '198.51.100.0/24', '203.0.113.0/24', '224.0.0.0/4', '240.0.0.0/4', '255.255.255.255/32'))


def public_address(ip):
    """True for an address on the public internet: never loopback, private, link-local,
    site-local, shared (CGNAT), reserved, unspecified or multicast, nor an IPv6 wrapper
    around IPv4. Python 3.9's is_global alone lets several of these through."""
    try:
        a = ipaddress.ip_address(ip.split('%', 1)[0])
    except ValueError:
        return False
    if a.version == 6 and (a not in _GLOBAL6 or any(a in n for n in _SPECIAL6)):
        return False
    if a.version == 4 and any(a in n for n in _SPECIAL4):
        return False
    return a.is_global and not (a.is_reserved or a.is_private or a.is_loopback or a.is_link_local
                                or a.is_multicast or a.is_unspecified)


def resolve_public(host):
    """host's addresses for port 443, if every address it has is public; else MetaError."""
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise MetaError(f"Couldn't find {host}; check the internet connection") from None
    if not addresses or not all(public_address(a[4][0]) for a in addresses):
        raise MetaError(f'{host} is on a private or local network address; not connecting')
    found = []
    for a in addresses:
        if a[4][:2] not in found:
            found.append(a[4][:2])
    return found


CONNECT_BUDGET = 20  # seconds for connecting, all addresses and TLS handshakes together


def _public_connection(host, timeout, budget=CONNECT_BUDGET):
    """A verified TLS connection to host:443, made to an address checked to be public.

    Each checked address is tried in turn, all within one deadline (budget, or timeout if
    shorter) covering both connecting and the handshake. The one connected to is always one
    that was checked, so a second lookup can't swap in another. timeout is for reads after."""
    deadline = time.monotonic() + min(budget, timeout)
    error = None
    for address in resolve_public(host):
        if time.monotonic() >= deadline:
            break
        try:
            return _connect(host, address, timeout, deadline)
        except OSError as e:  # unreachable, refused, or its TLS failed: try the next one
            error = e
    raise error or socket.timeout('Timed out connecting to ' + host)


def _connect(host, address, timeout, deadline):
    """TLS to exactly address, verified as host, connected and handshaken before deadline
    (a time.monotonic() value); reads then get timeout seconds each."""
    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout('Timed out connecting to ' + host)
        return left
    sock = socket.create_connection(address, timeout=remaining())
    try:
        sock.settimeout(remaining())
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host,
                                                        do_handshake_on_connect=False)
        sock.settimeout(remaining())
        sock.do_handshake()
        sock.settimeout(timeout)
    except BaseException:
        sock.close()
        raise
    conn = http.client.HTTPConnection(host, 443, timeout=timeout)
    conn.sock = sock
    return conn


class _Answer:
    """A response that closes its connection with it."""

    def __init__(self, conn, response):
        self.conn, self.response = conn, response

    def read(self, n=-1):
        return self.response.read(n)

    def close(self):
        try:
            self.response.close()
        finally:
            self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _send(url, data=None, headers=None, timeout=TIMEOUT):
    """One HTTPS request to a checked public address, redirects not followed: (status,
    headers, readable body). The tests replace this with a fake Meta."""
    p = urllib.parse.urlsplit(check_url(url))
    conn = _public_connection(p.hostname, timeout)
    try:
        conn.request('POST' if data is not None else 'GET', (p.path or '/') + ('?' + p.query if p.query else ''),
                     body=data, headers=headers or {})
        r = conn.getresponse()
    except BaseException:
        conn.close()
        raise
    return r.status, r.headers, _Answer(conn, r)


def check_url(url):
    """url if it is HTTPS on port 443 without credentials; else MetaError (never echoing the
    URL, which can hold the token). No lookup: addresses are checked when connecting."""
    try:
        p = urllib.parse.urlsplit(url)
        port = p.port
    except (ValueError, TypeError):
        raise MetaError("Meta's server gave an invalid address") from None
    if p.scheme != 'https' or p.username or p.password or port not in (None, 443) or not p.hostname:
        raise MetaError("Meta's server sent the download somewhere that isn't plain HTTPS; not following it")
    return url


def next_hop(current, location, token):
    """Where a redirect from current to location goes, checked before any lookup or
    connection: plain HTTPS, and off Meta's own hosts never carrying the token in any form,
    anywhere in the address (host, path, query or fragment). Refused, not rewritten."""
    url = check_url(urllib.parse.urljoin(current, location or ''))
    if (urllib.parse.urlsplit(url).hostname or '').lower() not in TOKEN_HOSTS and carries_token(url, token):
        raise MetaError("Meta's server pointed the download at an address carrying your token; refused, so "
                        'the token is not sent anywhere but Meta')
    return url


def _follow(url, headers, token):
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
            url = next_hop(url, location, token)
            continue
        return status, rheaders, body
    raise MetaError("Meta's server redirected the download too many times")


def graphql(token, doc_id, variables=None):
    """One query to Meta's GraphQL API: the token travels in the POST body, never the URL.
    Returns the answer's "data"; raises TokenRejected or MetaError with readable text."""
    body = urllib.parse.urlencode({'access_token': token, 'doc_id': doc_id,
                                   'variables': json.dumps(variables or {})}).encode()
    try:
        status, _, r = _send(GRAPH_URL, data=body, headers={
            'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json', 'User-Agent': UA})
        with r:
            raw = r.read(MAX_RESPONSE + 1)
    except MetaError:
        raise
    except (http.client.HTTPException, OSError, ValueError) as e:
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
        if state not in OWNED_STATES or (expires and expires < now) or not isinstance(item, dict):
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


class Budget:
    """The bytes one install (or CLI download) may still download, across all its files.
    Counted from the bytes actually received, so files of unknown size count too."""

    def __init__(self, total=None):
        self.left = MAX_TOTAL if total is None else total

    def take(self, n, name):
        if n > self.left:
            raise MetaError(f'{name} would take this install past the {MAX_TOTAL / 1024**3:.0f} GB Frame Control '
                            'downloads for one game; download stopped and removed')
        self.left -= n


def fetch(file_id, dest, limit, token, generation, expected=None, sha256=None, progress=None, budget=None):
    """Download one file by id from Meta's download server into dest. Bounded by limit, by
    the size Meta declared and by budget (the whole install's bytes); checked against its
    SHA-256 when Meta gives one. Stops if the account is disconnected meanwhile. Nothing is
    left behind on failure."""
    budget = budget if budget is not None else Budget()
    if expected is not None and (expected > limit or expected > budget.left):
        raise MetaError(f'{dest.name} is {expected / 1024**3:.1f} GB, more than Frame Control downloads '
                        f'({min(limit, budget.left) / 1024**3:.1f} GB left for this game)')
    url = DOWNLOAD_URL + '?' + urllib.parse.urlencode({'id': file_id, 'access_token': token})
    part = dest.with_name(dest.name + '.part')
    try:
        try:
            status, headers, body = _follow(url, {'User-Agent': UA}, token)
        except MetaError:
            raise
        except (http.client.HTTPException, OSError, ValueError) as e:
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
                    budget.take(len(chunk), dest.name)
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


def download_apk(app, work, token, generation, report, budget):
    report(f"Downloading {app['title']} from Meta", 5)
    got = fetch(app['binary_id'], work / 'base.apk', MAX_APK, token, generation, expected=app.get('size'),
                sha256=app.get('sha256'), progress=_bar(report, f"Downloading {app['title']} from Meta", 5, 40,
                                                        app.get('size')), budget=budget)
    try:
        with zipfile.ZipFile(got['path']) as z:
            if 'AndroidManifest.xml' not in z.namelist():
                raise MetaError(f"What Meta sent for {app['title']} is not an APK")
    except zipfile.BadZipFile:
        raise MetaError(f"What Meta sent for {app['title']} is not an APK") from None
    return Path(got['path'])


def download_data(app, work, token, generation, report, budget):
    """The app's OBB files, in work/obb: ([paths], [names not copied]). Every byte counts
    against budget as it arrives; a file that overflows it is removed."""
    plan, skipped = data_plan(app)
    folder = work / 'obb'
    folder.mkdir(exist_ok=True)
    paths, total = [], sum(f.get('size') or 0 for f, _ in plan)
    if total > budget.left:
        raise MetaError(f"{app['title']}'s game data is {total / 1024**3:.1f} GB, more than Frame Control "
                        f"downloads ({MAX_TOTAL // 1024**3} GB)")
    for i, (f, name) in enumerate(plan):
        stage = f"Downloading {app['title']}'s game data ({i + 1} of {len(plan)})"
        report(stage, 50)
        got = fetch(f['id'], folder / name, MAX_DATA_FILE, token, generation, expected=f.get('size'),
                    sha256=f.get('sha256'), progress=_bar(report, stage, 50, 15, f.get('size')), budget=budget)
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
        budget = Budget()
        apk = download_apk(app, work, token, generation, report, budget)
        obb, skipped = download_data(app, work, token, generation, report, budget)
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
    budget = Budget()
    apk = download_apk(app, work, token, generation, report, budget)
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
        obb, _ = download_data(app, work, token, generation, report, budget)
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
        obb, _ = download_data(app, work, token, generation, report, budget)
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
# kind): 'blocker' explains a quit or a stall, 'issue' a game that runs with something
# missing. Causes are fixed texts (CAUSES), never log text: reports carry only those. A
# short excerpt (an extension, library, exception or signal name, matched by a strict
# pattern) may be shown to the person, redacted before it is shortened, never reported.

SIGNATURES = [
    # Explicit failure evidence only: a game that merely calls the check (and passes) isn't one.
    ('licence', re.compile(r'(?i)entitle\w{0,40}\s{0,8}(?:check\s{0,8})?(?:has\s{1,8})?'
                           r'(?:failed|failure|denied|rejected|error)|'
                           r'\bnot entitled\b|failed to (?:verify|check|get) (?:the )?entitlement|'
                           r'(?:ovr_Entitlement\w{0,60}|IsUserEntitledToApplication)(?:\(\))?\s{0,8}'
                           r'(?:failed|returned (?:an )?error|error|=\s{0,8}false|:\s{0,8}false)'), 'blocker'),
    ('platform', re.compile(r'(?i)ovr_Platform(?:Initialize)?\w{0,60}\s{0,8}(?:failed|error)|'
                            r'libovrplatform\w{0,40}\.so.{0,200}?(?:not found|failed)|'
                            r'Failed to initialize (?:the )?(?:Oculus|Meta) Platform|'
                            r'Oculus Platform.{0,200}?(?:failed|not initialized)'),
     'blocker'),
    ('device', re.compile(r'(?i)Failed to launch SystemActivities|IsRunningOnSantaCruz|'
                          r'(?:device|headset) (?:is )?not supported|unsupported (?:device|headset)|not an? (?:Oculus|Meta) (?:Quest|device)'),
     'blocker'),
    ('vrapi', re.compile(r'(?i)vrapi_\w{1,60} was called before|libvrapi\.so.{0,200}?(?:not found|failed)|'
                         r'vrapi_Initialize\w{0,40}\s{0,8}(?:failed|error)'),
     'blocker'),
    ('extension', re.compile(r'XR_ERROR_EXTENSION_NOT_PRESENT'), 'blocker'),
    ('xr-version', re.compile(r'XR_ERROR_API_VERSION_UNSUPPORTED'), 'blocker'),
    ('runtime', re.compile(r'XR_ERROR_RUNTIME_(?:UNAVAILABLE|FAILURE)|XR_ERROR_INITIALIZATION_FAILED|'
                           r'(?i:xrCreateInstance\w{0,40}\s{0,8}(?:failed|returned -))'), 'blocker'),
    ('gpu', re.compile(r'(?i)VK_ERROR_DEVICE_LOST|DEVICE[ _]LOST|vkCreate\w{1,60} failed|VK_ERROR_\w+|'
                       r'libgallium_dri\.so|zink.{0,200}?(?:error|fail)|GL_INVALID_\w+|GL_OUT_OF_MEMORY|'
                       r'shader (?:compil|link)\w{0,20} (?:failed|error)|failed to (?:compile|link) shader'), 'issue'),
    ('clipboard', re.compile(r'ClipboardManager'), 'blocker'),
    ('android', re.compile(r'NoSuchMethodError|NoClassDefFoundError'), 'issue'),
    ('library', re.compile(r'UnsatisfiedLinkError|dlopen failed'), 'blocker'),
]
CAUSES = {
    'licence': "Meta's licence check failed (it needs Meta's services, which the Frame doesn't have). "
               "Frame Control won't remove the check.",
    'platform': "Meta's Platform SDK couldn't start (no Meta services on the Frame); online features, and the "
                'licence check if the game enforces it, won\'t work.',
    'device': 'The game checks that it is on a Meta Quest and stops on the Frame.',
    'vrapi': "The game uses Meta's legacy VrApi, which the Frame doesn't have.",
    'extension': 'The game asked for an OpenXR feature the Frame lacks.',
    'xr-version': "The game asked for an OpenXR version the Frame's runtime refuses.",
    'runtime': "The game couldn't start an OpenXR session with the Frame's runtime.",
    'gpu': "Graphics errors (the Frame uses the Mesa Vulkan and GLES drivers, which are stricter than the "
           "Quest's); it may draw wrongly or crash.",
    'clipboard': "The game needs Android's clipboard service, which Lepton doesn't have.",
    'android': 'The game called an Android feature Lepton (Android 11) lacks.',
    'library': "The game needs a library the Frame doesn't have.",
    'not-started': f"Its Lepton instance didn't start within {START_TIMEOUT} s.",
    'crash-java': 'It crashed (a Java exception).',
    'crash-native': 'It crashed (a native crash signal).',
    'crash': 'It crashed.',
    'quit': 'It closed by itself without a recognisable error.',
    'quit-no-log': 'It closed by itself, and its instance stopped before its log could be read.',
    'not-responding': 'It stopped responding (Android reported it not responding).',
    'no-session': "It stayed open, but its log didn't show its VR scene starting, so Frame Control can't tell "
                  "whether it runs. This result isn't recorded.",
}
CRASH = re.compile(r'FATAL EXCEPTION|Fatal signal \d+|\bSIG(?:SEGV|ABRT|BUS|ILL)\b|>>> [\w.]{1,200} <<<|Abort message:')
HANG = re.compile(r'ANR in [\w.]+|Application Not Responding|Input dispatching timed out')
# Signs the game got far enough to show its VR scene.
SESSION = re.compile(r'XR_SESSION_STATE_(?:SYNCHRONIZED|VISIBLE|FOCUSED)|xrEndFrame|xrBeginSession|'
                     r'session state (?:changed )?(?:to )?(?:SYNCHRONIZED|VISIBLE|FOCUSED)', re.I)
XR_STARTED = re.compile(r'FrameXrCompat.{0,200}?created instance|xrCreateInstance', re.I)
LINE_SCAN = 4096  # characters of each log line the signatures look at (Android's own line limit)
LOG_UNREADABLE = ("Frame Control couldn't read the game's log from the Frame, so the test has no result. "
                  'Check the Frame is connected, then test again.')
# The compatibility database's result for each outcome; None: not recorded at all.
RESULTS = {'runs': 'runs', 'runs-with-issues': 'runs', 'quits': 'crashes', 'crashes': 'crashes',
           'hangs': 'crashes', 'instance-failed': 'instance_failed', 'inconclusive': None}
OUTCOME_WORDS = {'runs': 'runs', 'runs-with-issues': 'runs, with issues', 'quits': 'quit',
                 'crashes': 'crashed', 'hangs': 'seems stuck', 'instance-failed': "didn't start",
                 'inconclusive': 'gave no clear result'}


def _excerpt(key, line):
    """A short name from the log line behind a cause, for the person only: an OpenXR
    extension, a library, an exception class or a signal. The whole line is redacted first,
    then the name is taken whole (never a cut-off end of something longer), and dropped
    unless it looks like a real identifier."""
    if key not in _NAMES or not line:
        return ''
    # The whole line is redacted, then split once into words; only words short enough to
    # be names are tested, each with one fullmatch: linear in the line, however long.
    for word in re.split(r'[^\w.+$-]+', redact(line)):
        word = word.strip('.')
        if 0 < len(word) <= 64 and _NAMES[key].fullmatch(word) and _plausible(word):
            return word
    return ''


_NAMES = {'extension': re.compile(r'XR_(?!ERROR_)[A-Z0-9]+_[A-Za-z0-9_]+'),
          'library': re.compile(r'[\w.+-]+\.so'),
          'crash-java': re.compile(r'[\w$]+(?:\.[\w$]+)*(?:Exception|Error)'),
          'crash-native': re.compile(r'SIG[A-Z]{2,6}')}


def _plausible(name):
    """A name short and plain enough to be a library, extension, class or signal name, not
    a credential: at most 64 characters, no run of 32+ letters and digits, and no run of 16+
    that mixes upper case, lower case and digits (as tokens and base64 do)."""
    if not name or len(name) > 64 or REDACTED in name:
        return False
    for run in re.findall(r'[A-Za-z0-9]+', name):
        if len(run) >= 32 or (len(run) >= 16 and re.search(r'[a-z]', run) and re.search(r'[A-Z]', run)
                               and re.search(r'[0-9]', run)):
            return False
    return True


def classify(log, alive, instance_up=True, seconds=None, started=True):
    """An outcome (runs, runs-with-issues, quits, crashes, hangs, instance-failed, or
    inconclusive when the game stayed open without showing its VR scene), the key of a fixed
    plain-English cause, and the signals behind it, from the game's Android log, whether its
    Lepton instance started at all, and whether its process and instance were up at the end."""
    # Signatures look at the start of each line only (LINE_SCAN), and every pattern is
    # bounded, so a hostile log can't make this slow. The full line is kept for the excerpt,
    # which redacts all of it before taking a name.
    full = (log or '').splitlines()
    lines = [l[:LINE_SCAN] for l in full]
    log = '\n'.join(lines)
    by_scan = dict(zip(lines, full))
    found = []  # (key, kind, line)
    for key, pattern, kind in SIGNATURES:
        line = next((l for l in lines if pattern.search(l)), None)
        if line is not None:
            found.append((key, kind, line))
    crash = next((l for l in lines if CRASH.search(l)), None)
    hang = bool(HANG.search(log))
    session = bool(SESSION.search(log))
    signals = {'alive': bool(alive), 'instance_up': bool(instance_up), 'crash': bool(crash), 'not_responding': hang,
               'xr_session': session, 'xr_started': session or bool(XR_STARTED.search(log)),
               'matched': [key for key, _, _ in found]}
    blockers = [(k, l) for k, kind, l in found if kind == 'blocker']
    issues = [(k, l) for k, kind, l in found if kind == 'issue']
    first = (blockers + issues or [(None, None)])[0]
    if not started:
        outcome, (key, line) = 'instance-failed', ('not-started', None)
    elif not alive:
        outcome = 'crashes' if crash else 'quits'
        if first[0]:
            key, line = first
        elif crash:
            java = next((l for l in lines if 'AndroidRuntime' in l and ('Exception' in l or 'Error' in l)), None)
            native = next((l for l in lines if re.search(r'Fatal signal|\bSIG[A-Z]{2,6}\b', l)), None)
            key, line = ('crash-java', java) if java else ('crash-native', native) if native else ('crash', None)
        else:
            key, line = ('quit' if log.strip() else 'quit-no-log'), None
    elif hang:
        outcome, (key, line) = 'hangs', (blockers[0] if blockers else ('not-responding', None))
    elif not session:
        # Open, but no sign of its VR scene: not enough to say it runs, nor that it hangs.
        outcome, (key, line) = 'inconclusive', (blockers[0] if blockers else ('no-session', None))
    elif first[0]:
        outcome, (key, line) = 'runs-with-issues', first
    else:
        outcome, key, line = 'runs', None, None
    cause = CAUSES[key] if key else ''
    if outcome == 'inconclusive' and key != 'no-session':
        cause += ' ' + CAUSES['no-session']
    if seconds is not None:
        signals['seconds'] = seconds
    return {'outcome': outcome, 'result': RESULTS[outcome], 'cause_key': key, 'cause': cause,
            'detail': _excerpt(key, by_scan.get(line, line)), 'signals': signals}


def collect_log(meta):
    """The game's Lepton instance's Android log (main, system and crash buffers), bounded."""
    ctr = f"lepton-steamlaunch-{int(meta['instance'])}"
    return android.ssh(f"podman exec {ctr} /system/bin/sh -c 'logcat -d -b main,system,crash -t 4000' "
                       "2>/dev/null | tail -c 400000 || true", timeout=60)


def run_test(pkg, wait=TEST_WAIT, app=None, report_result=True):
    """Start the game, watch it for `wait` seconds after its Lepton instance is up, then
    read its log and classify how it went. Records a clear result as a compatibility report
    (source meta-store, with the version code), shared only if the person chose to share
    compatibility results; an inconclusive one isn't recorded. The game is left running, as
    Test does, unless it hangs."""
    try:
        return _run_test(pkg, wait, app, report_result)
    except MetaError:
        raise  # fixed texts already
    except android.FrameError:
        # A failed ssh command's error is its output, which here can be the game's own log:
        # never shown, not even redacted.
        raise MetaError(LOG_UNREADABLE) from None


def _run_test(pkg, wait, app, report_result):
    meta = android._meta_or_fail(pkg)
    ctr = f"lepton-steamlaunch-{int(meta['instance'])}"
    label = meta.get('label') or (app or {}).get('title') or pkg
    if quest._running(pkg):
        closed, _ = quest._close(pkg)  # a fresh start gives a clean log
        if not closed:
            raise MetaError(f"{label} is open on the Frame and Frame Control couldn't close it, so it wasn't "
                            'tested. Close it from Steam, then test again.')
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
    code = meta.get('version_code') if meta.get('version_code') is not None else (app or {}).get('version_code')
    verdict.update(package=pkg, version=meta.get('version'), version_code=code, label=label, source='meta-store')
    verdict['notes'] = report_notes(verdict)
    word = OUTCOME_WORDS[verdict['outcome']]
    message = f'Test: {label} {word}.' + (f" {verdict['cause']}" if verdict['cause'] else '')
    if verdict['detail']:
        message += f" ({verdict['detail']})"
    if meta.get('vr_issues') and any('platform SDK' in i for i in meta['vr_issues']) and \
            verdict['outcome'] not in ('runs', 'inconclusive'):
        message += ' ' + RAN_PLATFORM_SDK
    if verdict['outcome'] == 'hangs':
        closed, _ = quest._close(pkg)
        verdict['closed'] = closed
        message += (' Frame Control closed it.' if closed else
                    " Frame Control couldn't close it: the game may still be running; close it from Steam.")
    verdict['message'] = message
    if report_result and verdict['result']:
        verdict['report'] = submit(verdict)
    elif verdict['result'] is None:
        verdict['report'] = {'id': None, 'shared': False, 'skipped': 'inconclusive results are not recorded'}
    return verdict


def report_notes(verdict):
    """The report's notes, from fixed texts only: the outcome, the cause, the version code and
    the names of the signals matched. Never log text, nothing from the account, never a file."""
    parts = [f"Meta library test: {verdict['outcome']}"]
    if verdict.get('cause_key'):
        parts.append(CAUSES[verdict['cause_key']])
    if isinstance(verdict.get('version_code'), int):
        parts.append(f"version code {verdict['version_code']}")
    matched = [k for k in verdict.get('signals', {}).get('matched', []) if k in CAUSES]
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
