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


def axml(package, split=None, split_required=False, resmap=False, feature=False, children=()):
    """A minimal binary AndroidManifest.xml: <manifest package split? isFeatureSplit?><application label
    isSplitRequired?>children</application>.

    resmap: name isSplitRequired by its android resource id (as real builds do), not only by name."""
    strings = ['isSplitRequired', 'package', 'split', 'manifest', 'application', 'label', package,
               split or '', 'Quest Game', 'isFeatureSplit', *children]
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
        + ([('isFeatureSplit', 0x12, 0xffffffff)] if feature else [])
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


def lie_about_size(data, entry, size):
    """The zip with entry's uncompressed size in both headers replaced by size (a forged bomb)."""
    out = bytearray(data)
    encoded = entry.encode()
    for signature, at in ((b'PK\x03\x04', 22), (b'PK\x01\x02', 24)):
        pos = 0
        while True:
            pos = out.find(signature, pos)
            if pos < 0:
                break
            name_at = pos + (30 if signature == b'PK\x03\x04' else 46)
            if out[name_at:name_at + len(encoded)] == encoded:
                struct.pack_into('<I', out, pos + at, size)
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

    def test_meta_licence_check_refused_in_base_or_split(self):
        base = self.write(apk_bytes(files=dict(OPENXR, **{'lib/arm64-v8a/libovrplatformloader.so': b''})))
        with self.assertRaises(quest.QuestError) as e:
            quest.inspect(PKG, base)
        self.assertIn('libovrplatformloader.so', str(e.exception))
        self.assertIn("won't remove or work around", str(e.exception))
        self.assertIn('only exist on Meta headsets', str(e.exception))
        base = self.write(apk_bytes(split_required=True))
        split = self.write(apk_bytes(split='config.arm64_v8a', files={'lib/arm64-v8a/libovrplatformloader.so': b''}))
        with self.assertRaisesRegex(quest.QuestError, 'licence'):
            quest.inspect(PKG, base, [split])

    def test_32_bit_only_refused(self):
        base = self.write(apk_bytes(files={'lib/armeabi-v7a/libgame.so': b''}))
        with self.assertRaisesRegex(quest.QuestError, '32-bit-only build \\(armeabi-v7a\\)'):
            quest.inspect(PKG, base)

    def test_vrapi_only_flagged_openxr_fine(self):
        base = self.write(apk_bytes(files={'lib/arm64-v8a/libvrapi.so': b'', 'lib/arm64-v8a/libgame.so': b''}))
        result = quest.inspect(PKG, base)
        self.assertIn('VrApi', result['warnings'][0])
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


class Import(unittest.TestCase):
    META = {'package': PKG, 'label': 'Quest Game', 'instance': 2800000001, 'vr_issues': [], 'version': ''}

    def setUp(self):
        self.order = []
        self.running = False
        self.installed = None

        def install(base, splits=(), source=None):
            self.order.append(('install', os.path.basename(base), [os.path.basename(s) for s in splits]))
            self.assertTrue(os.path.isfile(base))
            self.assertTrue(all(os.path.isfile(s) for s in splits))
            self.assertEqual(source, 'Meta Quest (USB)')
            return dict(self.META)

        def install_obb(pkg, paths):
            self.order.append(('obb', sorted(os.path.basename(p) for p in paths)))
            self.assertTrue(all(os.path.isfile(p) for p in paths))
            return {'obb': [{'name': os.path.basename(p)} for p in paths]}

        def push_data(m, folder):
            self.order.append(('data', sorted(p.relative_to(folder).as_posix() for p in Path(folder).rglob('*') if p.is_file())))
            return 1

        for target, name, effect in ((android, 'install', install), (android, 'install_obb', install_obb),
                                     (android, 'launch', lambda pkg: self.order.append(('launch',))),
                                     (android, 'stop', lambda pkg: self.order.append(('stop',))),
                                     (android, 'read_meta', lambda pkg: self.installed),
                                     (quest, '_running', lambda pkg: self.running),
                                     (quest, '_wait_ready', lambda m: self.order.append(('ready',))),
                                     (quest, 'push_data', push_data)):
            p = patch.object(target, name, side_effect=effect)
            p.start()
            self.addCleanup(p.stop)

    def open_on_frame(self, version=''):
        self.running, self.installed = True, dict(self.META, version=version)

    def cache_left(self):
        root = quest.cache_root()
        return list(root.iterdir()) if root.is_dir() else []

    def pulls(self, fake):
        return [c[3] for c in fake.calls() if c[2:3] == ['pull']]

    def game(self, fake):
        fake.add(PKG, [apk_bytes(files=OPENXR)],
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

    def test_second_import_with_the_game_open_copies_data_then_closes_it(self):
        fake = FakeQuest(self)
        self.game(fake)
        self.open_on_frame()
        result = quest.import_game(PKG, data=True)
        self.assertEqual(self.order, [('ready',), ('obb', [f'main.12.{PKG}.obb', f'patch.12.{PKG}.obb']),
                                      ('data', ['files/save.dat']), ('stop',)])  # no install, no launch
        pulls = self.pulls(fake)
        self.assertTrue(pulls[0].endswith('/base.apk'))  # the APK is checked before the OBB is copied
        self.assertEqual(pulls[1:], [f'/sdcard/Android/obb/{PKG}', f'/sdcard/Android/data/{PKG}'])
        self.assertIn('notes.txt', ' '.join(result['warnings']))
        self.assertEqual(len(result['obb']), 2)
        self.assertFalse(result['needs_game_data'])
        self.assertEqual(self.cache_left(), [])

    def test_open_game_is_never_replaced(self):
        fake = FakeQuest(self)
        self.game(fake)
        self.open_on_frame(version='9.9')
        with self.assertRaisesRegex(quest.QuestError, 'open on the Frame with a different version'):
            quest.import_game(PKG)
        fake.add('org.example.plain', [apk_bytes('org.example.plain', files=OPENXR)])
        self.installed = dict(self.META, package='org.example.plain')
        with self.assertRaisesRegex(quest.QuestError, 'no game data to add'):
            quest.import_game('org.example.plain')
        self.assertEqual(self.order, [])
        self.assertFalse([p for p in self.pulls(fake) if '/sdcard/' in p])
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

    def test_refusals_copy_nothing_more_and_clean_up(self):
        fake = FakeQuest(self)
        fake.add(PKG, [apk_bytes(files=dict(OPENXR, **{'lib/arm64-v8a/libovrplatformloader.so': b''}))],
                 obb={f'main.1.{PKG}.obb': b'big'})
        self.open_on_frame()
        with self.assertRaisesRegex(quest.QuestError, 'licence'):
            quest.import_game(PKG)
        self.running = False
        fake.add('org.example.old', [apk_bytes('org.example.old', files={'lib/armeabi-v7a/libgame.so': b''})],
                 obb={'main.1.org.example.old.obb': b'big'})
        with self.assertRaisesRegex(quest.QuestError, '32-bit'):
            quest.import_game('org.example.old')
        self.assertEqual(self.order, [])
        self.assertFalse([p for p in self.pulls(fake) if '/obb/' in p])
        self.assertEqual(self.cache_left(), [])

    def test_failures_clean_up_and_close_the_game(self):
        fake = FakeQuest(self)
        fake.add(PKG, [apk_bytes(files=OPENXR)], obb={f'main.1.{PKG}.obb': b'main'})
        self.open_on_frame()
        fake.state['fail_pull'] = f'/sdcard/Android/obb/{PKG}'
        fake.save()
        with self.assertRaisesRegex(quest.QuestError, 'Connection reset'):
            quest.import_game(PKG)
        self.assertEqual(self.cache_left(), [])
        fake.state.pop('fail_pull')
        fake.save()
        android.install_obb.side_effect = android.FrameError('checksum mismatch')
        with self.assertRaisesRegex(quest.QuestError, "Copying Quest Game's game data failed: checksum mismatch"):
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
        with patch.object(s, 'ensure_master'), patch.object(s, 'start_job', return_value={'job': 'j'}) as job:
            self.assertEqual(s.quest({'action': 'import', 'package': PKG, 'serial': QUEST['serial'], 'data': True}),
                             {'job': 'j'})
        self.assertTrue(job.call_args.kwargs['progress'])
        with patch.object(s, 'LOCAL', True):
            with self.assertRaisesRegex(s.Failure, 'runs on your computer'):
                s.quest({'action': 'import', 'package': PKG})
            self.assertIn('runs on your computer', s.quest_status('')['guidance'])


if __name__ == '__main__':
    unittest.main()
