"""Fake-Frame session clock/actions plus real helper serialization and sensor probes."""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import os
import shutil
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ui'))
import frame_comfort as comfort
import frame_status as status

OPTIONS = {'action': 'start', 'minutes': 3, 'breakMinutes': 1, 'stillMinutes': 1,
           'batteryAlert': True, 'heatAlert': True}


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.s = comfort.new_session(OPTIONS, 0, 'boot-one')
        self.warn, self.home = Mock(), Mock()

    def step(self, now, **sample):
        comfort.tick(self.s, now, sample, self.warn, self.home, read_clock=lambda: now)

    def test_warning_then_home_never_closes_a_game(self):
        self.step(119)
        self.warn.assert_not_called()
        self.step(120)
        self.warn.assert_called_once()
        self.step(179)
        self.home.assert_not_called()
        self.step(180)
        self.home.assert_called_once()
        self.assertFalse(self.s['active'])
        self.step(181)
        self.home.assert_called_once()

    def test_late_wakeup_always_gets_a_full_warning_minute(self):
        self.step(400)
        self.home.assert_not_called()
        self.step(459)
        self.home.assert_not_called()
        self.step(460)
        self.home.assert_called_once()

    def test_slow_warning_still_leaves_a_full_minute(self):
        comfort.tick(self.s, 120, {}, self.warn, self.home, read_clock=lambda: 140)
        self.assertEqual(self.s['warned'], 140)
        self.step(180)
        self.home.assert_not_called()
        self.step(199)
        self.home.assert_not_called()
        self.step(200)
        self.home.assert_called_once()

    def test_failed_warning_never_stops_session(self):
        self.warn.side_effect = RuntimeError('offline')
        with self.assertRaises(RuntimeError):
            self.step(200)
        self.assertIsNone(self.s['warned'])
        self.home.assert_not_called()
        self.warn.side_effect = None
        self.step(300)
        self.step(359)
        self.home.assert_not_called()
        self.step(360)
        self.home.assert_called_once()

    def test_failed_home_stays_active_and_retries(self):
        self.step(120)
        self.home.side_effect = RuntimeError('Steam offline')
        with self.assertRaises(RuntimeError):
            self.step(180)
        self.assertTrue(self.s['active'])
        self.home.side_effect = None
        self.step(185)
        self.assertFalse(self.s['active'])

    def test_cancel_prevents_all_actions(self):
        self.s['active'] = False
        self.step(999, battery={'percent': 1, 'status': 'Discharging'})
        self.warn.assert_not_called()
        self.home.assert_not_called()
        self.assertEqual(self.s['events'], [])

    def test_breaks_and_checkin_require_measured_activity(self):
        self.step(20, activity=1)
        self.step(40, activity=2)
        self.step(60, activity=1)
        self.assertEqual([e['kind'] for e in self.s['events']], ['break', 'still'])
        self.step(70, activity=1)
        self.assertEqual(len(self.s['events']), 2)
        self.step(75, activity=3)
        self.assertEqual(self.s['used'], 0)
        self.assertFalse(self.s['stillSent'])

    def test_unknown_activity_and_gaps_do_not_count_as_wear(self):
        self.step(25)
        self.assertEqual(self.s['used'], 0)
        self.assertEqual(self.s['unavailable'], ['battery', 'temperature', 'activity'])
        self.step(100, activity=1)
        self.assertEqual(self.s['used'], 30)

    def test_alerts_latch_and_rearm_without_battery_chatter(self):
        low = {'percent': 10, 'status': 'Discharging'}
        self.step(1, battery=low, thermal=['cpu'])
        self.step(2, battery=low, thermal=['cpu'])
        self.step(3)
        self.assertEqual(len(self.s['events']), 2)
        self.step(4, battery={'percent': 16, 'status': 'Discharging'}, thermal=[])
        self.step(5, battery=low, thermal=[])
        self.assertEqual(len(self.s['events']), 2)
        self.step(6, battery={'percent': 22, 'status': 'Discharging'}, thermal=[])
        self.step(7, battery=low, thermal=['cpu'])
        self.assertEqual([e['kind'] for e in self.s['events']], ['battery', 'heat', 'battery', 'heat'])

    def test_disabled_alerts_and_charging(self):
        self.s['options']['heatAlert'] = False
        self.step(1, battery={'percent': 2, 'status': 'Charging'}, thermal=['cpu'])
        self.assertEqual(self.s['events'], [])

    def test_invalid_options(self):
        for key, value in [('minutes', 0), ('minutes', 241), ('minutes', True), ('minutes', 2.5),
                           ('breakMinutes', -1), ('stillMinutes', '1'), ('heatAlert', 1)]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                comfort.validate({**OPTIONS, key: value})
        for value in (None, [], {'action': 'shutdown'}):
            with self.assertRaises(ValueError):
                comfort.validate(value)

    def test_restart_invalidates_session_and_stale_worker_is_explicit(self):
        with patch.object(comfort, 'boot', return_value='boot-one'), patch.object(comfort.time, 'time', return_value=999):
            current = comfort.current(self.s, 100)
            self.assertIn('not responding', current['error'])
            self.assertEqual(current['time'], 999)
        with patch.object(comfort, 'boot', return_value='boot-two'):
            result = comfort.current(self.s, 100)
            self.assertFalse(result['active'])
            self.assertIn('restarted', result['error'])

    @unittest.skipUnless(os.name == "posix", "on-headset state uses POSIX flock")
    def test_real_state_commands_share_one_session_and_cancel(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(comfort, 'ROOT', Path(tmp)), \
             patch.object(comfort, 'boot', return_value='boot-one'), \
             patch.object(comfort, 'clock', return_value=0), patch.object(comfort.subprocess, 'Popen') as spawn:
            started = comfort.command(OPTIONS)
            self.assertEqual(comfort.command({'action': 'status'})['id'], started['id'])
            with self.assertRaises(ValueError):
                comfort.command(OPTIONS)
            self.assertFalse(comfort.command({'action': 'cancel'})['active'])
            spawn.assert_called_once()
            self.assertEqual((Path(tmp) / 'session.json').stat().st_mode & 0o777, 0o600)

    @unittest.skipUnless(os.name == "posix", "on-headset state uses POSIX flock")
    def test_cancel_clears_stale_worker_error(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(comfort, 'ROOT', Path(tmp)), \
             patch.object(comfort, 'boot', return_value='boot-one'), \
             patch.object(comfort, 'clock', return_value=200):
            with comfort.locked():
                comfort.save(self.s)
            self.assertIn('not responding', comfort.command({'action': 'status'})['error'])
            cancelled = comfort.command({'action': 'cancel'})
            self.assertFalse(cancelled['active'])
            self.assertIsNone(cancelled['error'])
            self.assertIsNone(comfort.command({'action': 'status'})['error'])

    @unittest.skipUnless(os.name == "posix", "on-headset state uses POSIX flock")
    def test_failed_spawn_leaves_session_inactive_and_retryable(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(comfort, 'ROOT', Path(tmp)), \
             patch.object(comfort, 'boot', return_value='boot-one'), \
             patch.object(comfort, 'clock', return_value=0), patch.object(comfort.subprocess, 'Popen') as spawn:
            spawn.side_effect = OSError('process limit')
            with self.assertRaises(OSError):
                comfort.command(OPTIONS)
            failed = comfort.command({'action': 'status'})
            self.assertFalse(failed['active'])
            self.assertIn('Could not start', failed['error'])
            spawn.side_effect = None
            self.assertTrue(comfort.command(OPTIONS)['active'])

    @unittest.skipUnless(os.name == "posix", "on-headset state uses POSIX flock")
    def test_unreadable_state_is_preserved_and_can_be_replaced(self):
        for contents in (b'{broken', b'\xff', b'null', b'[]', b'42', b'"x"'):
            with self.subTest(contents=contents):
                with tempfile.TemporaryDirectory() as tmp, patch.object(comfort, 'ROOT', Path(tmp)), \
                     patch.object(comfort, 'boot', return_value='boot-one'), \
                     patch.object(comfort, 'clock', return_value=0), patch.object(comfort.subprocess, 'Popen'):
                    (Path(tmp) / 'session.json').write_bytes(contents)
                    failed = comfort.command({'action': 'status'})
                    self.assertFalse(failed['active'])
                    self.assertIn('unreadable', failed['error'])
                    backups = list(Path(tmp).glob('session-unreadable-*.json'))
                    self.assertEqual(len(backups), 1)
                    self.assertEqual(backups[0].read_bytes(), contents)
                    self.assertTrue(comfort.command(OPTIONS)['active'])

    def test_home_has_total_process_deadline_and_propagates_timeout(self):
        with patch.object(comfort.subprocess, 'run', side_effect=subprocess.TimeoutExpired('home', 15)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                comfort.home()
            self.assertEqual(run.call_args.kwargs['timeout'], 15)
            self.assertEqual(run.call_args.args[0][-1], '--home')

    def test_native_warning_reports_failures_and_quotes_as_one_argument(self):
        with patch.object(comfort.subprocess, 'run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, 'Notification succeeded', '')
            comfort.notify('Save "now"; $(nothing)')
            args = run.call_args.args[0]
            self.assertEqual(args, [comfort.VRCMD, '--notify', 'Frame Control: Save "now"; $(nothing)'])
            run.return_value.stdout = 'Notification failed with error 1'
            with self.assertRaises(RuntimeError):
                comfort.notify('test')

    @unittest.skipUnless(shutil.which("node"), "Node exercises the fake Steam JS context")
    def test_home_javascript_against_fake_steam_preserves_game(self):
        # Same JS runs in Steam CDP. This fake records navigation and refuses any
        # unexpected API call; it offers no shutdown or terminate-game primitive.
        js = '''let running = [123], path = '/routes/library/app/123', visible = false;
const location = {get pathname() {return path;}};
const SteamUIStore = {Navigate(p) {path = '/routes' + p;}};
const SteamClient = {OpenVR: {VROverlay: {
  async ShowDashboard(key) {if (key !== 'valve.steam.gamepadui.main') throw Error(key); visible = true;},
  async IsDashboardVisible() {return visible;}
}}};
'''
        js += comfort.HOME_JS + '.then(result => console.log(JSON.stringify({result, running, visible})));'
        r = subprocess.run(['node', '-e', js], capture_output=True, text=True, check=True)
        result = json.loads(r.stdout)
        self.assertEqual(result['running'], [123])
        self.assertTrue(result['visible'])
        self.assertEqual(result['result']['path'], '/routes/library/home')


class SensorTests(unittest.TestCase):
    def test_hot_trip_uses_its_own_zone_not_hottest_unrelated_chip(self):
        values = {'/z/a/temp': '90000', '/z/a/trip_point_0_type': 'hot', '/z/a/trip_point_0_temp': '110000',
                  '/z/b/temp': '45000', '/z/b/trip_point_0_type': 'hot', '/z/b/trip_point_0_temp': '44000', '/z/b/type': 'battery'}
        def glob(pattern):
            if pattern.endswith('thermal_zone*'):
                return ['/z/a', '/z/b']
            return [pattern.replace('*', '0')]
        with patch.object(status.glob, 'glob', side_effect=glob), patch.object(status, 'read', side_effect=values.get):
            self.assertEqual(status.thermal_alerts(), [{'zone': 'battery', 'tempC': 45, 'limitC': 44}])

    def test_missing_thermal_and_activity_are_unknown(self):
        with patch.object(status.glob, 'glob', return_value=[]):
            self.assertIsNone(status.thermal_alerts())
        with patch.object(status, 'run', return_value='unavailable'):
            self.assertIsNone(status.activity_level())
        for malformed in ('{}', '[null, 42, "bad"]'):
            with patch.object(status, 'run', return_value=malformed):
                self.assertIsNone(status.activity_level())
        with patch.object(status, 'run', return_value='[{"operation":"status","activity_level":3}]'):
            self.assertEqual(status.activity_level(), 3)


class DesktopBundleTests(unittest.TestCase):
    """server.comfort runs the copy cached on the headset; the sources go only when it's missing."""

    @unittest.skipUnless(os.name == "posix", "checks POSIX permissions")
    def test_sources_are_sent_once_per_version(self):
        import server
        with tempfile.TemporaryDirectory() as tmp:
            here, home = Path(tmp) / 'ui', Path(tmp) / 'frame'
            here.mkdir()
            home.mkdir()
            for name in server.COMFORT_FILES:
                (here / name).write_text('# ' + name + '\n')
            (here / 'frame_comfort.py').write_text('import json, sys\nprint(json.dumps({"active": False, "got": sys.argv[1]}))\n')
            sent = []

            def ssh(remote, *, stdin, timeout):  # the headset: the script run under its own home
                sent.append(stdin)
                return subprocess.run([sys.executable, '-'], input=stdin, capture_output=True, text=True, check=True,
                                      env={**os.environ, 'HOME': str(home)}).stdout

            with patch.object(server, 'HERE', here), patch.object(server, 'ssh', side_effect=ssh):
                first = server.comfort({'action': 'status'})
                self.assertEqual(json.loads(first['got']), {'action': 'status'})
                self.assertEqual(len(sent), 2)  # the check, then the sources
                self.assertNotIn('# frame_steam.py', sent[0])
                self.assertIn('# frame_steam.py', sent[1])
                self.assertEqual(server.comfort({'action': 'status'}), first)
                self.assertEqual(len(sent), 3)
                self.assertNotIn('# frame_steam.py', sent[2])
                cached, = (home / '.cache/frame-control/comfort').iterdir()
                self.assertEqual(sorted(p.name for p in cached.iterdir()), sorted(server.COMFORT_FILES))
                self.assertEqual(cached.stat().st_mode & 0o777, 0o700)
                self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in cached.iterdir()))
                # A copy cut short is completed; a changed helper is a new version.
                (cached / 'frame_status.py').unlink()
                server.comfort({'action': 'status'})
                self.assertEqual(len(sent), 5)
                self.assertTrue((cached / 'frame_status.py').exists())
                (here / 'frame_steam.py').write_text('# frame_steam.py, changed\n')
                server.comfort({'action': 'status'})
                self.assertEqual(len(sent), 7)
                self.assertEqual(len(list((home / '.cache/frame-control/comfort').iterdir())), 2)
