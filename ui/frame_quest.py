"""Import from my Quest: copy games you own off your own Meta Quest onto the Frame.

The Quest is plugged into this computer by USB with developer mode on; its own
adb copies the APK (and split APKs) into a per-user cache. The APK is checked
before anything goes to the Frame, then installed through frame_android.install
like any other APK. Game data (the OBB folder and, if asked, the external
app-data folder) is copied by a second import while the person has the game
open, since only a running Lepton instance can take it; Frame Control never
starts the game itself. The cache is always removed. Games that link Meta's Platform SDK
(libovrplatformloader.so) install with a warning: linking it doesn't mean the game
enforces a licence check, but if it does the game may quit or hang on the Frame, and
Frame Control does not remove the check or work around it. See docs/quest-import.md.

Python stdlib only. CLI: python3 ui/frame_quest.py
  devices | list [--serial SERIAL] | import PACKAGE [--serial SERIAL] [--data] [--allow-downgrade]
"""
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import zipfile

import frame_android as android
import frame_host

OBB = '/sdcard/Android/obb'
DATA = '/sdcard/Android/data'
OBB_NAME = r'(main|patch)\.[0-9]+\.{}\.obb'
# Serials of adb connections that aren't a USB headset: Frame Control's own SSH tunnels
# to the Frame's Lepton instances (127.0.0.1:<port>, server.AdbTunnel) and emulators.
TUNNEL_RE = re.compile(r'^(127\.0\.0\.1|localhost|\[::1\]):\d+$')
QUEST_MAKERS = {'oculus', 'meta'}
META_APPS = ('com.oculus.', 'com.meta.', 'com.facebook.')
CACHE_TTL = 6 * 3600  # an import left behind by a crash is swept after this
ADB_TIMEOUT = 30
PULL_TIMEOUT = 3 * 3600
READY_TIMEOUT = 150
BATCH = 15  # packages per adb shell command while listing

# Linking the Platform SDK doesn't mean a game enforces the licence check (Meta's
# VRC.Quest.Security.1 has only been Recommended since 2021), so it's a warning, not a refusal.
ENTITLEMENT = ("Uses Meta's licence check (Platform SDK, libovrplatformloader.so); it may quit, hang, or run "
               "without online features on the Frame. Frame Control won't remove the check.")
VRAPI = ("Uses Meta's legacy VrApi and no OpenXR loader. The Frame doesn't have VrApi, so it may not run.")
NO_DEVICE = ("No Quest found on this computer's USB. Connect the Quest with a cable that carries data, "
             "put the headset on and allow USB debugging. Developer mode must be on first: it needs a Meta "
             "developer organisation on a verified Meta account (developers.meta.com), then Developer Mode "
             "switched on for the headset in the Meta Horizon phone app.")
UNAUTHORISED = ("The Quest hasn't allowed this computer yet. Put the headset on and accept the \"Allow USB "
                "debugging\" prompt (tick Always allow from this computer), then try again. No prompt? Unplug "
                "and replug the cable, or switch developer mode off and on.")
NO_PERMISSIONS = ("This computer sees the Quest but isn't allowed to open it over USB. On Linux, add a udev "
                  "rule for Meta's USB vendor id 2833 (see docs/quest-import.md), then replug it.")
OFFLINE = "adb reports the Quest offline. Unplug and replug the cable, or restart the headset."
GONE = "The Quest was disconnected. Plug it back in, put it on, and try again."


class QuestError(android.FrameError):
    pass


def _adb_cmd():
    try:
        return [frame_host.adb()]
    except frame_host.HostError as e:
        raise QuestError(str(e)) from e


def _friendly(out):
    low = out.lower()
    if 'unauthorized' in low:
        return UNAUTHORISED
    if 'no permissions' in low or 'insufficient permissions' in low:
        return NO_PERMISSIONS
    if 'device offline' in low:
        return OFFLINE
    if 'no devices/emulators found' in low or re.search(r"device '[^']*' not found", low):
        return GONE
    return None


def adb(*args, serial=None, timeout=ADB_TIMEOUT):
    """Run this computer's adb (the one Frame Control already finds) and return stdout."""
    cmd = _adb_cmd() + (['-s', serial] if serial else []) + list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, text=True, errors='replace',
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        raise QuestError(f'adb {args[0]} took more than {timeout} s; check the Quest is still connected')
    except OSError as e:
        raise QuestError(f'could not run adb: {e}')
    if r.returncode != 0:
        out = (r.stderr or '').strip() or (r.stdout or '').strip()
        raise QuestError(_friendly(out) or out[-600:] or f'adb exited {r.returncode}')
    return r.stdout


# ---- Finding the Quest ------------------------------------------------------

def _parse_devices(out):
    found = []
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith('List of devices') or line.startswith('*'):
            continue
        parts = line.split(None, 1)
        serial, rest = parts[0], (parts[1] if len(parts) > 1 else '')
        state = 'no permissions' if rest.startswith('no permissions') else (rest.split() or ['unknown'])[0]
        fields = dict(t.split(':', 1) for t in rest.split() if re.match(r'^[a-z_]+:', t))
        found.append({'serial': serial, 'state': state, 'usb': 'usb' in fields,
                      'model': fields.get('model', '').replace('_', ' '), 'manufacturer': ''})
    return found


def devices():
    """Every adb device on this computer, each with a kind: quest, frame, android (another
    Android device), frame-tunnel (Frame Control's own SSH-tunnelled connection to a Lepton
    instance, never touched here), emulator, or unknown (not authorised, offline...)."""
    found = _parse_devices(adb('devices', '-l', timeout=15))
    for d in found:
        if TUNNEL_RE.match(d['serial']):
            d['kind'] = 'frame-tunnel'
        elif d['serial'].startswith('emulator-'):
            d['kind'] = 'emulator'
        elif d['state'] != 'device':
            d['kind'] = 'unknown'
        else:
            try:
                props = adb('shell', 'getprop ro.product.manufacturer; getprop ro.product.model',
                            serial=d['serial'], timeout=15).splitlines() + ['', '']
            except QuestError:
                d['kind'] = 'unknown'
                continue
            d['manufacturer'], d['model'] = props[0].strip(), props[1].strip() or d['model']
            maker = d['manufacturer'].lower()
            d['kind'] = ('quest' if maker in QUEST_MAKERS or 'quest' in d['model'].lower()
                         else 'frame' if maker == 'valve' else 'android')
    return found


def find_quest(serial=None):
    """The one connected, authorised Quest, or a QuestError saying what to do."""
    found = devices()
    if serial:
        d = next((d for d in found if d['serial'] == serial), None)
        if not d:
            raise QuestError(f'No adb device {serial} on this computer. ' + NO_DEVICE)
        if d['kind'] == 'quest':
            return d
        state = {'unauthorized': UNAUTHORISED, 'no permissions': NO_PERMISSIONS, 'offline': OFFLINE}.get(d['state'])
        what = ' '.join(x for x in (d['manufacturer'] or d['kind'], d['model']) if x)
        raise QuestError(state or f"{serial} isn't a Meta Quest ({what}).")
    quests = [d for d in found if d['kind'] == 'quest']
    if len(quests) == 1:
        return quests[0]
    if quests:
        raise QuestError('Several Quests are connected: ' + ', '.join(f"{d['model']} ({d['serial']})" for d in quests)
                         + '. Choose one, or unplug the others.')
    states = {d['state'] for d in found if d['kind'] == 'unknown'}
    for state, message in (('unauthorized', UNAUTHORISED), ('no permissions', NO_PERMISSIONS), ('offline', OFFLINE)):
        if state in states:
            raise QuestError(message)
    others = [d for d in found if d['kind'] in ('android', 'frame')]
    if others:
        raise QuestError(NO_DEVICE + ' (adb sees ' + ', '.join(
            f"{d['manufacturer']} {d['model']}".strip() for d in others) + ", which isn't a Quest.)")
    raise QuestError(NO_DEVICE)


# ---- What's on it -------------------------------------------------------------

def _package_script(packages):
    parts = []
    for p in packages:
        parts += [f'echo @@{p}', f'pm path {p}', f'du -sk {OBB}/{p} 2>/dev/null', f'du -sk {DATA}/{p} 2>/dev/null']
    return '; '.join(parts + ['true'])


def _parse_packages(out, packages):
    apps, current = {}, None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith('@@') and line[2:] in packages:
            current = apps[line[2:]] = {'package': line[2:], 'apks': [], 'obb': None, 'data': None}
        elif current is None or not line:
            continue
        elif line.startswith('package:'):
            current['apks'].append(line[len('package:'):])
        else:
            size, _, where = line.partition('\t') if '\t' in line else line.partition(' ')
            if size.isdigit():
                key = 'obb' if where.strip().startswith(OBB + '/') else 'data' if where.strip().startswith(DATA + '/') else None
                if key:
                    current[key] = int(size) * 1024
    result = []
    for p in packages:
        app = apps.get(p)
        if not app or not app['apks']:
            continue  # removed while listing
        app['split'] = len(app['apks']) > 1
        app['meta'] = p.startswith(META_APPS)
        result.append(app)
    return result


def packages(serial):
    """Third-party apps on the Quest: APK paths (several means a split APK), and the size
    in bytes of the OBB and external data folders (None when there is none)."""
    out = adb('shell', 'pm list packages -3', serial=serial)
    names = sorted({l.split(':', 1)[1].strip() for l in out.splitlines() if l.startswith('package:')})
    names = [n for n in names if android.PKG_RE.match(n)]
    apps = []
    for i in range(0, len(names), BATCH):
        batch = names[i:i + BATCH]
        apps += _parse_packages(adb('shell', _package_script(batch), serial=serial, timeout=120), batch)
    return apps


def package(serial, pkg):
    apps = _parse_packages(adb('shell', _package_script([pkg]), serial=serial, timeout=60), [pkg])
    if not apps:
        raise QuestError(f"{pkg} isn't installed on the Quest")
    return apps[0]


def status(serial=None, with_apps=True):
    """For the page: the Quest (or why there isn't one), what else adb sees, and its apps."""
    try:
        found = devices()
    except QuestError as e:
        return {'quest': None, 'devices': [], 'guidance': str(e), 'apps': []}
    try:
        quest = find_quest(serial)
    except QuestError as e:
        return {'quest': None, 'devices': found, 'guidance': str(e), 'apps': []}
    return {'quest': quest, 'devices': found, 'guidance': None,
            'apps': packages(quest['serial']) if with_apps else []}


# ---- Checking what was copied ---------------------------------------------------

def inspect(package_name, base, splits=()):
    """Refuse what can't run on the Frame (32-bit only, Android newer than Lepton's, splits
    that can't be merged), before anything is copied to it; warn about what may not run."""
    names = set()
    for path in [base, *splits]:
        try:
            with zipfile.ZipFile(path) as z:
                names |= set(z.namelist())
        except (zipfile.BadZipFile, OSError) as e:
            raise QuestError(f'{os.path.basename(path)} copied from the Quest is not a readable APK: {e}')
    info = android.apk_info(base)
    label = info['label']
    if info['package'] != package_name:
        raise QuestError(f"The APK copied for {package_name} says it is {info['package']}; not importing it")
    platform_sdk = any(n.rsplit('/', 1)[-1] == 'libovrplatformloader.so' for n in names)
    abis = sorted({n.split('/')[1] for n in names if n.startswith('lib/') and n.count('/') >= 2})
    if abis and 'arm64-v8a' not in abis:
        raise QuestError(f"{label} is a 32-bit-only build ({', '.join(abis)}). The Frame runs 64-bit ARM "
                         "(arm64-v8a) apps only; older Quest games are often 32-bit. Ask the developer "
                         "whether a 64-bit build exists.")
    android.check_installable(dict(info, abis=abis))  # Android version
    if splits:
        android.split_entries(base, splits)  # a split APK Frame Control can't merge fails now
    warnings = [ENTITLEMENT] if platform_sdk else []
    openxr = 'lib/arm64-v8a/libopenxr_loader.so' in names
    if 'lib/arm64-v8a/libvrapi.so' in names and not openxr:
        warnings.append(VRAPI)
    return {'package': info['package'], 'label': label, 'version': info['version'],
            'version_code': info.get('version_code'), 'abis': abis,
            'vr': bool(info.get('vr') or openxr), 'platform_sdk': platform_sdk, 'warnings': warnings}


# ---- Importing ------------------------------------------------------------------

_import_lock = threading.Lock()
_sleep = time.sleep


def cache_root():
    return Path(frame_host.cache_dir('quest-import'))


def sweep_cache(now=None):
    """Remove imports a crash left behind (older than CACHE_TTL)."""
    root, now = cache_root(), now or time.time()
    if root.is_dir():
        for d in root.iterdir():
            try:
                if now - d.stat().st_mtime > CACHE_TTL:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                pass


def _safe(name):
    return re.sub(r'[^A-Za-z0-9._-]', '_', name)[:120] or 'part.apk'


def _remote_sizes(serial, paths):
    out = adb('shell', 'stat -c %s ' + ' '.join(shlex.quote(p) for p in paths), serial=serial)
    return sum(int(x) for x in out.split() if x.isdigit())


def _pull(serial, remote, local):
    adb('pull', remote, str(local), serial=serial, timeout=PULL_TIMEOUT)


def _wait_ready(meta):
    """Until the app's Lepton instance has finished booting Android."""
    container = f"lepton-steamlaunch-{int(meta['instance'])}"
    deadline = time.monotonic() + READY_TIMEOUT
    while time.monotonic() < deadline:
        out = android.ssh(f"podman exec {container} /system/bin/sh -c 'getprop sys.boot_completed' "
                          "2>/dev/null || true", timeout=30)
        if out.strip() == '1':
            return
        _sleep(3)
    raise QuestError(f"{meta['label']} didn't start on the Frame within {READY_TIMEOUT // 60} minutes, so its "
                     "game data couldn't be added. Check it opens from the Steam library, then import again.")


def push_data(meta, folder):
    """Stream a pulled /sdcard/Android/data/<package> folder into the running instance as a
    tar, written straight into the SSH transfer: nothing is staged on this computer."""
    import frame_android_data
    pkg = meta['package']
    container = f"lepton-steamlaunch-{int(meta['instance'])}"
    script = f'set -eu; mkdir -p {DATA}/{pkg}; cd {DATA}/{pkg}; tar -x -o -f -'
    read_end, write_end = os.pipe()
    state = {'count': 0, 'error': None}

    def write():
        try:
            with os.fdopen(write_end, 'wb') as out, \
                    tarfile.open(fileobj=out, mode='w|', format=tarfile.GNU_FORMAT) as tar:
                for path in sorted(Path(folder).rglob('*')):
                    if path.is_symlink() or not (path.is_file() or path.is_dir()):
                        continue  # regular files and folders only
                    info = tar.gettarinfo(str(path), arcname=path.relative_to(folder).as_posix())
                    info.uid = info.gid = 0
                    info.uname = info.gname = ''
                    if path.is_file():
                        with path.open('rb') as f:
                            tar.addfile(info, f)
                        state['count'] += 1
                    else:
                        tar.addfile(info)
        except (OSError, tarfile.TarError, ValueError) as e:  # ValueError: the reader closed first
            state['error'] = e

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    try:
        with os.fdopen(read_end, 'rb') as src:
            frame_android_data._stream(shlex.join(['podman', 'exec', '-i', container, '/system/bin/sh', '-c', script]),
                                       src=src)
    finally:
        writer.join(60)
    if state['error']:
        raise QuestError(f'could not read the app files copied from the Quest: {state["error"]}')
    return state['count']


def space_needed(apk_bytes, merged_bytes, obb_bytes, data_bytes):
    """Bytes an import needs free on this computer: the APKs as copied, a merged APK and the
    install's patched copy of it (each up to merged_bytes), the OBB folder and the
    Android/data folder (streamed to the Frame, never staged twice)."""
    return apk_bytes + 2 * merged_bytes + obb_bytes + data_bytes


def _check_space(work, need, pkg):
    free = shutil.disk_usage(str(work)).free
    if need > free:
        raise QuestError(f'Importing {pkg} needs about {need / 1024**3:.1f} GB free on this computer '
                         f'({free / 1024**3:.1f} GB available in {work.parent})')


def import_game(package_name, serial=None, data=False, progress=None, allow_downgrade=False):
    """Copy package_name from the Quest to the Frame. progress(stage, percent).

    Frame Control never starts the game itself. Its game data (OBB, and Android/data if
    asked) can only be written into a running Lepton instance, and there is no way to
    boot one without its game, so that's a second import: the person starts the game,
    imports again, and Frame Control copies the data in and closes the game.

    allow_downgrade: replace a newer version on the Frame with the Quest's older one."""
    if not android.PKG_RE.match(package_name or ''):
        raise QuestError('invalid package name')
    report = progress or (lambda stage, percent=None: None)
    if not _import_lock.acquire(blocking=False):
        raise QuestError('Another Quest import is running; wait for it to finish')
    try:
        report('Looking for your Quest', 1)
        quest = find_quest(serial)
        app = package(quest['serial'], package_name)
        sweep_cache()
        work = cache_root() / f'{package_name}-{secrets.token_hex(4)}'
        work.mkdir(parents=True)
        try:
            return _import(quest['serial'], app, data, work, report, allow_downgrade)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    finally:
        _import_lock.release()


def _running(pkg):
    # strict: a failed `podman ps` raises, never reads as "not running".
    return f'lepton-steamlaunch-{android.instance_id(pkg)}' in android.running_instances(strict=True)


def _installed_code(pkg, meta):
    """The installed version code: from the install record, else (installs made before
    records kept it) read from the installed app.apk on the Frame; None if unknown."""
    code = (meta or {}).get('version_code')
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    return android.installed_version_code(pkg) if meta else None


def _version(name, code):
    return ' '.join(x for x in (f'version {name}' if name else '', f'code {code}' if code is not None else 'unknown code') if x)


def _check_downgrade(existing, checked, allow):
    """Refuse to replace a newer install on the Frame with the Quest's older copy."""
    if not existing or allow:
        return
    have, new = _installed_code(existing['package'], existing), checked['version_code']
    if have is None or new is None:
        raise QuestError(f"Frame Control can't tell which version of {checked['label']} is on the Frame, so it "
                         "won't replace it in case that one is newer. Allow replacing a newer version (or "
                         "--allow-downgrade) to install your Quest's copy anyway.")
    if int(new) < int(have):
        raise QuestError(f"The Frame has a newer {checked['label']} ({_version(existing.get('version'), have)}) than "
                         f"your Quest ({_version(checked['version'], new)}). Frame Control won't replace it with "
                         "an older version unless you allow it (Allow replacing a newer version, or "
                         "--allow-downgrade).")


def _check_target(pkg, meta, checked):
    """The install record of the game open on the Frame, if it is this package, in its own
    instance, at the Quest's exact version code; else a QuestError."""
    label = checked['label']
    if (not meta or meta.get('package') != pkg or android._int(meta.get('instance')) != android.instance_id(pkg)):
        raise QuestError(f"{label} is open on the Frame, but Frame Control has no matching install record for "
                         'it. Close it, then import again to install it.')
    have, new = _installed_code(pkg, meta), checked['version_code']
    if have is None or new is None or int(have) != int(new):
        raise QuestError(f"{label} is open on the Frame with a different version (Frame: "
                         f"{_version(meta.get('version'), have)}; Quest: {_version(checked['version'], new)}). "
                         "Close it, then import again to install the Quest's version.")
    return meta


def _check_android_package(meta, code):
    """The package Android itself has installed in the instance is this one, at this version."""
    pkg = meta['package']
    container = f"lepton-steamlaunch-{int(meta['instance'])}"
    out = android.ssh(f"podman exec {container} /system/bin/sh -c 'dumpsys package {pkg}' 2>/dev/null || true",
                      timeout=60)
    # Only this package's own block (dumpsys can list other packages too).
    block = re.search(r'^\s*Package \[' + re.escape(pkg) + r'\] \(.*?(?=^\s*Package \[|\Z)', out, re.M | re.S)
    found = re.search(r'\bversionCode=(\d+)', block.group(0)) if block else None
    if not found:
        raise QuestError(f'Android in {container} does not have {pkg} installed')
    if int(found.group(1)) != int(code):
        raise QuestError(f'Android in {container} has {pkg} version code {found.group(1)}, not the '
                         f"Quest's {code}")


def _close(pkg):
    """Stop the game and confirm its instance is gone: (closed, why not)."""
    try:
        android.stop(pkg)
        for _ in range(6):
            if not _running(pkg):
                return True, None
            _sleep(2)
        return False, 'it is still running'
    except Exception as e:  # never replaces the error that led here; unconfirmed counts as not closed
        return False, str(e) or type(e).__name__


def _import(serial, app, data, work, report, allow_downgrade=False):
    pkg = app['package']
    obb_bytes = app['obb'] or 0
    data_bytes = (app['data'] or 0) if data else 0
    apk_bytes = _remote_sizes(serial, app['apks'])
    _check_space(work, space_needed(apk_bytes, apk_bytes, obb_bytes, data_bytes), pkg)
    # 1. The APK (and its splits), checked before anything else is copied.
    local = []
    for i, remote in enumerate(app['apks']):
        report(f'Copying the app from your Quest ({i + 1} of {len(app["apks"])})', 5 + 20 * i // len(app['apks']))
        name = 'base.apk' if os.path.basename(remote) == 'base.apk' else f'{i:02d}-{_safe(os.path.basename(remote))}'
        _pull(serial, remote, work / name)
        if not (work / name).is_file():
            raise QuestError(f'adb did not copy {remote} from the Quest')
        local.append(work / name)
    base = next((p for p in local if p.name == 'base.apk'), local[0])
    splits = [p for p in local if p != base]
    report('Checking it can run on the Frame', 27)
    checked = inspect(pkg, str(base), [str(p) for p in splits])
    warnings = list(checked['warnings'])
    label = checked['label']
    # A merge stores what splits compressed: count it uncompressed, now the files are here.
    merged = base.stat().st_size + android.split_size([str(p) for p in splits])
    _check_space(work, space_needed(0, merged, obb_bytes, data_bytes), pkg)
    wants_data = app['obb'] is not None or bool(data and app['data'] is not None)
    if not _running(pkg):
        # 2a. Install. The game is never started here: see import_game. Checked and installed
        # under the shared install lock, so nothing installs or removes it in between.
        report('Installing on the Frame', 60)
        with android._install_lock:
            if _running(pkg):
                raise QuestError(f'{label} was started on the Frame during the import. Close it, then import again.')
            _check_downgrade(android.read_meta(pkg), checked, allow_downgrade)
            meta = android.install(str(base), splits=[str(p) for p in splits], source='Meta Quest (USB)')
        report('Done', 100)
        message = f"Imported {meta['label']} from your Quest. It's in the Steam library."
        if wants_data:
            what = ' and '.join(x for x in ('its OBB game data' if app['obb'] is not None else '',
                                            'its Android/data files' if data and app['data'] is not None else '') if x)
            message += (f" To add {what}, start {meta['label']} from the Steam library, leave it on its first "
                        "screen, then import it again here: Frame Control copies the data in and closes the game.")
        note = android.layer_note(meta)
        return {'message': ' '.join([message, *warnings, *([note] if note else [])]), 'app': meta, 'obb': [],
                'files': 0, 'warnings': warnings, 'split': bool(splits), 'needs_game_data': wants_data,
                'closed': None}
    # 2b. The game is open on the Frame: add its data to exactly this install, never replacing it.
    _check_target(pkg, android.read_meta(pkg), checked)  # early, before copying the data off the Quest
    if not wants_data:
        raise QuestError(f"{label} is already installed and open on the Frame, and has no game data to add. "
                         'Close it first to install it again.')
    obb_files = []
    if app['obb'] is not None:
        report('Copying game data (OBB) from your Quest', 30)
        (work / 'obb').mkdir()
        _pull(serial, f'{OBB}/{pkg}', work / 'obb')
        folder = work / 'obb' / pkg
        files = sorted(p for p in folder.iterdir() if p.is_file()) if folder.is_dir() else []
        obb_files = [p for p in files if re.fullmatch(OBB_NAME.format(re.escape(pkg)), p.name)]
        skipped = [p.name for p in files if p not in obb_files]
        if skipped:
            warnings.append('Not copied, not standard OBB file names: ' + ', '.join(skipped))
    data_folder = None
    if data and app['data'] is not None:
        report('Copying app files from your Quest', 50)
        (work / 'data').mkdir()
        _pull(serial, f'{DATA}/{pkg}', work / 'data')
        data_folder = work / 'data' / pkg if (work / 'data' / pkg).is_dir() else None
    result = add_game_data(pkg, checked, [str(p) for p in obb_files], data_folder, report, warnings)
    result['split'] = bool(splits)
    return result


def add_game_data(pkg, checked, obb_files, data_folder, report, warnings=(), source='your Quest'):
    """Copy game data into the game open on the Frame, then close it: the second step of a
    two-step install (Import from my Quest, and My Meta library). checked is inspect()'s
    result for the APK the data belongs to; obb_files are local OBB paths, data_folder a
    local Android/data/<package> folder or None. Raises QuestError, after closing the game,
    if anything fails."""
    warnings = list(warnings)
    label = checked['label']
    copied_obb, copied_files, failure = [], 0, None
    # Fetching the data can take a long time: check the target again, and hold the shared
    # install lock (install, remove and install_obb take it too) until the game is closed.
    with android._install_lock:
        if not _running(pkg):
            raise QuestError(f'{label} was closed on the Frame during the import. Start it, then import again.')
        meta = _check_target(pkg, android.read_meta(pkg), checked)
        closed, why = None, None
        try:
            report('Waiting for Android on the Frame', 70)
            _wait_ready(meta)
            _check_android_package(meta, checked['version_code'])
            if obb_files:
                report('Copying game data (OBB) to the Frame', 80)
                copied_obb = android.install_obb(pkg, [str(p) for p in obb_files])['obb']
            if data_folder:
                report('Copying app files to the Frame', 92)
                copied_files = push_data(meta, data_folder)
        except Exception as e:  # any failure, local (OSError) or on the Frame, is reported below
            failure = e
        finally:
            # Whatever happened, even Ctrl-C part-way through a write, the game is closed so it
            # never runs on half-copied data. A failed close never hides the original error.
            interrupted = sys.exc_info()[0] is not None
            closed, why = _close(pkg)
            if interrupted and not closed:
                print(f"Frame Control couldn't close {meta['label']} on the Frame ({why}); close it from Steam "
                      'before playing.', file=sys.stderr)
    if failure:
        message = f"Copying {meta['label']}'s game data failed: {failure}."
        message += (' The game was closed; start it and import again to retry.' if closed else
                    f" Frame Control also couldn't close the game ({why}): close it from Steam, then start it "
                    'and import again.')
        raise QuestError(message) from failure
    report('Done', 100)
    message = f"Added {meta['label']}'s game data from {source}"
    message += ('; the game is closed, start it again from the Steam library.' if closed else
                f", but Frame Control couldn't close the game ({why}). Close it from Steam before playing, so it "
                'starts with the new data.')
    if copied_obb:
        message += f' Added {len(copied_obb)} OBB file{"s" if len(copied_obb) != 1 else ""}.'
    if copied_files:
        message += f' Copied {copied_files} app file{"s" if copied_files != 1 else ""}.'
    return {'message': ' '.join([message, *warnings]), 'app': meta, 'obb': copied_obb, 'files': copied_files,
            'warnings': warnings, 'split': False, 'needs_game_data': False, 'closed': closed}


def main():
    cmd, *args = sys.argv[1:] or ['help']
    serial = args[args.index('--serial') + 1] if '--serial' in args and args.index('--serial') + 1 < len(args) else None
    try:
        if cmd == 'devices':
            r = devices()
        elif cmd == 'list':
            r = packages(find_quest(serial)['serial'])
        elif cmd == 'import':
            names = [a for a in args if not a.startswith('--') and a != serial]
            if len(names) != 1:
                raise QuestError('usage: import PACKAGE [--serial SERIAL] [--data] [--allow-downgrade]')
            r = import_game(names[0], serial, data='--data' in args, allow_downgrade='--allow-downgrade' in args,
                            progress=lambda stage, percent=None: print(f'{percent:>3}% {stage}' if percent is not None
                                                                       else stage, file=sys.stderr))
        else:
            sys.exit(__doc__)
    except (android.FrameError, OSError) as e:
        sys.exit(f'error: {e}')
    print(json.dumps(r, indent=1))


if __name__ == '__main__':
    main()
