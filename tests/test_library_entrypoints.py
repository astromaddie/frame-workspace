"""Every public sideload entry point reaches the mandatory five-slot writer."""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import contextlib
import io
import json
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ui'))
import frame_android as android
import frame_artwork as artwork
import frame_catalog as catalog
import frame_apk_versions as versions
import frame_titles as titles
import frame_steamgriddb as sgdb
import server
import frame_webinstall as webinstall


class EntryPoints(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.info = {'package':'org.example.game', 'label':'Example', 'version':'1', 'version_code':1,
                     'vr':False, 'repairable':False, 'launchable':True, 'min_sdk':24, 'abis':[], 'icon_png':None}
        self.stack.enter_context(patch.object(android, 'apk_info', return_value=self.info))
        self.stack.enter_context(patch.object(android, 'read_meta', return_value=None))
        self.stack.enter_context(patch.object(android, '_copy'))
        self.stack.enter_context(patch.object(android, 'ssh', return_value='/home/steamos'))
        self.stack.enter_context(patch.object(artwork, 'prepare', return_value=({}, [])))
        self.api = self.stack.enter_context(patch.object(android, 'shortcut_tool', side_effect=self.steam))

    def steam(self, *args, **kwargs):
        if args[0] == 'add': return '3346865537'
        if args[0] == 'render': return json.dumps({'paths': {s:'/tmp/'+s+'.png' for s in artwork.SLOTS}})
        if args[0] == 'list': return json.dumps([{'appid':3346865537,'name':'Example','devkit_gameid':'Example'}])
        return '{"warnings":[]}'

    def assert_art(self):
        calls = [c.args for c in self.api.call_args_list]
        self.assertEqual(sum(c[0] == 'render' for c in calls), 1)
        configs = [c for c in calls if c[0] == 'configure']
        self.assertEqual(len(configs), 1)
        self.assertEqual(set(json.loads(configs[0][7])), set(artwork.SLOTS))

    def test_cli_install(self):
        with patch.object(sys, 'argv', ['frame_android.py', 'install', 'game.apk']), patch('builtins.print'):
            android.main()
        self.assert_art()

    def test_file_upload(self):
        class Request:
            headers = {'X-Filename':'game.apk','X-Mode':'apk','Content-Length':'3'}
            rfile = io.BytesIO(b'apk')
        with patch.object(server, 'ensure_master'):
            result = server.Handler.upload(Request())
        self.assertEqual(result['app']['package'], self.info['package'])
        self.assert_art()

    def test_catalogue_install(self):
        with patch.object(catalog, 'app', return_value={'r':'yes','t':False,'n':'Example'}), \
                patch.object(catalog, 'fetch_apk', return_value='game.apk'), \
                patch.object(catalog, 'fetch_icon', return_value=None):
            catalog.install(self.info['package'])
        self.assert_art()

    def test_version_finder_install(self):
        record = {'url':'https://example.org/app.apk','sha256':'fixture','version_code':1,'source':'F-Droid'}
        with patch.object(versions, '_versions', return_value=([record], [])), \
                patch.object(catalog, 'fetch_apk', return_value='game.apk'):
            versions.install(self.info['package'], record['url'])
        self.assert_art()

    def test_web_download_install(self):
        result = webinstall.dispatch('game.apk', source='https://example.org/game.apk')
        self.assertEqual(result['kind'], 'apk')
        self.assert_art()

    def test_refresh_api_covers_android_apps_and_titles(self):
        with patch.object(server, 'ensure_master'), \
                patch.object(server, 'start_job', side_effect=lambda label, work: work()), \
                patch.object(android, 'refresh_art', return_value=[]) as refresh, \
                patch.object(titles, 'refresh_art', return_value=[{'id':'G','error':'x'}]) as title_refresh:
            self.assertEqual(server.android({'action':'refresh-art', 'all':True}),
                             {'apps':[], 'titles':[{'id':'G','error':'x'}]})
            refresh.assert_called_once_with(); title_refresh.assert_called_once_with()
            server.titles({'action':'refresh-art', 'id':'G'})
            title_refresh.assert_called_with('G')

    def test_backfill_fills_only_entries_pending_since_install(self):
        import threading
        ran = threading.Event()
        with patch.dict(server._backfill, {'running': False, 'last': 0.0}), \
                patch.object(android, 'refresh_art', side_effect=[RuntimeError('odd'), None]) as refresh, \
                patch.object(titles, 'refresh_art', side_effect=lambda gid, **kw: ran.set()) as title_refresh:
            apps = [{'package':'org.a.x','art_pending':True}, {'package':'org.b.x','art_pending':True},
                    {'package':'org.c.x','art_missing':True}]  # legacy: no record of art, but not pending
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertTrue(server.backfill_art(apps=apps, titles=[{'id':'G','art_pending':True}]))
                self.assertTrue(ran.wait(5))
            self.assertFalse(server.backfill_art(apps=apps))  # throttled
        self.assertEqual([c.args for c in refresh.call_args_list], [('org.a.x',), ('org.b.x',)])
        self.assertTrue(all(c.kwargs == {'fill_only': True} for c in refresh.call_args_list))
        title_refresh.assert_called_once_with('G', fill_only=True)

    def test_bulk_fill_only_refresh_keeps_names_and_art(self):
        meta = {**self.info, 'instance':2800000001, 'shortcut':3346865537, 'flatscreen':False}
        with patch.object(android, 'list_apps', return_value=[{'package':self.info['package']}]), \
                patch.object(android, '_meta_or_fail', return_value=meta), \
                patch.object(android, 'ssh', side_effect=lambda cmd, **kw: json.dumps({'icon_png':''}) if cmd == 'python3 -' else '/home/steamos'), \
                patch.object(android, '_write_meta'):
            android.refresh_art(fill_only=True)
        options = json.loads(next(c.args for c in self.api.call_args_list if c.args[0] == 'configure')[8])
        self.assertTrue(options['fill_only'])
        self.api.reset_mock()
        with patch.object(titles, 'list_titles', return_value=[{'id':'Game','name':'Game','frame_control':True}]), \
                patch.object(titles, '_check_id', side_effect=lambda gid: gid), \
                patch.object(titles, '_library_shortcut', return_value=7), \
                patch.object(titles, 'ssh', side_effect=lambda cmd, **kw: '{"name":"Game"}' if cmd.startswith('cat devkit')
                             else '{"artwork":{},"icon":""}' if cmd == 'python3 -' else '/home/steamos'):
            titles.refresh_art(fill_only=True)
        options = json.loads(next(c.args for c in self.api.call_args_list if c.args[0] == 'configure')[8])
        self.assertTrue(options['fill_only'])

    def test_upgrade_leaves_legacy_customised_titles_alone(self):
        # A title installed before art_pending existed, whose art the user has customised in Steam.
        legacy = [{'id':'Game','settings':{'compat_tool':'proton-experimental'},'argv':['game.exe'],
                   'meta':{'name':'Game','target':'game.exe','source':'game.zip'}}]
        with patch.object(titles, 'ssh', return_value=json.dumps(legacy)):
            listed = titles.list_titles()
        self.assertEqual((listed[0]['art_pending'], listed[0]['art_missing']), (False, False))
        with patch.dict(server._backfill, {'running': False, 'last': 0.0}), \
                patch.object(titles, 'refresh_art') as refresh, patch.object(android, 'refresh_art') as app_refresh:
            self.assertFalse(server.backfill_art(apps=[{'package':'org.old.app','library_version':1}], titles=listed))
        refresh.assert_not_called(); app_refresh.assert_not_called()
        self.api.assert_not_called()

    def test_art_missing_flags(self):
        self.assertTrue(android.art_missing({'artwork': {}}))
        self.assertTrue(android.art_missing({'artwork': {'grid': 'x'}}))
        self.assertFalse(android.art_missing({'artwork': {s: 'x' for s in artwork.SLOTS}}))

    def test_source_search_shared_installer_contract(self):
        # Source workers hand their download and optional images to this public seam.
        android.install('game.apk', name='Example', source='source-search', artwork={'banner': b'fixture'})
        self.assert_art()

    def test_native_title_install(self):
        with tempfile.TemporaryDirectory() as root:
            plan = {'id':'Example','name':'Example','root':root,'size':3,'target':'game.exe',
                    'runtime':'proton-experimental','source':'example.zip'}
            def ssh(cmd, **kwargs):
                if 'test -d' in cmd: return ''
                if 'steamos-prepare-upload' in cmd: return '{"directory":"/home/steamos/devkit-game/Example"}'
                if 'steam-client-create-shortcut' in cmd: return '{"success":"registered"}'
                return ''
            with patch.object(titles, 'ssh', side_effect=ssh), patch.object(titles, 'ensure_utils'), \
                    patch.object(titles, '_copy_tree'), patch.object(titles, '_rsync', return_value=True):
                result = titles._install(plan, lambda *args: None)
        self.assertEqual(result['shortcut'], 3346865537)
        self.assert_art()
        config = next(c.args for c in self.api.call_args_list if c.args[0] == 'configure')
        self.assertEqual(json.loads(config[8])['category'], 'Sideloaded')
        self.assertEqual(config[3:5], ('',''))  # Never replace devkit's executable/runtime wiring.
        self.assertEqual(config[6], '')  # nor the VR flag the title declares
        self.assertEqual(set(result['artwork']), set(artwork.SLOTS))

    def test_native_renamed_shortcut_uses_saved_identity(self):
        self.api.side_effect = lambda *args, **kw: '[{"appid":42,"name":"Renamed"}]'
        with patch.object(titles, 'ssh', return_value='{"shortcut":42}'):
            self.assertEqual(titles._library_shortcut('Original', '/home/steamos/devkit-game/Original'), 42)

    def test_native_shortcut_never_matched_by_name_alone(self):
        d = '/home/steamos/devkit-game/Game'
        shortcuts = [{'appid':1,'name':'Game','exe':'"/home/steamos/.local/bin/game"','start_dir':'/home/steamos'},
                     {'appid':2,'name':'Other','exe':'"/home/steamos/devkit-game/Game2/g.exe"','start_dir':''}]
        self.api.side_effect = lambda *args, **kw: json.dumps(shortcuts)
        with patch.object(titles, 'ssh', return_value=''):
            self.assertIsNone(titles._library_shortcut('Game', d))
            shortcuts.append({'appid':3,'name':'Renamed','exe':'"/home/steamos/devkit-game/Game/bin/g.exe"','start_dir':''})
            self.assertEqual(titles._library_shortcut('Game', d), 3)
            shortcuts.append({'appid':4,'name':'Copy','exe':'','start_dir':d})
            with self.assertRaisesRegex(android.FrameError, 'ambiguous'):
                titles._library_shortcut('Game', d)

    def test_native_cleanup_failure_keeps_original_error(self):
        with tempfile.TemporaryDirectory() as root:
            plan = {'id':'Example','name':'Example','root':root,'size':3,'target':'game.exe',
                    'runtime':'proton-experimental','source':'example.zip'}
            def ssh(cmd, **kwargs):
                if 'steamos-prepare-upload' in cmd: return '{"directory":"/home/steamos/devkit-game/Example"}'
                if 'steam-client-create-shortcut' in cmd: return '{"success":"registered"}'
                return ''
            def steam(*args, **kwargs):
                if args[0] == 'remove': raise android.FrameError('Steam went away')
                return self.steam(*args)
            self.api.side_effect = steam
            with patch.object(titles, 'ssh', side_effect=ssh), patch.object(titles, 'ensure_utils'), \
                    patch.object(titles, '_copy_tree'), patch.object(titles, '_rsync', return_value=True), \
                    patch.object(android, 'apply_library', side_effect=android.FrameError('render failed')):
                with self.assertRaisesRegex(android.FrameError, 'render failed'):
                    titles._install(plan, lambda *args: None)

    def test_native_remove_survives_steam_being_down(self):
        cmds = []
        def ssh(cmd, **kwargs):
            cmds.append(cmd)
            return 'yes' if 'test -d' in cmd else '/home/steamos' if 'HOME' in cmd else ''
        self.api.side_effect = android.FrameError('SharedJSContext not found')
        with patch.object(titles, 'ssh', side_effect=ssh), patch.object(titles, 'ensure_utils'):
            titles.remove('Game')
        self.assertTrue(any('steamos-delete --delete-title Game' in c for c in cmds))

    def test_native_remove_deletes_before_tidying_the_shortcut(self):
        # steamos-delete finds the Proton prefix through the shortcut, so the shortcut must still exist.
        order = []
        def ssh(cmd, **kwargs):
            if 'steamos-delete' in cmd:
                order.append('delete')
            return 'yes' if 'test -d' in cmd else '/home/steamos' if 'HOME' in cmd else ''
        with patch.object(titles, 'ssh', side_effect=ssh), patch.object(titles, 'ensure_utils'), \
             patch.object(titles, '_library_shortcut', return_value=42), \
             patch.object(titles.frame_android, 'shortcut_tool', side_effect=lambda *a: order.append(a)):
            titles.remove('Game')
        self.assertEqual(order, ['delete', ('remove', '42')])

    def test_native_refresh_art_backfills_registered_title(self):
        meta = {'id':'Game','name':'My Game','source':'game.zip'}
        writes = []
        def ssh(cmd, input=None, **kwargs):
            if 'test -d' in cmd: return 'yes'
            if 'HOME' in cmd: return '/home/steamos'
            if cmd.startswith('cat devkit-game/Game-framecontrol.json'): return json.dumps(meta)
            if cmd == 'python3 -':
                self.assertIn("/home/steamos/devkit-game/Game/.frame-artwork", input)
                return json.dumps({'artwork': {'banner': 'YmFubmVy'}, 'icon': ''})
            if cmd.startswith('cat > devkit-game/Game-framecontrol.json'): writes.append(json.loads(input))
            return ''
        shortcuts = [{'appid':7,'name':'My Game','exe':'','start_dir':'/home/steamos/devkit-game/Game'}]
        self.api.side_effect = lambda *args, **kw: json.dumps(shortcuts) if args[0] == 'list' else self.steam(*args)
        with patch.object(titles, 'ssh', side_effect=ssh), \
                patch.object(artwork, 'prepare', return_value=({}, [])) as prepare:
            result = titles.refresh_art('Game')
        self.assertEqual(prepare.call_args.args[2], {'banner': b'banner'})
        self.assertEqual(result['shortcut'], 7)
        self.assertEqual(set(writes[-1]['artwork']), set(artwork.SLOTS))
        self.assert_art()
        shortcuts.clear()
        with patch.object(titles, 'ssh', side_effect=ssh), \
                patch.object(titles, 'list_titles', return_value=[{'id':'Game','name':'My Game','frame_control':True},
                                                                 {'id':'Valve','name':'V','frame_control':False}]):
            results = titles.refresh_art()
        self.assertEqual(len(results), 1)
        self.assertIn("hasn't registered", results[0]['error'])

    def test_native_failure_removes_new_blank_shortcut(self):
        with tempfile.TemporaryDirectory() as root:
            plan = {'id':'Example','name':'Example','root':root,'size':3,'target':'game.exe',
                    'runtime':'proton-experimental','source':'example.zip'}
            def ssh(cmd, **kwargs):
                if 'steamos-prepare-upload' in cmd: return '{"directory":"/home/steamos/devkit-game/Example"}'
                if 'steam-client-create-shortcut' in cmd: return '{"success":"registered"}'
                return ''
            with patch.object(titles, 'ssh', side_effect=ssh), patch.object(titles, 'ensure_utils'), \
                    patch.object(titles, '_copy_tree'), patch.object(titles, '_rsync', return_value=True), \
                    patch.object(android, 'apply_library', side_effect=android.FrameError('render failed')):
                with self.assertRaisesRegex(android.FrameError, 'render failed'):
                    titles._install(plan, lambda *args: None)
        self.assertIn(('remove', '3346865537'), [c.args for c in self.api.call_args_list])

    def test_refresh_does_not_reinstall_or_stop(self):
        meta = {**self.info, 'instance':2800000001, 'shortcut':3346865537, 'flatscreen':False}
        with patch.object(android, '_meta_or_fail', return_value=meta), \
                patch.object(android, 'ssh', side_effect=lambda cmd, **kw: json.dumps({'icon_png':''}) if cmd == 'python3 -' else '/home/steamos'), \
                patch.object(android, 'stop') as stop, patch.object(android, '_copy') as copy:
            android.refresh_art(self.info['package'])
        stop.assert_not_called(); copy.assert_not_called(); self.assert_art()

    def test_all_refresh_reports_partial_failures(self):
        import http.client
        with patch.object(android, 'list_apps', return_value=[{'package':'org.a.game'},{'package':'org.b.game'}]), \
                patch.object(android, '_meta_or_fail', side_effect=[http.client.RemoteDisconnected('gone'),
                                                                    AttributeError('odd')]):
            result=android.refresh_art()
        self.assertEqual(len(result),2)
        self.assertTrue(all('error' in a for a in result))

    def test_render_failure_cannot_report_success(self):
        self.api.side_effect = lambda *args, **kw: '3346865537' if args[0]=='add' else '{"paths":{}}'
        with self.assertRaisesRegex(android.FrameError,'every slot'):
            android.install('game.apk')


class Renderer(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node is needed for the canvas contract fixture')
    def test_canvas_slots_and_title_placement(self):
        subprocess.run(['node', str(ROOT / 'tests/fixtures/library/check-renderer.js')],
                       cwd=str(ROOT), check=True, capture_output=True, timeout=30)

    def test_desktop_packages_include_renderer_and_settings(self):
        config = json.loads((ROOT / 'app/package.json').read_text())
        # Single-file resources (licenses/notices) do not need a filter.
        resources = {r['from']: r.get('filter', ['**/*']) for r in config['build']['extraResources']}
        self.assertIn('*.js', resources['../ui'])
        self.assertIn('*.js', resources['../frame/android'])


class SteamGridDB(unittest.TestCase):
    def test_no_key_is_silent_and_offline(self):
        with patch.object(sgdb,'api_key',return_value=''), patch.object(sgdb,'_get') as get:
            self.assertEqual(sgdb.lookup('Game'),({},[]))
        get.assert_not_called()

    def test_exact_match_and_top_votes_per_slot(self):
        calls=[]
        def get(path,key,deadline=None):
            calls.append(path)
            if 'search' in path: return [{'id':1,'name':'Other Game'},{'id':2,'name':'Game'}]
            dims=(600,900) if '600x900' in path else (920,430)
            return [{'url':'https://example.org/low.png','score':2,'width':dims[0],'height':dims[1]},
                    {'url':'https://example.org/top.png','score':20,'width':dims[0],'height':dims[1]},
                    {'url':'https://example.org/nsfw.png','score':99,'nsfw':True,'width':dims[0],'height':dims[1]}]
        with patch.object(sgdb,'api_key',return_value='test-key'),patch.object(sgdb,'_get',side_effect=get):
            images,warnings=sgdb.lookup('Game VR')
        self.assertEqual(set(images),set(artwork.SLOTS));self.assertEqual(warnings,[])
        self.assertTrue(all(url.endswith('/top.png') for url in images.values()))
        self.assertTrue(all('/game/2?' in p for p in calls[1:]))
        # SteamGridDB rejects JPEG in logo/icon queries (the live API returned an error for SuperTux).
        for p in calls[1:]:
            jpeg = 'image%2Fjpeg' in p
            self.assertEqual(jpeg, p.startswith(('/grids/', '/heroes/')), p)

    def test_unicode_titles_match_exactly_and_symbols_never_match_all(self):
        self.assertEqual(sgdb._name('ビートセイバー VR!'), 'ビートセイバーvr')
        with patch.object(sgdb,'api_key',return_value='test-key'), \
                patch.object(sgdb,'_get',return_value=[{'id':1,'name':'Unrelated'},{'id':2,'name':'!!!'}]) as get:
            self.assertEqual(sgdb.lookup('★★★'),({},[]))
            get.assert_not_called()
            self.assertEqual(sgdb.lookup('ビートセイバー'),({},[]))
        with patch.object(sgdb,'api_key',return_value='test-key'), \
                patch.object(sgdb,'_get',side_effect=AttributeError("'list' object has no attribute 'get'")):
            self.assertEqual(sgdb.lookup('Game')[0],{})

    def test_wrong_title_and_failed_lookup_fall_back(self):
        with patch.object(sgdb,'api_key',return_value='test-key'),patch.object(sgdb,'_get',return_value=[{'id':1,'name':'Unrelated'}]):
            self.assertEqual(sgdb.lookup('Game'),({},[]))
        with patch.object(sgdb,'api_key',return_value='test-key'),patch.object(sgdb,'_get',side_effect=OSError('private-secret')):
            result=sgdb.lookup('Game')
        self.assertNotIn('private-secret',str(result));self.assertEqual(result[0],{})

    def test_settings_never_return_key(self):
        with tempfile.TemporaryDirectory() as root, patch.object(sgdb,'settings_path',return_value=Path(root)/'settings.json'), \
                patch.dict(sgdb.os.environ,{},clear=True):
            result=sgdb.save_settings({'steamgriddb_api_key':'secret-fixture'})
            self.assertTrue(result['steamgriddb_configured'])
            self.assertNotIn('secret-fixture',json.dumps(result))
            self.assertEqual(sgdb.api_key(),'secret-fixture')
            if sys.platform!='win32':self.assertEqual((Path(root)/'settings.json').stat().st_mode&0o777,0o600)
            sgdb.save_settings({'steamgriddb_api_key':''})
            self.assertFalse(sgdb.settings()['steamgriddb_configured'])


if __name__=='__main__':unittest.main()
