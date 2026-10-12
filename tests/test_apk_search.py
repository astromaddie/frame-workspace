import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ui'))
from apk_sources import search, SourceError
import server

ENTRIES = json.loads((Path(__file__).parent / 'fixtures/apk-search/entries.json').read_text())


def fake(source_id='one', fn=None):
    return types.SimpleNamespace(KIND=source_id, sources=lambda: [dict(id=source_id, name=source_id,
                                 enabled=True, trust='official', builtin=True)],
                                 search=fn or (lambda s, q, limit=50: [e for e in ENTRIES if e['source'] == source_id]),
                                 details=lambda s, i: ENTRIES[0],
                                 download=Mock(return_value={'apk': '/fake.apk', 'obb': []}))


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = patch.object(search, 'settings_path', return_value=Path(self.tmp.name) / 'enabled.json')
        p.start()
        self.addCleanup(p.stop)
        for state in (search._running, search._pending, search._status, search._game_data):
            state.clear()
        from apk_sources import _web
        self.claims = []
        for name in ('claim', 'release'):  # fake downloads aren't real files
            p = patch.object(_web, name, side_effect=lambda path, name=name: self.claims.append((name, path)))
            p.start()
            self.addCleanup(p.stop)


class SearchTests(SettingsTest):
    def test_group_does_not_merge_distinct_or_unknown_packages(self):
        result = search.group(ENTRIES)
        self.assertEqual(len(result), 3)
        self.assertEqual(sorted(len(a['offers']) for a in result), [1, 2, 2])
        same_name = dict(ENTRIES[0], package=None)
        self.assertEqual(len(search.group(ENTRIES[:1] + [same_name])), 2)

    def test_rank_exact_and_installable_and_verified(self):
        result = search.group(ENTRIES, 'Open Brush')
        self.assertEqual(result[0]['package'], 'org.brush')
        self.assertEqual(result[0]['offers'][0]['source'], 'one')
        self.assertEqual(result[1]['package'], 'org.other')
        self.assertEqual(len(search.group(ENTRIES, vr=False)), 1)
        self.assertEqual(len(search.group(ENTRIES, installable=True)), 1)

    def test_unknown_vr_counts_as_flat(self):
        entries = [dict(ENTRIES[3], id='a', name='Flat', vr=False), dict(ENTRIES[3], id='b', name='Unknown', vr=None),
                   dict(ENTRIES[3], id='c', name='Headset', vr=True)]
        self.assertEqual(sorted(a['name'] for a in search.group(entries, vr=False)), ['Flat', 'Unknown'])
        self.assertEqual([a['name'] for a in search.group(entries, vr=True)], ['Headset'])

    def test_browse_puts_unknown_fit_vr_first_and_blocked_last(self):
        entries = [dict(source='s', id='flat', name='Flat', package='a.flat', vr=False, min_sdk=21, abis=[]),
                   dict(source='s', id='vr', name='Headset', package='a.vr', vr=True),
                   dict(source='s', id='bad', name='Blocked', package='a.bad', vr=True, min_sdk=34, abis=[])]
        self.assertEqual([a['name'] for a in search.group(entries)], ['Headset', 'Flat', 'Blocked'])

    def test_fit_unknown_and_native_free_and_vr_hints(self):
        self.assertIsNone(search.fit({})['installable'])
        self.assertTrue(search.fit({'min_sdk': 23, 'abis': []})['installable'])
        self.assertFalse(search.fit({'min_sdk': 23, 'abis': ['x86']})['installable'])
        self.assertIn('Legacy VrApi', search.fit({'engine': 'VrApi'})['reasons'][0])

    def test_timeout_and_failure_leave_other_results(self):
        release = threading.Event()
        calls = []
        def slow(s, q, limit=50):
            calls.append(q)
            release.wait(2)
            return []
        mods = [fake(), fake('slow', slow), fake('broken', Mock(side_effect=SourceError('offline')))]
        try:
            with patch.object(search, 'modules', return_value=(mods, [])):
                started = time.monotonic()
                result = search.search(timeout=.03)
                self.assertLess(time.monotonic() - started, .3)
                self.assertTrue(result['apps'])
                self.assertEqual([s['status'] for s in result['sources']], ['ok', 'loading', 'error'])
                self.assertEqual(search.search('other', timeout=.03)['sources'][1]['status'], 'loading')
                search.search('newest', timeout=.03)
                self.assertEqual(calls, [''])
                queued = search._pending['slow']
                release.set()
                self.assertTrue(queued['event'].wait(2))
                self.assertEqual(queued['entries'], [])
                self.assertEqual(calls, ['', 'newest'])  # 'other' was superseded, never run
        finally:
            release.set()

    def test_query_arriving_as_a_search_finishes_is_not_stranded(self):
        mod = fake()
        source = mod.sources()[0]
        arrived = []

        class Event(threading.Event):
            def set(self):
                if not arrived:  # a request lands just as the first search completes
                    arrived.append(None)
                    t = threading.Thread(target=lambda: arrived.append(search._launch(mod, source, 'second', 50)))
                    t.start()
                    t.join(.3)  # blocks on search._lock if completion is published atomically
                super().set()
        with patch.object(search, 'threading', types.SimpleNamespace(Event=Event, Thread=threading.Thread)):
            search._launch(mod, source, 'first', 50)
            for _ in range(200):
                if len(arrived) == 2:
                    break
                time.sleep(.01)
            self.assertTrue(arrived[1]['event'].wait(2))
        self.assertEqual(arrived[1]['query'], ('second', 50))

    def test_set_enabled_does_not_hold_search_lock_in_source(self):
        free = []
        def set_enabled(source_id, enabled):
            t = threading.Thread(target=lambda: free.append(search._lock.acquire(timeout=1) and not search._lock.release()))
            t.start()
            t.join()
        mod = fake()
        mod.set_enabled = set_enabled
        with patch.object(search, 'modules', return_value=([mod], [])):
            search.set_enabled('one', False)
        self.assertEqual(free, [True])

    def test_stale_source_status(self):
        mod = fake()
        mod.stale = lambda source: True
        with patch.object(search, 'modules', return_value=([mod], [])):
            status = search.search(timeout=1)['sources'][0]
        self.assertEqual((status['status'], status['stale']), ('ok', True))

    def test_limited_source_status(self):
        from apk_sources import SourceLimited
        mods = [fake('busy', Mock(side_effect=SourceLimited('busy is limiting requests', 60)))]
        with patch.object(search, 'modules', return_value=(mods, [])):
            status = search.search(timeout=1)['sources'][0]
        self.assertEqual((status['status'], status['error']), ('limited', 'busy is limiting requests'))

    def test_disable_persists_and_prevents_queries_and_installs(self):
        mod = fake()
        with patch.object(search, 'modules', return_value=([mod], [])):
            search.set_enabled('one', False)
            self.assertFalse(search.sources()[0]['enabled'])
            self.assertEqual(search.search()['apps'], [])
            with self.assertRaisesRegex(SourceError, 'disabled'):
                search.install('one', 'brush')

    def test_install_passes_metadata_artwork_and_obb(self):
        mod = fake()
        mod.download.return_value.update(obb=['main.obb'], artwork={'hero': '/hero.png'}, icon_png=b'png')
        def install(apk, name=None, icon_png=None, source=None, artwork=None):
            self.assertEqual((apk, name, icon_png, source, artwork),
                             ('/fake.apk', 'Open Brush', b'png', 'one', {'hero': '/hero.png'}))
            return {'package': 'org.brush'}
        with patch.object(search, 'modules', return_value=([mod], [])), \
             patch.object(server.frame_android, 'install_obb', create=True) as obb:
            # An actual function exposes the future signature for inspection.
            with patch.object(server.frame_android, 'install', install):
                result = search.install('one', 'brush', 1)
            # The app's instance isn't running right after install, so game data is a follow-up step.
            obb.assert_not_called()
            self.assertTrue(result['game_data'])
            self.assertIn('Add game data', result['message'])
            obb.return_value = {'package': 'org.brush', 'obb': []}
            self.assertEqual(search.add_game_data('org.brush')['message'], 'Game data added')
            obb.assert_called_once_with('org.brush', ['main.obb'])
            with self.assertRaisesRegex(SourceError, 'install it again'):
                search.add_game_data('org.brush')
            mod.download.assert_called_once_with(mod.sources()[0] | {'status': 'not searched'}, 'brush', version_code=1)

    def test_install_uses_source_image_urls_as_steam_artwork(self):
        mod = fake()
        plain = mod.details
        mod.details = lambda source, entry_id: dict(plain(source, entry_id), images={
            'icon': 'https://img.example/icon.png', 'banner': 'https://img.example/banner.png',
            'screenshots': ['https://img.example/1.png', None]})
        seen = {}
        def install(apk, name=None, icon_png=None, source=None, artwork=None):
            seen['artwork'] = artwork
            return {'package': 'org.brush'}
        with patch.object(search, 'modules', return_value=([mod], [])), \
             patch.object(server.frame_android, 'install', install):
            search.install('one', 'brush')
        self.assertEqual(seen['artwork'], {'icon': 'https://img.example/icon.png',
                                           'banner': 'https://img.example/banner.png',
                                           'screenshots': ['https://img.example/1.png']})

    def test_discovery_and_demo_are_opt_in(self):
        module = fake()
        with patch.object(search.pkgutil, 'iter_modules', return_value=[types.SimpleNamespace(name='example')]), \
             patch.object(search.importlib, 'import_module', return_value=module), \
             patch.dict(search.os.environ, {'FRAME_APK_SEARCH_DEMO': '0'}):
            self.assertEqual(search.modules(), ([module], []))
        with patch.object(search.pkgutil, 'iter_modules', return_value=[]), \
             patch.dict(search.os.environ, {'FRAME_APK_SEARCH_DEMO': '0'}):
            self.assertEqual(search.modules(), ([], []))

    def test_newest_compatible_then_official_offer(self):
        first = dict(ENTRIES[0], verified=False, trust='community')
        newer = dict(first, source='new', version_code=2, updated='2026-01-01')
        official = dict(newer, source='official', trust='official')
        incompatible = dict(newer, source='blocked', verified=True, min_sdk=40)
        offers = search.group([first, incompatible, newer, official])[0]['offers']
        self.assertEqual([e['source'] for e in offers], ['official', 'new', 'one', 'blocked'])

    def test_missing_obb_support_stops_before_install(self):
        mod = fake()
        mod.download.return_value['obb'] = ['main.obb']
        with patch.object(search, 'modules', return_value=([mod], [])), \
             patch.object(server.frame_android, 'install') as install, \
             patch.dict(server.frame_android.__dict__):
            server.frame_android.__dict__.pop('install_obb', None)
            with self.assertRaisesRegex(SourceError, 'OBB'):
                search.install('one', 'brush')
            install.assert_not_called()

    def test_downloaded_apk_is_protected_from_pruning_while_installing(self):
        mod = fake()
        def install(apk, **kwargs):
            self.assertEqual(self.claims, [('claim', '/fake.apk')])
            raise server.frame_android.FrameError('adb failed')
        with patch.object(search, 'modules', return_value=([mod], [])), \
                patch.object(server.frame_android, 'install', install):
            with self.assertRaises(server.frame_android.FrameError):
                search.install('one', 'brush')
        self.assertEqual(self.claims, [('claim', '/fake.apk'), ('release', '/fake.apk')])

    def test_listing_cannot_download(self):
        mod = fake()
        mod.details = lambda s, i: dict(ENTRIES[0], downloadable=False)
        with patch.object(search, 'modules', return_value=([mod], [])):
            with self.assertRaisesRegex(SourceError, 'developer page'):
                search.install('one', 'brush')
            mod.download.assert_not_called()


class EndpointTests(SettingsTest):
    def setUp(self):
        super().setUp()
        self.mod = fake()
        p = patch.object(search, 'modules', return_value=([self.mod], []))
        p.start()
        self.addCleanup(p.stop)
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, method, path, body=None):
        c = http.client.HTTPConnection('127.0.0.1', self.httpd.server_port)
        c.request(method, path, json.dumps(body) if body is not None else None,
                  {'X-Frame-UI': '1', 'Content-Type': 'application/json'})
        r = c.getresponse()
        result = r.status, json.loads(r.read())
        c.close()
        return result

    def test_http_search_and_validation(self):
        self.assertEqual(self.request('GET', '/api/sources')[1]['sources'][0]['id'], 'one')
        self.assertTrue(self.request('GET', '/api/search?q=Brush&vr=true')[1]['apps'])
        self.assertEqual(self.request('GET', '/api/search?vr=invalid')[0], 400)
        self.assertEqual(self.request('GET', '/api/search?source=missing')[0], 400)
        for body in ({'source': 'one'}, {'source': 'one', 'id': 'brush', 'version_code': True}):
            self.assertEqual(self.request('POST', '/api/sources/install', body)[0], 400)

    def test_http_install_background_job(self):
        with patch.object(server.frame_android, 'install', return_value={'package': 'org.brush'}) as install:
            status, reply = self.request('POST', '/api/sources/install', {'source': 'one', 'id': 'brush'})
            self.assertEqual(status, 200)
            for _ in range(100):
                job = self.request('GET', '/api/job?id=' + reply['job'])[1]
                if job['done']:
                    break
                time.sleep(.01)
            self.assertTrue(job['done'])
            self.assertIsNone(job['error'])
            install.assert_called_once_with('/fake.apk', name='Open Brush', icon_png=None, source='one')

    def test_details_endpoint_and_real_install_stages(self):
        code, entry = self.request('GET', '/api/sources/details?source=one&id=brush')
        self.assertEqual(code, 200)
        self.assertEqual(entry['name'], 'Open Brush')
        self.assertEqual(entry['verdict']['label'], 'Ready to try on the Frame')
        self.assertIn('artwork', entry)
        self.assertEqual(self.request('GET', '/api/sources/details?source=one')[0], 400)
        stages = []
        with patch.object(server.frame_android, 'install', return_value={'package':'org.brush'}):
            search.install('one', 'brush', progress=lambda stage, percent: stages.append((stage,percent)))
        self.assertEqual(stages, [('Downloading',None),('Installing',None)])

    def test_http_repository_management(self):
        self.assertEqual(self.request('POST', '/api/sources', {'action': 'enable', 'source': 'one', 'enabled': False})[0], 200)
        code, reply = self.request('POST', '/api/sources', {'action': 'add', 'url': 'https://repo.example/repo'})
        self.assertEqual(code, 400)
        self.assertIn('not available', reply['error'])
        mod = fake('fdroid')
        added = {'id': 'fdroid-user-1', 'name': 'repo.example', 'fingerprint': 'ab' * 32, 'trust_on_first_use': True}
        mod.add_repo, mod.remove_repo, mod.set_enabled = Mock(return_value=added), Mock(), Mock()
        with patch.object(search, 'modules', return_value=([mod], [])):
            code, reply = self.request('POST', '/api/sources', {'action': 'add', 'url': 'https://repo.example/repo'})
            self.assertEqual(code, 200)
            job = self.wait(reply['job'])
            self.assertEqual(job['message'], 'Added repo.example. Trusted on first use: ' + 'AB' * 32)
            self.assertEqual(job['result']['source']['fingerprint'], 'ab' * 32)
            mod.add_repo.assert_called_once_with(url='https://repo.example/repo', fingerprint=None, name=None)
            link = 'fdroidrepos://repo.example/repo?fingerprint=' + 'ab' * 32
            mod.add_repo.return_value = dict(added, trust_on_first_use=False)
            job = self.wait(self.request('POST', '/api/sources', {'action': 'add', 'url': link})[1]['job'])
            self.assertEqual(job['message'], 'Added repo.example')
            mod.add_repo.assert_called_with(url=link, fingerprint=None, name=None)
            mod.add_repo.side_effect = SourceError('repository fingerprint mismatch')
            job = self.wait(self.request('POST', '/api/sources', {'action': 'add', 'url': link})[1]['job'])
            self.assertEqual(job['error'], 'repository fingerprint mismatch')  # no "SourceError:" prefix
            for url in ('http://repo.example/repo', 'fdroidrepo://repo.example/repo', 'https://u@repo.example/'):
                self.assertEqual(self.request('POST', '/api/sources', {'action': 'add', 'url': url})[0], 400)
            self.assertEqual(self.request('POST', '/api/sources', {'action': 'remove', 'source': 'fdroid'})[0], 200)
            mod.remove_repo.assert_called_once_with(source_id='fdroid')

    def test_http_add_game_data_job(self):
        search._game_data['org.brush'] = ['/cache/main.1.org.brush.obb']
        self.addCleanup(search._game_data.clear)
        with patch.object(server.frame_android, 'install_obb', create=True,
                          side_effect=server.frame_android.FrameError('start this app instance before installing OBB data')):
            job = self.wait(self.request('POST', '/api/sources', {'action': 'game-data', 'package': 'org.brush'})[1]['job'])
        self.assertEqual(job['error'], 'start this app instance before installing OBB data')
        with patch.object(server.frame_android, 'install_obb', create=True, return_value={'package': 'org.brush'}) as obb:
            job = self.wait(self.request('POST', '/api/sources', {'action': 'game-data', 'package': 'org.brush'})[1]['job'])
        self.assertEqual(job['message'], 'Game data added')
        obb.assert_called_once_with('org.brush', ['/cache/main.1.org.brush.obb'])

    def wait(self, job_id):
        for _ in range(200):
            job = self.request('GET', '/api/job?id=' + job_id)[1]
            if job['done']:
                return job
            time.sleep(.01)
        self.fail('job did not finish')
