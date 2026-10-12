"""Read an APK's package, label, version, SDK level, ABIs and icon, stdlib only.

Replaces `aapt2 dump badging`, so installing APKs needs no Android SDK. It
parses the binary AndroidManifest.xml and, for values the manifest points at
(the label, version name and icon are often @string or @mipmap references),
the resource table in resources.arsc.
"""
import struct
import zipfile
import zlib

# android: attribute resource ids; names can be stripped by shrinkers, ids can't.
ATTR = {0x01010001: 'label', 0x01010002: 'icon', 0x01010003: 'name',
        0x01010024: 'value', 0x0101021b: 'versionCode', 0x0101021c: 'versionName', 0x0101020c: 'minSdkVersion',
        0x01010202: 'targetActivity'}
T_REF, T_STRING, T_INT_DEC, T_INT_HEX = 0x01, 0x03, 0x10, 0x11
# APKs can come from websites (install links), so nothing read from one may be
# unbounded. zipfile stops at a member's declared size, so checking it is enough.
MAX_MANIFEST = 16 * 1024**2
MAX_ARSC = 128 * 1024**2      # real ones are a few MB; the largest apps' tens of MB
MAX_ICON = 8 * 1024**2
MAX_VALUES = 256              # resolved values per reference, across all its hops
MAX_STEPS = 4096              # entries examined per reference, dead ends and cycles included


class ApkError(Exception):
    pass


def _string_pool(buf, off):
    """Strings of the ResStringPool chunk at off."""
    _, hsize, _, count, _, flags, start = struct.unpack_from('<HHIIIII', buf, off)
    utf8 = flags & 0x100
    offsets = struct.unpack_from(f'<{count}I', buf, off + hsize)
    base = off + start
    out = []
    for o in offsets:
        p = base + o
        if utf8:
            for _ in range(2):  # UTF-16 length, then UTF-8 byte length
                n = buf[p]
                if n & 0x80:
                    n = ((n & 0x7f) << 8) | buf[p + 1]
                    p += 2
                else:
                    p += 1
            out.append(buf[p:p + n].decode('utf-8', 'replace'))
        else:
            n, = struct.unpack_from('<H', buf, p)
            p += 2
            if n & 0x8000:
                n = ((n & 0x7fff) << 16) | struct.unpack_from('<H', buf, p)[0]
                p += 2
            out.append(buf[p:p + 2 * n].decode('utf-16-le', 'replace'))
    return out


def _chunks(buf, off, end):
    while off + 8 <= end:
        ctype, hsize, size = struct.unpack_from('<HHI', buf, off)
        if size < 8 or off + size > end:
            break
        yield ctype, hsize, off, size
        off += size


def manifest_elements(data):
    """[(tag, {attr: (type, data, raw string or None)})] for each start tag."""
    if len(data) < 8 or struct.unpack_from('<H', data, 0)[0] != 0x0003:
        raise ApkError('AndroidManifest.xml is not binary XML')
    strings, resmap, out = [], [], []
    for ctype, hsize, off, size in _chunks(data, 8, len(data)):
        if ctype == 0x0001:
            strings = _string_pool(data, off)
        elif ctype == 0x0180:
            resmap = struct.unpack_from(f'<{(size - hsize) // 4}I', data, off + hsize)
        elif ctype == 0x0102:
            name, astart, asize, count = struct.unpack_from('<4xIHHH', data, off + hsize)
            attrs = {}
            for i in range(count):
                a = off + hsize + astart + i * asize
                aname, raw, dtype, value = struct.unpack_from('<4xII3xBI', data, a)
                raw = strings[raw] if raw < len(strings) else None
                if raw is None and dtype == T_STRING and value < len(strings):
                    raw = strings[value]  # some repackers keep only the typed value
                android = ATTR.get(resmap[aname]) if aname < len(resmap) else None
                if android:  # android: attributes win over same-named ones in other namespaces
                    attrs[android] = (dtype, value, raw)
                else:
                    attrs.setdefault(strings[aname] if aname < len(strings) else '', (dtype, value, raw))
            out.append((strings[name] if name < len(strings) else '', attrs))
    return out


class Resources:
    """Just enough of resources.arsc to resolve a reference to its values."""

    def __init__(self, data):
        self.entries = {}  # resid -> [(language, density, type, data)]
        self.strings = []
        if len(data) < 12 or struct.unpack_from('<H', data, 0)[0] != 0x0002:
            return
        hsize, = struct.unpack_from('<H', data, 2)
        for ctype, _, off, size in _chunks(data, hsize, len(data)):
            if ctype == 0x0001 and not self.strings:
                self.strings = _string_pool(data, off)
            elif ctype == 0x0200:
                self._package(data, off, size)

    def _package(self, data, off, size):
        pid, = struct.unpack_from('<I', data, off + 8)
        phsize, = struct.unpack_from('<H', data, off + 2)
        for ctype, thsize, t, tsize in _chunks(data, off + phsize, off + size):
            if ctype != 0x0201:
                continue
            tid, flags, count, estart = struct.unpack_from('<BB2xII', data, t + 8)
            cfg = t + 20
            language = data[cfg + 8:cfg + 10].rstrip(b'\0').decode('latin-1')
            density, = struct.unpack_from('<H', data, cfg + 14)
            if flags & 0x01:  # sparse: (entry index, offset / 4) pairs
                pairs = [struct.unpack_from('<HH', data, t + thsize + 4 * i) for i in range(count)]
                offsets = [(i, o * 4) for i, o in pairs]
            elif flags & 0x02:  # 16-bit offsets / 4
                offsets = [(i, o * 4) for i, o in enumerate(struct.unpack_from(f'<{count}H', data, t + thsize))
                           if o != 0xffff]
            else:
                offsets = [(i, o) for i, o in enumerate(struct.unpack_from(f'<{count}I', data, t + thsize))
                           if o != 0xffffffff]
            for index, o in offsets:
                e = t + estart + o
                esize, eflags = struct.unpack_from('<HH', data, e)
                if eflags & 0x08:  # compact entry: type in the flags' high byte
                    dtype, value = eflags >> 8, struct.unpack_from('<I', data, e + 4)[0]
                elif eflags & 0x01:  # bag (style, plural...): not a plain value
                    continue
                else:
                    dtype, value = struct.unpack_from('<3xBI', data, e + esize)
                resid = (pid << 24) | (tid << 16) | index
                self.entries.setdefault(resid, []).append((language, density, dtype, value))

    def values(self, resid, depth=0, seen=frozenset(), steps=None):
        """[(language, density, type, data)] with references followed: never round a
        cycle, at most MAX_VALUES results and MAX_STEPS entries examined in all."""
        steps = steps if steps is not None else [MAX_STEPS]
        out = []
        seen = seen | {resid}
        for lang, dens, dtype, value in self.entries.get(resid, []):
            steps[0] -= 1
            if steps[0] < 0 or len(out) >= MAX_VALUES:
                break
            if dtype == T_REF and depth < 5:
                if value not in seen:
                    out += [(lang or l2, dens or d2, t2, v2)
                            for l2, d2, t2, v2 in self.values(value, depth + 1, seen, steps)]
            else:
                out.append((lang, dens, dtype, value))
        return out[:MAX_VALUES]

    def string(self, dtype, value):
        return self.strings[value] if dtype == T_STRING and value < len(self.strings) else None


def _text(attr, res):
    """An attribute's string value, preferring the default then English resource."""
    if not attr:
        return None
    dtype, value, raw = attr
    if raw is not None:
        return raw
    if dtype in (T_INT_DEC, T_INT_HEX):
        return str(value)
    if dtype == T_REF:
        vals = [(lang, res.string(t, v)) for lang, _, t, v in res.values(value)]
        vals = [(lang, s) for lang, s in vals if s is not None]
        for want in ('', 'en'):
            for lang, s in vals:
                if lang == want:
                    return s
        return vals[0][1] if vals else None
    return None


def _icons(attr, res):
    """Icon file paths, largest density first."""
    if not attr or attr[0] != T_REF:
        return []
    vals = [(0 if d in (0xfffe, 0xffff) else d, res.string(t, v)) for _, d, t, v in res.values(attr[1])]
    return [s for _, s in sorted(vals, key=lambda x: -x[0]) if s]


def _read(z, name, limit):
    """A member's bytes, inflating at most limit + 1 of them whatever its header claims
    (ZipFile.read inflates everything first, then trims to the declared size)."""
    info = z.getinfo(name)
    # Android only reads stored and deflated entries, and only those bound what
    # a read inflates (Python 3.9's bzip2 and lzma readers don't).
    if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        raise ApkError(f'{name} in the APK uses a compression Android does not')
    size = info.file_size
    if size > limit:
        raise ApkError(f'{name} in the APK is {size / 1024**2:.0f} MB, more than a real one ({limit // 1024**2} MB)')
    with z.open(name) as f:
        data = f.read(limit + 1)
    if len(data) > limit:
        raise ApkError(f'{name} in the APK is larger than a real one ({limit // 1024**2} MB)')
    return data


def apk_info(path):
    """Package, label, version, min_sdk, abis and the best PNG icon inside the APK."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        raise ApkError(f'not a readable APK: {e}')
    with z:
        names = set(z.namelist())
        if 'AndroidManifest.xml' not in names:
            raise ApkError('not an APK: no AndroidManifest.xml')
        try:
            manifest_data = _read(z, 'AndroidManifest.xml', MAX_MANIFEST)
            elements = manifest_elements(manifest_data)
            res = Resources(_read(z, 'resources.arsc', MAX_ARSC) if 'resources.arsc' in names else b'')
            from frame_apk_vr import detection
            vr_info = detection(manifest_data, names)
        # A damaged download: corrupt deflate data (zlib.error), a member cut short (EOFError),
        # undecodable text (UnicodeDecodeError) or an encrypted entry (RuntimeError).
        except (struct.error, IndexError, zipfile.BadZipFile, zlib.error, EOFError, ValueError, RuntimeError) as e:
            raise ApkError(f'could not read the APK manifest: {e}')
        tags = {}
        for tag, attrs in elements:
            tags.setdefault(tag, attrs)
        manifest, app, sdk = tags.get('manifest', {}), tags.get('application', {}), tags.get('uses-sdk', {})
        package = _text(manifest.get('package'), res)
        if not package:
            raise ApkError('the APK manifest has no package name')
        min_sdk = sdk.get('minSdkVersion')
        info = {
            'package': package,
            'version_code': manifest.get('versionCode', (None, None))[1],
            'version': _text(manifest.get('versionName'), res) or '',
            'label': _text(app.get('label'), res) or package,
            'abis': sorted({n.split('/')[1] for n in names if n.startswith('lib/') and n.count('/') >= 2}),
            'min_sdk': min_sdk[1] if min_sdk and min_sdk[0] in (T_INT_DEC, T_INT_HEX) else None,
            'icon_png': None,
        }
        info.update(vr_info)
        try:
            info['icon_png'] = _icon_png(z, names, _icons(app.get('icon'), res))
        except Exception:  # noqa: BLE001 - any unreadable icon just means no icon
            pass
    return info


def _icon_png(z, names, icons):
    for icon in icons:
        if icon.endswith('.png') and icon in names:
            return _read(z, icon, MAX_ICON)
    # Adaptive icons are XML; fall back to the largest launcher PNG.
    pngs = sorted((n for n in names if n.endswith('.png') and 'ic_launcher' in n and 'foreground' not in n),
                  key=lambda n: z.getinfo(n).file_size)
    if pngs:
        return _read(z, pngs[-1], MAX_ICON)
    # Godot exports can keep only an adaptive launcher plus the project icon.
    return _read(z, 'assets/icon.png', MAX_ICON) if 'assets/icon.png' in names else None


if __name__ == '__main__':
    import sys
    for p in sys.argv[1:]:
        i = apk_info(p)
        i['icon_png'] = len(i['icon_png'] or b'')
        print(p, i)
