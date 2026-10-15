"""Import from my Quest (ui/frame_quest.py) against a fake adb (tests/fakeadb.py), and
split APK merging in the shared install path (frame_android.install(splits=...))."""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ui'))
import frame_android as android  # noqa: E402
import frame_apk  # noqa: E402
import frame_apk_sign  # noqa: E402
import frame_apk_vr  # noqa: E402
import frame_quest as quest  # noqa: E402
from apk_fixtures import pool  # noqa: E402

FAKEADB = str(Path(__file__).resolve().parent / 'fakeadb.py')
PKG = 'com.example.questgame'
QUEST = {'serial': '1WMHH000ABCDEF', 'state': 'device', 'extra': 'usb:1-1 product:eureka model:Quest_3 transport_id:2',
         'props': {'ro.product.manufacturer': 'Oculus', 'ro.product.model': 'Quest 3'}}
TUNNEL = {'serial': '127.0.0.1:40123', 'state': 'device', 'extra': 'product:lepton model:Lepton transport_id:1',
          'props': {'ro.product.manufacturer': 'Valve', 'ro.product.model': 'Lepton'}}
PHONE = {'serial': 'R58M000PHONE', 'state': 'device', 'extra': 'usb:1-2 model:SM_G990 transport_id:3',
         'props': {'ro.product.manufacturer': 'samsung', 'ro.product.model': 'SM-G990'}}


def axml(package, split=None, split_required=False, resmap=False, feature=False, children=(), version_code=None,
         version_name=None):
    """A minimal binary AndroidManifest.xml: <manifest package split? isFeatureSplit?><application label
    isSplitRequired?>children</application>.

    resmap: name isSplitRequired by its android resource id (as real builds do), not only by name."""
    strings = ['isSplitRequired', 'package', 'split', 'manifest', 'application', 'label', package,
               split or '', 'Quest Game', 'isFeatureSplit', 'versionCode', 'versionName', version_name or '',
               *children]
    none = 0xffffffff

    def start(tag, attrs):
        body = struct.pack('<IIHHHHHH', none, strings.index(tag), 20, 20, len(attrs), 0, 0, 0)
        for name, dtype, value in attrs:
            raw = strings.index(value) if dtype == frame_apk.T_STRING else none
            data = strings.index(value) if dtype == frame_apk.T_STRING else value
            body += struct.pack('<IIIHBBI', none, strings.index(name), raw, 8, 0, dtype, data)
        return struct.pack('<HHIII', 0x0102, 16, 16 + len(body), 1, none) + body

    def end(tag):
        return struct.pack('<HHIIIII', 0x0103, 16, 24, 1, none, none, strings.index(tag))

    chunks = pool(strings)
    if resmap:
        chunks += struct.pack('<HHII', 0x0180, 8, 12, frame_apk_vr.IS_SPLIT_REQUIRED)
    mattrs = [('package', frame_apk.T_STRING, package)] + ([('split', frame_apk.T_STRING, split)] if split else []) \
        + ([('isFeatureSplit', 0x12, 0xffffffff)] if feature else []) \
        + ([('versionCode', frame_apk.T_INT_DEC, version_code)] if version_code is not None else []) \
        + ([('versionName', frame_apk.T_STRING, version_name)] if version_name else [])
    aattrs = ([('isSplitRequired', 0x12, 0xffffffff)] if split_required else []) + [('label', frame_apk.T_STRING, 'Quest Game')]
    inner = b''.join(start(tag, []) + end(tag) for tag in children)
    chunks += start('manifest', mattrs) + start('application', aattrs) + inner + end('application') + end('manifest')
    return struct.pack('<HHI', 3, 8, 8 + len(chunks)) + chunks


def apk_bytes(package=PKG, files=None, deflate=False, **manifest):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED) as z:
        z.writestr('AndroidManifest.xml', axml(package, **manifest))
        for name, data in (files or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def lie_about_size(data, entry, size, crc=None):
    """The zip with entry's uncompressed size in both headers replaced by size (a forged bomb),
    and its checksum by crc if given; the compressed size and bytes are left alone."""
    out = bytearray(data)
    encoded = entry.encode()
    for signature, at, crc_at in ((b'PK\x03\x04', 22, 14), (b'PK\x01\x02', 24, 16)):
        pos = 0
        while True:
            pos = out.find(signature, pos)
            if pos < 0:
                break
            name_at = pos + (30 if signature == b'PK\x03\x04' else 46)
            if out[name_at:name_at + len(encoded)] == encoded:
                struct.pack_into('<I', out, pos + at, size)
                if crc is not None:
                    struct.pack_into('<I', out, pos + crc_at, crc)
            pos += 4
    return bytes(out)


OPENXR = {'lib/arm64-v8a/libopenxr_loader.so': b'loader', 'lib/arm64-v8a/libgame.so': b'game'}


class FakeQuest:
    """A fake adb with a Quest file system in a temporary folder."""

    def __init__(self, test, devices=(QUEST,), apps=None):
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'quest'
        self.log = Path(self.tmp.name) / 'adb.log'
        self.log.write_text('')
        self.state = {'devices': list(devices), 'root': str(self.root), 'packages': {}}
        for pkg, parts in (apps or {}).items():
            self.add(pkg, parts)
        self.save()
        for p in (patch.object(quest, '_adb_cmd', return_value=[sys.executable, FAKEADB]),
                  patch.dict(os.environ, {'FAKEADB_STATE': str(Path(self.tmp.name) / 'state.json'),
                                          'FAKEADB_LOG': str(self.log)})):
            p.start()
            test.addCleanup(p.stop)

    def add(self, pkg, parts, obb=None, data=None):
        paths = []
        for i, content in enumerate(parts):
            name = 'base.apk' if i == 0 else f'split_config.part{i}.apk'
            remote = f'/data/app/~~x==/{pkg}-y==/{name}'
            self.write(remote, content)
            paths.append(remote)
        self.state['packages'][pkg] = paths
        for name, content in (obb or {}).items():
            self.write(f'/sdcard/Android/obb/{pkg}/{name}', content)
        for name, content in (data or {}).items():
            self.write(f'/sdcard/Android/data/{pkg}/{name}', content)
        self.save()

    def write(self, remote, content):
        path = self.root / remote.lstrip('/')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def save(self):
        (Path(self.tmp.name) / 'state.json').write_text(json.dumps(self.state))

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()]


class Detection(unittest.TestCase):
    def test_quest_found_and_frame_tunnel_left_alone(self):
        fake = FakeQuest(self, devices=(TUNNEL, QUEST))
        found = {d['serial']: d for d in quest.devices()}
        self.assertEqual(found[QUEST['serial']]['kind'], 'quest')
        self.assertEqual(found[QUEST['serial']]['model'], 'Quest 3')
        self.assertTrue(found[QUEST['serial']]['usb'])
        self.assertEqual(found[TUNNEL['serial']]['kind'], 'frame-tunnel')
        self.assertEqual(quest.find_quest()['serial'], QUEST['serial'])
        # Frame Control's own tunnelled adb connection is never queried.
        self.assertFalse([c for c in fake.calls() if TUNNEL['serial'] in c])

    def test_quest_recognised_by_model_and_the_frame_on_usb(self):
        meta_branded = dict(QUEST, props={'ro.product.manufacturer': 'Meta', 'ro.product.model': 'Quest 3S'})
        frame_usb = dict(PHONE, serial='FRAME0USB', props={'ro.product.manufacturer': 'Valve', 'ro.product.model': 'Frame'})
        FakeQuest(self, devices=(meta_branded, frame_usb))
        kinds = {d['serial']: d['kind'] for d in quest.devices()}
        self.assertEqual(kinds, {QUEST['serial']: 'quest', 'FRAME0USB': 'frame'})

    def test_nothing_connected(self):
        for devices in ((), (TUNNEL,), ({'serial': 'emulator-5554', 'state': 'device'},)):
            FakeQuest(self, devices=devices)
            with self.assertRaises(quest.QuestError) as e:
                quest.find_quest()
            self.assertIn('No Quest found', str(e.exception))
            self.assertIn('developer organisation', str(e.exception))
            self.assertIn('verified Meta account', str(e.exception))

    def test_unauthorised(self):
        FakeQuest(self, devices=(dict(QUEST, state='unauthorized', extra='usb:1-1 transport_id:2'),))
        with self.assertRaisesRegex(quest.QuestError, 'Allow USB debugging'):
            quest.find_quest()
        status = quest.status()
        self.assertIsNone(status['quest'])
        self.assertIn('Allow USB debugging', status['guidance'])
        self.assertEqual(status['devices'][0]['kind'], 'unknown')

    def test_no_permissions_and_offline(self):
        FakeQuest(self, devices=(dict(QUEST, state='no', extra='permissions (user in plugdev group; are your udev '
                                                              'rules wrong?); see [http://developer.android.com/tools/device.html]'),))
        with self.assertRaisesRegex(quest.QuestError, 'udev'):
            quest.find_quest()
        FakeQuest(self, devices=(dict(QUEST, state='offline'),))
        with self.assertRaisesRegex(quest.QuestError, 'offline'):
            quest.find_quest()

    def test_other_android_device_and_several_quests(self):
        FakeQuest(self, devices=(PHONE,))
        with self.assertRaisesRegex(quest.QuestError, "samsung SM-G990, which isn't a Quest"):
            quest.find_quest()
        with self.assertRaisesRegex(quest.QuestError, "isn't a Meta Quest"):
            quest.find_quest(PHONE['serial'])
        FakeQuest(self, devices=(QUEST, dict(QUEST, serial='2G0000SECOND')))
        with self.assertRaisesRegex(quest.QuestError, 'Several Quests'):
            quest.find_quest()
        self.assertEqual(quest.find_quest('2G0000SECOND')['serial'], '2G0000SECOND')

    def test_adb_missing(self):
        with patch.object(quest.frame_host, 'adb', side_effect=quest.frame_host.HostError("adb isn't installed")):
            status = quest.status()
        self.assertIn("adb isn't installed", status['guidance'])


class Listing(unittest.TestCase):
    def test_packages_splits_obb_and_data(self):
        fake = FakeQuest(self)
        fake.add(PKG, [apk_bytes()], obb={f'main.3.{PKG}.obb': b'x' * 5000}, data={'files/save.dat': b'save'})
        fake.add('org.example.split', [apk_bytes('org.example.split'), b'split'])
        fake.add('com.oculus.extra', [b'apk'])
        apps = {a['package']: a for a in quest.packages(QUEST['serial'])}
        self.assertEqual(set(apps), {PKG, 'org.example.split', 'com.oculus.extra'})
        game = apps[PKG]
        self.assertFalse(game['split'])
        self.assertEqual(len(game['apks']), 1)
        self.assertGreater(game['obb'], 0)
        self.assertGreater(game['data'], 0)
        self.assertFalse(game['meta'])
        self.assertTrue(apps['org.example.split']['split'])
        self.assertEqual(len(apps['org.example.split']['apks']), 2)
        self.assertIsNone(apps['org.example.split']['obb'])
        self.assertIsNone(apps['org.example.split']['data'])
        self.assertTrue(apps['com.oculus.extra']['meta'])
        status = quest.status()
        self.assertEqual(status['quest']['serial'], QUEST['serial'])
        self.assertEqual(len(status['apps']), 3)


class Inspection(unittest.TestCase):
    def write(self, data):
        tmp = tempfile.NamedTemporaryFile(suffix='.apk', delete=False)
        tmp.write(data)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def test_meta_licence_check_warned_not_refused_in_base_or_split(self):
        # Linking the Platform SDK doesn't mean the check is enforced: install, with a warning.
        base = self.write(apk_bytes(files=dict(OPENXR, **{'lib/arm64-v8a/libovrplatformloader.so': b''})))
        result = quest.inspect(PKG, base)
        self.assertTrue(result['platform_sdk'])
        self.assertEqual(len(result['warnings']), 1)
        warning = result['warnings'][0]
        self.assertIn("uses meta's licence check", warning.lower())
        self.assertIn('may quit, hang, or run without online features', warning)
        self.assertIn("Frame Control won't remove the check", warning)
        base = self.write(apk_bytes(split_required=True))
        split = self.write(apk_bytes(split='config.arm64_v8a', files={'lib/arm64-v8a/libovrplatformloader.so': b''}))
        result = quest.inspect(PKG, base, [split])
        self.assertTrue(result['platform_sdk'])
        self.assertIn('licence check', result['warnings'][0])
        self.assertFalse(quest.inspect(PKG, self.write(apk_bytes(files=OPENXR)))['platform_sdk'])

    def test_32_bit_only_refused(self):
        base = self.write(apk_bytes(files={'lib/armeabi-v7a/libgame.so': b''}))
        with self.assertRaisesRegex(quest.QuestError, '32-bit-only build \\(armeabi-v7a\\)'):
            quest.inspect(PKG, base)

    def test_vrapi_only_flagged_openxr_fine(self):
        base = self.write(apk_bytes(files={'lib/arm64-v8a/libvrapi.so': b'', 'lib/arm64-v8a/libgame.so': b''}))
        result = quest.inspect(PKG, base)
        self.assertIn('VrApi', result['warnings'][0])
        self.assertIn('may not run', result['warnings'][0])
        result = quest.inspect(PKG, self.write(apk_bytes(files=dict(OPENXR, **{'lib/arm64-v8a/libvrapi.so': b''}))))
        self.assertEqual(result['warnings'], [])
        self.assertTrue(result['vr'])
        self.assertEqual(result['label'], 'Quest Game')

    def test_wrong_package_and_unreadable(self):
        with self.assertRaisesRegex(quest.QuestError, 'says it is org.other'):
            quest.inspect(PKG, self.write(apk_bytes('org.other.game')))
        with self.assertRaisesRegex(quest.QuestError, 'not a readable APK'):
            quest.inspect(PKG, self.write(b'not a zip'))


class SplitInstall(unittest.TestCase):
    """frame_android.install(base, splits=[...]): Lepton runs one APK, so splits are merged into it."""

    def files(self, *parts):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = []
        for i, data in enumerate(parts):
            p = os.path.join(tmp.name, f'part{i}.apk')
            with open(p, 'wb') as f:
                f.write(data)
            paths.append(p)
        return paths

    def test_merge_adds_split_files_drops_split_required_and_signs(self):
        for resmap in (False, True):
            base, abi, assets = self.files(
                apk_bytes(split_required=True, resmap=resmap, files={'assets/base.txt': b'base'}),
                apk_bytes(split='config.arm64_v8a', files=dict(OPENXR, **{'META-INF/CERT.RSA': b'sig'})),
                apk_bytes(split='install_time_pack', files={'assets/level1.pak': b'level'}))
            installed = {}

            def fake_install(apk_path, info, pkg, *rest):
                with zipfile.ZipFile(apk_path) as z:
                    installed.update(names=set(z.namelist()), so=z.read('lib/arm64-v8a/libgame.so'),
                                     manifest=z.read('AndroidManifest.xml'))
                installed.update(info=info, signed=frame_apk_sign.verify(apk_path))
                return {'package': pkg, 'label': info['label']}
            with patch.object(android, '_install', side_effect=fake_install), \
                    patch.object(android, 'xr_compat_files', return_value={}):
                meta = android.install(base, splits=[abi, assets])
            self.assertEqual(meta['package'], PKG)
            self.assertTrue({'lib/arm64-v8a/libgame.so', 'lib/arm64-v8a/libopenxr_loader.so',
                             'assets/level1.pak', 'assets/base.txt'} <= installed['names'])
            self.assertNotIn('META-INF/CERT.RSA', installed['names'])
            self.assertEqual(installed['so'], b'game')
            self.assertTrue(installed['signed'])
            self.assertTrue(installed['info']['vr'])  # the OpenXR loader came from the split
            self.assertEqual(installed['info']['label'], 'Quest Game')  # other attributes intact
            app = dict(frame_apk.manifest_elements(installed['manifest']))['application']
            self.assertNotIn('isSplitRequired', app)
            self.assertEqual(frame_apk.apk_info(base)['package'], PKG)  # the inputs are left alone

    def test_resource_splits_and_strangers_refused_before_the_frame(self):
        base, density, other = self.files(
            apk_bytes(split_required=True),
            apk_bytes(split='config.xxhdpi', files={'resources.arsc': b'', 'res/drawable/icon.png': b''}),
            apk_bytes('org.someone.else', split='config.arm64_v8a', files=OPENXR))
        reported = []
        with patch.object(android, '_install') as frame, \
                patch.object(android, 'install_hooks', [lambda *a: reported.append(a)]):
            with self.assertRaisesRegex(android.FrameError, 'density or language'):
                android.install(base, splits=[density])
            with self.assertRaisesRegex(android.FrameError, 'not part of ' + PKG):
                android.install(base, splits=[other])
            frame.assert_not_called()
        self.assertEqual(len(reported), 2)
        self.assertTrue(all(r[2] is not None for r in reported))

    def test_duplicate_entries_refused(self):
        base, split = self.files(apk_bytes(files=OPENXR), apk_bytes(split='config.arm64_v8a', files=OPENXR))
        with self.assertRaisesRegex(android.FrameError, 'both contain'):
            android.split_entries(base, [split])

    def test_cli_passes_splits(self):
        with patch.object(android, 'install', return_value={}) as install, \
                patch.object(sys, 'argv', ['frame_android.py', 'install', 'base.apk', 'split.apk', '--vr']):
            with patch('builtins.print'):
                android.main()
        install.assert_called_once_with('base.apk', flatscreen=False, xr_compat=None, splits=['split.apk'])


class SplitLimits(unittest.TestCase):
    """Only splits that add native libraries or assets are merged, and never unbounded."""

    def files(self, *parts):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = []
        for i, data in enumerate(parts):
            p = os.path.join(tmp.name, f'part{i}.apk')
            with open(p, 'wb') as f:
                f.write(data)
            paths.append(p)
        return paths

    def refused(self, split, words):
        base, part = self.files(apk_bytes(split_required=True), split)
        with patch.object(android, '_install') as frame:
            with self.assertRaisesRegex(android.FrameError, words):
                android.install(base, splits=[part])
            frame.assert_not_called()

    def test_code_components_and_feature_splits_refused(self):
        self.refused(apk_bytes(split='config.arm64_v8a', files=dict(OPENXR, **{'classes2.dex': b'dex'})),
                     r'contains code \(classes2\.dex\)')
        self.refused(apk_bytes(split='config.arm64_v8a', children=('service',), files=OPENXR), '<service>')
        self.refused(apk_bytes(split='config.arm64_v8a', children=('uses-library',), files=OPENXR), '<uses-library>')
        self.refused(apk_bytes(split='feature_vr', feature=True, files=OPENXR), 'feature split')
        self.refused(apk_bytes(split='config.arm64_v8a', files={'kotlin/x.kotlin_builtins': b''}), 'contains kotlin/')
        self.refused(apk_bytes(split='config.arm64_v8a', files={'lib/arm64-v8a/sub/libx.so': b''}), 'contains lib/')

    def test_resource_tables(self):
        from apk_fixtures import resources
        self.refused(apk_bytes(split='config.en', files={'resources.arsc': resources({(1, 'en', 0): {0: 1}})}),
                     'density or language')
        base, empty = self.files(apk_bytes(split_required=True),
                                 apk_bytes(split='config.arm64_v8a', files=dict(OPENXR, **{'resources.arsc': resources({})})))
        self.assertEqual(set(android.split_entries(base, [empty])), set(OPENXR))

    def test_zip_bomb_refused_by_headers_and_by_reads(self):
        bomb = {'assets/bomb.bin': b'\0' * (8 * 1024**2)}
        split = apk_bytes(split='install_time', files=bomb, deflate=True)
        self.assertLess(len(split), 64 * 1024)  # tiny on disk
        base, part = self.files(apk_bytes(split_required=True), split)
        with patch.object(android, 'MAX_SPLIT_MEMBER', 1024**2):
            with self.assertRaisesRegex(android.FrameError, 'more than Frame Control merges'):
                android.split_entries(base, [part])
        with patch.object(android, 'MAX_SPLIT_TOTAL', 4 * 1024**2):
            with self.assertRaisesRegex(android.FrameError, 'more than 0 GB|more than Frame Control merges'):
                android.split_entries(base, [part])
        # Headers that understate the size: the read stops at what they promise, and the
        # truncated member fails its checksum instead of filling memory.
        base, forged = self.files(apk_bytes(split_required=True), lie_about_size(split, 'assets/bomb.bin', 1000))
        self.assertEqual(android.split_size([forged]), 1000)
        with patch.object(android, 'repack') as repack:
            with self.assertRaises(android.FrameError):
                android.merge_splits(base, [forged], os.path.join(os.path.dirname(base), 'out.apk'))
            repack.assert_not_called()


class SplitForgeries(unittest.TestCase):
    """Members whose headers understate their size, with a checksum that matches the prefix."""

    def merge(self, split):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base, part = os.path.join(tmp.name, 'base.apk'), os.path.join(tmp.name, 'split.apk')
        with open(base, 'wb') as f:
            f.write(apk_bytes(split_required=True))
        with open(part, 'wb') as f:
            f.write(split)
        with patch.object(android, 'repack') as repack:
            with self.assertRaisesRegex(android.FrameError, 'does not match its header'):
                android.merge_splits(base, [part], os.path.join(tmp.name, 'out.apk'))
            repack.assert_not_called()

    def test_stored_and_deflated_truncation_refused(self):
        content = bytes(range(256)) * 64  # 16 KB
        prefix = zlib.crc32(content[:1000])
        for deflate in (False, True):
            split = apk_bytes(split='install_time', deflate=deflate, files={'assets/level.pak': content})
            forged = lie_about_size(split, 'assets/level.pak', 1000, crc=prefix)
            # Python's own reader is fooled: it stops at the declared size and the prefix checksum matches.
            with zipfile.ZipFile(io.BytesIO(forged)) as z:
                self.assertEqual(z.read('assets/level.pak'), content[:1000])
            self.merge(forged)

    def test_honest_members_still_merge(self):
        content = bytes(range(256)) * 64
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for deflate in (False, True):
            part = os.path.join(tmp.name, f'split{deflate}.apk')
            with open(part, 'wb') as f:
                f.write(apk_bytes(split='install_time', deflate=deflate, files={'assets/level.pak': content}))
            self.assertEqual(android.read_member(part, 'assets/level.pak'), content)

    def test_bag_only_resource_table_is_not_empty(self):
        from apk_fixtures import resources
        simple = resources({(1, '', 0): {0: 1}})
        entry = struct.pack('<HHI', 8, 0, 0) + struct.pack('<HBBI', 8, 0, frame_apk.T_STRING, 1)
        self.assertIn(entry, simple)
        bag = simple.replace(entry, struct.pack('<HHI', 8, 0x0001, 0) + entry[8:])
        self.assertEqual(frame_apk.Resources(bag).entries, {})  # the label parser skips bags
        self.assertFalse(android.resources_empty(bag))
        self.assertFalse(android.resources_empty(simple))
        self.assertTrue(android.resources_empty(resources({})))
        self.assertFalse(android.resources_empty(resources({}) + b'\x07\x02\x08\x00\x08\x00\x00\x00'))  # unknown chunk
        self.assertFalse(android.resources_empty(b'junk'))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base, part = os.path.join(tmp.name, 'b.apk'), os.path.join(tmp.name, 's.apk')
        with open(base, 'wb') as f:
            f.write(apk_bytes(split_required=True))
        with open(part, 'wb') as f:
            f.write(apk_bytes(split='config.arm64_v8a', files=dict(OPENXR, **{'resources.arsc': bag})))
        with self.assertRaisesRegex(android.FrameError, 'density or language'):
            android.split_entries(base, [part])


class Import(unittest.TestCase):
    META = {'package': PKG, 'label': 'Quest Game', 'instance': android.instance_id(PKG), 'vr_issues': [],
            'version': '1.0', 'version_code': 100}

    def setUp(self):
        self.order = []
        self.running = False
        self.installed = None
        self.stop_fails = None
        self.android_code = 100
        self.apk_code = None  # what the installed app.apk on the Frame says (legacy records)

        def install(base, splits=(), source=None):
            self.order.append(('install', os.path.basename(base), [os.path.basename(s) for s in splits]))
            self.assertTrue(os.path.isfile(base))
            self.assertTrue(all(os.path.isfile(s) for s in splits))
            self.assertEqual(source, 'Meta Quest (USB)')
            self.assertTrue(android._install_lock._is_owned())  # checked and installed under the lock
            return dict(self.META)

        def install_obb(pkg, paths):
            self.order.append(('obb', sorted(os.path.basename(p) for p in paths)))
            self.assertTrue(all(os.path.isfile(p) for p in paths))
            self.assertTrue(android._install_lock._is_owned())
            return {'obb': [{'name': os.path.basename(p)} for p in paths]}

        def push_data(m, folder):
            self.order.append(('data', sorted(p.relative_to(folder).as_posix() for p in Path(folder).rglob('*') if p.is_file())))
            self.assertTrue(android._install_lock._is_owned())
            return 1

        def stop(pkg):
            self.order.append(('stop',))
            if self.stop_fails == 'error':
                raise android.FrameError('Steam is not answering')
            if self.stop_fails != 'still running':
                self.running = False

        def android_package(meta, code):
            self.order.append(('check',))
            if self.android_code != code:
                raise quest.QuestError(f'Android has version code {self.android_code}')

        for target, name, effect in ((android, 'install', install), (android, 'install_obb', install_obb),
                                     (android, 'launch', lambda pkg: self.order.append(('launch',))),
                                     (android, 'stop', stop),
                                     (android, 'read_meta', lambda pkg: self.installed),
                                     (quest, '_running', lambda pkg: self.running),
                                     (quest, '_sleep', lambda s: None),
                                     (quest, '_check_android_package', android_package),
                                     (android, 'installed_version_code', lambda pkg: self.apk_code),
                                     (quest, '_wait_ready', lambda m: self.order.append(('ready',))),
                                     (quest, 'push_data', push_data)):
            p = patch.object(target, name, side_effect=effect)
            p.start()
            self.addCleanup(p.stop)

    def open_on_frame(self, **meta):
        self.running, self.installed = True, dict(self.META, **meta)

    def cache_left(self):
        root = quest.cache_root()
        return list(root.iterdir()) if root.is_dir() else []

    def pulls(self, fake):
        return [c[3] for c in fake.calls() if c[2:3] == ['pull']]

    def game(self, fake, code=100, name='1.0'):
        fake.add(PKG, [apk_bytes(files=OPENXR, version_code=code, version_name=name)],
                 obb={f'main.12.{PKG}.obb': b'main', f'patch.12.{PKG}.obb': b'patch', 'notes.txt': b'?'},
                 data={'files/save.dat': b'save'})

    def test_first_import_installs_and_never_starts_the_game(self):
        fake = FakeQuest(self)
        self.game(fake)
        stages = []
        result = quest.import_game(PKG, data=True, progress=lambda stage, percent=None: stages.append(percent))
        self.assertEqual(self.order, [('install', 'base.apk', [])])
        self.assertTrue(result['needs_game_data'])
        self.assertIn('start Quest Game from the Steam library', result['message'])
        self.assertIn('OBB game data and its Android/data files', result['message'])
        self.assertEqual(len(self.pulls(fake)), 1)  # the game data waits for the second import
        self.assertEqual(stages, sorted(stages))
        self.assertEqual(stages[-1], 100)
        self.assertEqual(self.cache_left(), [])

    def test_older_quest_copy_never_silently_replaces_a_newer_install(self):
        fake = FakeQuest(self)
        self.game(fake, code=100, name='1.0')
        self.installed = dict(self.META, version='1.0', version_code=999)  # same name, newer code
        with self.assertRaisesRegex(quest.QuestError, r'newer Quest Game \(version 1.0 code 999\).*code 100'):
            quest.import_game(PKG)
        self.assertEqual(self.order, [])
        quest.import_game(PKG, allow_downgrade=True)
        self.assertEqual(self.order, [('install', 'base.apk', [])])
        self.installed = dict(self.META, version_code=50)  # an upgrade goes ahead
        quest.import_game(PKG)
        self.assertEqual(len(self.order), 2)
        # Installs made before records kept a version code: read from the installed app.apk.
        self.installed = dict(self.META, version_code=None)
        self.apk_code = 999
        with self.assertRaisesRegex(quest.QuestError, r'newer Quest Game \(version 1.0 code 999\)'):
            quest.import_game(PKG)
        self.apk_code = None  # unreadable: can't tell, so only with permission
        with self.assertRaisesRegex(quest.QuestError, "can't tell which version"):
            quest.import_game(PKG)
        self.assertEqual(len(self.order), 2)
        quest.import_game(PKG, allow_downgrade=True)
        self.apk_code = 50
        quest.import_game(PKG)
        self.assertEqual(len(self.order), 4)
        self.assertEqual(self.cache_left(), [])

    def test_second_import_with_the_game_open_copies_data_then_closes_it(self):
        fake = FakeQuest(self)
        self.game(fake)
        self.open_on_frame()
        result = quest.import_game(PKG, data=True)
        self.assertEqual(self.order, [('ready',), ('check',), ('obb', [f'main.12.{PKG}.obb', f'patch.12.{PKG}.obb']),
                                      ('data', ['files/save.dat']), ('stop',)])  # no install, no launch
        pulls = self.pulls(fake)
        self.assertTrue(pulls[0].endswith('/base.apk'))  # the APK is checked before the OBB is copied
        self.assertEqual(pulls[1:], [f'/sdcard/Android/obb/{PKG}', f'/sdcard/Android/data/{PKG}'])
        self.assertIn('notes.txt', ' '.join(result['warnings']))
        self.assertEqual(len(result['obb']), 2)
        self.assertTrue(result['closed'])
        self.assertIn('the game is closed', result['message'])
        self.assertFalse(result['needs_game_data'])
        self.assertEqual(self.cache_left(), [])

    def test_data_goes_only_to_this_exact_install(self):
        fake = FakeQuest(self)
        self.game(fake, code=100, name='1.0')
        cases = [  # (install record, what the refusal says)
            (dict(version='1.0', version_code=999), 'different version'),  # same name, other code
            (dict(version_code=None), 'different version'),  # a legacy record, app.apk unreadable
            (dict(package='org.other.game'), 'no matching install record'),
            (dict(instance=android.instance_id('org.other.game')), 'no matching install record'),
        ]
        for meta, words in cases:
            self.open_on_frame(**meta)
            with self.assertRaisesRegex(quest.QuestError, words):
                quest.import_game(PKG, data=True)
        self.running, self.installed = True, None
        with self.assertRaisesRegex(quest.QuestError, 'no matching install record'):
            quest.import_game(PKG)
        self.assertEqual(self.order, [])
        self.assertFalse([p for p in self.pulls(fake) if '/sdcard/' in p])
        # Android in the instance has another version than the record says: nothing is copied.
        self.open_on_frame()
        self.android_code = 999
        with self.assertRaisesRegex(quest.QuestError, 'Android has version code 999.*The game was closed'):
            quest.import_game(PKG, data=True)
        self.assertEqual(self.order, [('ready',), ('check',), ('stop',)])
        fake.add('org.example.plain', [apk_bytes('org.example.plain', files=OPENXR, version_code=100)])
        self.open_on_frame(package='org.example.plain', instance=android.instance_id('org.example.plain'))
        with self.assertRaisesRegex(quest.QuestError, 'no game data to add'):
            quest.import_game('org.example.plain')
        self.assertEqual(self.cache_left(), [])

    def test_target_checked_again_after_the_long_pull(self):
        fake = FakeQuest(self)
        self.game(fake)
        self.open_on_frame()
        pull = quest._pull

        def pull_then_reinstall(serial, remote, local):
            pull(serial, remote, local)
            if '/obb/' in remote:  # someone installs another version meanwhile
                self.installed = dict(self.META, version='2.0', version_code=200)
        with patch.object(quest, '_pull', side_effect=pull_then_reinstall):
            with self.assertRaisesRegex(quest.QuestError, 'different version'):
                quest.import_game(PKG)
        self.assertEqual(self.order, [])
        self.installed = dict(self.META)

        def pull_then_close(serial, remote, local):
            pull(serial, remote, local)
            if '/obb/' in remote:
                self.running = False
        with patch.object(quest, '_pull', side_effect=pull_then_close):
            with self.assertRaisesRegex(quest.QuestError, 'was closed on the Frame during the import'):
                quest.import_game(PKG)
        self.assertEqual(self.order, [])
        self.assertEqual(self.cache_left(), [])

    def test_the_data_copy_holds_the_install_lock(self):
        fake = FakeQuest(self)
        self.game(fake)
        self.open_on_frame()
        import threading
        seen = []

        def other_install():
            seen.append(android._install_lock.acquire(blocking=False))

        def push_and_race(m, folder):
            t = threading.Thread(target=other_install)
            t.start()
            t.join()
            return 1
        quest.push_data.side_effect = push_and_race
        quest.import_game(PKG, data=True)
        self.assertEqual(seen, [False])  # another thread's install or remove waits
        self.assertTrue(android._install_lock.acquire(blocking=False))
        android._install_lock.release()

    def test_closing_failures_are_reported(self):
        fake = FakeQuest(self)
        self.game(fake)
        self.open_on_frame()
        self.stop_fails = 'still running'
        result = quest.import_game(PKG)
        self.assertFalse(result['closed'])
        self.assertIn("couldn't close the game (it is still running)", result['message'])
        self.assertNotIn('the game is closed', result['message'])
        self.stop_fails = 'error'
        result = quest.import_game(PKG)
        self.assertFalse(result['closed'])
        self.assertIn('Steam is not answering', result['message'])
        android.install_obb.side_effect = android.FrameError('checksum mismatch')
        with self.assertRaisesRegex(quest.QuestError, "checksum mismatch.*also couldn't close the game "
                                                      r"\(Steam is not answering\)"):
            quest.import_game(PKG)
        self.assertEqual(self.cache_left(), [])

    def test_data_only_copied_when_asked(self):
        fake = FakeQuest(self)
        fake.add(PKG, [apk_bytes(files=OPENXR)], data={'files/save.dat': b'save'})
        result = quest.import_game(PKG)
        self.assertEqual(self.order, [('install', 'base.apk', [])])
        self.assertFalse(result['needs_game_data'])
        self.assertEqual(len(self.pulls(fake)), 1)

    def test_split_apk(self):
        fake = FakeQuest(self)
        fake.add(PKG, [apk_bytes(split_required=True), apk_bytes(split='config.arm64_v8a', files=OPENXR)])
        result = quest.import_game(PKG)
        self.assertTrue(result['split'])
        self.assertEqual(self.order, [('install', 'base.apk', ['01-split_config.part1.apk'])])
        self.assertEqual(len(self.pulls(fake)), 2)
        self.assertEqual(self.cache_left(), [])

    def test_licence_check_installs_with_a_warning(self):
        fake = FakeQuest(self)
        fake.add(PKG, [apk_bytes(files=dict(OPENXR, **{'lib/arm64-v8a/libovrplatformloader.so': b''}))])
        result = quest.import_game(PKG)
        self.assertEqual(self.order, [('install', 'base.apk', [])])
        self.assertIn("Meta's licence check", result['message'])
        self.assertIn("Frame Control won't remove the check", result['message'])
        self.assertEqual(result['warnings'], [quest.ENTITLEMENT])
        self.assertEqual(self.cache_left(), [])

    def test_refusals_copy_nothing_more_and_clean_up(self):
        fake = FakeQuest(self)
        fake.add('org.example.old', [apk_bytes('org.example.old', files={'lib/armeabi-v7a/libgame.so': b''})],
                 obb={'main.1.org.example.old.obb': b'big'})
        with self.assertRaisesRegex(quest.QuestError, '32-bit'):
            quest.import_game('org.example.old')
        fake.add(PKG, [apk_bytes(files=OPENXR)], obb={f'main.1.{PKG}.obb': b'big'})
        real_info = android.apk_info
        with patch.object(android, 'apk_info', side_effect=lambda p: dict(real_info(p), min_sdk=31)):
            with self.assertRaisesRegex(android.FrameError, 'API 31'):
                quest.import_game(PKG)
        self.assertEqual(self.order, [])
        self.assertFalse([p for p in self.pulls(fake) if '/obb/' in p])
        self.assertEqual(self.cache_left(), [])

    def test_failures_clean_up_and_close_the_game(self):
        fake = FakeQuest(self)
        fake.add(PKG, [apk_bytes(files=OPENXR, version_code=100)], obb={f'main.1.{PKG}.obb': b'main'})
        self.open_on_frame()
        fake.state['fail_pull'] = f'/sdcard/Android/obb/{PKG}'
        fake.save()
        with self.assertRaisesRegex(quest.QuestError, 'Connection reset'):
            quest.import_game(PKG)
        self.assertEqual(self.cache_left(), [])
        fake.state.pop('fail_pull')
        fake.save()
        android.install_obb.side_effect = android.FrameError('checksum mismatch')
        with self.assertRaisesRegex(quest.QuestError, "Copying Quest Game's game data failed: checksum mismatch"
                                                      r'\. The game was closed'):
            quest.import_game(PKG)
        self.assertEqual(self.order[-1], ('stop',))  # never left running half-set-up
        self.assertNotIn(('launch',), self.order)
        self.assertEqual(self.cache_left(), [])

    def test_not_on_quest_bad_names_and_one_at_a_time(self):
        FakeQuest(self)
        with self.assertRaisesRegex(quest.QuestError, "isn't installed on the Quest"):
            quest.import_game(PKG)
        with self.assertRaisesRegex(quest.QuestError, 'invalid package'):
            quest.import_game('../etc; rm -rf')
        with quest._import_lock:
            with self.assertRaisesRegex(quest.QuestError, 'Another Quest import'):
                quest.import_game(PKG)

    def test_space_estimate(self):
        # APKs as copied, merged + patched copies, OBB, and Android/data once (streamed, not staged).
        self.assertEqual(quest.space_needed(10, 30, 100, 1000), 10 + 60 + 100 + 1000)
        fake = FakeQuest(self)
        fake.add(PKG, [apk_bytes(files=OPENXR)])
        with patch.object(quest.shutil, 'disk_usage', return_value=SimpleNamespace(total=0, used=0, free=10)):
            with self.assertRaisesRegex(quest.QuestError, 'GB free'):
                quest.import_game(PKG)
        self.assertEqual(self.cache_left(), [])

    def test_space_counts_splits_uncompressed(self):
        fake = FakeQuest(self)
        fake.add(PKG, [apk_bytes(split_required=True),
                       apk_bytes(split='install_time', deflate=True, files={'assets/level.pak': b'\0' * (4 * 1024**2)})])
        # Enough for the compressed copies, not for merging the 4 MB the split inflates to.
        with patch.object(quest.shutil, 'disk_usage', return_value=SimpleNamespace(total=0, used=0, free=1024**2)):
            with self.assertRaisesRegex(quest.QuestError, 'GB free'):
                quest.import_game(PKG)
        self.assertEqual(self.order, [])
        self.assertEqual(len(self.pulls(fake)), 2)
        self.assertEqual(self.cache_left(), [])

    def test_leftovers_from_a_crash_are_swept(self):
        old = quest.cache_root() / 'org.example.crashed-0000'
        old.mkdir(parents=True)
        os.utime(str(old), (1, 1))
        fresh = quest.cache_root() / 'org.example.running-0000'
        fresh.mkdir()
        quest.sweep_cache()
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        fresh.rmdir()


class AndroidChecks(unittest.TestCase):
    META = {'package': PKG, 'label': 'Quest Game', 'instance': 2800000001}

    def test_android_package_version_in_the_instance(self):
        dump = 'Packages:\n  Package [com.example.questgame] (abc):\n    versionCode=100 minSdk=29 targetSdk=29\n'
        with patch.object(android, 'ssh', return_value=dump) as ssh:
            quest._check_android_package(self.META, 100)
        self.assertIn("podman exec lepton-steamlaunch-2800000001 /system/bin/sh -c 'dumpsys package "
                      f"{PKG}'", ssh.call_args[0][0])
        with patch.object(android, 'ssh', return_value=dump):
            with self.assertRaisesRegex(quest.QuestError, 'version code 100, not the Quest.s 999'):
                quest._check_android_package(self.META, 999)
        with patch.object(android, 'ssh', return_value=''):
            with self.assertRaisesRegex(quest.QuestError, 'does not have'):
                quest._check_android_package(self.META, 100)

    def test_close_confirms_the_instance_stopped(self):
        states = iter([True, True, False])
        with patch.object(android, 'stop'), patch.object(quest, '_running', side_effect=lambda p: next(states)), \
                patch.object(quest, '_sleep'):
            self.assertEqual(quest._close(PKG), (True, None))
        with patch.object(android, 'stop'), patch.object(quest, '_running', return_value=True), \
                patch.object(quest, '_sleep'):
            self.assertEqual(quest._close(PKG), (False, 'it is still running'))
        with patch.object(android, 'stop', side_effect=android.FrameError('no Steam')):
            self.assertEqual(quest._close(PKG), (False, 'no Steam'))


class Round3(unittest.TestCase):
    """Strict ZIP reading, closing the game whatever happens, fail-closed listing, scoped dumpsys."""

    def tmpfile(self, data):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, 'split.apk')
        with open(path, 'wb') as f:
            f.write(data)
        return path

    @staticmethod
    def build_zip(entries):
        """A ZIP from raw entries: name, method, data (the bytes after the local header), crc,
        csize, usize, flags, tail (a data descriptor), local/central field overrides."""
        out, central = bytearray(), bytearray()
        for e in entries:
            local = dict(dict(crc=e['crc'], csize=e['csize'], usize=e['usize'], name=e['name']), **e.get('local', {}))
            cen = dict(dict(crc=e['crc'], csize=e['csize'], usize=e['usize'], name=e['name']), **e.get('central', {}))
            offset = len(out)
            name = local['name'].encode()
            out += struct.pack('<IHHHHHIIIHH', 0x04034b50, 20, e.get('flags', 0), e['method'], 0, 0x21,
                               local['crc'], local['csize'], local['usize'], len(name), 0) + name
            out += e['data'] + e.get('tail', b'')
            name = cen['name'].encode()
            central += struct.pack('<IHHHHHHIIIHHHHHII', 0x02014b50, 20, 20, e.get('flags', 0), e['method'], 0, 0x21,
                                   cen['crc'], cen['csize'], cen['usize'], len(name), 0, 0, 0, 0, 0, offset) + name
        start = len(out)
        out += central + struct.pack('<IHHHHIIH', 0x06054b50, 0, 0, len(entries), len(entries), len(central), start, 0)
        return bytes(out)

    def entry(self, name, content, deflate=False, **extra):
        data = content
        if deflate:
            c = zlib.compressobj(6, zlib.DEFLATED, -15)
            data = c.compress(content) + c.flush()
        e = dict(name=name, method=8 if deflate else 0, data=data, crc=zlib.crc32(content),
                 csize=len(data), usize=len(content))
        e.update(extra)
        return e

    def refused(self, data, words, member='assets/x'):
        with self.assertRaisesRegex(android.FrameError, words):
            android.read_member(self.tmpfile(data), member)

    def test_valid_formats_read(self):
        content = bytes(range(256)) * 40
        for deflate in (False, True):
            self.assertEqual(android.read_member(self.tmpfile(self.build_zip([self.entry('assets/x', content, deflate)])),
                                                 'assets/x'), content)
            # Python's own writer: plain, ZIP64 local extra fields, and data descriptors (unseekable output).
            method = zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w', method) as z:
                z.writestr('assets/plain', content)
                with z.open('assets/zip64', 'w', force_zip64=True) as f:
                    f.write(content)
            path = self.tmpfile(buf.getvalue())
            self.assertEqual(android.read_member(path, 'assets/plain'), content)
            self.assertEqual(android.read_member(path, 'assets/zip64'), content)

            class Sink:
                def __init__(self):
                    self.buf = io.BytesIO()

                def write(self, b):
                    return self.buf.write(b)

                def flush(self):
                    pass
            sink = Sink()
            with zipfile.ZipFile(sink, 'w', method) as z:
                z.writestr('assets/streamed', content)
                # Streamed with ZIP64 forced: a 64-bit data descriptor for a small file.
                with z.open('assets/streamed64', 'w', force_zip64=True) as f:
                    f.write(content)
                z.writestr('assets/after', b'next entry')
            path = self.tmpfile(sink.buf.getvalue())
            with zipfile.ZipFile(path) as z:
                self.assertTrue(z.getinfo('assets/streamed').flag_bits & 0x08)
                self.assertTrue(z.getinfo('assets/streamed64').flag_bits & 0x08)
                self.assertLess(z.getinfo('assets/streamed64').file_size, 0xffffffff)
                self.assertEqual(z.read('assets/streamed64'), content)  # zipfile accepts it
            self.assertEqual(android.read_member(path, 'assets/streamed'), content)
            self.assertEqual(android.read_member(path, 'assets/streamed64'), content)
            self.assertEqual(android.read_member(path, 'assets/after'), b'next entry')

    def test_central_sizes_and_checksum_forged(self):
        # The reviewer's case: stored 'abcdef', central sizes 3 and the prefix's checksum, local untouched.
        forged = self.build_zip([self.entry('assets/x', b'abcdef', central=dict(csize=3, usize=3, crc=zlib.crc32(b'abc')))])
        with zipfile.ZipFile(io.BytesIO(forged)) as z:
            self.assertEqual(z.read('assets/x'), b'abc')  # Python's reader is fooled
        self.refused(forged, 'local and central entries differ')
        self.refused(self.build_zip([self.entry('assets/x', b'abcdef', deflate=True,
                                                central=dict(usize=3, crc=zlib.crc32(b'abc')))]),
                     'local and central entries differ')

    def test_local_name_mismatch(self):
        self.refused(self.build_zip([self.entry('assets/x', b'abcdef', local=dict(name='assets/y'))]),
                     "local header names 'assets/y'")

    def test_trailing_compressed_bytes(self):
        e = self.entry('assets/x', b'abcdef' * 100, deflate=True)
        e['data'] += b'\x00junk'
        e['csize'] = len(e['data'])
        self.refused(self.build_zip([e]), 'nothing after it')

    def test_data_descriptor_checked(self):
        content = b'abcdef' * 10
        good = self.entry('assets/x', content, flags=0x08, local=dict(crc=0, csize=0, usize=0),
                          tail=struct.pack('<IIII', 0x08074b50, zlib.crc32(content), len(content), len(content)))
        self.assertEqual(android.read_member(self.tmpfile(self.build_zip([good])), 'assets/x'), content)
        unsigned = dict(good, tail=struct.pack('<III', zlib.crc32(content), len(content), len(content)))
        self.assertEqual(android.read_member(self.tmpfile(self.build_zip([unsigned])), 'assets/x'), content)
        bad = dict(good, tail=struct.pack('<IIII', 0x08074b50, zlib.crc32(content), len(content), 3))
        self.refused(self.build_zip([bad]), 'local and central entries differ')
        # A cut-off descriptor: what follows is the central directory, which never matches.
        self.refused(self.build_zip([dict(good, tail=b'PK')]), 'truncated data descriptor|overlaps|entries differ')

    def test_overlapping_entries(self):
        second = self.entry('assets/y', b'second')
        probe = self.build_zip([self.entry('assets/x', b'abc'), second])
        span = b'abc' + probe[30 + len('assets/x') + 3:30 + len('assets/x') + 3 + 30 + len('assets/y') + 6]
        first = dict(self.entry('assets/x', b'abc'), crc=zlib.crc32(span), csize=len(span), usize=len(span))
        forged = self.build_zip([first, second])
        self.refused(forged, 'overlaps the next ZIP entry')

    def test_listing_fails_closed(self):
        with patch.object(android, 'ssh', return_value='') as ssh:
            self.assertEqual(android.running_instances(strict=True), {})
        self.assertNotIn('|| true', ssh.call_args[0][0])
        with patch.object(android, 'ssh', return_value='lepton-steamlaunch-1 5556\nlepton-steamlaunch-2 <no value>\n'):
            self.assertEqual(android.running_instances(strict=True),
                             {'lepton-steamlaunch-1': '5556', 'lepton-steamlaunch-2': None})
        with patch.object(android, 'ssh', side_effect=android.FrameError('podman: cannot connect')):
            with self.assertRaises(android.FrameError):
                android.running_instances(strict=True)
            with self.assertRaises(android.FrameError):
                quest._running(PKG)
        with patch.object(android, 'stop'), patch.object(quest, '_sleep'), \
                patch.object(android, 'ssh', side_effect=android.FrameError('podman: cannot connect')):
            self.assertEqual(quest._close(PKG), (False, 'podman: cannot connect'))  # never "closed"
        with patch.object(android, 'stop'), patch.object(android, 'ssh', return_value=''):
            self.assertEqual(quest._close(PKG), (True, None))
        # Other callers keep the lenient listing.
        with patch.object(android, 'ssh', return_value='') as ssh:
            self.assertEqual(android.running_instances(), {})
        self.assertIn('|| true', ssh.call_args[0][0])

    def test_dumpsys_scoped_to_the_package(self):
        meta = {'package': PKG, 'instance': 2800000001}
        dump = ('Packages:\n  Package [com.other.app] (1a):\n    versionCode=5 minSdk=21\n'
                f'  Package [{PKG}] (2b):\n    userId=10050\n    versionCode=100 minSdk=29\n'
                '  Package [com.third.app] (3c):\n    versionCode=7\n')
        with patch.object(android, 'ssh', return_value=dump):
            quest._check_android_package(meta, 100)
            with self.assertRaisesRegex(quest.QuestError, 'version code 100'):
                quest._check_android_package(meta, 5)
        other_only = 'Packages:\n  Package [com.other.app] (1a):\n    versionCode=100\n'
        with patch.object(android, 'ssh', return_value=other_only):
            with self.assertRaisesRegex(quest.QuestError, 'does not have'):
                quest._check_android_package(meta, 100)

    def test_installed_version_code_reads_the_frame_apk(self):
        """Runs the Frame-side script here, against an APK laid out as on the Frame."""
        import subprocess
        with tempfile.TemporaryDirectory() as home:
            app = Path(home) / android.APPS_DIR / PKG
            app.mkdir(parents=True)
            (app / 'app.apk').write_bytes(apk_bytes(version_code=4321))

            def run_here(cmd, input=None, timeout=None):
                self.assertEqual(cmd, 'python3 -')
                return subprocess.run([sys.executable, '-'], input=input, cwd=home, capture_output=True,
                                      text=True, check=True).stdout
            with patch.object(android, 'ssh', side_effect=run_here):
                self.assertEqual(android.installed_version_code(PKG), 4321)
                (app / 'app.apk').write_bytes(b'not an apk')
                self.assertIsNone(android.installed_version_code(PKG))
                (app / 'app.apk').unlink()
                self.assertIsNone(android.installed_version_code(PKG))
        with self.assertRaises(android.FrameError):
            android.installed_version_code('../x')


class CloseInFinally(unittest.TestCase):
    """The game is closed even when the copy fails locally or is interrupted."""

    META = Import.META

    def setUp(self):
        self.order = []
        self.stop_error = None

        def stop(pkg):
            self.order.append('stop')
            if self.stop_error:
                raise android.FrameError(self.stop_error)
        self.running = True
        for target, name, effect in ((android, 'stop', stop),
                                     (android, 'read_meta', lambda pkg: dict(self.META)),
                                     (android, 'install', lambda *a, **k: self.fail('no install')),
                                     (quest, '_running', lambda pkg: 'stop' not in self.order),
                                     (quest, '_sleep', lambda s: None),
                                     (quest, '_check_android_package', lambda m, c: None),
                                     (quest, '_wait_ready', lambda m: None)):
            p = patch.object(target, name, side_effect=effect)
            p.start()
            self.addCleanup(p.stop)
        fake = FakeQuest(self)
        fake.add(PKG, [apk_bytes(files=OPENXR, version_code=100, version_name='1.0')],
                 obb={f'main.1.{PKG}.obb': b'main'}, data={'files/save.dat': b'save'})

    def test_local_oserror_closes_the_game_and_keeps_the_cause(self):
        with patch.object(android, 'install_obb', return_value={'obb': []}), \
                patch.object(quest, 'push_data', side_effect=OSError(5, 'Input/output error')):
            with self.assertRaisesRegex(quest.QuestError, 'Input/output error.*The game was closed') as e:
                quest.import_game(PKG, data=True)
        self.assertIsInstance(e.exception.__cause__, OSError)
        self.assertEqual(self.order, ['stop'])

    def test_ctrl_c_closes_the_game_and_still_raises(self):
        with patch.object(android, 'install_obb', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                quest.import_game(PKG)
        self.assertEqual(self.order, ['stop'])
        self.order.clear()
        self.stop_error = 'Steam is not answering'
        err = io.StringIO()
        with patch.object(android, 'install_obb', side_effect=KeyboardInterrupt), patch.object(sys, 'stderr', err):
            with self.assertRaises(KeyboardInterrupt):  # a failed close never hides the interrupt
                quest.import_game(PKG)
        self.assertEqual(self.order, ['stop'])
        self.assertIn("couldn't close Quest Game on the Frame (Steam is not answering)", err.getvalue())
        self.assertFalse(quest._import_lock.locked())
        self.assertTrue(android._install_lock.acquire(blocking=False))
        android._install_lock.release()


class FrameSide(unittest.TestCase):
    META = {'package': PKG, 'label': 'Quest Game', 'instance': 2800000001}

    @unittest.skipUnless(os.name == 'posix', 'symlinks')
    def test_push_data_streams_regular_files_without_staging(self):
        import frame_android_data
        import tarfile
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / PKG
            (folder / 'files').mkdir(parents=True)
            (folder / 'files/save.dat').write_bytes(b'save' * 100000)
            os.symlink('/etc/passwd', str(folder / 'files/link'))
            got = {}

            def stream(command, src=None, dst=None):
                got['command'] = command
                got['staged'] = sorted(p.name for p in Path(tmp).iterdir())
                with tarfile.open(fileobj=io.BytesIO(src.read())) as tar:
                    got['members'] = {m.name: (m.type, m.uid) for m in tar.getmembers()}
            with patch.object(frame_android_data, '_stream', side_effect=stream):
                self.assertEqual(quest.push_data(self.META, folder), 1)
        self.assertEqual(got['staged'], [PKG])  # nothing written beside the pulled folder
        self.assertIn('podman exec -i lepton-steamlaunch-2800000001 /system/bin/sh -c', got['command'])
        self.assertIn(f'cd /sdcard/Android/data/{PKG}', got['command'])
        self.assertEqual(set(got['members']), {'files', 'files/save.dat'})
        self.assertEqual(got['members']['files/save.dat'], (tarfile.REGTYPE, 0))

    def test_push_data_transfer_failure(self):
        import frame_android_data
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'big.bin').write_bytes(b'x' * (4 * 1024**2))  # more than a pipe holds
            with patch.object(frame_android_data, '_stream', side_effect=android.FrameError('ssh died')):
                with self.assertRaisesRegex(android.FrameError, 'ssh died'):
                    quest.push_data(self.META, tmp)

    def test_wait_ready(self):
        answers = iter(['', '0\n', '1\n'])
        with patch.object(android, 'ssh', side_effect=lambda cmd, timeout=None: next(answers)) as ssh, \
                patch.object(quest, '_sleep'):
            quest._wait_ready(self.META)
        self.assertEqual(ssh.call_count, 3)
        self.assertIn('podman exec lepton-steamlaunch-2800000001', ssh.call_args[0][0])
        with patch.object(android, 'ssh', return_value=''), patch.object(quest, '_sleep'), \
                patch.object(quest, 'READY_TIMEOUT', 0):
            with self.assertRaisesRegex(quest.QuestError, "didn't start on the Frame"):
                quest._wait_ready(self.META)


class Server(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        cls.server = server

    def test_import_request_validation(self):
        s = self.server
        for body in ({'action': 'nope'}, {'action': 'import', 'package': '../x'},
                     {'action': 'import', 'package': PKG, 'data': 'yes'},
                     {'action': 'import', 'package': PKG, 'serial': 'a b; rm'}):
            with self.assertRaises(s.Failure) as e:
                s.quest(body)
            self.assertEqual(e.exception.status, 400, body)
        with patch.object(s, 'ensure_master'), patch.object(s, 'queue_job', return_value={'job': 'j'}) as job:
            self.assertEqual(s.quest({'action': 'import', 'package': PKG, 'serial': QUEST['serial'], 'data': True}),
                             {'job': 'j'})
        self.assertTrue(job.call_args.kwargs['progress'])
        with patch.object(s, 'LOCAL', True):
            with self.assertRaisesRegex(s.Failure, 'runs on your computer'):
                s.quest({'action': 'import', 'package': PKG})
            self.assertIn('runs on your computer', s.quest_status('')['guidance'])


if __name__ == '__main__':
    unittest.main()
