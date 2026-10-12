"""Small APKs built in memory (binary manifest plus resource table), for test_frame_apk
and the end-to-end tests. Deliberately doesn't import sandbox: tests/e2e runs in a
throwaway container and keeps its real home, which sandbox would move."""
import io
import os
import struct
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'ui'))
import frame_apk  # noqa: E402


def pool(strings, utf8=False):
    """A ResStringPool chunk."""
    data, offsets = b'', []
    for s in strings:
        offsets.append(len(data))
        if utf8:
            b = s.encode()
            data += bytes([len(s), len(b)]) + b + b'\0'
        else:
            data += struct.pack('<H', len(s)) + s.encode('utf-16-le') + b'\0\0'
    data += b'\0' * (-len(data) % 4)
    start = 28 + 4 * len(strings)
    body = struct.pack(f'<{len(strings)}I', *offsets) + data
    return struct.pack('<HHIIIIII', 1, 28, 28 + len(body), len(strings), 0, 0x100 if utf8 else 0, start, 0) + body


def manifest(package, label_ref, version_ref, min_sdk, package_raw=True, foreign_label=False):
    """<manifest package versionName><uses-sdk minSdkVersion/><application label icon/></manifest>.

    package_raw=False drops the package's raw string (as some repackers do);
    foreign_label adds a non-android `label` attribute after android:label.
    """
    strings = ['label', 'icon', 'versionName', 'minSdkVersion', 'package', 'manifest', 'uses-sdk',
               'application', package, 'junk', 'label', 'versionCode']  # the second 'label' has no android id
    resmap = struct.pack('<4I', 0x01010001, 0x01010002, 0x0101021c, 0x0101020c)
    resmap = struct.pack('<HHI', 0x0180, 8, 8 + len(resmap)) + resmap

    def element(name, attrs):
        body = struct.pack('<IIHHHHHH', 0xffffffff, name, 20, 20, len(attrs), 0, 0, 0)
        for aname, raw, dtype, value in attrs:
            body += struct.pack('<IIIHBBI', 0xffffffff, aname, raw, 8, 0, dtype, value)
        return struct.pack('<HHIII', 0x0102, 16, 16 + len(body), 1, 0xffffffff) + body

    none = 0xffffffff
    chunks = (pool(strings) + resmap
              + element(5, [(4, 8 if package_raw else none, frame_apk.T_STRING, 8),
                            (2, none, frame_apk.T_REF, version_ref),
                            (11, none, frame_apk.T_INT_DEC, 210)])
              + element(6, [(3, none, frame_apk.T_INT_DEC, min_sdk)])
              + element(7, [(0, none, frame_apk.T_REF, label_ref), (1, none, frame_apk.T_REF, 0x7f020000)]
                        + ([(10, 9, frame_apk.T_STRING, 9)] if foreign_label else [])))
    return struct.pack('<HHI', 3, 8, 8 + len(chunks)) + chunks


def resources(values):
    """resources.arsc with package 0x7f; values: {(type id, entry, language, density): global string index}."""
    strings = ['French label', 'App label', '2.1', 'res/icon_lo.png', 'res/icon_hi.png', 'res/icon.xml']
    pkg_body = b''
    for (tid, lang, density), entries in values.items():
        cfg = struct.pack('<I4x2s4xH', 64, lang.encode().ljust(2, b'\0'), density).ljust(64, b'\0')
        count = max(entries) + 1
        offsets, data = [], b''
        for i in range(count):
            if i in entries:
                offsets.append(len(data))
                data += struct.pack('<HHI', 8, 0, 0) + struct.pack('<HBBI', 8, 0, frame_apk.T_STRING, entries[i])
            else:
                offsets.append(0xffffffff)
        header = 20 + 64
        estart = header + 4 * count
        body = struct.pack(f'<{count}I', *offsets) + data
        pkg_body += struct.pack('<HHIBBHII', 0x0201, header, header + len(body), tid, 0, 0, count, estart) + cfg + body
    pkg_header = struct.pack('<HHII', 0x0200, 288, 288 + len(pkg_body), 0x7f).ljust(288, b'\0')
    pkg = pkg_header + pkg_body
    table = pool(strings, utf8=True) + pkg
    return struct.pack('<HHII', 2, 12, 12 + len(table), 1) + table


def apk(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()
