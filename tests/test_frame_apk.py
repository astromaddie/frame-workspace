"""frame_apk against a small APK built here: binary manifest plus resource table."""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import io
import os
import struct
import sys
import tempfile
import tracemalloc
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'ui'))
import frame_apk  # noqa: E402
from apk_fixtures import apk, manifest, pool, resources  # noqa: E402,F401


class ApkInfo(unittest.TestCase):
    def read(self, data):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, 'app.apk')
            with open(p, 'wb') as f:
                f.write(data)
            return frame_apk.apk_info(p)

    def test_resolves_references(self):
        # string type 1: label (entry 0), version (entry 1); mipmap type 2: icon at three densities.
        arsc = resources({(1, 'fr', 0): {0: 0}, (1, '', 0): {0: 1, 1: 2},
                          (2, '', 160): {0: 3}, (2, '', 640): {0: 4}, (2, '', 0xfffe): {0: 5}})
        info = self.read(apk({
            'AndroidManifest.xml': manifest('com.example.demo', 0x7f010000, 0x7f010001, 26),
            'resources.arsc': arsc,
            'res/icon_lo.png': b'lo', 'res/icon_hi.png': b'hi', 'res/icon.xml': b'<xml/>',
            'lib/arm64-v8a/libx.so': b'', 'lib/x86_64/libx.so': b'',
        }))
        self.assertEqual(info['package'], 'com.example.demo')
        self.assertEqual(info['label'], 'App label')  # the default, not French
        self.assertEqual(info['version'], '2.1')
        self.assertEqual(info['version_code'], 210)
        self.assertEqual(info['min_sdk'], 26)
        self.assertEqual(info['abis'], ['arm64-v8a', 'x86_64'])
        self.assertEqual(info['icon_png'], b'hi')  # largest-density PNG, skipping the XML icon

    def test_missing_label_falls_back_to_package(self):
        info = self.read(apk({'AndroidManifest.xml': manifest('com.example.bare', 0x7f010000, 0x7f010001, 21)}))
        self.assertEqual(info['label'], 'com.example.bare')
        self.assertEqual(info['version'], '')
        self.assertEqual(info['abis'], [])

    def test_repacked_manifest(self):
        # Package kept only as a typed value; a foreign `label` mustn't beat android:label.
        arsc = resources({(1, '', 0): {0: 1, 1: 2}})
        info = self.read(apk({
            'AndroidManifest.xml': manifest('com.example.repacked', 0x7f010000, 0x7f010001, 24,
                                            package_raw=False, foreign_label=True),
            'resources.arsc': arsc,
        }))
        self.assertEqual(info['package'], 'com.example.repacked')
        self.assertEqual(info['label'], 'App label')
        self.assertIsNone(info['icon_png'])

    def test_rejects_non_apks(self):
        for data in (b'not a zip', apk({'classes.dex': b''}), apk({'AndroidManifest.xml': b'<manifest/>'})):
            with self.assertRaises(frame_apk.ApkError):
                self.read(data)

    def test_refuses_oversized_members(self):
        # An APK from a website mustn't make the server inflate gigabytes.
        data = apk({'AndroidManifest.xml': manifest('com.example.big', 0x7f010000, 0x7f010001, 21)})
        limit, frame_apk.MAX_MANIFEST = frame_apk.MAX_MANIFEST, 16
        try:
            with self.assertRaises(frame_apk.ApkError):
                self.read(data)
        finally:
            frame_apk.MAX_MANIFEST = limit

    def test_forged_sizes_dont_inflate_everything(self):
        # The central directory claims 1 byte; the deflated data holds 16 MB of zeros.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('AndroidManifest.xml', bytes(16 * 1024**2))
        data = bytearray(buf.getvalue())
        for sig, field in ((b'PK\x01\x02', 24), (b'PK\x03\x04', 22)):
            at = data.index(sig)
            data[at + field:at + field + 4] = struct.pack('<I', 1)
        limit, frame_apk.MAX_MANIFEST = frame_apk.MAX_MANIFEST, 1024**2
        tracemalloc.start()
        try:
            with self.assertRaises(frame_apk.ApkError):
                self.read(bytes(data))
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
            frame_apk.MAX_MANIFEST = limit
        self.assertLess(peak, 8 * 1024**2)

    def test_damaged_download_is_an_apk_error(self):
        # Corrupt deflate data used to escape as zlib.error, which the upload path didn't catch
        # (a 500, and no install report) and frame_android.install reported as 'other'.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('AndroidManifest.xml', manifest('com.example.bad', 0x7f010000, 0x7f010001, 21) * 20)
        data = bytearray(buf.getvalue())
        at = data.index(b'PK\x03\x04')
        start = at + 30 + sum(struct.unpack_from('<HH', data, at + 26))  # past the name and extra field
        for i in range(start, start + 40):
            data[i] ^= 0x5a
        with self.assertRaisesRegex(frame_apk.ApkError, 'could not read the APK manifest'):
            self.read(bytes(data))

    def test_refuses_compression_android_cant_read(self):
        for method in (zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w', method) as z:
                z.writestr('AndroidManifest.xml', manifest('com.example.odd', 0x7f010000, 0x7f010001, 21))
            with self.assertRaisesRegex(frame_apk.ApkError, 'compression'):
                self.read(buf.getvalue())

    def test_reference_cycles_and_fan_out_are_bounded(self):
        res = frame_apk.Resources(b'')
        ref = frame_apk.T_REF
        res.entries = {1: [('', 0, ref, 1)] * 5}  # five references to itself
        self.assertEqual(res.values(1), [])
        # Five references at each of five hops: 3125 leaves without a budget.
        res.entries = {i: [('', 0, ref, i + 1)] * 5 for i in range(1, 6)}
        res.entries[6] = [('', 0, frame_apk.T_STRING, 0)]
        self.assertEqual(len(res.values(1)), frame_apk.MAX_VALUES)
        # Forty references at each hop round a four-id cycle: millions of dead ends.
        looked = []

        class Counting(dict):
            def get(self, key, default=None):
                looked.append(key)
                return dict.get(self, key, default)
        res.entries = Counting({i: [('', 0, ref, i % 4 + 1)] * 40 for i in range(1, 5)})
        self.assertEqual(res.values(1), [])
        self.assertLess(len(looked), frame_apk.MAX_STEPS + 10)


if __name__ == '__main__':
    unittest.main()
