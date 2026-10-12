"""Own VR telemetry, optional catalogue and entitlement guards; no headset needed."""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import ctypes
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ui'))
import frame_status as status
import frame_steam as steam
import frame_utilities as utilities
import frame_vr as vr
import server


class Telemetry(unittest.TestCase):
    def test_background_client_does_not_request_steamvr_start(self):
        with mock.patch.object(status.C, 'CDLL') as load:
            with status.OpenVR():
                pass
        self.assertEqual(load.return_value.VR_InitInternal2.call_args.args[1], 3)
        load.return_value.VR_ShutdownInternal.assert_called_once()

    def test_linux_arm64_timing_layout(self):
        self.assertEqual(ctypes.sizeof(status.Timing), 192)
        self.assertEqual(status.Timing.pose.offset, 96)

    def test_cpu_excludes_guest_and_counts_iowait_idle(self):
        with mock.patch.object(status, 'read', return_value='cpu 20 0 10 50 20 0 0 0 7 1\n'):
            self.assertEqual(status.cpu_ticks(), (100, 70))
        self.assertEqual(status.cpu_percent((100, 70), (200, 110)), 60)
        self.assertIsNone(status.cpu_percent((100, 70), (100, 70)))
        self.assertIsNone(status.cpu_percent(None, (100, 70)))

    def test_compositor_and_app_fps_are_distinct(self):
        a, b = status.Timing(), status.Timing()
        a.index, a.time = 100, 10
        b.index, b.time, b.interval = 118, 10.2, 22.2222
        b.gpu, b.compositorCpu = 4, 1
        result = status.timing_values(a, b)
        self.assertEqual(result['compositorFps'], 90)
        self.assertEqual(result['appFps'], 45)
        self.assertEqual(result['frameMs'], 11.11)
        b.interval = 0
        self.assertIsNone(status.timing_values(a, b)['appFps'])
        b.index = a.index
        self.assertEqual(status.timing_values(a, b), {})
        b.index = 5  # compositor restart / wrap
        self.assertEqual(status.timing_values(a, b), {})

    def test_missing_openvr_keeps_sensor_values(self):
        with mock.patch.object(status, 'OpenVR', side_effect=OSError('missing')), \
             mock.patch.object(status, 'num', return_value=629), \
             mock.patch.object(status, 'cpu_ticks', side_effect=[(100, 50), (200, 100)]), \
             mock.patch.object(status.time, 'sleep'):
            data = status.performance()
        self.assertEqual(data['gpuMHz'], 629)
        self.assertEqual(data['cpuPercent'], 50)
        self.assertIsNone(data['compositorFps'])
        self.assertIsNone(data['appFps'])

    def test_import_has_no_device_side_effect(self):
        out = subprocess.run([sys.executable, '-c', 'import frame_status'],
                             cwd=Path(__file__).resolve().parents[1] / 'ui', capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout, '')


class UtilityGuards(unittest.TestCase):
    def test_all_paid_utilities_refused_before_steam_url(self):
        with mock.patch.object(steam, 'Page') as page, mock.patch.object(steam, 'steam_url') as url:
            page.return_value.eval.return_value = [{'id': i, 'owned': False} for i in steam.UTILITY_IDS]
            for appid in set(steam.UTILITY_IDS) - set(steam.FREE_UTILITIES):
                with self.assertRaisesRegex(steam.Fail, 'not owned'):
                    steam.install(appid)
            url.assert_not_called()

    def test_unknown_ownership_fails_closed(self):
        with mock.patch.object(steam, 'Page') as page, mock.patch.object(steam, 'steam_url') as url:
            page.return_value.eval.side_effect = steam.Fail('library unavailable')
            with self.assertRaises(steam.Fail):
                steam.install(908520)
            url.assert_not_called()

    def test_owned_software_uses_existing_install_flow(self):
        with mock.patch.object(steam, 'Page') as page, mock.patch.object(steam, 'steam_url') as url:
            page.return_value.eval.side_effect = [[{'id': 908520, 'owned': True}], {'name': 'fpsVR', 'installed': True}]
            self.assertEqual(steam.install(908520)['state'], 'installed')
            url.assert_not_called()

    def test_catalogue_separates_public_claims_ownership_and_reports(self):
        data = utilities.catalogue([{'id': 1173510, 'owned': True}], [
            {'package': 'com.android.app', 'rating': 'works', 'date': '2026-09-28'},
            {'package': 'steam:908520', 'rating': 'broken', 'date': '2026-09-28', 'notes': 'test report'}])
        rows = {r['id']: r for r in data['utilities']}
        self.assertEqual(rows[1173510]['status'], 'untested')
        self.assertTrue(rows[1173510]['canInstall'])
        self.assertFalse(rows[908520]['canInstall'])
        self.assertEqual(rows[908520]['status'], 'broken')
        self.assertTrue(rows[1494460]['canInstall'])
        self.assertFalse(rows[1009850]['free'])

    @unittest.skipUnless(shutil.which('node'), 'Node needed to evaluate Steam JavaScript')
    def test_ownership_js_includes_software_and_rejects_empty_library(self):
        script = 'let appStore = {allApps: [{appid:908520,app_type:2,local_per_client_data:{installed:true}}]};\n'
        script += 'console.log(JSON.stringify(' + steam.UTILITIES_JS + '));'
        out = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True)
        rows = {r['id']: r for r in json.loads(out.stdout)}
        self.assertTrue(rows[908520]['owned'])
        self.assertTrue(rows[908520]['installed'])
        self.assertFalse(rows[1173510]['owned'])
        out = subprocess.run(['node', '-e', 'let appStore={allApps:[]};' + steam.UTILITIES_JS], capture_output=True)
        self.assertNotEqual(out.returncode, 0)


class HUD(unittest.TestCase):
    def test_invalid_action_never_reaches_frame(self):
        with mock.patch.object(server, 'ssh') as ssh:
            for action in ('hud-start; reboot', 'adjust', 'recenter', 'restore'):
                with self.assertRaises(server.Failure) as err:
                    server.vr({'action': action})
                self.assertEqual(err.exception.status, 400)
            ssh.assert_not_called()

    def test_stop_wont_kill_reused_pid(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(vr, 'ROOT', Path(tmp)), \
             mock.patch.object(vr, 'process_identity', return_value='different'), mock.patch.object(vr.os, 'kill') as kill:
            (Path(tmp) / 'hud.json').write_text(json.dumps({'pid': 1234, 'start': 'old'}))
            vr.panel('hud-stop')
            kill.assert_not_called()
            self.assertFalse((Path(tmp) / 'hud.json').exists())

    def test_duplicate_start_does_not_spawn(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(vr, 'ROOT', Path(tmp)), \
             mock.patch.object(vr, 'process_identity', return_value='same'), mock.patch.object(vr.subprocess, 'Popen') as popen:
            (Path(tmp) / 'hud.json').write_text(json.dumps({'pid': 1234, 'start': 'same'}))
            self.assertIn('already open', vr.panel('hud-start')['message'])
            popen.assert_not_called()

    def test_start_failure_cleans_up_process(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(vr, 'ROOT', Path(tmp)), \
             mock.patch.object(vr.subprocess, 'Popen') as popen, \
             mock.patch.object(vr.subprocess, 'check_output', side_effect=OSError('no display')):
            popen.return_value.poll.return_value = None
            with self.assertRaises(OSError):
                vr.panel('hud-start')
            popen.return_value.terminate.assert_called_once()
            popen.return_value.wait.assert_called_once()
