"""Fake-Frame panel responses and the headset page's real HTTP guards."""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'ui'))
import frame_panels as panels

# Shape verified with vrcmd on SteamVR 2.18.1 / BUILD_ID 20260925.6191901.
OVERLAYS = """---- OVERLAYS ----
'valve.steam.desktopgame.12' -- 'Alex's <notes>', 1920x1080 visible VROverlayType_Dashboard_Main
'valve.steam.desktopgame.12.thumb' -- 'Thumb', not_visible VROverlayType_Dashboard_Thumbnail
'valve.steam.desktopgame.12.layer1' -- 'Layer', visible VROverlayType_Subview
'system.pointer' -- 'Pointer', visible VROverlayType_Basic
'valve.steam.desktopgame.13' -- 'Other', not_visible VROverlayType_Dashboard_Main
'com.frame-workspace.native.1000.workspace' -- 'Frame Workspace', visible VROverlayType_Overlay
'com.frame-workspace.native.999.workspace' -- 'Other Native', visible VROverlayType_Overlay
"""


class Panels(unittest.TestCase):
    def test_enumerates_only_main_panels_including_hidden(self):
        rows = panels.parse_overlays(OVERLAYS)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['title'], "Alex's <notes>")
        self.assertTrue(rows[0]['visible'])
        self.assertFalse(rows[1]['visible'])

    def test_unavailable_runtime_is_not_an_empty_workspace(self):
        with self.assertRaises(panels.PanelError):
            panels.parse_overlays('SteamVR not running')
        self.assertEqual(panels.parse_overlays('---- OVERLAYS ----'), [])

    @patch.object(panels, 'run', return_value=OVERLAYS)
    def test_focus_revalidates_and_dispatches_without_shell(self, run):
        key = 'valve.steam.desktopgame.12'
        self.assertEqual(panels.focus(key)['requested'], key)
        self.assertEqual(run.call_args.args[0], [panels.VRCMD, '--showdashboard', key])

    @patch.object(panels, 'run', return_value=OVERLAYS)
    def test_closed_or_injected_panel_never_dispatches(self, run):
        for key in (None, 12, 'x;touch /tmp/bad', '../other', 'missing'):
            with self.assertRaises(panels.PanelError):
                panels.focus(key)
        self.assertEqual(run.call_args_list[0].args[0], [panels.VRCMD, '--overlays'])
        self.assertFalse(any(call.args[0][:2] == [panels.VRCMD, '--showdashboard']
                             for call in run.call_args_list))

    def test_lists_only_the_ready_exact_native_workspace_owner(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'XDG_RUNTIME_DIR': temp}):
            folder = Path(temp) / 'frame-workspace'
            folder.mkdir()
            key = panels.native_workspace_key()
            owner = {'state': 'streaming', 'ready': True, 'overlayKey': key,
                     'placement': {'width': 3.0, 'distance': 2.5},
                     'curvature': {'readable': True, 'value': 0.25, 'errorCode': 0}}
            state_file = folder / 'state.json'
            state_file.write_text(json.dumps(owner))
            commands = []

            def run(args):
                commands.append(args)
                if args == [panels.VRCMD, '--overlays']:
                    return OVERLAYS.replace('com.frame-workspace.native.1000.workspace', key)
                if args[-2] == '--property=ActiveState':
                    return 'active\n'
                if args[-2] == '--property=MainPID':
                    return '123\n'
                raise AssertionError(args)

            with patch.object(panels, 'run', side_effect=run):
                rows = panels.state()['panels']
            native = [row for row in rows if row.get('owner') == 'native-workspace']
            self.assertEqual(len(native), 1)
            self.assertEqual(native[0]['key'], key)
            self.assertEqual(native[0]['title'], 'Frame Workspace')
            self.assertTrue(native[0]['ready'])
            self.assertEqual(native[0]['placement'], owner['placement'])
            self.assertEqual(native[0]['curvature'], owner['curvature'])
            self.assertNotIn('com.frame-workspace.native.999.workspace', {row['key'] for row in rows})

            owner['overlayKey'] = 'com.frame-workspace.native.999.workspace'
            state_file.write_text(json.dumps(owner))
            with patch.object(panels, 'run', side_effect=run):
                rows = panels.state()['panels']
            self.assertEqual([row['key'] for row in rows],
                             ['valve.steam.desktopgame.12', 'valve.steam.desktopgame.13'])

    def test_native_workspace_focus_uses_owner_request_not_dashboard(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {'XDG_RUNTIME_DIR': temp}):
            folder = Path(temp) / 'frame-workspace'
            folder.mkdir()
            key = panels.native_workspace_key()
            (folder / 'state.json').write_text(json.dumps({
                'state': 'streaming', 'ready': True, 'overlayKey': key,
                'placement': {'width': 3.0, 'distance': 2.5},
                'curvature': {'readable': False, 'value': None, 'errorCode': -1}}))
            control = folder / 'control.json'
            control.write_text(json.dumps({'revision': 10**18, 'width': 3.0, 'distance': 2.5}))
            commands = []

            def run(args):
                commands.append(args)
                if args == [panels.VRCMD, '--overlays']:
                    return OVERLAYS.replace('com.frame-workspace.native.1000.workspace', key)
                if args[-2] == '--property=ActiveState':
                    return 'active\n'
                if args[-2] == '--property=MainPID':
                    return '123\n'
                raise AssertionError(args)

            with patch.object(panels, 'run', side_effect=run):
                result = panels.focus(key)
            self.assertEqual(result['requested'], key)
            self.assertIn('native Workspace owner', result['message'])
            self.assertEqual(json.loads(control.read_text()),
                             {'revision': 1, 'focus': True, 'recenter': True})
            with patch.object(panels, 'run', side_effect=run):
                panels.focus(key)
            self.assertEqual(json.loads(control.read_text()),
                             {'revision': 2, 'focus': True, 'recenter': True})
            self.assertFalse(any(args[:2] == [panels.VRCMD, '--showdashboard'] for args in commands))


class HeadsetHTTP(unittest.TestCase):
    def setUp(self):
        self.server = panels.HTTPServer(('127.0.0.1', 0), panels.Handler)
        self.server.key = 'test-key'
        self.server.closing = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def request(self, path, body=None, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        try:
            c.request('POST' if body is not None else 'GET', path, body=body, headers=headers or {})
            r = c.getresponse()
            return r.status, r.read().decode()
        finally:
            c.close()

    def test_page_is_public_but_contains_no_key_or_private_titles(self):
        status, page = self.request('/')
        self.assertEqual(status, 200)
        self.assertNotIn('test-key', page)
        self.assertIn('textContent=p.title', page)  # titles never become HTML
        self.assertEqual(self.request('/panels')[0], 403)

    @patch.object(panels, 'state', return_value={'panels': []})
    def test_auth_host_and_origin_checks(self, state):
        auth = {'X-Panel-Key': 'test-key'}
        self.assertEqual(self.request('/panels', headers=auth)[0], 200)
        for extra in ({'Host': 'evil.test'}, {'Origin': 'https://evil.test'}, {'X-Panel-Key': 'wrong'}):
            self.assertEqual(self.request('/panels', headers={**auth, **extra})[0], 403)
        self.assertEqual(state.call_count, 1)

    @patch.object(panels, 'focus', return_value={'requested': 'panel'})
    def test_post_validation_and_close(self, focus):
        auth = {'X-Panel-Key': 'test-key'}
        for body in ('[]', '{broken', '0', '"text"', 'x' * 1025):
            self.assertEqual(self.request('/focus', body, auth)[0], 400)
        self.assertEqual(focus.call_count, 0)
        self.assertEqual(self.request('/focus', '{"key":"panel"}', auth)[0], 200)
        self.assertEqual(focus.call_args.args, ('panel',))
        self.assertEqual(self.request('/close', '{}', auth)[0], 200)
        self.assertTrue(self.server.closing)

    @patch.object(panels, 'state', side_effect=panels.PanelError('offline'))
    def test_offline_is_an_error_not_a_successful_empty_list(self, state):
        status, body = self.request('/panels', headers={'X-Panel-Key': 'test-key'})
        self.assertEqual(status, 502)
        self.assertEqual(json.loads(body), {'error': 'offline'})
