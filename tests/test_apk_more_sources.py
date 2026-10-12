import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import hashlib, io, json, os, sys, tempfile, time, unittest, urllib.error, urllib.request, zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ui'))
from apk_sources import SourceError, github, itch, _web

FIX = Path(__file__).parent / 'fixtures' / 'more_sources'


class PublisherSources(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = patch.object(_web, 'cache', return_value=self.tmp.name)
        self.cache.start()
        self.addCleanup(self.cache.stop)
        self.network = patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network in test'))
        self.network.start()
        self.addCleanup(self.network.stop)
        _web._limited.clear()
        self.addCleanup(_web._limited.clear)
        self.root = Path(self.tmp.name) / 'caches' / 'apk-sources'
        roots = patch.object(_web.frame_host, 'cache_dir', lambda *p: self.root.parent.joinpath(*p))
        roots.start()
        self.addCleanup(roots.stop)

    def test_curated_search_is_offline(self):
        self.assertEqual(github.search(github.sources()[0], 'hello')[0]['id'], 'KhronosGroup/OpenXR-SDK-Source')
        self.assertEqual(github.search(github.sources()[0], '', 0), [])

    def test_curated_artwork_has_recorded_image_evidence(self):
        evidence = {r['url']: r for r in json.loads((FIX / 'artwork-check.json').read_text())}
        entries = github.search(github.sources()[0], '')
        self.assertEqual(len(entries), 4)
        for entry in entries:
            self.assertTrue(entry['summary'])
            images = entry['images']
            self.assertEqual(entry['icon'], images['icon'])
            for url in [u for u in [images['icon'], images['banner']] if u] + images['screenshots']:
                self.assertTrue(url.startswith('https://'))
                self.assertEqual(evidence[url]['status'], 200)
                self.assertTrue(evidence[url]['image'])
        self.assertTrue(entries[1]['images']['screenshots'])
        self.assertTrue(entries[2]['images']['screenshots'])

    def test_topic_keeps_curated_artwork(self):
        curated = github._curated()[1]
        repo = {'full_name': curated['repo'], 'name': 'open-brush',
                'owner': {'avatar_url': 'https://avatars.githubusercontent.com/u/1'}}
        with patch.object(github, '_api', return_value={'items': [repo]}):
            entry = github.search(github.sources()[0], 'topic:openxr')[0]
        self.assertEqual(entry['images'], curated['images'])
        unknown = github.details(github.sources()[0], 'unknown/project')
        self.assertEqual(unknown['icon'], 'https://github.com/unknown.png')
        self.assertIsNone(unknown['images']['banner'])

    def test_real_releases(self):
        for key, repo in [('khronos', 'KhronosGroup/OpenXR-SDK-Source'),
                          ('brush', 'icosa-foundation/open-brush'), ('tux', 'SgtBilko76/SuperTux-3D')]:
            with patch.object(github, '_api', return_value=json.loads((FIX / (key + '-releases.json')).read_text())):
                e = github.details(github.sources()[0], repo)
                self.assertTrue(e['downloadable'])
                self.assertTrue(e['versions'][0]['name'].endswith('.apk'))
                self.assertIsNone(e['version_code'])
                search_entry = github.search(github.sources()[0], repo)[0]
                self.assertEqual(e['images'], search_entry['images'])
                self.assertEqual(e['icon'], e['images']['icon'])
                with self.assertRaises(SourceError):
                    github.download(github.sources()[0], repo, 123)

    def test_topic_results_need_approval(self):
        data = json.loads((FIX / 'topic.json').read_text())
        with patch.object(github, '_api', return_value=data):
            entries = github.search(github.sources()[0], 'topic:openxr')
        self.assertTrue(entries)
        self.assertTrue(any(not e['downloadable'] for e in entries))
        for entry, repo in zip(entries, data['items']):
            self.assertEqual(entry['icon'], repo['owner']['avatar_url'])
            self.assertEqual(entry['images']['icon'], entry['icon'])
            self.assertIsNone(entry['images']['banner'])
            self.assertEqual(entry['images']['screenshots'], [])
        self.assertFalse(github.details(github.sources()[0], 'unknown/project')['downloadable'])
        with self.assertRaises(SourceError):
            github.search(github.sources()[0], 'topic:piracy')

    def test_feed_free_android_only_and_deduplicated(self):
        with patch.object(_web, 'read', return_value=(FIX / 'itch-feed.txt').read_bytes()):
            entries = itch.search(itch.sources()[0], '')
            self.assertEqual(len(entries), 9)
            e = itch.details(itch.sources()[0], entries[0]['id'])
            self.assertTrue(e['vr'])
            self.assertTrue(e['images']['banner'])
            for item in entries:
                self.assertEqual(item['icon'], item['images']['icon'])
                self.assertEqual(item['icon'], item['images']['banner'])
                self.assertEqual(item['images']['screenshots'], [])
            self.assertFalse(e['downloadable'])
            self.assertEqual(itch.search(itch.sources()[0], 'off nominal')[0]['name'], 'Off Nominal')
        with self.assertRaises(SourceError):
            itch.download(itch.sources()[0], entries[0]['id'])
        with self.assertRaises(SourceError):
            itch._parse(itch.sources()[0], b'not xml')

    def test_paid_and_unsafe_feed(self):
        raw = (FIX / 'itch-feed.txt').read_bytes().replace(b'$0.00', b'$1.00')
        self.assertEqual(itch._parse(itch.sources()[0], raw), [])
        raw = (FIX / 'itch-feed.txt').read_bytes().replace(b'https://absyo.itch.io', b'http://localhost')
        self.assertFalse(any(e['name'] == 'Off Nominal' for e in itch._parse(itch.sources()[0], raw)))

    def test_download_hash_and_cleanup(self):
        b = io.BytesIO()
        with zipfile.ZipFile(b, 'w') as z:
            z.writestr('AndroidManifest.xml', b'fixture')
        raw = b.getvalue()
        digest = hashlib.sha256(raw).hexdigest()
        with patch.object(_web, 'open_url', return_value=io.BytesIO(raw)):
            result = _web.apk('https://github.com/owner/repo/file.apk', github.HOSTS, digest)
        self.assertTrue(result['verified'])
        self.assertEqual(Path(result['apk']).read_bytes(), raw)
        with patch.object(_web, 'open_url', return_value=io.BytesIO(raw)):
            self.assertFalse(_web.apk('https://github.com/file.apk', github.HOSTS)['verified'])
        for content, expected in [(raw, '0' * 64), (b'html challenge', None)]:
            with patch.object(_web, 'open_url', return_value=io.BytesIO(content)), self.assertRaises(SourceError):
                _web.apk('https://github.com/file.apk', github.HOSTS, expected)
        self.assertFalse(list(Path(self.tmp.name).glob('*.part')))

    def test_origin_and_redirect(self):
        for url in ['http://github.com/x', 'https://evil.test/x', 'https://user@github.com/x']:
            with self.assertRaises(SourceError):
                _web.checked_url(url, github.HOSTS)
        req = urllib.request.Request('https://api.github.com/x', headers={'Authorization': 'Bearer secret'})
        handler = _web.Redirect(('api.github.com', 'github.com'))
        redirected = handler.redirect_request(req, None, 302, '', {}, 'https://github.com/x')
        self.assertFalse(redirected.has_header('Authorization'))
        with self.assertRaises(SourceError):
            handler.redirect_request(req, None, 302, '', {}, 'https://evil.test/x')

    def test_cache_rate_limit_and_invalid_json(self):
        with patch.object(_web, 'open_url', return_value=io.BytesIO(b'index')) as op:
            self.assertEqual(_web.read('https://itch.io/test', ('itch.io',)), b'index')
            self.assertEqual(_web.read('https://itch.io/test', ('itch.io',)), b'index')
            self.assertEqual(op.call_count, 1)
        error = urllib.error.HTTPError('https://api.github.com/x', 403, 'limited', {}, None)
        with patch.object(_web, 'open_url', side_effect=error), patch.dict(os.environ, {'FRAME_GITHUB_TOKEN': ''}), \
                self.assertRaisesRegex(SourceError, 'FRAME_GITHUB_TOKEN'):
            github._api('/x')
        with patch.object(_web, 'open_url', side_effect=error), patch.object(_web.time, 'time', return_value=1e12):
            self.assertEqual(_web.read('https://itch.io/test', ('itch.io',)), b'index')  # throttled: stale copy
        with patch.object(_web, 'read', return_value=b'<html>'), self.assertRaises(SourceError):
            github._api('/x')

    def test_prune_caps_apks_by_age_and_removes_orphans(self):
        now = 1e9
        self.root.mkdir(parents=True)
        def make(folder, name, size, age):
            path = Path(folder) / name
            path.mkdir() if size is None else path.write_bytes(b'x' * size)
            os.utime(str(path), (now - age, now - age))
            return path
        pub = self.tmp.name
        oldest = make(self.root, 'a.apk', 40, 9000)
        old = make(pub, 'b.apk', 40, 8000)
        kept = make(self.root, 'c.apk', 40, 7200)
        recent = make(pub, 'd.apk', 40, 60)  # just downloaded: never pruned
        orphan, busy = make(self.root, 'x.part', 5, 90000), make(pub, 'y.part', 5, 60)
        listing, fresh = make(pub, 'l.data', 5, 8 * 86400), make(pub, 'm.data', 5, 3600)
        tmpdir = make(self.root, 'tmpabc', None, 90000)
        with patch.object(_web, 'APK_CAP', 100), patch.object(_web.time, 'time', return_value=now):
            _web.prune()
        self.assertEqual([p.exists() for p in (oldest, old, kept, recent)], [False, False, True, True])
        self.assertEqual([p.exists() for p in (orphan, busy, listing, fresh, tmpdir)], [False, True, False, True, False])

    def test_prune_rechecks_before_deleting_and_spares_apks_in_use(self):
        self.root.mkdir(parents=True)
        old = time.time() - 7200
        reused, claimed, stale = self.root / 'a.apk', self.root / 'b.apk', self.root / 'c.apk'
        for path in (reused, claimed, stale):
            path.write_bytes(b'x' * 40)
        _web.claim(claimed)
        self.addCleanup(_web.release, claimed)
        for path in (reused, claimed, stale):
            os.utime(str(path), (old, old))
        real = os.listdir
        def listdir(folder):
            if folder == self.tmp.name:  # scanning the second folder: a download reuses a.apk meanwhile
                self.assertTrue(_web.touch(reused))
            return real(folder)
        with patch.object(_web, 'APK_CAP', 10), patch.object(_web.os, 'listdir', listdir):
            _web.prune()
        self.assertEqual([p.exists() for p in (reused, claimed, stale)], [True, True, False])
        _web.release(claimed)
        with patch.object(_web, 'APK_CAP', 10):
            _web.prune()
        self.assertFalse(claimed.exists())
        self.assertFalse(_web.touch(stale))  # a pruned APK is reported gone, so it's downloaded again

    def test_backoff_honours_retry_after_per_host(self):
        from apk_sources import SourceLimited
        from email.utils import formatdate
        now = [1e9]
        clock = patch.object(_web.time, 'time', side_effect=lambda: now[0])
        clock.start()
        self.addCleanup(clock.stop)
        error = urllib.error.HTTPError('https://itch.io/a', 429, 'slow down', {'Retry-After': '120'}, None)
        with patch.object(_web, 'open_url', side_effect=error) as op:
            with self.assertRaisesRegex(SourceLimited, '^itch.io is limiting requests; try again in 2 minutes$'):
                itch.search(itch.sources()[0], '')
            with self.assertRaises(SourceLimited) as caught:  # other URLs on the host wait too
                _web.read('https://itch.io/b', ('itch.io',), name='itch.io')
            self.assertEqual(op.call_count, 1)
            self.assertAlmostEqual(caught.exception.retry_after, 120)
            with self.assertRaises(SourceLimited):
                _web.apk('https://itch.io/c.apk', ('itch.io',))
            self.assertEqual(op.call_count, 1)
        with patch.object(_web, 'open_url', return_value=io.BytesIO(b'fresh')):
            self.assertEqual(_web.read('https://api.github.com/x', ('api.github.com',)), b'fresh')  # other hosts unaffected
        now[0] += 121
        with patch.object(_web, 'open_url', return_value=io.BytesIO(b'feed')):
            self.assertEqual(_web.read('https://itch.io/b', ('itch.io',)), b'feed')
        cases = [({}, 600), ({'Retry-After': formatdate(now[0] + 300, usegmt=True)}, 300),
                 ({'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': str(int(now[0]) + 60)}, 60)]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                _web._limited.clear()
                self.assertAlmostEqual(_web.throttle('https://h.test/x', headers), expected, delta=1)
                self.assertAlmostEqual(_web.wait_time('https://h.test/y'), expected, delta=1)
        error = urllib.error.HTTPError('https://api.github.com/x', 403, 'limited', {}, None)
        with patch.object(_web, 'open_url', side_effect=error), patch.dict(os.environ, {'FRAME_GITHUB_TOKEN': ''}), \
                self.assertRaisesRegex(SourceLimited, '^GitHub is limiting requests; try again in 10 minutes .*TOKEN'):
            github._api('/y')


if __name__ == '__main__':
    unittest.main()
