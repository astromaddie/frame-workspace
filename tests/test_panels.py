"""Fake-Frame panel responses and the headset page's real HTTP guards."""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import http.client
import json
from pathlib import Path
import sys
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
        self.assertEqual(run.call_count, 1)  # only valid-looking 'missing' enumerates


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
