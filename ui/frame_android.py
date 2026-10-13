"""Android apps on the Frame, each in its own persistent Lepton instance.

Every APK gets ~/Applications/Android/<package>/ on the Frame with app.apk,
launch.sh (frame/android/lepton-app.sh), instance.id, meta.json and, for 2D
apps, the lepton-show-flatscreen marker; plus a non-Steam shortcut, so it shows
in the Steam library and gets its own SteamVR panel. Nothing goes through
Lepton Development, which wipes its apps on exit. See docs/apks.md.

Python stdlib only. CLI: python3 ui/frame_android.py
  install APK [SPLIT.apk ...] [--vr|--flat] [--no-xr-compat] | info APK | versions APK-or-PKG
  install-obb PKG OBB [OBB ...] | backup-data PKG ARCHIVE | restore-data PKG ARCHIVE
  refresh-art PKG|--all | patch SRC DST [--add NAME=PATH ...] | list | launch PKG | stop PKG | remove PKG | probe PKG
"""
import base64, gzip, json, os, re, shlex, shutil, struct, subprocess, sys, threading, time, zlib

import frame_apk
import frame_artwork
import frame_host
import tempfile
import zipfile
from frame_apk_vr import add_launcher_category, drop_split_required
from frame_apk_sign import repack

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRAME = os.environ.get('FRAME_ALIAS', 'frame')
TMP_PREFIX = 'frame-vr-'  # then the server's PID, so server.sweep_tmp can clear a stopped run's patched APK
APPS_DIR = 'Applications/Android'          # relative to the Frame's $HOME
COMPAT = '.local/share/Steam/steamapps/compatdata'
SHADERS = '.local/share/Steam/steamapps/shadercache'
LAUNCHER = os.path.join(ROOT, 'frame', 'android', 'lepton-app.sh')
SHORTCUTS = os.path.join(ROOT, 'frame', 'android', 'steam_shortcuts.py')
# OpenXR API layer that lets OpenXR 1.1 apps run on SteamVR's 1.0-only Android
# runtime (frame/openxr-compat, docs/vr-apks.md). Injected into VR APKs.
XR_COMPAT = os.path.join(ROOT, 'frame', 'openxr-compat')
XR_COMPAT_FILES = {
    'assets/openxr/1/api_layers/implicit.d/XrApiLayer_FRAME_compat.json': 'XrApiLayer_FRAME_compat.json',
    # Gzipped in the repo and the app; see frame/openxr-compat/build.sh for why.
    'lib/arm64-v8a/libXrApiLayer_FRAME_compat.so': 'prebuilt/arm64-v8a/libXrApiLayer_FRAME_compat.so.gz',
}
PKG_RE = re.compile(r'^[A-Za-z][\w]*(\.[A-Za-z_][\w]*)+$')
SSH_OPTS = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8']


class FrameError(RuntimeError):
    pass


class LayerMissing(FrameError):
    """The OpenXR compatibility layer's files aren't in this copy of Frame Control."""


LAYER_MISSING = ("Frame Control's OpenXR compatibility layer is missing from this copy "
                 "(frame/openxr-compat/prebuilt); reinstall Frame Control, or in a source "
                 "checkout run frame/openxr-compat/build.sh")
LAYER_MISSING_NOTE = ("Installed without the OpenXR compatibility layer, which is missing from this "
                      "copy of Frame Control; apps that need OpenXR 1.1 may not start. Reinstalling "
                      "Frame Control restores it.")


def ssh(cmd, input=None, timeout=120):
    try:
        # No inherited stdin (see server.ssh): Windows' ssh.exe would wait on it.
        feed = {'input': input} if input is not None else {'stdin': subprocess.DEVNULL}
        # The Frame writes UTF-8, whatever this computer's code page (Windows would decode it as
        # cp1252). errors='replace': stray bytes (a file name) mustn't turn a failed command, or
        # one that worked, into a UnicodeDecodeError.
        text = ({'text': True, 'encoding': 'utf-8', 'errors': 'replace'}
                if isinstance(input, str) or input is None else {})
        p = frame_host.run_ssh(['ssh', *SSH_OPTS, FRAME, cmd], capture_output=True, **feed,
                               timeout=timeout, **text)
    except subprocess.TimeoutExpired:
        raise frame_host.link_failure(FrameError(f'timed out talking to {FRAME}'))
    if p.returncode != 0:
        # Bytes input gives bytes output: decode it, so the message isn't "b'...'".
        stderr, stdout = (v if isinstance(v, str) else (v or b'').decode(errors='replace')
                          for v in (p.stderr, p.stdout))
        error = FrameError((stderr or stdout or f'ssh exited {p.returncode}').strip()[-600:])
        raise frame_host.link_failure(error) if frame_host.ssh_link_failed(p.returncode, stderr) else error
    return p.stdout


def shortcut_tool(*args, timeout=60):
    with open(SHORTCUTS) as f:
        script = f.read()
    if args and args[0] == 'render':
        with open(os.path.join(ROOT, 'frame/android/library_artwork.js')) as f:
            script = 'ART_RENDERER = ' + repr(f.read()) + '\n' + script
    return ssh('python3 - ' + ' '.join(shlex.quote(a) for a in args), input=script,
               timeout=timeout).strip()


def instance_id(pkg):
    # Stable per package, well above real Steam app ids, below 2^32. T3 Code's
    # hand-picked 2873873873 sits outside this range.
    return 2800000000 + zlib.crc32(pkg.encode()) % 70000000


def game_id(shortcut_appid):
    return (int(shortcut_appid) << 32) | 0x02000000


def apk_info(path):
    """Package, label, version, native ABIs and the best PNG icon inside the APK."""
    try:
        return frame_apk.apk_info(path)
    except frame_apk.ApkError as e:
        raise FrameError(f'{os.path.basename(path)}: {e}')


def xr_compat_files(apk_path):
    """The layer's files to add, or {} if the APK has no OpenXR loader or already has the layer."""
    with zipfile.ZipFile(apk_path) as z:
        names = set(z.namelist())
    if 'lib/arm64-v8a/libopenxr_loader.so' not in names or names & set(XR_COMPAT_FILES):
        return {}
    add = {}
    for entry, rel in XR_COMPAT_FILES.items():
        try:
            with open(os.path.join(XR_COMPAT, rel), 'rb') as f:
                data = f.read()
            add[entry] = gzip.decompress(data) if rel.endswith('.gz') else data
        except (OSError, EOFError, zlib.error):  # gzip.BadGzipFile is an OSError
            add[entry] = b''
        if not add[entry]:  # missing, unreadable (antivirus, permissions) or truncated
            raise LayerMissing(LAYER_MISSING)
    return add


def layer_note(meta):
    """The missing-layer warning for an install's completion message, or ''."""
    return LAYER_MISSING_NOTE if LAYER_MISSING_NOTE in ((meta or {}).get('vr_issues') or []) else ''


def check_installable(info):
    if info['min_sdk'] and info['min_sdk'] > 30:
        raise FrameError(f"{info['label']} needs Android API {info['min_sdk']}; Lepton is Android 11 (API 30)")
    if info['abis'] and 'arm64-v8a' not in info['abis']:
        # Sites that offer one APK per ABI (Grayjay: arm64-v8a, armeabi-v7a, x86, x86_64,
        # universal) leave the choice to the user; say which file to fetch instead.
        raise FrameError(f"{info['label']} has no arm64-v8a build ({', '.join(info['abis'])}); Lepton is 64-bit ARM only. "
                         "This file is for other devices: download the APK marked arm64-v8a "
                         "(or arm64, or universal) and install that instead")


# Installs are rare; one at a time avoids every race. Reentrant so a caller (a Quest import's
# final checks and data copy) can hold it around install_obb and install, which take it too.
_install_lock = threading.RLock()
# The slowest transfer a copy is allowed before it times out, in bytes a second (about 4 Mbit/s,
# a slow or busy Wi-Fi link). A fixed 10 minutes failed any APK over about 300 MB at that rate.
COPY_MIN_RATE = 512 * 1024


def _copy(src, dest, executable=False, timeout=600):
    """Copy a local file to the Frame: rsync where installed (not on Windows), else scp.
    timeout is the least time allowed; a large file gets longer (COPY_MIN_RATE)."""
    name = os.path.basename(src)
    try:
        timeout = max(timeout, os.path.getsize(src) / COPY_MIN_RATE)
    except OSError:
        pass  # rsync or scp says what's wrong with the file
    rsync = None if frame_host.WINDOWS else shutil.which('rsync')  # see server.push_file
    if rsync:
        cmd = ['rsync', '-a', *(['--chmod=u+x'] if executable else []),
               '-e', shlex.join(['ssh', *SSH_OPTS]), src, f'{FRAME}:{dest}']
    else:
        cmd = ['scp', *SSH_OPTS, src, f'{FRAME}:{dest}']
    try:
        frame_host.run_ssh(cmd, check=True, capture_output=True, stdin=subprocess.DEVNULL, text=True,
                           errors='replace', timeout=timeout)
    except subprocess.TimeoutExpired:
        raise FrameError(f'copying {name} to the Frame timed out')
    except subprocess.CalledProcessError as e:
        raise FrameError(f'copying {name} to the Frame failed: {(e.stderr or "").strip()[-300:]}')
    if executable and not rsync:
        ssh(f'chmod u+x {shlex.quote(dest)}')


def _shortcut_ids():
    try:
        return {int(x['appid']) for x in json.loads(shortcut_tool('list'))}
    except (ValueError, TypeError, KeyError) as e:
        raise FrameError(f'could not read the Steam shortcut list: {e}')


def _write_meta(d, meta):
    # Write then rename, so a dropped connection can't leave torn JSON behind.
    ssh(f'cat > {d}/meta.json.tmp && mv {d}/meta.json.tmp {d}/meta.json', input=json.dumps(meta, indent=1))


# Called after every install, worked or not, as fn(info, meta, error, seconds):
# info is None if the APK couldn't be read, meta None and error set if it failed.
install_hooks = []


# Split APK members that belong to the split itself, not the app: never merged.
SPLIT_OWN = re.compile(r'^(AndroidManifest\.xml|stamp-cert-sha256|META-INF/.*)$')
# What a mergeable split may add: native libraries and assets, nothing else.
SPLIT_ADDS = re.compile(r'^(lib/[^/]+/[^/]+\.so|assets/.*[^/])$')
# Manifest elements a config or install-time asset split carries. Anything else (an
# activity, service, uses-library...) belongs to a feature split, which needs its manifest.
SPLIT_TAGS = {'manifest', 'application', 'uses-sdk', 'module', 'delivery', 'install-time', 'fusing'}
# Every merged byte is held in memory while the APK is repacked and signed (as for every
# patched APK), so splits are bounded by what's actually inflated, not only the headers.
MAX_SPLIT_MEMBER = 512 * 1024**2
MAX_SPLIT_TOTAL = 1024**3


def _split_manifest(path, name):
    """(package, split name) from a split APK's manifest, refusing splits that declare
    anything the merged app would lose with that manifest."""
    with zipfile.ZipFile(path) as z:
        elements = frame_apk.manifest_elements(frame_apk._read(z, 'AndroidManifest.xml', frame_apk.MAX_MANIFEST))
    attrs = next((a for tag, a in elements if tag == 'manifest'), {})
    split = (attrs.get('split') or (None, None, None))[2]
    feature = attrs.get('isFeatureSplit')
    if feature and feature[1]:
        raise FrameError(f'{name} ({split or "split"}) is a feature split with its own code and manifest; '
                         'Frame Control can only merge splits that add native libraries or assets')
    extra = sorted({tag for tag, _ in elements if tag not in SPLIT_TAGS})
    if extra:
        raise FrameError(f'{name} ({split or "split"}) declares <{">, <".join(extra)}> in its manifest; '
                         'Frame Control can only merge splits that add native libraries or assets')
    return (attrs.get('package') or (None, None, None))[2], split


def split_entries(base, splits):
    """What merging splits into base adds: {entry name: (split path, entry name, size)}.

    Lepton runs one APK per instance, so a split app is merged into one APK. Only splits
    that add native libraries (the arm64 ABI split) or install-time assets can be: a split
    with code, components or resources (screen density, language) needs its own manifest
    or resource table, which Frame Control doesn't merge, so those are refused."""
    try:
        with zipfile.ZipFile(base) as z:
            taken = set(z.namelist())
        package = apk_info(base)['package']
        plan, total = {}, 0
        for path in splits:
            name = os.path.basename(path)
            split_pkg, split_name = _split_manifest(path, name)
            label = f'{name} ({split_name or "split"})'
            if split_pkg != package:
                raise FrameError(f'{name} is not part of {package} (it says {split_pkg or "nothing"})')
            with zipfile.ZipFile(path) as z:
                for member in z.infolist():
                    entry = member.filename
                    if entry.endswith('/') or SPLIT_OWN.match(entry):
                        continue
                    if entry == 'resources.arsc':
                        # A config split for native code may carry an empty table; any resource needs merging.
                        if not resources_empty(frame_apk._read(z, entry, frame_apk.MAX_ARSC)):
                            raise FrameError(f'{label} holds screen-density or language resources; Frame Control '
                                             'can only merge splits that add native libraries or assets')
                        continue
                    if entry.startswith('res/'):
                        raise FrameError(f'{label} holds screen-density or language resources ({entry}); Frame '
                                         'Control can only merge splits that add native libraries or assets')
                    if re.match(r'^classes\d*\.dex$', entry):
                        raise FrameError(f'{label} contains code ({entry}); Frame Control can only merge splits '
                                         'that add native libraries or assets')
                    if not SPLIT_ADDS.match(entry):
                        raise FrameError(f'{label} contains {entry}; Frame Control can only merge splits that '
                                         'add native libraries or assets')
                    if entry in taken:
                        raise FrameError(f'{name} and another part of the app both contain {entry}')
                    if member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                        raise FrameError(f'{entry} in {name} uses a compression Android does not')
                    if member.file_size > MAX_SPLIT_MEMBER:
                        raise FrameError(f'{entry} in {name} is {member.file_size / 1024**2:.0f} MB, more than '
                                         f'Frame Control merges ({MAX_SPLIT_MEMBER // 1024**2} MB)')
                    total += member.file_size
                    if total > MAX_SPLIT_TOTAL:
                        raise FrameError(f'the split APKs add more than {MAX_SPLIT_TOTAL // 1024**3} GB, more than '
                                         'Frame Control merges')
                    taken.add(entry)
                    plan[entry] = (path, entry, member.file_size)
        return plan
    except (OSError, zipfile.BadZipFile, struct.error, IndexError, frame_apk.ApkError) as e:
        raise FrameError(f'could not read the split APKs: {e}') from e


def resources_empty(data):
    """True only for a resource table that provably defines nothing: every type chunk's
    entries absent. Bags (styles, plurals) count, and anything not understood counts too."""
    def covered(start, end):
        chunks, at = [], start
        for chunk in frame_apk._chunks(data, start, end):
            if chunk[2] != at:
                return None
            chunks.append(chunk)
            at = chunk[2] + chunk[3]
        return chunks if at == end else None

    try:
        if len(data) < 12 or struct.unpack_from('<H', data, 0)[0] != 0x0002:
            return False
        top = covered(struct.unpack_from('<H', data, 2)[0], len(data))
        if top is None:
            return False
        for ctype, _, off, size in top:
            if ctype == 0x0001:  # the global string pool
                continue
            if ctype != 0x0200:
                return False
            inner = covered(off + struct.unpack_from('<H', data, off + 2)[0], off + size)
            if inner is None:
                return False
            for kind, hs, t, _ in inner:
                if kind in (0x0001, 0x0202):  # type and key names, type specs
                    continue
                if kind != 0x0201:
                    return False
                flags, count = struct.unpack_from('<xBxxI', data, t + 8)
                if flags & 0x01:  # sparse: only present entries are listed
                    if count:
                        return False
                elif flags & 0x02:
                    if any(o != 0xffff for o in struct.unpack_from(f'<{count}H', data, t + hs)):
                        return False
                elif any(o != 0xffffffff for o in struct.unpack_from(f'<{count}I', data, t + hs)):
                    return False
        return True
    except (struct.error, IndexError):
        return False


def _zip64_sizes(extra, usize, csize):
    """(uncompressed, compressed) with 0xffffffff fields taken from a ZIP64 extra field."""
    at = 0
    while at + 4 <= len(extra):
        kind, length = struct.unpack_from('<HH', extra, at)
        body = extra[at + 4:at + 4 + length]
        if kind == 0x0001:
            values, pos = [], 0
            for field in (usize, csize):
                if field == 0xffffffff:
                    if pos + 8 > len(body):
                        raise FrameError('truncated ZIP64 extra field')
                    values.append(struct.unpack_from('<Q', body, pos)[0])
                    pos += 8
                else:
                    values.append(field)
            return tuple(values)
        at += 4 + length
    if 0xffffffff in (usize, csize):
        raise FrameError('ZIP64 sizes without a ZIP64 extra field')
    return usize, csize


def _has_zip64_extra(extra):
    at = 0
    while at + 4 <= len(extra):
        kind, length = struct.unpack_from('<HH', extra, at)
        if kind == 0x0001:
            return True
        at += 4 + length
    return False


def read_member(path, member):
    """A split member's bytes, read from its raw data and checked against both headers.

    The local header must name the same file, with the same method and (unless a data
    descriptor carries them, which is then checked) the same sizes and checksum as the
    central directory; the data must end before the next member; a stored member must be
    exactly its size and a deflated stream must end exactly at the end of its data, with
    nothing after it. Output is bounded by the declared size: never trusts zipfile, which
    stops early at a smaller declared size."""
    with zipfile.ZipFile(path) as z:
        info = z.getinfo(member)
        starts = sorted({i.header_offset for i in z.infolist()} | {z.start_dir})
    name, size = f'{member} in {os.path.basename(path)}', info.file_size
    if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise FrameError(f'{name} is encrypted or uses a compression Android does not')
    if size > MAX_SPLIT_MEMBER or info.compress_size > MAX_SPLIT_MEMBER:
        raise FrameError(f'{name} is larger than Frame Control merges ({MAX_SPLIT_MEMBER // 1024**2} MB)')
    following = next((o for o in starts if o > info.header_offset), None)
    with open(path, 'rb') as raw:
        raw.seek(info.header_offset)
        header = raw.read(30)
        if len(header) != 30 or header[:4] != b'PK\3\4':
            raise FrameError(f'{name}: invalid local ZIP header')
        flags, method, crc, csize, usize, nl, el = struct.unpack_from('<2xHH4xIIIHH', header, 4)
        local_name, extra = raw.read(nl), raw.read(el)
        if local_name != info.orig_filename.encode('utf-8' if flags & 0x800 else 'cp437', 'surrogateescape'):
            raise FrameError(f'{name}: the local header names {local_name.decode("utf-8", "replace")!r}')
        if method != info.compress_type or (flags & 0x08) != (info.flag_bits & 0x08):
            raise FrameError(f'{name} does not match its header (local and central entries differ)')
        start = info.header_offset + 30 + nl + el
        compressed = raw.read(info.compress_size)
        if len(compressed) != info.compress_size:
            raise FrameError(f'{name} is truncated')
        if flags & 0x08:
            # Sizes and checksum follow the data: (signature,) crc, then sizes of 4 bytes each,
            # or 8 for ZIP64. Streaming writers use the ZIP64 form even for small files when
            # sizes weren't known up front, so the width isn't implied by the final sizes: take
            # the layout that matches the central directory and fits before the next entry,
            # preferring one that ends exactly there, then the width the local header implies.
            zip64 = 0xffffffff in (csize, usize) or _has_zip64_extra(extra)
            tail = raw.read(24)
            if len(tail) < 12:
                raise FrameError(f'{name}: truncated data descriptor')
            end = start + info.compress_size
            fits = []
            for signed in (True, False):
                if signed and tail[:4] != b'PK\7\x08':
                    continue
                for width in (20, 12):
                    body = tail[4:4 + width] if signed else tail[:width]
                    if len(body) < width:
                        continue
                    values = struct.unpack_from('<IQQ' if width == 20 else '<III', body)
                    length = width + (4 if signed else 0)
                    if values == (info.CRC, info.compress_size, size) and \
                            (following is None or end + length <= following):
                        exact = following is not None and end + length == following
                        fits.append((not exact, (width == 20) != zip64, length))
            if not fits:
                raise FrameError(f'{name} does not match its header (local and central entries differ, '
                                 'or its data descriptor is truncated)')
            crc, csize, usize = info.CRC, info.compress_size, size
            descriptor = min(fits)[2]
        else:
            usize, csize = _zip64_sizes(extra, usize, csize)
            descriptor = 0
    if (crc, csize, usize) != (info.CRC, info.compress_size, size):
        raise FrameError(f'{name} does not match its header (local and central entries differ)')
    if following is not None and start + info.compress_size + descriptor > following:
        raise FrameError(f'{name} overlaps the next ZIP entry')
    if info.compress_type == zipfile.ZIP_STORED:
        if info.compress_size != size:
            raise FrameError(f'{name} does not match its header (stored {info.compress_size} bytes, says {size})')
        data = compressed
    else:
        inflate = zlib.decompressobj(-15)
        try:
            data = inflate.decompress(compressed, size + 1)
        except zlib.error as e:
            raise FrameError(f'{name} is corrupt: {e}') from e
        if len(data) != size or not inflate.eof or inflate.unconsumed_tail or inflate.unused_data:
            raise FrameError(f'{name} does not match its header (its compressed data is not exactly '
                             f'{size} bytes with nothing after it)')
    if zlib.crc32(data) != info.CRC:
        raise FrameError(f'{name} does not match its checksum')
    return data


def merge_splits(base, splits, dst):
    """One installable APK from a base APK and its splits (see split_entries), re-signed
    with Frame Control's key, without the base's isSplitRequired flag."""
    plan = split_entries(base, splits)
    add, total = {}, 0
    try:
        for entry, (path, member, size) in plan.items():
            data = read_member(path, member)
            total += len(data)
            if total > MAX_SPLIT_TOTAL:
                raise FrameError(f'the split APKs add more than {MAX_SPLIT_TOTAL // 1024**3} GB')
            add[entry] = data
        with zipfile.ZipFile(base) as z:
            original = frame_apk._read(z, 'AndroidManifest.xml', frame_apk.MAX_MANIFEST)
        manifest = drop_split_required(original)
        repack(base, dst, replace={'AndroidManifest.xml': manifest} if manifest != original else None, add=add)
    except (OSError, ValueError, IndexError, struct.error, zipfile.BadZipFile, frame_apk.ApkError) as e:
        raise FrameError(f'could not merge the split APKs: {e}') from e
    return {'merged': sorted(os.path.basename(p) for p in splits), 'entries': len(plan)}


def split_size(splits):
    """Bytes a merge adds, from the split headers (merge_splits enforces the real sizes)."""
    total = 0
    for path in splits:
        with zipfile.ZipFile(path) as z:
            total += sum(m.file_size for m in z.infolist() if SPLIT_ADDS.match(m.filename))
    return total


def install(apk_path, flatscreen=None, name=None, source=None, icon_png=None, xr_compat=None, artwork=None,
            splits=None):
    """splits: the rest of a split APK (base first, as apk_path); merged into one APK first."""
    if splits:
        start = time.time()
        with tempfile.TemporaryDirectory(prefix=f'{TMP_PREFIX}{os.getpid()}-') as tmp:
            merged = os.path.join(tmp, 'merged.apk')
            try:
                merge_splits(apk_path, list(splits), merged)
            except Exception as e:  # reported like any other failed install
                _after_install(None, None, e, start)
                raise
            meta = install(merged, flatscreen, name, source or os.path.basename(apk_path), icon_png, xr_compat, artwork)
            return meta
    start, info = time.time(), None
    try:
        info = apk_info(apk_path)
        if icon_png:
            info['icon_png'] = icon_png
        check_installable(info)
        pkg = info['package']
        if not PKG_RE.match(pkg):
            raise FrameError(f'unexpected package name {pkg!r}')
        if flatscreen is None:
            flatscreen = not info['vr']
        # VR apps get the OpenXR compatibility layer unless told otherwise; it only
        # changes calls SteamVR would otherwise reject. Without it OpenXR 1.0 apps
        # still run, so a copy of Frame Control that lacks it installs anyway and
        # says so, unless the layer was asked for explicitly.
        add = {}
        if info['vr'] if xr_compat is None else xr_compat:
            try:
                add = xr_compat_files(apk_path)
            except LayerMissing:
                if xr_compat:
                    raise
                info['vr_issues'] = list(info.get('vr_issues') or []) + [LAYER_MISSING_NOTE]
                info['xr_layer_missing'] = True
        with _install_lock:
            if add or info['repairable']:
                with tempfile.TemporaryDirectory(prefix=f'{TMP_PREFIX}{os.getpid()}-') as tmp:
                    patched = os.path.join(tmp, 'app.apk')
                    try:
                        info['patched'] = patch(apk_path, patched, add)['patched']
                    except FrameError as e:
                        raise FrameError(f'could not prepare the APK for the Frame: {e}') from e
                    info['launchable'] = True
                    meta = _install(patched, info, pkg, flatscreen, name, source or os.path.basename(apk_path), artwork)
            else:
                meta = _install(apk_path, info, pkg, flatscreen, name, source, artwork)
    except Exception as e:  # not only FrameError: every failed install is reported
        _after_install(info, None, e, start)
        raise
    _after_install(info, meta, None, start)
    return meta


def _after_install(info, meta, error, start):
    for hook in install_hooks:
        try:
            hook(info, meta, error, time.time() - start)
        except Exception:
            pass  # reporting must never change an install's outcome


def _install(apk_path, info, pkg, flatscreen, name, source, artwork=None):
    try:
        images, art_warnings = frame_artwork.prepare(name or info['label'], info.get('icon_png'), artwork)
    except (ValueError, OSError) as e:
        raise FrameError(f'could not prepare artwork: {e}') from e
    iid = instance_id(pkg)
    d = f'{APPS_DIR}/{pkg}'
    existing = read_meta(pkg)
    ok, created = False, None
    try:
        ssh(f'mkdir -p {d}')
        _copy(apk_path, f'{d}/app.apk.part')
        _copy(LAUNCHER, f'{d}/launch.sh', executable=True, timeout=120)
        marker = f'touch {d}/lepton-show-flatscreen' if flatscreen else f'rm -f {d}/lepton-show-flatscreen'
        ssh(f'mv {d}/app.apk.part {d}/app.apk && echo {iid} > {d}/instance.id && {marker}')
        home = ssh('echo $HOME').strip()
        shortcut = _int((existing or {}).get('shortcut'))
        if not shortcut or shortcut not in _shortcut_ids():
            reply = shortcut_tool('add', name or info['label'], f'{home}/{d}/launch.sh', f'{home}/{d}',
                                  '')
            shortcut = _int(reply.strip().splitlines()[-1] if reply.strip() else None)
            if not shortcut:
                raise FrameError(f'Steam did not return a shortcut id (got {reply[:80]!r})')
            created = shortcut
        presentation = apply_library(shortcut, name or info['label'], d, images,
                                     vr=not flatscreen, home=home,
                                     exe=f'{home}/{d}/launch.sh', start_dir=f'{home}/{d}',
                                     details={'package': pkg, 'version': info['version'],
                                              'source': source or os.path.basename(apk_path)})
        presentation['warnings'] = art_warnings + presentation.get('warnings', [])
        meta = {'package': pkg, 'label': name or info['label'], 'version': info['version'],
                'version_code': info.get('version_code'),
                'instance': iid, 'shortcut': shortcut, 'game_id': game_id(shortcut),
                'vr': info.get('vr', False), 'vr_issues': info.get('vr_issues', []),
                'launchable': info.get('launchable', False), 'patched': info.get('patched', []),
                'flatscreen': flatscreen, 'installed': time.strftime('%Y-%m-%dT%H:%M:%S'),
                'source': source or os.path.basename(apk_path),
                'library_warnings': presentation.get('warnings', []),
                'artwork': presentation.get('artwork', {}), 'library_version': 2}
        _write_meta(d, meta)
        ok = True
        return meta
    finally:
        if not ok and created:
            try:
                shortcut_tool('remove', str(created))
            except FrameError:
                pass
        if not ok and not existing:
            # A first install that failed part-way: don't leave an orphan folder behind.
            try:
                ssh(f'rm -rf {d}', timeout=30)
            except FrameError:
                pass
        elif not ok:
            # An update that failed: the app still works, but a partial copy of the new APK
            # (scp leaves one; it can be gigabytes) mustn't stay beside it.
            try:
                ssh(f'rm -f {d}/app.apk.part', timeout=30)
            except FrameError:
                pass



def apply_library(shortcut, label, directory, images, vr=None, home=None, exe='', start_dir='', details=None,
                  category='Android', fill_only=False):
    """Mandatory for every sideload: render all five slots before reporting success.

    vr None leaves Steam's VR flag as it is (devkit titles declare their own). fill_only (automatic
    backfill) sets only slots Steam has no art for, and never the name, exe or flags."""
    home = home or ssh('echo $HOME').strip()
    d = directory
    ssh(f'mkdir -p {shlex.quote(d)}/artwork')
    paths = {}
    for slot, (ext, data) in images.items():
        path = f'{d}/artwork/source-{slot}.{ext}'
        ssh(f'cat > {shlex.quote(path)}', input=data)
        paths[slot] = f'{home}/{path}' if not path.startswith('/') else path
    manifest = {'label': label, 'images': paths}
    plan = f'{d}/artwork/input.json'
    ssh(f'cat > {shlex.quote(plan)}', input=json.dumps(manifest))
    absolute = f'{home}/{plan}' if not plan.startswith('/') else plan
    # The Frame retries once with generated art, each attempt allowed 75 s.
    rendered = json.loads(shortcut_tool('render', absolute, timeout=200))
    art = rendered['paths']
    if set(art) != set(frame_artwork.SLOTS):
        raise FrameError('Steam artwork renderer did not produce every slot')
    result = json.loads(shortcut_tool('configure', str(shortcut), label, exe, start_dir, art['icon'],
                                     '' if vr is None else '1' if vr else '0', json.dumps(art),
                                     json.dumps({'category': category, 'details': details or {},
                                                 'fill_only': fill_only}), timeout=120))
    result['warnings'] = rendered.get('warnings', []) + result.get('warnings', [])
    result['artwork'] = art
    if category == 'Android':
        ssh(f'cat > {shlex.quote(d)}/shortcut.id', input=str(int(shortcut)))
    return result


def cached_art_script(d):
    """Frame-side Python that loads the source images kept beside d's last render into `cached`."""
    return f"""import base64, json, os
cached = {{}}
directory = os.path.realpath({d!r})
try:
    with open(os.path.join(directory, 'artwork/input.json')) as f:
        plan = json.load(f)
    for slot, path in plan.get('images', {{}}).items():
        if os.path.commonpath([os.path.realpath(path), directory]) != directory:
            continue
        with open(path, 'rb') as f:
            data = f.read(12 * 1024 * 1024 + 1)
        if len(data) <= 12 * 1024 * 1024:
            cached[slot] = base64.b64encode(data).decode()
except (OSError, ValueError, TypeError, AttributeError):
    pass
"""


def _apk_parser_script():
    """Frame-side Python preamble that loads this copy's stdlib APK parser (frame_apk)."""
    script = 'import sys, types, json, base64\n'
    for module in ('frame_apk', 'frame_apk_vr'):
        with open(os.path.join(ROOT, 'ui', module + '.py')) as f:
            source = f.read()
        script += f'm = types.ModuleType({module!r}); sys.modules[{module!r}] = m; exec({source!r}, m.__dict__)\n'
    return script


def installed_version_code(pkg):
    """The version code of pkg's installed app.apk, read on the Frame (only its manifest
    crosses the link), or None if there is no readable APK. Connection failures raise."""
    if not PKG_RE.match(pkg or ''):
        raise FrameError(f'bad package name {pkg!r}')
    script = _apk_parser_script() + f"""try:
    code = sys.modules['frame_apk'].apk_info({(APPS_DIR + '/' + pkg + '/app.apk')!r}).get('version_code')
except Exception:
    code = None
print(json.dumps(code if isinstance(code, int) else None))
"""
    try:
        code = json.loads(ssh('python3 -', input=script, timeout=120).strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def art_missing(m):
    """True when a Frame Control install has no complete Steam artwork on record."""
    return set((m or {}).get('artwork') or {}) != set(frame_artwork.SLOTS)


def refresh_art(pkg=None, artwork=None, fill_only=False):
    """Refresh existing APK library entries without reinstalling or stopping them.

    fill_only: the automatic backfill; it only fills empty Steam slots (see apply_library)."""
    if pkg is None:
        results = []
        for app in list_apps():
            try:
                results.append(refresh_art(app['package'], artwork, fill_only))
            except Exception as e:  # one app's failure must not stop the others
                results.append({'package': app['package'], 'label': app.get('label'), 'error': str(e) or type(e).__name__})
        return results
    with _install_lock:  # shared with install and remove: a removed app is never recreated
        m = _meta_or_fail(pkg)
        d = f'{APPS_DIR}/{pkg}'
        # Parse the APK on the Frame, transferring only its icon/label, not the APK.
        script = _apk_parser_script()
        script += f"info = sys.modules['frame_apk'].apk_info({(d + '/app.apk')!r})\n"
        script += "info['icon_png'] = base64.b64encode(info.get('icon_png') or b'').decode()\n"
        script += cached_art_script(d) + "info['artwork'] = cached\nprint(json.dumps(info))\n"
        info = json.loads(ssh('python3 -', input=script))
        icon = base64.b64decode(info['icon_png'])
        cached = {k: base64.b64decode(v) for k, v in info.get('artwork', {}).items()}
        images, warnings = frame_artwork.prepare(m['label'], icon, artwork if artwork is not None else cached)
        home = ssh('echo $HOME').strip()
        created = False
        if not m['shortcut'] or m['shortcut'] not in _shortcut_ids():
            m['shortcut'] = _int(shortcut_tool('add', m['label'], f'{home}/{d}/launch.sh', f'{home}/{d}'))
            if not m['shortcut']:
                raise FrameError('Steam did not create the missing shortcut')
            m['game_id'] = game_id(m['shortcut'])
            created = True
        try:
            result = apply_library(m['shortcut'], m['label'], d, images, vr=not m.get('flatscreen', True),
                                   home=home, exe=f'{home}/{d}/launch.sh', start_dir=f'{home}/{d}', details=m,
                                   fill_only=fill_only and not created)
        except Exception:
            if created:
                try:
                    shortcut_tool('remove', str(m['shortcut']))
                except FrameError:
                    pass  # keep the render error, not the cleanup's
            raise
        m.update(artwork=result.get('artwork', {}), library_version=2, art_pending=False)
        m['library_warnings'] = warnings + result.get('warnings', [])
        m['artwork_refreshed'] = time.strftime('%Y-%m-%dT%H:%M:%S')
        _write_meta(d, m)
        return m

def _int(v):
    try:
        n = int(v)
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def read_meta(pkg):
    try:
        m = json.loads(ssh(f'cat {APPS_DIR}/{pkg}/meta.json 2>/dev/null || true') or 'null')
    except (ValueError, FrameError):
        return None
    return _clean_meta(m)


def _clean_meta(m):
    """A usable meta dict with integer ids, or None if it's missing what we need."""
    if not isinstance(m, dict) or not PKG_RE.match(str(m.get('package', ''))):
        return None
    iid, shortcut = _int(m.get('instance')), _int(m.get('shortcut'))
    if not iid:
        return None
    m.update(instance=iid, shortcut=shortcut, game_id=game_id(shortcut) if shortcut else None,
             label=str(m.get('label') or m['package']), version=str(m.get('version') or ''))
    return m


def running_instances(strict=False):
    """Lepton container name -> adb port, for the instances that are running now.

    strict: a failed `podman ps` raises FrameError instead of reading as "nothing running",
    and containers without an adb port label count too (port None); for callers that must
    not mistake an error for a stopped instance."""
    if not strict:
        out = ssh('podman ps --format "{{.Names}} {{.Labels.adb_port}}" 2>/dev/null || true')
        return dict(line.split()[:2] for line in out.splitlines() if len(line.split()) >= 2)
    out = ssh('podman ps --format "{{.Names}} {{.Labels.adb_port}}"')
    found = {}
    for line in out.splitlines():
        cols = line.split()
        if cols:
            found[cols[0]] = cols[1] if len(cols) > 1 and cols[1] != '<no' else None
    return found


def list_apps():
    out = ssh(f'for f in {APPS_DIR}/*/meta.json; do [ -f "$f" ] && cat "$f" && echo; echo "@@"; done 2>/dev/null || true')
    running = running_instances()
    apps = []
    for chunk in out.split('@@'):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            m = _clean_meta(json.loads(chunk))
        except ValueError:
            continue
        if m:
            m['running'] = f"lepton-steamlaunch-{m['instance']}" in running
            m['art_missing'] = art_missing(m)
            apps.append(m)
    return sorted(apps, key=lambda m: m['label'].lower())


def _meta_or_fail(pkg):
    if not PKG_RE.match(pkg or ''):
        raise FrameError(f'bad package name {pkg!r}')
    m = read_meta(pkg)
    if not m:
        raise FrameError(f'{pkg} is not installed')
    return m


def launch(pkg):
    m = _meta_or_fail(pkg)
    if not m['game_id']:
        raise FrameError(f"{m['label']} has no Steam shortcut; reinstall it")
    ssh(f"steam steam://rungameid/{int(m['game_id'])} >/dev/null 2>&1 &")
    return m


def stop(pkg):
    m = _meta_or_fail(pkg)
    if m['shortcut']:
        try:
            shortcut_tool('stop', str(int(m['shortcut'])))
        except FrameError:
            pass  # Steam unavailable: the container stop also ends its waiting launcher.
    ssh(f"podman stop -t 5 lepton-steamlaunch-{int(m['instance'])} >/dev/null 2>&1 || true", timeout=60)
    return m


def remove(pkg, keep_data=False):
    with _install_lock:  # never while an install or artwork refresh of any app is writing
        return _remove(pkg, keep_data)


def _remove(pkg, keep_data):
    m = _meta_or_fail(pkg)
    stop(pkg)
    if m['shortcut']:
        # Best effort: Steam may not be running, and the files must still go.
        try:
            result = json.loads(shortcut_tool('remove', str(int(m['shortcut']))) or '{}')
            m['library_warnings'] = result.get('warnings', [])
        except (FrameError, ValueError, AttributeError) as e:
            m['library_warnings'] = [f'Steam shortcut not removed: {e}']
    iid = int(m['instance'])
    extra = '' if keep_data else f' {COMPAT}/{iid} {SHADERS}/{iid}'
    ssh(f'rm -rf {APPS_DIR}/{pkg}{extra}')
    return m


def probe(pkg, wait=20):
    """Launch the app's instance and report whether it stays up (for compat reports)."""
    m = _meta_or_fail(pkg)
    ctr = f"lepton-steamlaunch-{int(m['instance'])}"
    launch(pkg)
    t0 = time.time()
    while time.time() - t0 < 90 and ctr not in running_instances():
        time.sleep(3)
    if ctr not in running_instances():
        return {'package': pkg, 'version': m['version'], 'result': 'instance_failed',
                'detail': 'Lepton instance did not start within 90 s'}
    # Android boots inside the container; then give the app time to crash, or not.
    time.sleep(wait)
    sh = f'podman exec {ctr} /system/bin/sh -c'
    alive = ssh(f"{sh} 'pidof {pkg}' 2>/dev/null || true").strip()
    crash = ssh(f"{sh} 'logcat -d -b crash' 2>/dev/null | tail -n 60 || true")
    reason = next((l.split('AndroidRuntime: ', 1)[1] for l in crash.splitlines()
                   if 'AndroidRuntime: ' in l and ('Exception' in l or 'Error' in l)), '')
    if not reason and 'Fatal signal' in crash:
        reason = next(l[l.find('Fatal signal'):] for l in crash.splitlines() if 'Fatal signal' in l)
    if 'ClipboardManager' in reason:
        reason = 'no clipboard service: ' + reason
    return {'package': pkg, 'version': m['version'], 'result': 'runs' if alive else 'crashes',
            'detail': reason[:300], 'seconds': wait,
            'container_up': ctr in running_instances()}


def patch(src, dst, add=None):
    try:
        info = apk_info(src)
        with zipfile.ZipFile(src) as z:
            original = frame_apk._read(z, 'AndroidManifest.xml', frame_apk.MAX_MANIFEST)
        manifest = add_launcher_category(original) if info['repairable'] else original
        if not info['launchable'] and not info['repairable']:
            raise FrameError('APK has no MAIN/LAUNCHER activity that Frame Control can patch')
        repack(src, dst, replace={'AndroidManifest.xml': manifest}, add=add)
        result = apk_info(dst)
        result.pop('icon_png', None)
        result['patched'] = (['launcher'] if manifest != original else []) + \
            (['openxr-compat'] if add and set(XR_COMPAT_FILES) <= set(add) else [])
        return result
    except (OSError, ValueError, IndexError, struct.error, zipfile.BadZipFile, frame_apk.ApkError) as e:
        raise FrameError(str(e)) from e


def install_obb(pkg, paths):
    import frame_android_data
    return frame_android_data.install_obb(pkg, paths)


def backup_data(pkg, destination):
    import frame_android_data
    return frame_android_data.backup_data(pkg, destination)


def restore_data(pkg, archive):
    import frame_android_data
    return frame_android_data.restore_data(pkg, archive)


def main():
    cmd, *args = sys.argv[1:] or ['help']
    try:
        if cmd in ('info', 'versions'):
            import frame_apk_versions
            if cmd == 'info':
                info = apk_info(args[0])
                print(frame_apk_versions.describe(info))
                fix = '; Frame Control adds the LAUNCHER entry Lepton needs' if info.get('repairable') else ''
                if info.get('vr') or fix:
                    print(('VR app' if info.get('vr') else 'Android app') + fix)
                for note in info.get('vr_issues', []):
                    print(note)
                return
            info = apk_info(args[0]) if os.path.isfile(args[0]) or args[0].lower().endswith('.apk') else None
            r = frame_apk_versions.alternatives(
                info['package'] if info else args[0], info.get('version_code') if info else None)
        elif cmd == 'install':
            files = [a for a in args if not a.startswith('--')]
            if not files:
                raise FrameError('install requires APK [SPLIT.apk ...]')
            r = install(files[0], flatscreen=False if '--vr' in args else True if '--flat' in args else None,
                        xr_compat=False if '--no-xr-compat' in args else None, splits=files[1:])
        elif cmd == 'refresh-art':
            if len(args) != 1:
                raise FrameError('usage: refresh-art PACKAGE or refresh-art --all')
            r = refresh_art(None if args[0] == '--all' else args[0])
            if isinstance(r, list) and any('error' in item for item in r):
                print(json.dumps(r, indent=1))
                raise SystemExit(1)
        elif cmd == 'patch':
            import argparse
            parser = argparse.ArgumentParser(description='Patch and v2-sign an APK locally')
            parser.add_argument('src')
            parser.add_argument('dst')
            parser.add_argument('--add', action='append', default=[], metavar='NAME=PATH')
            opts = parser.parse_args(args)
            additions = {}
            for item in opts.add:
                if '=' not in item:
                    raise FrameError('--add requires NAME=PATH')
                entry, path = item.split('=', 1)
                with open(path, 'rb') as f:
                    additions[entry] = f.read()
            r = patch(opts.src, opts.dst, additions)
        elif cmd == 'install-obb':
            if len(args) < 2:
                raise FrameError('install-obb requires PACKAGE OBB [OBB ...]')
            r = install_obb(args[0], args[1:])
        elif cmd in ('backup-data', 'restore-data'):
            if len(args) != 2:
                raise FrameError(cmd + ' requires PACKAGE ARCHIVE.tar.gz')
            r = (backup_data if cmd == 'backup-data' else restore_data)(*args)
        elif cmd == 'list':
            r = list_apps()
            if any(a['art_missing'] for a in r):
                print('Some apps have no Steam artwork: python3 ui/frame_android.py refresh-art --all', file=sys.stderr)
        elif cmd in ('launch', 'stop', 'probe'):
            r = globals()[cmd](args[0])
        elif cmd == 'remove':
            r = remove(args[0], keep_data='--keep-data' in args)
        else:
            sys.exit(__doc__)
    except (FrameError, OSError) as e:
        sys.exit(f'error: {e}')
    print(json.dumps(r, indent=1))


if __name__ == '__main__':
    main()
