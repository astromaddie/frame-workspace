"""Offline authenticated repository fixtures; no tests contact a server."""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ui'))
from apk_sources import SourceError, SourceLimited, _web, fdroid

FIXTURES = Path(__file__).parent / 'fixtures' / 'fdroid'
PIN = (FIXTURES / 'fingerprint.txt').read_text().strip()
URL = 'https://example.org/repo/'
_KEY = []


def signed_jar(member, content, digest='sha256'):
    """A JAR signed like fdroidserver's (no CMS signed attributes) with a throwaway test key."""
    import base64, hashlib
    from frame_apk_sign import certificate, der, integer, sequence, signing_key
    if not _KEY:
        with tempfile.TemporaryDirectory() as tmp:
            _KEY.append(signing_key(Path(tmp) / 'key.json'))
    key = _KEY[0]
    label = 'SHA1' if digest == 'sha1' else 'SHA-256'
    b64 = lambda data: base64.b64encode(hashlib.new(digest, data).digest()).decode()
    manifest = ('Manifest-Version: 1.0\r\n\r\nName: %s\r\n%s-Digest: %s\r\n\r\n' % (member, label, b64(content))).encode()
    sf = ('Signature-Version: 1.0\r\n%s-Digest-Manifest: %s\r\n\r\n' % (label, b64(manifest))).encode()
    oid, prefix = next((bytes.fromhex(o), bytes.fromhex(p)) for o, (d, p) in fdroid._DIGESTS.items() if d == digest)
    alg = sequence(der(6, oid), der(5, b''))
    size = (key['n'].bit_length() + 7) // 8
    value = prefix + hashlib.new(digest, sf).digest()
    padded = b'\0\1' + b'\xff' * (size - len(value) - 3) + b'\0' + value
    signature = pow(int.from_bytes(padded, 'big'), key['d'], key['n']).to_bytes(size, 'big')
    cert = certificate(key)
    issuer = fdroid._der_parts(fdroid._der_parts(fdroid._der_parts(cert)[0][1])[0][1])[3][2]
    signer = sequence(integer(1), sequence(issuer, integer(1)), alg,
                      sequence(der(6, bytes.fromhex('2a864886f70d010101')), der(5, b'')), der(4, signature))
    signed = sequence(integer(1), der(0x31, alg), sequence(der(6, bytes.fromhex('2a864886f70d010701'))),
                      der(0xa0, cert), der(0x31, signer))
    block = sequence(der(6, bytes.fromhex('2a864886f70d010702')), der(0xa0, signed))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w') as z:
        for name, data in (('META-INF/MANIFEST.MF', manifest), ('META-INF/TEST.SF', sf),
                           ('META-INF/TEST.RSA', block), (member, content)):
            z.writestr(name, data)
    return stream.getvalue(), fdroid.hashlib.sha256(cert).hexdigest()


def entry_jar(timestamp, digest='sha256'):
    entry = json.loads(zipfile.ZipFile(FIXTURES / 'entry.jar').read('entry.json'))
    return signed_jar('entry.json', json.dumps(dict(entry, timestamp=timestamp)).encode(), digest)


class Repositories(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for name, value in [('data_dir', lambda *p: self.root.joinpath('data', *p)),
                            ('cache_dir', lambda *p: self.root.joinpath('cache', *p))]:
            mock = patch.object(fdroid.frame_host, name, value)
            mock.start()
            self.addCleanup(mock.stop)
        net = patch.object(fdroid.urllib.request, 'build_opener', side_effect=AssertionError('network forbidden'))
        net.start()
        self.addCleanup(net.stop)
        mock = self.fetch_patch = patch.object(fdroid, '_fetch', side_effect=self.fetch)
        self.fetch_mock = mock.start()
        self.addCleanup(mock.stop)
        self.v1 = False
        self.corrupt = None
        self.files = {}
        for state in (_web._limited, fdroid._stale, fdroid._retry_at, fdroid._refreshing):
            state.clear()
            self.addCleanup(state.clear)

    def fetch(self, url, path, maximum):
        name = url.rsplit('/', 1)[-1]
        if self.v1 and name == 'entry.jar':
            raise urllib.error.HTTPError(url, 404, 'missing', None, None)
        payload = self.files.get(name) or (FIXTURES / ('example.apk' if name.endswith('.apk') else name)).read_bytes()
        if name == self.corrupt:
            payload += b'tampered'
        Path(path).write_bytes(payload)

    def add(self):
        return fdroid.add_repo(URL, PIN)

    def test_add_search_details_download_cache(self):
        source = self.add()
        self.assertEqual(source['fingerprint'], PIN)
        self.assertFalse(source['trust_on_first_use'])
        result = fdroid.search(source, 'example offline')
        self.assertEqual(len(result), 1)
        self.assertNotIn('versions', result[0])
        self.assertEqual(result[0]['version_code'], 2)
        self.assertEqual([v['version_code'] for v in fdroid.details(source, 'org.example.app')['versions']], [2, 1])
        downloaded = fdroid.download(source, 'org.example.app', 1)
        self.assertTrue(downloaded['verified'])
        self.assertEqual(Path(downloaded['apk']).read_bytes(), (FIXTURES / 'example.apk').read_bytes())
        count = self.fetch_mock.call_count
        os.utime(downloaded['apk'], (1, 1))
        fdroid.download(source, 'org.example.app', 1)
        self.assertEqual(self.fetch_mock.call_count, count)
        self.assertGreater(Path(downloaded['apk']).stat().st_mtime, 1)  # reuse counts as recent use

    def test_wrong_pin_is_not_saved(self):
        with self.assertRaisesRegex(SourceError, 'fingerprint mismatch'):
            fdroid.add_repo(URL, '0' * 64)
        self.assertEqual(fdroid.user_repos(), [])

    def test_tampered_index_is_not_saved(self):
        self.corrupt = 'index-v2.json'
        with self.assertRaisesRegex(SourceError, 'SHA-256'):
            self.add()
        self.assertEqual(fdroid.user_repos(), [])

    def test_tampered_apk_is_not_cached(self):
        source = self.add()
        self.corrupt = 'example2.apk'
        with self.assertRaisesRegex(SourceError, 'APK SHA-256'):
            fdroid.download(source, 'org.example.app')
        self.assertEqual(list(self.root.rglob('*.apk')), [])
        self.assertEqual(list(self.root.rglob('*.part')), [])

    def test_v1_fallback(self):
        self.v1 = True
        source = self.add()
        self.assertEqual(fdroid.search(source, 'Example')[0]['version_code'], 1)
        self.assertTrue(fdroid.download(source, 'org.example.app')['verified'])

    def test_bad_v2_never_downgrades(self):
        self.corrupt = 'index-v2.json'
        with self.assertRaises(SourceError):
            self.add()
        self.assertFalse(any(c.args[0].endswith('index-v1.jar') for c in self.fetch_mock.call_args_list))

    def test_transient_error_never_downgrades(self):
        self.fetch_mock.side_effect = urllib.error.HTTPError(URL, 503, 'unavailable', None, None)
        with self.assertRaises(SourceError):
            self.add()
        self.assertEqual(self.fetch_mock.call_count, 1)

    def test_tofu_preserves_pin_and_settings(self):
        source = fdroid.add_repo('fdroidrepos://example.org/repo')
        self.assertTrue(source['trust_on_first_use'])
        self.assertEqual(source['fingerprint'], PIN)
        fdroid.add_repo(URL)
        self.assertEqual(len(fdroid.user_repos()), 1)
        with self.assertRaisesRegex(SourceError, 'different pinned'):
            fdroid.add_repo(URL, '0' * 64)
        fdroid.set_enabled(source['id'], False)
        self.assertEqual(fdroid.search(fdroid.user_repos()[0], ''), [])
        with self.assertRaisesRegex(SourceError, 'disabled'):
            fdroid.download(fdroid.user_repos()[0], 'org.example.app')
        fdroid.set_enabled('fdroid', False)
        self.assertFalse(fdroid.sources()[0]['enabled'])
        fdroid.remove_repo(source['id'])
        self.assertEqual(fdroid.user_repos(), [])
        with self.assertRaises(SourceError):
            fdroid.remove_repo('fdroid')

    def test_urls(self):
        self.assertEqual(fdroid._url(URL + '?fingerprint=' + PIN.upper()), (URL, PIN))
        for url in ['http://example.org/repo', 'fdroidrepo://example.org', 'https://u:p@example.org', URL+'?other=x']:
            with self.subTest(url=url), self.assertRaises(SourceError):
                fdroid._url(url)
        with self.assertRaisesRegex(SourceError, 'conflicting'):
            fdroid._url(URL + '?fingerprint=' + PIN, '0' * 64)
        self.assertEqual(fdroid._child(URL, '/app/en-US/phoneScreenshots/#0 a.png'),
                         URL + 'app/en-US/phoneScreenshots/%230%20a.png')  # real F-Droid screenshot name
        for name in ['../x.apk', '%2e%2e/x.apk', 'https://evil.org/a.apk', '//evil.org/../x', 'x?token=y', 'x\\y']:
            with self.subTest(name=name), self.assertRaises(SourceError):
                fdroid._child(URL, name)

    def test_recorded_real_signature(self):
        content, fingerprint = fdroid._jar(FIXTURES / 'izzy-entry.jar', 'entry.json', fdroid.IZZY_PIN, strong=True)
        self.assertEqual(fingerprint, fdroid.IZZY_PIN)
        self.assertIn('index', json.loads(content))

    def test_tampering_each_signature_layer(self):
        for member in ['entry.json', 'META-INF/MANIFEST.MF', 'META-INF/TEST.SF', 'META-INF/TEST.RSA']:
            stream = io.BytesIO()
            with zipfile.ZipFile(FIXTURES / 'entry.jar') as src, zipfile.ZipFile(stream, 'w') as dst:
                for item in src.infolist():
                    data = src.read(item.filename)
                    if item.filename == member:
                        data = data[:-1] + bytes([data[-1] ^ 1])
                    dst.writestr(item.filename, data)
            with self.subTest(member=member), self.assertRaises(SourceError):
                fdroid._jar(io.BytesIO(stream.getvalue()), 'entry.json', PIN)

    def test_duplicate_jar_member_rejected(self):
        stream = io.BytesIO((FIXTURES / 'entry.jar').read_bytes())
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(stream, 'a') as z:
                z.writestr('entry.json', '{}')
        stream.seek(0)
        with self.assertRaisesRegex(SourceError, 'duplicate'):
            fdroid._jar(stream, 'entry.json', PIN)

    def test_corrupt_cache_refetches_verified_index(self):
        source = self.add()
        fdroid.frame_host.cache_dir('apk-sources', source['id'] + '.json').write_text('{')
        self.assertEqual(len(fdroid.search(source, 'example')), 1)
        self.assertEqual(self.fetch_mock.call_count, 4)

    def test_artwork_v1_and_v2_survives_source_cache(self):
        source = self.add()
        for version in ('v1', 'v2'):
            with self.subTest(version=version):
                raw = FIXTURES / ('artwork-' + version + '.json')
                if version == 'v1':
                    normalized = self.root / 'normalized.json'
                    fdroid._v1(raw.read_bytes(), normalized)
                    raw = normalized
                apps = fdroid._reduce(raw, source)
                cache = fdroid.frame_host.cache_dir('apk-sources', source['id'] + '.json')
                fdroid._write(cache, {'version': fdroid.CACHE_VERSION, 'url': URL,
                                     'fingerprint': PIN, 'apps': apps})
                before = self.fetch_mock.call_count
                result = fdroid.search(source, 'example offline')[0]
                self.assertEqual(result['developer'], 'Example Developer')
                self.assertEqual(result['summary'], 'Offline fixture & music. One line.')
                self.assertEqual(result['icon'], URL + 'org.example.app/en-US/icon.png')
                self.assertEqual(result['images'], {
                    'icon': result['icon'],
                    'banner': URL + 'org.example.app/fr/featureGraphic.png',
                    'screenshots': [URL + 'org.example.app/en-US/phoneScreenshots/' + str(i) + '.png' for i in range(1, 5)] +
                                   [URL + 'org.example.app/fr/sevenInchScreenshots/' + str(i) + '.png' for i in range(1, 3)]})
                self.assertEqual(fdroid.details(source, result['id'])['images'], result['images'])
                self.assertEqual(self.fetch_mock.call_count, before)

    def test_missing_artwork_is_not_invented(self):
        result = fdroid.search(self.add(), 'example')[0]
        self.assertEqual(result['images'], {'icon': None, 'banner': None, 'screenshots': []})
        self.assertIsNone(result['icon'])
        self.assertIsNone(result['developer'])

    def test_v1_legacy_icon_and_tablet_fallback(self):
        index = json.loads((FIXTURES / 'artwork-v1.json').read_text())
        app = index['apps'][0]
        app['localized'] = {'fr': {'sevenInchScreenshots': ['tablet.png']}}
        app['icon'] = 'legacy.1.png'
        raw = self.root / 'legacy.json'
        fdroid._v1(json.dumps(index).encode(), raw)
        result = fdroid._reduce(raw, {'id': 'test', 'url': URL})['org.example.app']
        self.assertEqual(result['icon'], URL + 'icons/legacy.1.png')
        self.assertEqual(result['images']['screenshots'], [URL + 'org.example.app/fr/sevenInchScreenshots/tablet.png'])

    def test_per_abi_builds_offer_and_download_the_arm64_one(self):
        import hashlib

        def build(code, name, abis):
            self.files[name] = name.encode()
            return {'manifest': {'versionName': '391', 'versionCode': code, 'usesSdk': {'minSdkVersion': 28},
                                 'nativecode': abis},
                    'file': {'name': '/' + name, 'sha256': hashlib.sha256(name.encode()).hexdigest(), 'size': code},
                    'added': 0}
        # One APK per ABI under different codes (the x86_64 one highest, as F-Droid often does),
        # plus a universal and an arm64-only build sharing a code.
        builds = [build(3911, 'app-armeabi-v7a.apk', ['armeabi-v7a']), build(3914, 'app-x86_64.apk', ['x86_64']),
                  build(3913, 'app-x86.apk', ['x86']),
                  build(3912, 'app-universal.apk', ['arm64-v8a', 'armeabi-v7a', 'x86_64']),
                  build(3912, 'app-arm64-v8a.apk', ['arm64-v8a'])]
        raw = self.root / 'splits.json'
        raw.write_text(json.dumps({'packages': {'com.futo.platformplayer': {
            'metadata': {'name': {'en-US': 'Grayjay'}},
            'versions': {str(i): b for i, b in enumerate(builds)}}}}))
        source = {'id': 'test', 'url': URL}
        app = fdroid._reduce(raw, source)['com.futo.platformplayer']
        self.assertEqual([v['name'] for v in app['versions']], ['/app-arm64-v8a.apk', '/app-universal.apk'])
        self.assertEqual((app['version_code'], app['abis']), (3912, ['arm64-v8a']))
        with patch.object(fdroid, 'details', return_value=app):
            for code in (None, 3912):
                fdroid.download(source, 'com.futo.platformplayer', version_code=code)
                self.assertTrue(self.fetch_mock.call_args[0][0].endswith('/app-arm64-v8a.apk'))
            with self.assertRaises(SourceError):  # the x86_64 build is never offered
                fdroid.download(source, 'com.futo.platformplayer', version_code=3914)

    def test_v2_legacy_screenshot_keys_and_limit(self):
        meta = {'phoneScreenshots': {'fr': [{'name': '/phone/' + str(i) + '.png'} for i in range(8)]},
                'sevenInchScreenshots': {'en-US': [{'name': '/tablet.png'}]}}
        images = fdroid._images(meta, URL)
        self.assertEqual(images['screenshots'], [URL + 'phone/' + str(i) + '.png' for i in range(6)])
        meta.pop('phoneScreenshots')
        self.assertEqual(fdroid._images(meta, URL)['screenshots'], [URL + 'tablet.png'])

    def test_old_cache_refreshes_for_artwork(self):
        source = self.add()
        path = fdroid.frame_host.cache_dir('apk-sources', source['id'] + '.json')
        saved = json.loads(path.read_text())
        saved.pop('version')
        for app in saved['apps'].values():
            app.pop('images')
        fdroid._write(path, saved)
        self.assertIn('images', fdroid.search(source, 'example')[0])
        self.assertEqual(self.fetch_mock.call_count, 4)

    def test_slow_download_blocks_neither_settings_nor_other_repos(self):
        import threading
        source = self.add()
        other = dict(source, id='other-repo')
        started, release = threading.Event(), threading.Event()
        def fetch(url, path, maximum):
            if threading.current_thread().name == 'slow':
                started.set()
                release.wait(5)
            self.fetch(url, path, maximum)
        self.fetch_mock.side_effect = fetch
        slow = threading.Thread(target=fdroid._load, args=(other, True), name='slow')
        slow.start()
        try:
            self.assertTrue(started.wait(2))
            results = []
            # The settings lock is free and another repo still loads while this one downloads.
            check = threading.Thread(target=lambda: results.append(
                (fdroid.set_enabled('fdroid', False), len(fdroid._load(source, force=True)[0]))))
            check.start()
            check.join(2)
            self.assertEqual(results, [(None, 1)])
        finally:
            release.set()
            slow.join()

    def test_rollback_to_older_index_is_refused(self):
        self.files['entry.jar'], pin = entry_jar(2000)
        source = fdroid.add_repo(URL)
        self.assertEqual(source['fingerprint'], pin)
        self.files['entry.jar'], _ = entry_jar(1000)
        with self.assertRaisesRegex(SourceError, 'older'):
            fdroid._load(source, force=True)
        for timestamp in (2000, 3000):  # unchanged and newer indexes are fine
            self.files['entry.jar'], _ = entry_jar(timestamp)
            self.assertEqual(len(fdroid._load(source, force=True)[0]), 1)
        self.files['entry.jar'], _ = entry_jar(2000)
        with self.assertRaisesRegex(SourceError, 'older'):
            fdroid._load(source, force=True)
        fdroid.remove_repo(source['id'])  # a deliberate re-add starts over
        self.assertEqual(fdroid.add_repo(URL)['fingerprint'], pin)

    def test_concurrent_processes_cannot_publish_an_older_index_last(self):
        # Threads with their own in-memory locks, as separate processes would have; each
        # _state_file_lock() opens its own file description, so flock contends for real.
        import threading
        self.files['entry.jar'], _ = entry_jar(50)
        source = fdroid.add_repo(URL)
        jars = {'older': entry_jar(100)[0], 'newer': entry_jar(200)[0]}
        def fetch(url, path, maximum):
            name = threading.current_thread().name
            if name in jars and url.endswith('entry.jar'):
                Path(path).write_bytes(jars[name])
            else:
                self.fetch(url, path, maximum)
        self.fetch_mock.side_effect = fetch
        inside, go, order = threading.Event(), threading.Event(), []
        real_write = fdroid._write
        def write(path, value):
            if path.name.endswith('.json') and 'apps' in value and threading.current_thread().name == 'older':
                inside.set()  # the older load has passed its locked recheck; hold it there
                go.wait(5)
            order.append(threading.current_thread().name)
            real_write(path, value)
        errors = {}
        def load():
            try:
                fdroid._load(source, force=True)
            except SourceError as e:
                errors[threading.current_thread().name] = str(e)
        with patch.object(fdroid, '_source_lock', lambda source_id: threading.Lock()), \
                patch.object(fdroid, '_write', write):
            older = threading.Thread(target=load, name='older')
            older.start()
            self.assertTrue(inside.wait(5))
            newer = threading.Thread(target=load, name='newer')
            newer.start()
            newer.join(.5)
            self.assertTrue(newer.is_alive())  # blocked on the file lock, not publishing
            go.set()
            older.join(5)
            newer.join(5)
        self.assertEqual(errors, {})
        self.assertEqual(order, ['older', 'older', 'newer', 'newer'])  # cache+state, one load at a time
        self.assertEqual(fdroid._state(source)['timestamp'], 200)

    def test_overlapping_v1_load_cannot_replace_accepted_v2(self):
        import threading
        v1 = json.loads(zipfile.ZipFile(FIXTURES / 'index-v1.jar').read('index-v1.json'))
        v1['repo'] = {'timestamp': 100}
        self.files['index-v1.jar'], pin = signed_jar('index-v1.json', json.dumps(v1).encode())
        self.files['entry.jar'], _ = entry_jar(100)  # the same timestamp as the v1 index
        source = dict(id='overlap', name='Overlap', url=URL, fingerprint=None)
        self.v1 = True
        inner = []
        def fetch(url, path, maximum):
            if url.endswith('index-v1.jar') and not inner:
                inner.append(1)  # the v1 load passed its fallback check; a v2 load finishes now
                self.v1 = False
                t = threading.Thread(target=lambda: inner.append(fdroid._load(source, force=True)))
                with patch.object(fdroid, '_source_lock', lambda source_id: threading.Lock()):
                    t.start()
                    t.join(5)
                self.v1 = True
            self.fetch(url, path, maximum)
        self.fetch_mock.side_effect = fetch
        with self.assertRaisesRegex(SourceError, 'v2'):
            fdroid._load(source, force=True)
        self.assertEqual(inner[1][1], pin)
        self.assertTrue(fdroid._state(source)['v2'])
        self.v1 = False
        count = self.fetch_mock.call_count
        apps, _ = fdroid._load(dict(source, fingerprint=pin))
        self.assertEqual((apps['org.example.app']['version_code'], self.fetch_mock.call_count), (2, count))  # v2 cache stayed

    def test_apk_removed_during_cache_check_is_downloaded_again(self):
        source = self.add()
        first = fdroid.download(source, 'org.example.app', 1)
        count = self.fetch_mock.call_count
        real = fdroid._sha256
        def removed_first(path):
            if str(path) == first['apk'] and not hashed:
                hashed.append(1)
                os.remove(first['apk'])  # deleted between the existence check and the open
            return real(path)
        hashed = []
        with patch.object(fdroid, '_sha256', removed_first):
            again = fdroid.download(source, 'org.example.app', 1)
        self.assertEqual(self.fetch_mock.call_count, count + 1)
        self.assertEqual(Path(again['apk']).read_bytes(), (FIXTURES / 'example.apk').read_bytes())

    def test_cached_apk_is_touched_before_hashing(self):
        source = self.add()
        first = fdroid.download(source, 'org.example.app', 1)
        os.utime(first['apk'], (1, 1))
        count = self.fetch_mock.call_count
        real = fdroid._sha256
        def prune_first(path):
            if str(path) == first['apk']:
                with patch.object(_web, 'APK_CAP', 0):
                    _web.prune()  # a pruner running now sees a just-used APK
            return real(path)
        with patch.object(fdroid, '_sha256', prune_first):
            fdroid.download(source, 'org.example.app', 1)
        self.assertEqual(self.fetch_mock.call_count, count)
        self.assertTrue(Path(first['apk']).exists())

    def test_cli_search_waits_for_background_refresh(self):
        source = self.add()
        self.expire(source)
        before = self.fetch_mock.call_count
        out = io.StringIO()
        with patch.object(sys, 'argv', ['fdroid.py', 'search', source['id'], 'example']), \
                patch('sys.stdout', out):
            fdroid.main()
        self.assertEqual(json.loads(out.getvalue())[0]['id'], 'org.example.app')
        self.assertEqual(self.fetch_mock.call_count, before + 2)  # the refresh finished before exit
        self.assertFalse(fdroid.stale(source))
        self.assertNotIn(source['id'], fdroid._refreshing)

    def test_cli_error_still_waits_for_background_refresh(self):
        import threading
        source = self.add()
        self.expire(source)
        release = threading.Event()
        def slow(url, path, maximum):
            release.wait(5)
            self.fetch(url, path, maximum)
        self.fetch_mock.side_effect = slow
        threading.Timer(.3, release.set).start()
        with patch.object(sys, 'argv', ['fdroid.py', 'download', source['id'], 'org.missing']), \
                patch('sys.stderr', io.StringIO()) as err, self.assertRaises(SystemExit):
            fdroid.main()
        self.assertIn('no Lepton-compatible version', err.getvalue())
        self.assertTrue(release.is_set())
        self.assertEqual(fdroid._refreshing, {})  # joined before exiting
        self.assertFalse(fdroid.stale(source))

    def test_no_v1_fallback_once_v2_accepted(self):
        source = self.add()
        self.v1 = True
        with self.assertRaisesRegex(SourceError, 'v2'):
            fdroid._load(source, force=True)
        self.assertFalse(any(c.args[0].endswith('index-v1.jar') for c in self.fetch_mock.call_args_list))

    def test_v1_then_v2_upgrade_is_allowed(self):
        self.v1 = True
        source = self.add()
        self.v1 = False
        self.assertEqual(fdroid._load(source, force=True)[0]['org.example.app']['version_code'], 2)

    def test_sha1_only_entry_jar_rejected(self):
        self.files['entry.jar'], _ = entry_jar(1, 'sha1')
        with self.assertRaisesRegex(SourceError, 'SHA-1'):
            fdroid.add_repo(URL)
        self.assertEqual(fdroid.user_repos(), [])
        stream = io.BytesIO(self.files['entry.jar'])
        self.assertIn(b'index', fdroid._jar(stream, 'entry.json', None)[0])  # index-v1.jar may still use SHA-1

    def test_rate_limited_host_backs_off(self):
        source = dict(self.add(), name='My repo')
        self.fetch_patch.stop()
        error = urllib.error.HTTPError(URL, 429, 'slow down', {'Retry-After': '300'}, None)
        with patch.object(fdroid.urllib.request, 'build_opener') as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaisesRegex(SourceLimited, '^My repo is limiting requests; try again in 5 minutes$'):
                fdroid._load(source, force=True)
            with self.assertRaisesRegex(SourceLimited, 'My repo'):
                fdroid.download(source, 'org.example.app', 1)
            self.assertEqual(opener.return_value.open.call_count, 1)
        self.fetch_mock = self.fetch_patch.start()

    def expire(self, source):
        cache = fdroid.frame_host.cache_dir('apk-sources', source['id'] + '.json')
        old = cache.stat().st_mtime - fdroid.MAX_AGE - 1
        fdroid.os.utime(str(cache), (old, old))

    def test_expired_index_served_stale_while_refreshing(self):
        source = self.add()
        self.expire(source)
        before = self.fetch_mock.call_count
        release = __import__('threading').Event()
        def slow(url, path, maximum):
            release.wait(5)
            self.fetch(url, path, maximum)
        self.fetch_mock.side_effect = slow
        self.assertEqual(len(fdroid.search(source, 'example')), 1)  # immediately, from the old index
        self.assertTrue(fdroid.stale(source))
        refresh = fdroid._refreshing[source['id']]
        fdroid.search(source, 'example')
        self.assertIs(fdroid._refreshing.get(source['id']), refresh)  # one refresh at a time
        release.set()
        refresh.join(5)
        self.assertEqual(self.fetch_mock.call_count, before + 2)
        self.assertFalse(fdroid.stale(source))

    def test_failed_refresh_keeps_serving_stale_index(self):
        source = self.add()
        self.expire(source)
        self.fetch_mock.side_effect = urllib.error.HTTPError(URL, 503, 'unavailable', None, None)
        self.assertEqual(len(fdroid.search(source, 'example')), 1)
        # A fast failed refresh may already have removed itself from the registry.
        refresh = fdroid._refreshing.get(source['id'])
        if refresh is not None:
            refresh.join(5)
        calls = self.fetch_mock.call_count
        self.assertEqual(fdroid.details(source, 'org.example.app')['version_code'], 2)
        self.assertTrue(fdroid.stale(source))
        self.assertNotIn(source['id'], fdroid._refreshing)  # failed refresh waits before retrying
        self.assertEqual(self.fetch_mock.call_count, calls)

    def test_cached_index_does_not_cross_pins(self):
        source = self.add()
        source['fingerprint'] = '0' * 64
        with self.assertRaisesRegex(SourceError, 'fingerprint mismatch'):
            fdroid.search(source, '')


if __name__ == '__main__':
    unittest.main()
