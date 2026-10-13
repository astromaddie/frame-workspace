"""Binary-manifest VR inspection and minimal launcher repair (stdlib only)."""
import struct
import frame_apk

MAIN = 'android.intent.action.MAIN'
LAUNCHER = 'android.intent.category.LAUNCHER'
VR = {'com.oculus.intent.category.VR', 'org.khronos.openxr.intent.category.IMMERSIVE_HMD'}


def inspect(data):
    elements = iter(frame_apk.manifest_elements(data))
    stack, filters, samsung = [], [], False
    current, owner, package = None, None, ''
    for kind, hs, off, size in frame_apk._chunks(data, 8, len(data)):
        if kind == 0x0102:
            tag, attrs = next(elements)
            value = attrs.get('name', (None, None, None))[2]
            if tag == 'manifest':
                package = attrs.get('package', (None, None, None))[2] or ''
            if tag in ('activity', 'activity-alias'):
                owner = attrs.get('targetActivity', (None, None, None))[2] if tag == 'activity-alias' else value
                if owner and '.' not in owner:  # PackageParser.buildClassName: bare names are relative too
                    owner = '.' + owner
                owner = package + owner if owner and owner.startswith('.') else owner
            if tag == 'intent-filter' and stack and stack[-1] in ('activity', 'activity-alias'):
                current = {'actions': set(), 'categories': set(), 'templates': [], 'alias': stack[-1] == 'activity-alias', 'activity': owner}
            if current is not None and stack and stack[-1] == 'intent-filter':
                if tag == 'action':
                    current['actions'].add(value)
                if tag == 'category':
                    current['categories'].add(value)
                    current['templates'].append((off, size, hs))
            if tag == 'meta-data' and stack and stack[-1] == 'application':
                samsung |= value == 'com.samsung.android.vr.application.mode' and attrs.get('value', (0, 0, None))[2] == 'vr_only'
            stack.append(tag)
        elif kind == 0x0103 and stack:
            tag = stack.pop()
            if tag == 'intent-filter' and current is not None:
                current['end'] = off
                filters.append(current)
                current = None
    mains = [f for f in filters if MAIN in f['actions']]
    vr_filters = [f for f in mains if VR & f['categories']]
    # Lepton's apk-info-extractor ignores <activity-alias>; Godot 4 puts LAUNCHER only there.
    real = [f for f in mains if not f['alias']]
    targets = [f for f in real if VR & f['categories']]
    if not targets and any(LAUNCHER in f['categories'] or VR & f['categories'] for f in mains if f['alias']):
        aimed = {f['activity'] for f in mains if f['alias'] and (LAUNCHER in f['categories'] or VR & f['categories'])}
        targets = [f for f in real if f['templates']]  # the patch copies an existing <category>
        targets = [f for f in targets if f['activity'] in aimed] or targets
    launchable = any(LAUNCHER in f['categories'] for f in real)
    return {'launchable': launchable, 'repairable': not launchable and bool(targets),
            'vr_activity': bool(vr_filters), 'vr': bool(vr_filters) or samsung}, targets


def _append_string(chunk, text):
    _, hs, size, count, styles, flags, start, style_start = struct.unpack_from('<HHIIIIII', chunk)
    strings_end = style_start or size
    encoded = text.encode('utf-8' if flags & 0x100 else 'utf-16-le')
    # LAUNCHER is short enough for both single-unit length encodings.
    new = (bytes([len(text), len(encoded)]) + encoded + b'\0' if flags & 0x100
           else struct.pack('<H', len(text)) + encoded + b'\0\0')
    string_data = chunk[start:strings_end] + new
    string_data += bytes(-len(string_data) % 4)
    header = bytearray(chunk[:hs])
    new_start = start + 4
    new_styles = new_start + len(string_data) if style_start else 0
    body = (chunk[hs:hs + count * 4] + struct.pack('<I', strings_end - start)
            + chunk[hs + count * 4:start] + string_data + (chunk[style_start:] if style_start else b''))
    struct.pack_into('<IIIII', header, 8, count + 1, styles, flags & ~1, new_start, new_styles)
    struct.pack_into('<I', header, 4, len(header) + len(body))
    return bytes(header) + body, count


def add_launcher_category(axml_bytes):
    info, filters = inspect(axml_bytes)
    if info['launchable']:
        return axml_bytes
    if not filters:
        raise frame_apk.ApkError('no activity with a MAIN intent filter that Frame Control can patch')
    target = filters[0]
    chunks = list(frame_apk._chunks(axml_bytes, 8, len(axml_bytes)))
    pool = next(c for c in chunks if c[0] == 1)
    _, _, po, ps = pool
    new_pool, index = _append_string(axml_bytes[po:po + ps], LAUNCHER)
    off, size, hs = target['templates'][0]
    start = bytearray(axml_bytes[off:off + size])
    attr_start, attr_size, count = struct.unpack_from('<HHH', start, hs + 8)
    strings = frame_apk._string_pool(axml_bytes, po)
    resmap = []
    for kind, header_size, offset, chunk_size in chunks:
        if kind == 0x180:
            resmap = struct.unpack_from('<%dI' % ((chunk_size - header_size) // 4),
                                        axml_bytes, offset + header_size)
    for i in range(count):
        a = hs + attr_start + i * attr_size
        name = struct.unpack_from('<I', start, a + 4)[0]
        if (name < len(resmap) and resmap[name] == 0x01010003) or strings[name] == 'name':
            struct.pack_into('<I', start, a + 8, index)
            struct.pack_into('<HBBI', start, a + 12, 8, 0, 3, index)
            break
    else:
        raise frame_apk.ApkError('category has no name attribute')
    end = struct.pack('<HHI', 0x0103, hs, hs + 8) + start[8:hs] + start[hs:hs + 8]
    result = bytearray(axml_bytes[:8])
    for _, _, off, size in chunks:
        if off == target['end']:
            result += start + end
        result += new_pool if off == po else axml_bytes[off:off + size]
    struct.pack_into('<I', result, 4, len(result))
    result = bytes(result)
    if not inspect(result)[0]['launchable']:
        raise frame_apk.ApkError('launcher repair failed verification')
    return result


def detection(data, names):
    info, _ = inspect(data)
    issues = []
    if 'lib/arm64-v8a/libvrapi.so' in names:
        issues.append("Uses Meta's legacy VrApi, which the Frame doesn't have; it may not run.")
    if any(n.endswith('/libovrplatformloader.so') for n in names):
        issues.append("Uses Meta's platform SDK; if it checks your Quest store licence it will quit.")
    if 'lib/arm64-v8a/libopenxr_loader.so' in names:
        info['vr'] = True
        issues.append('Uses OpenXR (good).')
    info['vr_issues'] = issues
    return info


IS_SPLIT_REQUIRED = 0x01010591  # android:isSplitRequired


def drop_split_required(axml_bytes):
    """The manifest without <application android:isSplitRequired>, for a base APK whose
    splits were merged into it: Android refuses a lone base that still says it needs them."""
    chunks = list(frame_apk._chunks(axml_bytes, 8, len(axml_bytes)))
    pool = next((c for c in chunks if c[0] == 1), None)
    if pool is None:
        raise frame_apk.ApkError('AndroidManifest.xml has no string pool')
    strings = frame_apk._string_pool(axml_bytes, pool[2])
    resmap = ()
    for kind, header_size, offset, chunk_size in chunks:
        if kind == 0x180:
            resmap = struct.unpack_from('<%dI' % ((chunk_size - header_size) // 4), axml_bytes, offset + header_size)
    result, changed = bytearray(axml_bytes[:8]), False
    for kind, hs, off, size in chunks:
        chunk = bytearray(axml_bytes[off:off + size])
        if kind == 0x0102 and not changed:
            name, astart, asize, count = struct.unpack_from('<4xIHHH', chunk, hs)
            if name < len(strings) and strings[name] == 'application':
                for i in range(count):
                    a = hs + astart + i * asize
                    attr = struct.unpack_from('<I', chunk, a + 4)[0]
                    known = resmap[attr] if attr < len(resmap) else None
                    if known == IS_SPLIT_REQUIRED or (known is None and attr < len(strings)
                                                      and strings[attr] == 'isSplitRequired'):
                        del chunk[a:a + asize]
                        struct.pack_into('<I', chunk, 4, len(chunk))
                        struct.pack_into('<H', chunk, hs + 12, count - 1)
                        # id/class/style attribute indexes are 1-based positions in the list.
                        for at in (hs + 14, hs + 16, hs + 18):
                            index = struct.unpack_from('<H', chunk, at)[0]
                            struct.pack_into('<H', chunk, at, 0 if index == i + 1 else index - 1 if index > i + 1 else index)
                        changed = True
                        break
        result += chunk
    if not changed:
        return axml_bytes
    struct.pack_into('<I', result, 4, len(result))
    return bytes(result)
