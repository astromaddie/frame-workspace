"""Import from my Quest: copy games you own off your own Meta Quest onto the Frame.

The Quest is plugged into this computer by USB with developer mode on; its own
adb copies the APK (and split APKs), the OBB folder and, if asked, the external
app-data folder into a per-user cache. The APK is checked before anything goes
to the Frame, then installed through frame_android.install like any other APK,
and the cache is removed. Games that check their Meta store licence
(libovrplatformloader.so) are refused: that check needs Meta's services, and
Frame Control does not remove it or work around it. See docs/quest-import.md.

Python stdlib only. CLI: python3 ui/frame_quest.py
  devices | list [--serial SERIAL] | import PACKAGE [--serial SERIAL] [--data]
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

ENTITLEMENT = ("{label} checks your Meta Quest store licence with Meta's Platform SDK "
               "(libovrplatformloader.so). That check needs Meta's services, which only exist on Meta "
               "headsets, so the game would quit on the Frame. Frame Control won't remove or work around "
               "the check. Ask the developer for a build without it, or look for the game on Steam or itch.io.")
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
    """Refuse what can't or mustn't run on the Frame, before anything is copied to it."""
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
    if any(n.rsplit('/', 1)[-1] == 'libovrplatformloader.so' for n in names):
        raise QuestError(ENTITLEMENT.format(label=label))
    abis = sorted({n.split('/')[1] for n in names if n.startswith('lib/') and n.count('/') >= 2})
    if abis and 'arm64-v8a' not in abis:
        raise QuestError(f"{label} is a 32-bit-only build ({', '.join(abis)}). The Frame runs 64-bit ARM "
                         "(arm64-v8a) apps only; older Quest games are often 32-bit. Ask the developer "
                         "whether a 64-bit build exists.")
    android.check_installable(dict(info, abis=abis))  # Android version
    if splits:
        android.split_entries(base, splits)  # a split APK Frame Control can't merge fails now
    warnings = []
    openxr = 'lib/arm64-v8a/libopenxr_loader.so' in names
    if 'lib/arm64-v8a/libvrapi.so' in names and not openxr:
        warnings.append("Uses Meta's legacy VrApi and no OpenXR. The Frame doesn't have VrApi, so it "
                        "will install but won't run.")
    return {'package': info['package'], 'label': label, 'version': info['version'], 'abis': abis,
            'vr': bool(info.get('vr') or openxr), 'warnings': warnings}


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


def push_data(meta, folder, work):
    """Copy a pulled /sdcard/Android/data/<package> folder into the running instance."""
    import frame_android_data
    pkg = meta['package']
    archive = Path(work) / 'data.tar'
    count = 0
    with tarfile.open(str(archive), 'w', format=tarfile.GNU_FORMAT) as tar:
        for path in sorted(Path(folder).rglob('*')):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                continue  # regular files and folders only
            info = tar.gettarinfo(str(path), arcname=path.relative_to(folder).as_posix())
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            if path.is_file():
                with path.open('rb') as f:
                    tar.addfile(info, f)
                count += 1
            else:
                tar.addfile(info)
    container = f"lepton-steamlaunch-{int(meta['instance'])}"
    script = f'set -eu; mkdir -p {DATA}/{pkg}; cd {DATA}/{pkg}; tar -x -o -f -'
    with archive.open('rb') as src:
        frame_android_data._stream(shlex.join(['podman', 'exec', '-i', container, '/system/bin/sh', '-c', script]),
                                   src=src)
    return count


def import_game(package_name, serial=None, data=False, progress=None):
    """Copy package_name from the Quest to the Frame. progress(stage, percent)."""
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
            return _import(quest['serial'], app, data, work, report)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    finally:
        _import_lock.release()


def _import(serial, app, data, work, report):
    pkg = app['package']
    # The APKs, a merged copy of a split APK and the install's patched copy, then the data.
    need = _remote_sizes(serial, app['apks']) * 3 + (app['obb'] or 0) + ((app['data'] or 0) if data else 0)
    free = shutil.disk_usage(str(work)).free
    if need > free:
        raise QuestError(f'Importing {pkg} needs about {need / 1024**3:.1f} GB free on this computer '
                         f'({free / 1024**3:.1f} GB available in {work.parent})')
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
    # 2. Game data.
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
    # 3. The Frame.
    report('Installing on the Frame', 60)
    meta = android.install(str(base), splits=[str(p) for p in splits], source='Meta Quest (USB)')
    copied_obb, copied_files = [], 0
    if obb_files or data_folder:
        report('Starting it on the Frame to add its game data', 75)
        android.launch(pkg)
        try:
            _wait_ready(meta)
            if obb_files:
                report('Copying game data (OBB) to the Frame', 80)
                copied_obb = android.install_obb(pkg, [str(p) for p in obb_files])['obb']
            if data_folder:
                report('Copying app files to the Frame', 92)
                copied_files = push_data(meta, data_folder, work)
        except android.FrameError as e:
            raise QuestError(f"Installed {meta['label']}, but copying its game data failed: {e}. "
                             "Run Import again to retry.") from e
        finally:
            try:
                android.stop(pkg)  # the next start reads the new data from the beginning
            except android.FrameError:
                pass
    report('Done', 100)
    message = f"Imported {meta['label']} from your Quest. It's in the Steam library."
    if copied_obb:
        message += f' Added {len(copied_obb)} OBB file{"s" if len(copied_obb) != 1 else ""}.'
    if copied_files:
        message += f' Copied {copied_files} app file{"s" if copied_files != 1 else ""}.'
    note = android.layer_note(meta)
    return {'message': ' '.join([message, *warnings, *([note] if note else [])]), 'app': meta,
            'obb': copied_obb, 'files': copied_files, 'warnings': warnings, 'split': bool(splits)}


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
                raise QuestError('usage: import PACKAGE [--serial SERIAL] [--data]')
            r = import_game(names[0], serial, data='--data' in args,
                            progress=lambda stage, percent=None: print(f'{percent:>3}% {stage}' if percent is not None
                                                                       else stage, file=sys.stderr))
        else:
            sys.exit(__doc__)
    except (android.FrameError, OSError) as e:
        sys.exit(f'error: {e}')
    print(json.dumps(r, indent=1))


if __name__ == '__main__':
    main()
