"""Anonymous analytics for Frame Control, sent to PostHog. Python stdlib only.

Three levels, each chosen in the page's Privacy panel (docs/privacy.md lists
every event and property):

- usage (on by default, after the first-run notice has been shown): installs of
  Frame Control, daily opens, updates, which tabs are used, and whether installs
  on the Frame worked, with an error category from a fixed list. Never file
  names, paths, hostnames, IP addresses, window titles or account data.
- compat (opt-in): Android compatibility reports, the same fields the Report
  dialog shows, so they reach the shared database (frame_compat_db.py). The
  maintainer's sync (python3 ui/frame_compat_db.py sync) moves them there.
- diagnostics (opt-in): error messages and Python tracebacks, scrubbed of
  home folders, user names, addresses and keys.

The first-run notice offers compat and diagnostics together, and the page's
Report a problem dialog (frame_report.py) sends bug reports privately to the
same project whatever is chosen here.

Events are identified by a random id made on first run, not by the person or
computer, and sent without person profiles or GeoIP. Opens, installs and updates
carry a country instead, worked out on this computer from its time zone setting
(frame_timezones.py), to see roughly where Frame Control is used. Nothing is sent without a
project key (ui/telemetry.json or $FRAME_CONTROL_POSTHOG_KEY), from a source
checkout unless $FRAME_CONTROL_TELEMETRY=1, or when $DO_NOT_TRACK=1 or
$FRAME_CONTROL_TELEMETRY=0.

Events wait in an outbox file and are sent in batches from a background thread,
so going offline loses nothing. The last SENT_KEEP sent events are kept on this
computer so the page can show exactly what left it.
"""
import ipaddress
import json
import os
import platform
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import frame_host
import frame_timezones

HERE = Path(__file__).resolve().parent
STATE = frame_host.data_dir('telemetry')
SETTINGS = STATE / 'settings.json'
OUTBOX = STATE / 'outbox.jsonl'
SENT = STATE / 'sent.jsonl'
SENT_KEEP = 200
FRAMES_SEEN_KEEP = 100  # build/headset pairs already reported, enough for several headsets' updates
OUTBOX_MAX = 2000  # events kept while offline; the oldest go first
FLUSH_EVERY = 60
REPEAT_WINDOW = 600  # the same diagnostic error is sent at most once in this many seconds
# Not faults in Frame Control: the headset asleep, away or not set up yet, or ssh and the headset
# not trusting each other (a host key that changed, a key not authorised): the page says what to do
# about each (Devices tab, Set Up Connection). The status poll
# meets these every few seconds, so each is sent at most once per session, whatever the wording,
# but only for an error marked where ssh ran as ssh failing to reach the headset
# (frame_host.link_failure). The message alone isn't evidence: a download's "Connection reset by
# peer", or a file name with ssh's words in it, keeps the usual window.
EXPECTED_CATEGORIES = ('frame_unreachable', 'frame_not_set_up', 'frame_auth')
DEFAULT_HOST = 'https://us.i.posthog.com'

LEVELS = ('usage', 'compat', 'diagnostics')
NOTICE_VERSION = 2  # raise when the notice lists something new: it's shown again (2: country)
# Events the page may send through /api/telemetry, and the properties each may carry.
PAGE_EVENTS = {'tab_viewed': {'tab'}, 'update_offered': {'to_version'},
               'update_started': {'to_version'}, 'update_failed': {'to_version', 'error_category'}}
TABS = {'home', 'games', 'android', 'tools', 'devices', 'control'}

_lock = threading.RLock()
_send_lock = threading.Lock()  # held while sending; consent changes wait for it
_seen_errors = {}
_flusher = None
_wake = threading.Event()


# ---- configuration and settings -------------------------------------------------

def config():
    """PostHog host and project key: the environment, else ui/telemetry.json."""
    try:
        with open(HERE / 'telemetry.json') as f:
            c = json.load(f)
    except (OSError, ValueError):
        c = {}
    host = os.environ.get('FRAME_CONTROL_POSTHOG_HOST') or c.get('host') or DEFAULT_HOST
    key = os.environ.get('FRAME_CONTROL_POSTHOG_KEY') or c.get('key') or ''
    project = os.environ.get('FRAME_CONTROL_POSTHOG_PROJECT') or c.get('project') or ''
    return {'host': host.rstrip('/'), 'key': key, 'project': str(project)}


def blocked():
    """Why nothing may be sent at all, whatever the settings say, or None."""
    if os.environ.get('DO_NOT_TRACK') == '1' or os.environ.get('FRAME_CONTROL_TELEMETRY') == '0':
        return 'turned off by DO_NOT_TRACK or FRAME_CONTROL_TELEMETRY=0'
    if not config()['key']:
        return 'no PostHog project key in this build'
    if not os.environ.get('FRAME_CONTROL_PACKAGED') and os.environ.get('FRAME_CONTROL_TELEMETRY') != '1':
        return 'running from a source checkout (set FRAME_CONTROL_TELEMETRY=1 to send)'
    return None


def _defaults():
    return {'id': str(uuid.uuid4()), 'usage': True, 'compat': False, 'diagnostics': False,
            'notice_shown': False, 'notice_version': 0, 'installed_sent': False, 'last_version': None, 'last_open_day': None,
            'frames_seen': [], 'compat_sent': []}


def settings():
    with _lock:
        s = _defaults()
        try:
            with open(SETTINGS) as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                s.update({k: v for k, v in saved.items() if k in s})
        except (OSError, ValueError):
            pass
        if not SETTINGS.exists():
            _save(s)  # keep the id stable from the first call
        return s


def _save(s):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS.with_suffix('.tmp')
        tmp.write_text(json.dumps(s, indent=1))
        os.replace(tmp, SETTINGS)
    except OSError:
        pass


def enabled(level):
    """Whether events of this level are collected: never when sending is blocked, so a
    source checkout or a test run leaves nothing behind."""
    if blocked():
        return False
    return bool(settings().get(level))


def update_settings(changes):
    """Apply the page's choices. Turning a level off drops its unsent events; a send already
    under way finishes first, so nothing leaves after this returns."""
    with _send_lock, _lock:
        s = settings()
        if changes.get('noticeShown'):
            s['notice_shown'], s['notice_version'] = True, NOTICE_VERSION
        for level in LEVELS:
            if level in changes:
                s[level] = bool(changes[level])
                s['notice_shown'] = True  # only the notice itself moves notice_version on
        _save(s)
        _drop_unwanted(s)
    if changes.get('compat'):
        backfill_compat()
    _wake.set()
    return state()


def _notice_current(s):
    """Whether the notice this person saw lists everything that's sent now. If not, the page
    shows it again, and what it added waits until it has been seen."""
    return s['notice_shown'] and s['notice_version'] >= NOTICE_VERSION


def state():
    """What the page shows: the choices, why sending is blocked, and what was sent."""
    s = settings()
    return {'usage': s['usage'], 'compat': s['compat'], 'noticeShown': _notice_current(s),
            'diagnostics': s['diagnostics'],
            'blocked': blocked(), 'id': s['id'], 'queued': len(_read_lines(OUTBOX)),
            'sent': list(reversed(_read_lines(SENT)))[:50]}


# ---- scrubbing and error categories ---------------------------------------------

def _user_names():
    names = set()
    for v in (os.environ.get('USER'), os.environ.get('USERNAME'), Path.home().name):
        if v and len(v) > 2:
            names.add(v)
    return names


URL_RE = re.compile(r'[A-Za-z][A-Za-z0-9+.-]*://[^\s\'"<>]+')
SCRUBS = [
    (re.compile(r'ssh-(?:rsa|ed25519|dss)\s+\S+'), '<ssh-key>'),
    (re.compile(r'-----BEGIN [^-]+-----.*?-----END [^-]+-----', re.S), '<pem>'),
    (re.compile(r'\b(?:phc|phx|ghp|gho|ghu|ghs|github_pat|sk|pk|rk|xox[abpr])[_-][A-Za-z0-9_-]{12,}'), '<token>'),
    (re.compile(r'(?i)\b(token|key|secret|password|passwd|pwd|auth|signature|sig)=[^\s&]+'), r'\1=<redacted>'),
    (re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+'), '<email>'),
    (re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b'), '<ip>'),
    (re.compile(r'\b(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b'), '<mac>'),
    (re.compile(r'\b7656119\d{10}\b'), '<steamid>'),
    (re.compile(r'\b(?:[\w-]+\.)+(?:local|lan|home|internal|localdomain|ts\.net)\b'), '<host>'),
    (re.compile(r'\b[0-9a-fA-F]{32,}\b'), '<hex>'),
]
IPV6_RE = re.compile(r'(?<![\w:])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?:%\w+)?(?![\w:])')


def _ipv6(m):
    try:
        ipaddress.IPv6Address(m.group(0).split('%')[0])
        return '<ip>'
    except ValueError:
        return m.group(0)


def public_host(host):
    """A host name that's safe to send: not an address, not a private or single-label name."""
    host = (host or '').lower().rstrip('.')
    if not host or '.' not in host:
        return None
    try:
        ipaddress.ip_address(host.strip('[]'))
        return None
    except ValueError:
        pass
    if re.search(r'\.(?:local|lan|home|internal|localdomain|ts\.net|arpa)$', host) or not re.fullmatch(r'[a-z0-9.-]+', host):
        return None
    return host


def _scrub_url(u):
    """Only the scheme and a public host name of a URL; never user names, passwords, ports,
    paths or queries."""
    try:
        parts = urlsplit(u)
        host = public_host(parts.hostname)
    except ValueError:
        host = None
    return f'{parts.scheme}://{host}/…' if host else '<url>'


def scrub(text, limit=2000):
    """Text with URLs, home folders, user names, addresses, hosts, ids and keys replaced."""
    if text is None:
        return None
    t = URL_RE.sub(lambda m: _scrub_url(m.group(0)), str(text))  # first, before anything splits a URL
    home = str(Path.home())
    if len(home) > 3:
        t = t.replace(home, '~')
    # A home folder's whole name ("C:\Users\Jane Doe", "/Users/O'Brien"), up to the next separator.
    t = re.sub(r'''(?i)(/Users/|/home/|[A-Za-z]:[\\/]+Users[\\/]+)[^\\/\n"]+''', r'\1<user>', t)
    # ssh's "user@host: ..." and "user@host's password:", the whole field: the user even with
    # spaces or a DOMAIN\ prefix, the host even a full domain name. Before the email rule, which
    # would otherwise take only the last word of the user.
    t = re.sub(r'''(?m)(?:^|(?<=: )|(?<=\| ))[^@\n:|"<]{1,64}@[\w.\[\]%:<>-]+?(?=:(?:\s|$)|'s\s)''',
               '<user>@<host>', t)
    for pattern, repl in SCRUBS:
        t = pattern.sub(repl, t)
    t = re.sub(r'(?<![\w.+\\<-])[\w.+\\-]+@(?=[A-Za-z\[<])', '<user>@', t)  # any other word@host
    t = IPV6_RE.sub(_ipv6, t)
    for name in _user_names():
        t = re.sub(r'\b%s\b' % re.escape(name), '<user>', t)
    return t[:limit]


# From the most to the least specific; the first match wins.
CATEGORIES = [
    # A web-link download (frame_webinstall) that broke or didn't check out: not the headset,
    # whatever the reason, and first so a file name in the message can't put it elsewhere.
    ('download_failed', re.compile(r"\A(?:download failed: |download cut off at |downloaded \d+ bytes; |"
                                   r"the server says \d+ bytes; )|doesn't match the manifest's sha256; "
                                   r"not installing it\Z")),
    ('android_installer', re.compile(r'INSTALL_(?:FAILED|PARSE_FAILED)_[A-Z_]+')),
    ('apk_needs_newer_android', re.compile(r'needs Android API')),
    ('apk_wrong_abi', re.compile(r'no arm64-v8a build')),
    ('layer_missing', re.compile(r'OpenXR compatibility layer')),
    ('apk_repack_failed', re.compile(r'could not prepare the APK for the Frame')),
    ('tool_missing', re.compile(r"\[WinError 2\]|No such file or directory: '(?:ssh|scp|rsync|adb)")),
    ('apk_unreadable', re.compile(r'(?i)not a zip|bad apk|AndroidManifest|ApkError|unexpected package name|'
                                  r'not a readable APK|not an APK|APK manifest|in the APK (?:uses a compression|is )')),
    ('cant_run_on_frame', re.compile(r"can't run on the Frame")),
    ('steam_shortcut', re.compile(r'(?i)steam did not return a shortcut|shortcut list|no Steam shortcut')),
    # The Frame-side scripts that talk to the Steam client's UI (frame/android/steam_shortcuts.py), which
    # fail with their own words or a Python traceback, its last lines kept. Before frame_unreachable:
    # "Connection refused" and "timed out" here are Steam's debug port on the Frame, not ssh.
    ('steam_not_running', re.compile(r'SharedJSContext not found|'
                                     r'URLError: <urlopen error \[Errno 111\] Connection refused>|'
                                     r'ConnectionRefusedError: \[Errno 111\]')),
    ('steam_not_responding', re.compile(r'websocket handshake failed|(?:socket\.timeout|TimeoutError): timed out|'
                                        r'(?:\A|\n)EOFError\Z')),
    ('steam_js_error', re.compile(r'JS error: ')),
    ('artwork_failed', re.compile(r'incomplete artwork render|artwork renderer did not produce|'
                                  r'render is \d+ bytes of|could not prepare artwork|all five Steam artwork slots')),
    # Copying the APK (or its launcher) to the Frame: rsync or scp ran out of time.
    ('copy_timeout', re.compile(r'to the Frame timed out\Z')),
    ('frame_not_set_up', re.compile(r'(?i)Could not resolve hostname|no "?frame"? (?:SSH )?alias')),
    ('frame_auth', re.compile(r'(?i)Permission denied|Host key verification failed|'
                              r'REMOTE HOST IDENTIFICATION HAS CHANGED|host key is known for')),
    # Anything ssh says about its own connection: "ssh: connect to host … port 22: <reason>" (Windows
    # says "Unknown error"), a dropped shared connection (mux_client_…, client_loop), and Windows'
    # "banner exchange: Connection to UNKNOWN port -1" (its ssh can't name a peer whose connect
    # failed late). Not a bare "ssh exited 255": a command on the Frame can exit 255 too.
    ('frame_unreachable', re.compile(r'(?i)timed out|Connection (?:refused|reset|closed)|No route to host|'
                                     r'Network is unreachable|Host is down|asleep|kex_exchange|banner exchange|'
                                     r'ssh: connect to host |mux_client_|client_loop: ')),
    ('frame_disk_full', re.compile(r'(?i)No space left|disk full|ENOSPC')),
    # rsync or scp failed for another reason ("connection unexpectedly closed", or no words at all).
    ('copy_failed', re.compile(r'\Acopying .+ to the Frame failed: ')),
    ('download_failed', re.compile(r'(?i)HTTP (?:Error )?\d{3}|URLError|download|certificate verify failed')),
    ('flatpak', re.compile(r'(?i)flatpak|flathub')),
    ('cancelled', re.compile(r'(?i)cancel')),
    ('lepton', re.compile(r'(?i)lepton|podman|instance')),
]


# Python's own errors for a mistake in Frame Control's code, whatever their message says.
BUG_ERRORS = (KeyError, IndexError, TypeError, AttributeError, NameError, AssertionError,
              ZeroDivisionError, RecursionError, UnicodeError, json.JSONDecodeError)


def categorize(message):
    """(category, detail): a fixed category name, plus an Android installer code when there is one.
    message may be the exception itself: one of BUG_ERRORS is internal_error."""
    if isinstance(message, BUG_ERRORS):
        return 'internal_error', None
    text = str(message or '')
    for name, pattern in CATEGORIES:
        m = pattern.search(text)
        if m:
            return name, (m.group(0) if name == 'android_installer' else None)
    return 'other', None


# ---- capturing ------------------------------------------------------------------

def common():
    return {'app_version': app_version(), 'os': frame_host.NAME, 'arch': platform.machine().lower(),
            'python': '%d.%d' % sys.version_info[:2], '$lib': 'frame-control',
            # Anonymous events: no person profile, no location lookup, and a placeholder address,
            # since PostHog stores the sender's IP unless an event gives one.
            '$process_person_profile': False, '$geoip_disable': True, '$ip': '0.0.0.0'}


def app_version():
    v = os.environ.get('FRAME_CONTROL_VERSION')
    if v:
        return v
    try:
        with open(HERE.parent / 'app' / 'package.json') as f:
            return json.load(f).get('version') or 'dev'
    except (OSError, ValueError):
        return 'dev'


def capture(event, props=None, level='usage'):
    """Queue an event if its level is on. Never raises."""
    try:
        if level not in LEVELS or not enabled(level):
            return False
        s = settings()
        e = {'event': event, 'distinct_id': s['id'], 'uuid': str(uuid.uuid4()),
             'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
             'properties': {**common(), **(props or {}), 'level': level}}
        with _lock:
            lines = _read_lines(OUTBOX) + [e]
            _write_lines(OUTBOX, lines[-OUTBOX_MAX:])
        return True
    except Exception:
        return False


def page_event(body):
    """An event from the page, checked against PAGE_EVENTS."""
    name = body.get('event')
    allowed = PAGE_EVENTS.get(name)
    if allowed is None:
        raise ValueError('unknown event')
    props = {k: str(v)[:40] for k, v in (body.get('properties') or {}).items() if k in allowed}
    if name == 'tab_viewed' and props.get('tab') not in TABS:
        raise ValueError('unknown tab')
    return {'queued': capture(name, props)}


def country():
    """The two-letter country of the computer's time zone setting, or None. Only names in the
    tz database's table count, so nothing else in the setting can be sent. The app passes the
    zone from Electron, which knows it on every OS; from a checkout it's read from /etc/localtime."""
    tz = os.environ.get('FRAME_CONTROL_TIMEZONE') or os.environ.get('TZ', '').lstrip(':')
    if not tz:
        try:
            tz = os.path.realpath('/etc/localtime').split('/zoneinfo/', 1)[1]
        except (OSError, IndexError):
            return None
    return frame_timezones.COUNTRY.get(tz)


def app_started():
    """Once per server start: first install, an update, and one open a day, with the country
    (left out when sending if the notice this person saw came before it: flush)."""
    if blocked():
        return
    with _lock:
        s = settings()
        version, today = app_version(), time.strftime('%Y-%m-%d')
        cc = country()
        where = {'country': cc} if cc else {}
        if not s['installed_sent']:
            capture('app_installed', where)
            s['installed_sent'] = True
        elif s['last_version'] and s['last_version'] != version:
            capture('app_updated', {'from_version': s['last_version'], **where})
        if s['last_open_day'] != today:
            capture('app_opened', where)
            s['last_open_day'] = today
        s['last_version'] = version
        _save(s)


def frame_seen(build, version, headset=None, saved=None):
    """The Frame's SteamOS build (public build numbers), once per build for each headset.
    headset: the anonymous hash from frame_status.headset_id; saved: how many headsets
    this copy of the app has set up."""
    headset = headset if isinstance(headset, str) and re.fullmatch(r'[0-9a-f]{16}', headset) else None
    key = f'{build}/{version}' + (f'/{headset}' if headset else '')
    with _lock:
        s = settings()
        if not build or key in s['frames_seen']:
            return
        s['frames_seen'] = (s['frames_seen'] + [key])[-FRAMES_SEEN_KEEP:]
        _save(s)
    p = {'steamos_build': str(build)[:40], 'steamos_version': str(version or '')[:40]}
    if headset:
        p['headset_id'] = headset
    if isinstance(saved, int) and saved >= 0:
        p['headsets_saved'] = saved
    capture('frame_connected', p)


def install_finished(kind, ok, seconds=None, error=None, diagnose=True, **props):
    """kind: apk, flatpak, steam, title or web. props must already be public (no file names).
    diagnose=False when the error is reported as a diagnostic elsewhere."""
    p = {'kind': kind, 'ok': bool(ok), **{k: v for k, v in props.items() if v is not None}}
    if seconds is not None:
        p['seconds'] = round(seconds, 1)
    if error is not None:
        p['error_category'], code = categorize(error)
        if code:
            p['installer_code'] = code
    elif not ok:
        p['error_category'] = 'other'
    capture('install_finished', p)
    if error is not None and not ok and diagnose:
        diagnostic(f'{kind} install failed', error)


def link_failed(error):
    """Whether an ssh helper marked this error (frame_host.link_failure) as ssh failing to reach the
    headset: the error itself, or one it was re-raised from (`raise Failure(...) from e`)."""
    for _ in range(10):  # a cause chain is short; never loop on a cycle
        if error is None:
            return False
        if getattr(error, 'frame_link_failed', False):
            return True
        error = getattr(error, '__cause__', None)
    return False


def diagnostic(where, error, tb=None):
    """An error for the opt-in diagnostics level: scrubbed text, and a traceback if there is one."""
    if not enabled('diagnostics'):
        return
    message = scrub(error)
    category = categorize(error)[0]
    now = time.time()
    with _lock:
        if category in EXPECTED_CATEGORIES and link_failed(error):
            fingerprint = f'expected|{category}'
            if fingerprint in _seen_errors:
                return
        else:
            fingerprint = f'{where}|{message[:120]}'
            if now - _seen_errors.get(fingerprint, 0) < REPEAT_WINDOW:
                return
        _seen_errors[fingerprint] = now
    exc_type = type(error).__name__ if isinstance(error, BaseException) else 'Error'
    frames = []
    if tb is None and isinstance(error, BaseException):
        tb = error.__traceback__
    for fs in traceback.extract_tb(tb) if tb else []:
        frames.append({'filename': os.path.basename(fs.filename), 'lineno': fs.lineno, 'function': fs.name,
                       'in_app': True, 'platform': 'python'})
    capture('$exception', {'$exception_list': [{'type': exc_type, 'value': message,
                                                'mechanism': {'handled': True, 'type': 'generic'},
                                                'stacktrace': {'type': 'raw', 'frames': frames[-30:]}}],
                           '$exception_type': exc_type, '$exception_message': message,
                           'where': scrub(where, 200), 'error_category': category},
            level='diagnostics')


COMPAT_FIELDS = ('package', 'version', 'result', 'rating', 'notes', 'via', 'date', 'steamos', 'lepton',
                 'runtime', 'label', 'source', 'id')


def compat_report(report):
    """A compatibility report for the shared database (compat level only). Free text is
    scrubbed; the source is kept only as F-Droid or a public download host."""
    if not report.get('id') or not enabled('compat'):
        return False
    p = {k: report.get(k) for k in COMPAT_FIELDS if report.get(k) not in (None, '')}
    for k, n in (('notes', 1000), ('label', 120), ('version', 80)):
        if k in p:
            p[k] = scrub(p[k], n)
    src = str(p.pop('source', '') or '')
    if src in ('F-Droid', 'meta-store'):  # fixed source tags, never a person's file name
        p['source'] = src
    elif src.startswith(('http://', 'https://')) and _scrub_url(src) != '<url>':
        p['source'] = _scrub_url(src)
    return capture('compat_report', p, level='compat')


def backfill_compat():
    """On opting in, share the reports this computer kept before (not ones already sent or queued)."""
    try:
        import frame_compat_db
        if frame_compat_db.shared():
            return 0  # the maintainer's copy writes to the database directly
        done = set(settings()['compat_sent'])
        done |= {e['properties'].get('id') for e in _read_lines(OUTBOX) if e.get('event') == 'compat_report'}
        n = 0
        for r in frame_compat_db._outbox():
            if r.get('id') not in done and compat_report(r):
                n += 1
        return n
    except Exception:
        return 0


# ---- the outbox -----------------------------------------------------------------

def _read_lines(path):
    try:
        with open(path) as f:
            out = []
            for line in f:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
            return out
    except OSError:
        return []


def _write_lines(path, rows):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + '.tmp')
    with open(tmp, 'w') as f:
        f.writelines(json.dumps(r, ensure_ascii=False) + '\n' for r in rows)
    os.replace(tmp, path)


def _drop_unwanted(s):
    """Unsent events whose level is now off never leave the computer."""
    keep = {level: s[level] for level in LEVELS}
    rows = _read_lines(OUTBOX)
    kept = [e for e in rows if keep.get(e.get('properties', {}).get('level'), False)]
    if len(kept) != len(rows):
        _write_lines(OUTBOX, kept)


def post(batch, timeout=20):
    """Send events to PostHog now. Raises SendError if they weren't accepted."""
    cfg = config()
    if not cfg['key']:
        raise SendError('no PostHog project key in this build')
    for e in batch:  # also events queued by versions that didn't add the placeholder address
        e.setdefault('properties', {})['$ip'] = '0.0.0.0'
    body = json.dumps({'api_key': cfg['key'], 'batch': batch}).encode()
    req = urllib.request.Request(cfg['host'] + '/batch/', data=body, method='POST',
                                 headers={'content-type': 'application/json',
                                          'user-agent': f'FrameControl/{app_version()}'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
    except urllib.error.HTTPError as e:
        e.close()
        raise SendError(f'PostHog said HTTP {e.code}')
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SendError(f"couldn't reach PostHog: {e}")


def record_sent(events):
    """Add events sent outside the outbox to the log the page shows."""
    with _lock:
        _write_lines(SENT, (_read_lines(SENT) + list(events))[-SENT_KEEP:])


class SendError(RuntimeError):
    pass


def flush(timeout=20):
    """Send what's queued. Returns how many were sent; on failure they stay queued."""
    with _send_lock:
        if blocked() or not settings()['notice_shown']:
            return 0
        with _lock:
            s = settings()
            _drop_unwanted(s)
            batch = _read_lines(OUTBOX)[:100]
        if not batch:
            return 0
        if not _notice_current(s):  # what a newer notice added stays here until it's been seen
            for e in batch:
                e['properties'].pop('country', None)
        try:
            post(batch, timeout)
        except SendError:
            return 0
        sent_ids = {e['uuid'] for e in batch}
        with _lock:
            _write_lines(OUTBOX, [e for e in _read_lines(OUTBOX) if e.get('uuid') not in sent_ids])
            _write_lines(SENT, (_read_lines(SENT) + batch)[-SENT_KEEP:])
            compat = [e['properties'].get('id') for e in batch if e.get('event') == 'compat_report']
            if compat:  # remembered only once PostHog has them, so an opt-out before sending can't lose them
                s = settings()
                s['compat_sent'] = (s['compat_sent'] + compat)[-5000:]
                _save(s)
        return len(batch)


def start():
    """Record this start and send in the background from now on."""
    global _flusher
    try:
        app_started()
    except Exception:
        pass
    if _flusher:
        return

    def loop():
        while True:
            try:
                while flush() == 100:  # a full batch: there may be more
                    pass
            except Exception:
                pass
            _wake.wait(FLUSH_EVERY)
            _wake.clear()

    _flusher = threading.Thread(target=loop, name='telemetry', daemon=True)
    _flusher.start()


def wake():
    _wake.set()
