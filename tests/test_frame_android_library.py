"""Offline artwork, Steam API and launcher supervision regressions."""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import importlib.util
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'ui'))
import frame_android as android
import frame_artwork as art

spec = importlib.util.spec_from_file_location('steam_shortcuts', ROOT / 'frame/android/steam_shortcuts.py')
shortcuts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shortcuts)


@unittest.skipIf(os.name == 'nt', 'POSIX launcher')
class LauncherTests(unittest.TestCase):
    def exercise(self, terminate, sig=signal.SIGTERM, blocked=None, orphan=False):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            app = d / 'Applications/Android/org.test.app'
            app.mkdir(parents=True)
            (app / 'launch.sh').write_bytes((ROOT / 'frame/android/lepton-app.sh').read_bytes())
            (app / 'app.apk').touch()
            (app / 'instance.id').write_text('2800000001')
            (app / 'shortcut.id').write_text('3346865537')
            bin_dir = d / 'bin'
            bin_dir.mkdir()
            lepton = d / '.local/share/Steam/steamapps/common/Lepton/lepton'
            lepton.parent.mkdir(parents=True)
            def script(path, body):
                path.write_text('#!' + sys.executable + '\n' + body)
                path.chmod(0o755)
            script(lepton, 'import os,time\nfrom pathlib import Path\n'
                   'assert os.environ["SteamAppId"] == "2800000001"\n'
                   'assert os.environ["LEPTON_ENV_SteamAppId"] == "3346865537"\n'
                   'Path(os.environ["HOME"],"started").write_text(str(os.getpid()))\n'
                   'try:\n os.fstat(9); Path(os.environ["HOME"],"inherited-lock").touch()\nexcept OSError: pass\n'
                   + ('time.sleep(30)\n' if terminate else 'raise SystemExit(23)\n'))
            script(bin_dir / 'setsid', 'import os,sys\nos.setsid()\nos.execv(sys.argv[2],sys.argv[2:])\n')
            script(bin_dir / 'flock', 'import os\nraise SystemExit(1 if os.environ.get("TEST_LOCKED") else 0)\n')  # lock semantics belong to Linux; no flock on macOS
            script(bin_dir / 'podman', 'import os,sys\nfrom pathlib import Path\n'
                   'p=Path(os.environ["HOME"],"podman-calls")\n'
                   'with p.open("a") as f: f.write(" ".join(sys.argv[1:])+"\\n")\n'
                   'if sys.argv[1:2]==["inspect"] and os.environ.get("TEST_RUNNING"): print("true")\n')
            env = {**os.environ, 'HOME': str(d), 'PATH': str(bin_dir) + os.pathsep + os.environ['PATH']}
            if blocked:
                env['TEST_' + blocked] = '1'
            if orphan:
                env['TEST_RUNNING'] = '1'
            saved = d / '.local/share/Steam/steamapps/compatdata/2800000001/internal/save'
            saved.parent.mkdir(parents=True)
            saved.write_text('saved game')
            proc = subprocess.Popen(['bash', str(app / 'launch.sh')], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                if blocked:
                    proc.communicate(timeout=5)
                    self.assertEqual(proc.returncode, 1)
                    self.assertFalse((d / 'started').exists())
                    calls = (d / 'podman-calls').read_text() if (d / 'podman-calls').exists() else ''
                    self.assertNotIn('stop ', calls)
                    return
                deadline = time.monotonic() + 5
                while not (d / 'started').exists() and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertTrue((d / 'started').exists(), 'launcher did not start Lepton')
                self.assertFalse((d / 'inherited-lock').exists(), 'Lepton inherited the launch lock')
                if terminate:
                    self.assertIsNone(proc.poll(), 'Steam-tracked wrapper exited during the session')
                    proc.send_signal(sig)
                _, err = proc.communicate(timeout=5)
                calls = (d / 'podman-calls').read_text() if (d / 'podman-calls').exists() else ''
                self.assertIn('stop -t 5 lepton-steamlaunch-2800000001', calls, err.decode())
                self.assertEqual(calls.count('stop -t 5'), 2 if orphan else 1)
                self.assertEqual(proc.returncode, 128 + sig if terminate else 23)
                self.assertEqual(saved.read_text(), 'saved game')
                self.assertTrue((app / 'app.apk').exists())
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
                if (d / 'started').exists():
                    try:
                        os.kill(int((d / 'started').read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_steam_stop_cleans_container(self):
        self.exercise(True)

    def test_hangup_and_interrupt_cleanup(self):
        # macOS's stock bash 3.2 doesn't run a SIGINT trap while blocked in `wait`;
        # the Frame's bash (5.x) does, and that's where the launcher runs.
        major = subprocess.run(['bash', '-c', 'echo ${BASH_VERSINFO[0]}'], capture_output=True, text=True).stdout.strip()
        sigs = (signal.SIGHUP, signal.SIGINT) if major.isdigit() and int(major) >= 4 else (signal.SIGHUP,)
        for sig in sigs:
            with self.subTest(sig=sig):
                self.exercise(True, sig)

    def test_duplicate_launch_leaves_existing_session_alone(self):
        self.exercise(False, blocked='LOCKED')

    def test_orphaned_container_is_stopped_and_play_proceeds(self):
        # Container running but the lock free: its launcher was SIGKILLed.
        self.exercise(False, orphan=True)

    def test_normal_exit_cleans_container_and_keeps_exit_code(self):
        self.exercise(False)


FIXTURES = ROOT / 'tests/fixtures/library'


class ArtworkTests(unittest.TestCase):
    def test_source_inputs_and_url(self):
        from apk_sources import _images
        data = (FIXTURES / 'icon.png').read_bytes()
        with patch('frame_steamgriddb.lookup', return_value=({}, [])), \
                patch.object(_images, 'fetch', return_value=(data, 'image/png')) as fetch:
            images, warnings = art.prepare('Game', artwork={'banner': data, 'icon': 'https://example.org/icon.png'})
        self.assertEqual(images['banner'], ('png', data))
        self.assertEqual(images['icon'], ('png', data))
        self.assertEqual(warnings, [])
        self.assertEqual(fetch.call_args.args[0], 'https://example.org/icon.png')
        self.assertIsNotNone(fetch.call_args.kwargs['deadline'])

    def test_provider_precedence_and_bad_source_fallback(self):
        data = (FIXTURES / 'icon.png').read_bytes()
        jpg = (FIXTURES / 'icon.jpg').read_bytes()
        with patch('frame_steamgriddb.lookup', return_value=({'hero': jpg}, [])):
            images, warnings = art.prepare('Game', data, {'hero': data, 'wide': b'bad', 'screenshots': [b'bad', data]})
        self.assertEqual(images['hero'], ('jpg', jpg))
        self.assertEqual(images['icon'], ('png', data))
        self.assertEqual(images['screenshot'], ('png', data))
        self.assertNotIn('wide', images)
        self.assertEqual(warnings, ['Source wide unavailable; using fallback art'])  # one per slot, not per candidate

    def test_any_source_failure_falls_back_to_generated_art(self):
        import http.client
        from apk_sources import _images
        data = (FIXTURES / 'icon.png').read_bytes()
        for error in (http.client.RemoteDisconnected('gone'), http.client.IncompleteRead(b''), AttributeError('x')):
            with self.subTest(error=type(error).__name__), \
                    patch.object(_images, 'fetch', side_effect=error), \
                    patch('frame_steamgriddb.lookup', side_effect=error):
                images, warnings = art.prepare('Game', data, {'banner': 'https://example.org/b.png'})
            self.assertEqual(set(images), {'icon'})
            self.assertEqual(len(warnings), 2)

    def test_url_fetch_refuses_private_hosts_and_honours_deadline(self):
        from apk_sources import _images, SourceError
        local = [(2, 1, 6, '', ('127.0.0.1', 443))]
        with patch.object(_images.socket, 'getaddrinfo', return_value=local), \
                self.assertRaisesRegex(SourceError, 'Private'):
            art.fetch('https://example.org/icon.png')
        public = [(2, 1, 6, '', ('93.184.216.34', 443))]
        with patch.object(_images.socket, 'getaddrinfo', return_value=public), \
                patch.object(_images.socket, 'create_connection') as connect, \
                self.assertRaisesRegex(SourceError, 'too long'):
            art.fetch('https://example.org/icon.png', deadline=time.monotonic() - 1)
        connect.assert_not_called()
        with self.assertRaises(SourceError):
            art.fetch('file:///etc/passwd')

    def trickle(self, head, seconds):
        # A server that answers one byte every 20 ms, over a socketpair standing in for the network.
        import socket
        import threading
        from apk_sources import _images
        client, server = socket.socketpair()
        def serve():
            try:
                server.recv(65536)
                for byte in head + b'x' * 1000:
                    server.sendall(bytes([byte]))
                    time.sleep(0.02)
            except OSError:
                pass
            finally:
                server.close()
        threading.Thread(target=serve, daemon=True).start()
        public = [(2, 1, 6, '', ('93.184.216.34', 80))]
        with patch.object(_images.socket, 'getaddrinfo', return_value=public), \
                patch.object(_images.socket, 'create_connection', return_value=client):
            start = time.monotonic()
            with self.assertRaisesRegex(_images.SourceError, 'too long'):
                _images.get('http://example.org/a.png', deadline=start + seconds)
            return time.monotonic() - start

    def test_deadline_bounds_trickling_headers_and_body(self):
        self.assertLess(self.trickle(b'HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n', 0.15), 0.4)
        self.assertLess(self.trickle(b'HTTP/1.1 200 OK\r\n', 0.15), 0.4)  # headers never finish

    def test_deadline_covers_a_stalled_tls_handshake(self):
        import socket
        from apk_sources import _images
        client, server = socket.socketpair()
        public = [(2, 1, 6, '', ('93.184.216.34', 443))]
        try:
            with patch.object(_images.socket, 'getaddrinfo', return_value=public), \
                    patch.object(_images.socket, 'create_connection', return_value=client):
                start = time.monotonic()
                with self.assertRaisesRegex(_images.SourceError, 'too long'):
                    _images.get('https://example.org/a.png', deadline=start + 0.25)  # server never answers
                self.assertLess(time.monotonic() - start, 0.45)
        finally:
            server.close()

    def test_timed_out_lookups_are_capped(self):
        import threading
        from apk_sources import _images
        gate = threading.Event()
        try:
            with patch.object(_images.socket, 'getaddrinfo', side_effect=lambda *a, **k: gate.wait(5) and []):
                errors = []
                for _ in range(6):
                    try:
                        _images.get('https://example.org/a.png', deadline=time.monotonic() + 0.05)
                    except _images.SourceError as e:
                        errors.append(str(e))
            self.assertEqual(sum('too long' in e for e in errors), 4)
            self.assertEqual(sum('Too many' in e for e in errors), 2)
        finally:
            gate.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _images._resolvers.acquire(blocking=False):
            time.sleep(0.01)
        _images._resolvers.release()  # the stuck lookups finished and gave their slots back

    def test_resolver_slot_released_when_thread_cannot_start(self):
        from apk_sources import _images
        with patch.object(_images.threading.Thread, 'start', side_effect=RuntimeError("can't start new thread")):
            for _ in range(6):
                with self.assertRaises(RuntimeError):
                    _images.get('https://example.org/a.png', deadline=time.monotonic() + 1)
        for _ in range(4):  # every slot came back
            self.assertTrue(_images._resolvers.acquire(blocking=False))
        for _ in range(4):
            _images._resolvers.release()

    def test_deadline_covers_name_resolution(self):
        import threading
        from apk_sources import _images
        gate = threading.Event()
        with patch.object(_images.socket, 'getaddrinfo', side_effect=lambda *a, **k: gate.wait(5) and []):
            start = time.monotonic()
            with self.assertRaisesRegex(_images.SourceError, 'too long'):
                _images.get('https://example.org/a.png', deadline=start + 0.1)
            self.assertLess(time.monotonic() - start, 0.4)
        gate.set()
        with self.assertRaisesRegex(_images.SourceError, 'too long'):
            _images.get('https://example.org/a.png', deadline=time.monotonic() - 1)

    def test_steamgriddb_uses_the_bounded_fetch_without_redirects(self):
        import frame_steamgriddb as sgdb
        from apk_sources import _images
        deadline = time.monotonic() + 5
        with patch.object(_images, 'get', return_value=b'{"success": true, "data": [1]}') as get:
            self.assertEqual(sgdb._get('/search/x', 'secret', deadline), [1])
        self.assertEqual(get.call_args.kwargs['redirects'], 0)
        self.assertEqual(get.call_args.args[1]['Authorization'], 'Bearer secret')
        # The caller's deadline is passed on, never extended. (Comparing against a fresh
        # time.monotonic() flaked on Windows, whose clock ticks coarsely.)
        self.assertLessEqual(get.call_args.kwargs['deadline'], deadline)

    def test_supplied_jpeg(self):
        data = (FIXTURES / 'icon.jpg').read_bytes()
        self.assertEqual(art.image_type(data), 'jpg')
        self.assertEqual(art.image_type(data + b'\0' * 64), 'jpg')  # trailing padding after EOI
        with self.assertRaises(ValueError):
            art.image_type(data[:30])

    FRAME = b'\x21\xf9\x04\x01\x00\x00\x00\x00' + b'\x2c' + struct.pack('<HHHHB', 0, 0, 1, 1, 0) + b'\x02\x02\x44\x01\x00'

    def gif(self, frames=1, screen=(1, 1), frame=None):
        head = b'GIF89a' + struct.pack('<HHBBB', *screen, 0x80, 0, 0) + b'\xff\xff\xff\x00\x00\x00'
        return head + (frame or self.FRAME) * frames + b'\x3b'

    def test_gif_first_frame_is_bounded_and_re_emitted(self):
        one = self.gif()
        self.assertEqual(art.image_type(one), 'gif')
        self.assertEqual(art.gif_frame(one), one)  # already minimal: unchanged
        self.assertEqual(art.fetch(self.gif(frames=3)), ('gif', one))  # animation: first frame only
        start = time.monotonic()
        self.assertEqual(art.gif_frame(self.gif(frames=500000)), one)  # ~10 MB of frames, never parsed
        self.assertLess(time.monotonic() - start, 1)
        big = b'\x2c' + struct.pack('<HHHHB', 0, 0, 8192, 8192, 0) + b'\x02\x02\x44\x01\x00'
        for bomb in (self.gif(frame=big), self.gif(screen=(8192, 8192), frame=big), self.gif(screen=(5000, 10)),
                     self.gif(frame=b'\x2c' + struct.pack('<HHHHB', 1, 0, 1, 1, 0) + b'\x02\x02\x44\x01\x00'),
                     b'GIF89a' + struct.pack('<HHBBB', 1, 1, 0, 0, 0) + self.FRAME + b'\x3b',  # no colour table
                     self.gif(frame=b'\x2c' + struct.pack('<HHHHB', 0, 0, 1, 1, 0) + b'\x0c\x02\x44\x01\x00'),
                     self.gif(frame=b'\x99')):
            with self.subTest(bomb=bomb[:40]), self.assertRaises(ValueError):
                art.image_type(bomb)
        for cut in range(len(one) - 1):  # every truncation before the image's last block
            with self.subTest(cut=cut), self.assertRaises(ValueError):
                art.gif_frame(one[:cut])

    def test_gif_control_block_and_empty_image(self):
        image = self.FRAME[8:]  # the image without its graphic control block
        head = b'GIF89a' + struct.pack('<HHBBB', 1, 1, 0x80, 0, 0) + b'\xff\xff\xff\x00\x00\x00'
        good = b'\x21\xf9\x04\x00\x00\x00\x00\x00'
        self.assertEqual(art.gif_frame(head + good + image + b'\x3b'), head + good + image + b'\x3b')
        bad = b'\x21\xf9\x02\x00\x00\x00'  # wrong payload size: dropped, not passed on
        self.assertEqual(art.gif_frame(head + bad + image + b'\x3b'), head + image + b'\x3b')
        empty = b'\x2c' + struct.pack('<HHHHB', 0, 0, 1, 1, 0) + b'\x02\x00'
        with self.assertRaises(ValueError):
            art.gif_frame(head + empty + b'\x3b')

    def test_png_variants_left_to_chromium_and_limits(self):
        def png(w, h, depth, color, interlace):
            return art.PNG + art.chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, depth, color, 0, 0, interlace)) + \
                art.chunk(b'IEND', b'')
        self.assertEqual(art.image_type(png(3840, 1240, 16, 6, 0)), 'png')
        self.assertEqual(art.image_type(png(3840, 2160, 8, 2, 1)), 'png')
        for bad, message in ((png(10000, 10, 8, 6, 0), 'dimensions'), (png(5000, 5000, 8, 6, 0), 'dimensions'),
                             (png(10, 10, 3, 6, 0), 'encoding'), (png(10, 10, 8, 5, 0), 'encoding')):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                art.image_type(bad)
        broken = bytearray(png(10, 10, 8, 6, 0))
        broken[20] ^= 1
        with self.assertRaisesRegex(ValueError, 'checksum'):
            art.image_type(bytes(broken))
        with self.assertRaises(ValueError):
            art.image_type(art.PNG + b'junk')

    def test_bad_artwork_arguments(self):
        for value in ({'bad': b'bad'}, ['hero']):
            with self.subTest(value=value), self.assertRaises(ValueError):
                art.prepare('Game', artwork=value)

    def test_godot_project_icon(self):
        import io
        import zipfile
        data = (FIXTURES / 'icon.png').read_bytes()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('assets/icon.png', data)
        with zipfile.ZipFile(buffer) as archive:
            self.assertEqual(android.frame_apk._icon_png(archive, set(archive.namelist()), []), data)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.responses = json.loads((FIXTURES / 'steam-responses.json').read_text())
        self.info = {'package': 'org.test.vr', 'label': 'VR', 'version': '1', 'icon_png': None,
                     'vr': True, 'launchable': True, 'repairable': False, 'abis': [], 'min_sdk': 24}
        self.images = {slot: ('png', b'PNG ' + slot.encode()) for slot in art.SLOTS}
        self.existing = {'package': 'org.test.vr', 'instance': 2800000001,
                         'shortcut': 3346865537, 'label': 'Old name'}

    def install(self, existing, tool=None):
        def shortcut(*args, **kwargs):
            if args[0] == 'add':
                return '3346865537'
            if args[0] == 'render':
                return json.dumps({'paths': {slot: '/home/steamos/Applications/Android/org.test.vr/artwork/' + slot + '.png' for slot in art.SLOTS}})
            if args[0] == 'list':
                return json.dumps(self.responses['shortcuts'])
            return json.dumps(self.responses['configure'])
        with patch.object(android.frame_artwork, 'prepare', return_value=(self.images, [])), \
                patch.object(android, 'read_meta', return_value=existing), \
                patch.object(android, '_copy') as copy, \
                patch.object(android, 'ssh', return_value=self.responses['home']) as ssh, \
                patch.object(android, 'shortcut_tool', side_effect=tool or shortcut) as api, \
                patch.object(android, '_write_meta') as meta:
            result = android._install('game.apk', self.info, self.info['package'], False, 'New name', 'test')
        return result, ssh, api, meta

    def test_existing_shortcut_refreshes_name_vr_and_every_slot(self):
        result, ssh, api, meta = self.install(self.existing)
        calls = [c.args for c in api.call_args_list]
        self.assertNotIn('add', [c[0] for c in calls])
        configure = next(c for c in calls if c[0] == 'configure')
        self.assertEqual(configure[1:3], ('3346865537', 'New name'))
        self.assertEqual(configure[6], '1')
        self.assertEqual(set(json.loads(configure[7])), set(art.SLOTS))
        self.assertTrue(configure[5].endswith('/org.test.vr/artwork/icon.png'))
        self.assertEqual(result['shortcut'], self.existing['shortcut'])
        self.assertEqual(result['label'], 'New name')
        self.assertEqual(result['library_warnings'], [])
        self.assertEqual(len([c for c in ssh.call_args_list if isinstance(c.kwargs.get('input'), bytes)]), 5)
        meta.assert_called_once()

    def test_first_install_adds_shortcut(self):
        _, _, api, _ = self.install(None)
        self.assertEqual([c.args[0] for c in api.call_args_list], ['add', 'render', 'configure'])

    def test_artwork_forwarded_through_patch(self):
        artwork = {'hero': b'provided'}
        with patch.object(android, 'apk_info', return_value={**self.info, 'repairable': True}), \
                patch.object(android, 'xr_compat_files', return_value={}), \
                patch.object(android, 'patch', return_value={'patched': ['launcher']}), \
                patch.object(android, '_install', return_value={}) as install:
            android.install('x.apk', artwork=artwork)
        self.assertIs(install.call_args.args[-1], artwork)

    def test_failed_new_install_removes_shortcut(self):
        def tool(*args, **kwargs):
            if args[0] == 'add':
                return '3346865537'
            if args[0] == 'render':
                return json.dumps({'paths': {slot: '/tmp/' + slot + '.png' for slot in art.SLOTS}})
            if args[0] == 'configure':
                raise android.FrameError('write failed')
            return '{}'
        with patch.object(android.frame_artwork, 'prepare', return_value=(self.images, [])), \
                patch.object(android, 'read_meta', return_value=None), \
                patch.object(android, '_copy'), patch.object(android, 'ssh', return_value='/home/steamos') as ssh, \
                patch.object(android, 'shortcut_tool', side_effect=tool) as api:
            with self.assertRaisesRegex(android.FrameError, 'write failed'):
                android._install('x.apk', self.info, 'org.test.vr', False, None, None)
        self.assertIn(('remove', '3346865537'), [c.args for c in api.call_args_list])
        self.assertTrue(any(c.args[0] == 'rm -rf Applications/Android/org.test.vr' for c in ssh.call_args_list))

    def test_failed_update_removes_partial_apk_and_keeps_the_app(self):
        with patch.object(android.frame_artwork, 'prepare', return_value=(self.images, [])), \
                patch.object(android, 'read_meta', return_value=dict(self.existing)), \
                patch.object(android, '_copy', side_effect=android.FrameError('copying x.apk to the Frame timed out')), \
                patch.object(android, 'ssh', return_value='') as ssh, \
                patch.object(android, 'shortcut_tool') as api:
            with self.assertRaisesRegex(android.FrameError, 'timed out'):
                android._install('x.apk', self.info, 'org.test.vr', False, None, None)
        commands = [c.args[0] for c in ssh.call_args_list]
        self.assertIn('rm -f Applications/Android/org.test.vr/app.apk.part', commands)
        self.assertFalse(any(c.startswith('rm -rf') for c in commands))
        api.assert_not_called()

    def test_remove_keeps_data_when_requested_and_survives_steam_failure(self):
        with patch.object(android, '_meta_or_fail', side_effect=lambda pkg: dict(self.existing)), \
                patch.object(android, 'stop'), \
                patch.object(android, 'shortcut_tool', return_value='{"warnings": []}') as api, \
                patch.object(android, 'ssh') as ssh:
            android.remove('org.test.vr', keep_data=True)
            api.assert_called_once_with('remove', '3346865537')
            ssh.assert_called_once_with('rm -rf Applications/Android/org.test.vr')
            api.side_effect = android.FrameError('SharedJSContext not found: is the Steam client running?')
            ssh.reset_mock()
            result = android.remove('org.test.vr')
            ssh.assert_called_once_with('rm -rf Applications/Android/org.test.vr '
                                        '.local/share/Steam/steamapps/compatdata/2800000001 '
                                        '.local/share/Steam/steamapps/shadercache/2800000001')
            self.assertIn('Steam client running', result['library_warnings'][0])

    def test_remove_waits_for_refresh_and_is_never_undone(self):
        import threading
        state = {'meta': dict(self.existing, flatscreen=False), 'shortcuts': {3346865537}}
        in_refresh, release, added = threading.Event(), threading.Event(), []
        def meta(pkg):
            if not state['meta']:
                raise android.FrameError(pkg + ' is not installed')
            return dict(state['meta'])
        def ssh(cmd, input=None, **kw):
            if cmd.startswith('rm -rf Applications/Android/org.test.vr'):
                state['meta'] = None
            return json.dumps({'icon_png': ''}) if cmd == 'python3 -' else '/home/steamos'
        def tool(*args, **kw):
            if args[0] == 'list': return json.dumps([{'appid': a} for a in state['shortcuts']])
            if args[0] == 'remove': state['shortcuts'].discard(int(args[1])); return '{"warnings": []}'
            if args[0] == 'add': added.append(args); return '99'
            if args[0] == 'render': return json.dumps({'paths': {s: '/tmp/' + s + '.png' for s in art.SLOTS}})
            return '{"warnings": []}'
        def prepare(*args, **kw):
            in_refresh.set(); release.wait(5)
            return self.images, []
        with patch.object(android, '_meta_or_fail', side_effect=meta), patch.object(android, 'ssh', side_effect=ssh), \
                patch.object(android, 'shortcut_tool', side_effect=tool), patch.object(android, 'stop'), \
                patch.object(android, '_write_meta', side_effect=lambda d, m: state.__setitem__('meta', m)), \
                patch.object(android.frame_artwork, 'prepare', side_effect=prepare):
            refresh = threading.Thread(target=android.refresh_art, args=('org.test.vr',), kwargs={'fill_only': True})
            refresh.start()
            self.assertTrue(in_refresh.wait(5))
            remove = threading.Thread(target=android.remove, args=('org.test.vr',))
            remove.start()
            remove.join(0.3)
            self.assertTrue(remove.is_alive(), 'remove ran while a refresh was writing')
            release.set(); refresh.join(5); remove.join(5)
            self.assertIsNone(state['meta']); self.assertEqual(state['shortcuts'], set())
            with self.assertRaisesRegex(android.FrameError, 'not installed'):
                android.refresh_art('org.test.vr', fill_only=True)  # a queued backfill after removal
        self.assertEqual(added, [])

    def test_stop_requests_steam_and_has_container_fallback(self):
        with patch.object(android, '_meta_or_fail', return_value=self.existing), \
                patch.object(android, 'shortcut_tool', side_effect=android.FrameError('offline')) as api, \
                patch.object(android, 'ssh') as ssh:
            android.stop('org.test.vr')
        api.assert_called_once_with('stop', '3346865537')
        self.assertIn('podman stop -t 5 lepton-steamlaunch-2800000001', ssh.call_args.args[0])


class CopyAndSshTests(unittest.TestCase):
    """The commands behind an install fail as FrameErrors with readable words, and a large APK
    gets the time it needs."""

    def fake(self, stderr, code=1, stdout=b''):
        """frame_host.run_ssh running a stand-in that writes these bytes, with the caller's options."""
        script = (f'import sys; sys.stdout.buffer.write({stdout!r}); sys.stderr.buffer.write({stderr!r}); '
                  f'sys.exit({code})')
        return lambda argv, **kwargs: subprocess.run([sys.executable, '-c', script], **kwargs)

    def test_large_apk_copy_timeout_grows_with_its_size(self):
        seen = {}
        with tempfile.NamedTemporaryFile(suffix='.apk') as f, \
                patch.object(android.frame_host, 'run_ssh', side_effect=lambda argv, **kw: seen.update(kw)), \
                patch.object(android.os.path, 'getsize', return_value=2 * 1024 ** 3):
            android._copy(f.name, 'Applications/Android/x/app.apk.part')
        # 2 GB at the slowest rate allowed: 4096 s, not the old fixed 600 (300 MB at 4 Mbit/s).
        self.assertGreaterEqual(seen['timeout'], 2 * 1024 ** 3 / (512 * 1024))
        with tempfile.NamedTemporaryFile(suffix='.apk') as f, \
                patch.object(android.frame_host, 'run_ssh', side_effect=lambda argv, **kw: seen.update(kw)):
            android._copy(f.name, 'x/launch.sh', timeout=120)
        self.assertEqual(seen['timeout'], 120)  # a small file keeps the least time given

    def test_copy_failure_in_another_encoding_is_a_frame_error(self):
        # A message from a Windows code page, or a file name, isn't UTF-8.
        with tempfile.NamedTemporaryFile(suffix='.apk') as f, \
                patch.object(android.shutil, 'which', return_value=None), \
                patch.object(android.frame_host, 'run_ssh', side_effect=self.fake(b'scp: Verbindung \x81ber\xff\n')):
            with self.assertRaisesRegex(android.FrameError, r'\Acopying .* to the Frame failed: scp: Verbindung'):
                android._copy(f.name, 'x/app.apk.part')

    def test_ssh_failure_messages_are_text(self):
        with patch.object(android.frame_host, 'run_ssh', side_effect=self.fake(b'mv: \xff cannot move\n')):
            with self.assertRaisesRegex(android.FrameError, r'\Amv: .* cannot move\Z'):
                android.ssh('mv a b')
        with patch.object(android.frame_host, 'run_ssh', side_effect=self.fake(b'cat: write error: No space left on device\n')):
            with self.assertRaises(android.FrameError) as caught:
                android.ssh('cat > x', input=b'\x89PNG')  # bytes in, bytes out
        self.assertEqual(str(caught.exception), 'cat: write error: No space left on device')
        with patch.object(android.frame_host, 'run_ssh', side_effect=self.fake(b'', code=0, stdout=b'ok \xff\n')):
            self.assertEqual(android.ssh('echo ok').strip(), 'ok �')


class SteamAPITests(unittest.TestCase):
    def test_artwork_api_enums_and_safe_serialization(self):
        with patch.object(shortcuts, 'evaluate', return_value={'warnings': []}) as evaluate:
            shortcuts.configure(42, 'A "name"\n', '/path', '/start', '/icon', True,
                                {slot: str(FIXTURES / 'icon.png') for slot in art.SLOTS})
        js = evaluate.call_args.args[0]
        self.assertIn('SetShortcutIsVR(id, true)', js)
        self.assertIn('SetShortcutName(id, "A \\"name\\"\\n")', js)
        self.assertIn('SetCustomArtworkForApp(id, data, ext, type)', js)
        self.assertLess(js.index('ClearCustomArtworkForApp(id, type)'), js.index('SetCustomArtworkForApp(id, data, ext, type)'))
        self.assertEqual(shortcuts.ASSETS, {'grid': 0, 'hero': 1, 'logo': 2, 'wide': 3, 'icon': 4})
        self.assertIn('NewUnsavedCollection(name, undefined, [app])', js)

    def test_remove_clears_every_slot_before_shortcut(self):
        with patch.object(shortcuts, 'evaluate', return_value={}) as evaluate:
            shortcuts.remove(42)
        js = evaluate.call_args.args[0]
        self.assertIn('[0, 1, 2, 3]', js)
        self.assertLess(js.index('ClearCustomArtworkForApp(id, type)'), js.index('RemoveShortcut(id)'))
        self.assertIn('const wanted = []', js)
        self.assertNotIn('throw', js)  # tidy-up failures are warnings; RemoveShortcut always runs

    def test_devkit_configure_leaves_vr_flag_and_uses_sideloaded(self):
        slots = {slot: str(FIXTURES / 'icon.png') for slot in art.SLOTS}
        with patch.object(shortcuts, 'evaluate', return_value={'warnings': []}) as evaluate:
            shortcuts.configure(42, 'Game', '', '', '/icon', None, slots, {'category': 'Sideloaded'})
        js = evaluate.call_args.args[0]
        self.assertIn('if (null !== null && !fill)', js)
        self.assertIn('const wanted = ["Sideloaded"]', js)
        with patch.object(sys, 'argv', ['steam_shortcuts.py', 'configure', '42', 'Game', '', '', '/icon', '',
                                        json.dumps(slots), '{}']), \
                patch.object(shortcuts, 'configure', return_value={}) as configure, patch('builtins.print'):
            shortcuts.main()
        self.assertIsNone(configure.call_args.args[5])

    def test_render_writes_jpeg_and_retries_oversized_photo_with_generated_art(self):
        import base64
        png = base64.b64encode((FIXTURES / 'icon.png').read_bytes()).decode()
        jpg = base64.b64encode((FIXTURES / 'icon.jpg').read_bytes()).decode()
        huge = base64.b64encode(b'\xff\xd8\xff' + b'\0' * (12 * 1024 * 1024)).decode()
        calls = []
        def evaluate(js, timeout=20):
            calls.append((json.loads(js[js.rindex('renderLibraryArtwork(') + 21:-1]), timeout))
            hero = ['jpg', huge] if len(calls) == 1 else ['png', png]
            return {'images': {'grid': ['jpg', jpg], 'wide': ['jpg', jpg], 'hero': hero,
                               'logo': ['png', png], 'icon': ['png', png]}, 'warnings': []}
        with tempfile.TemporaryDirectory() as tmp:
            for name in ('icon.png', 'hero.jpg'):
                Path(tmp, 'source-' + name).write_bytes((FIXTURES / ('icon.jpg' if name.endswith('jpg') else 'icon.png')).read_bytes())
            Path(tmp, 'hero.png').write_bytes(b'stale')
            plan = Path(tmp, 'input.json')
            plan.write_text(json.dumps({'label': 'Game', 'images': {'icon': str(Path(tmp, 'source-icon.png')),
                                                                     'hero': str(Path(tmp, 'source-hero.jpg'))}}))
            with patch.object(shortcuts, 'evaluate', side_effect=evaluate):
                result = shortcuts.render(str(plan))
            self.assertEqual(set(calls[0][0]['images']), {'icon', 'hero'})
            self.assertEqual(set(calls[1][0]['images']), {'icon'})
            self.assertEqual([c[1] for c in calls], [75, 75])
            self.assertTrue(result['paths']['grid'].endswith('grid.jpg'))
            self.assertTrue(result['paths']['hero'].endswith('hero.png'))
            self.assertIn('generated art used', result['warnings'][0])
            self.assertFalse(Path(tmp, 'hero.jpg').exists())
            self.assertEqual(Path(tmp, 'grid.jpg').read_bytes(), (FIXTURES / 'icon.jpg').read_bytes())

    def test_stop_uses_exact_64_bit_game_id_string(self):
        with patch.object(sys, 'argv', ['steam_shortcuts.py', 'stop', '3346865537']), \
                patch.object(shortcuts, 'evaluate') as evaluate, patch('builtins.print'):
            shortcuts.main()
        self.assertEqual(evaluate.call_args.args[0], 'SteamClient.Apps.TerminateApp("14374678025558032384", false)')


class SteamContextTests(unittest.TestCase):
    @unittest.skipUnless(__import__('shutil').which('node'), 'optional V8 fixture check requires node')
    def test_collection_lifecycle_and_native_artwork_calls(self):
        steam = {'apps': [], 'shortcuts': [{'appid': 42, 'name': 'Before'}], 'compat_tools': {},
                 'collections': [{'name': 'Android', 'apps': [999]}]}
        def evaluate(expression, timeout=20):
            nonlocal steam
            proc = subprocess.run(['node', str(ROOT / 'tests/fakeframe/rootfs/usr/local/lib/fakeframe/cef_shim.js')],
                                  input=json.dumps({'id': 1, 'expression': expression, 'awaitPromise': True,
                                                    'steam': steam}) + '\n',
                                  text=True, capture_output=True, timeout=10, check=True)
            reply = json.loads(proc.stdout)
            self.assertNotIn('exceptionDetails', reply['result'])
            steam = reply['steam']
            return reply['result']['result'].get('value')
        with patch.object(shortcuts, 'evaluate', side_effect=evaluate):
            result = shortcuts.configure(42, 'Game', '/exe', '/dir', '/icon', True,
                                         {slot: str(FIXTURES / 'icon.png') for slot in art.SLOTS})
            self.assertEqual(result['warnings'], [])
            self.assertTrue(steam['shortcuts'][0]['vr'])
            self.assertEqual(set(steam['shortcuts'][0]['artwork']), {'0', '1', '2', '3'})
            self.assertEqual(steam['collections'], [{'name': 'Android', 'apps': [999, 42]},
                                                   {'name': 'Android VR', 'apps': [42]}])
            shortcuts.configure(42, 'Renamed', '/exe', '/dir', '/icon', False,
                                {slot: str(FIXTURES / 'icon.png') for slot in art.SLOTS})
            self.assertEqual(steam['shortcuts'][0]['name'], 'Renamed')
            self.assertEqual(steam['collections'][1]['apps'], [])
            with patch.object(sys, 'argv', ['steam_shortcuts.py', 'list']), patch('builtins.print') as out:
                shortcuts.main()
            self.assertEqual(json.loads(out.call_args.args[0]),
                             [{'appid': 42, 'name': 'Renamed', 'exe': '/exe', 'start_dir': '/dir'}])
            shortcuts.remove(42)
            self.assertEqual(steam['shortcuts'], [])
            self.assertEqual(steam['collections'][0]['apps'], [999])

    @unittest.skipUnless(__import__('shutil').which('node'), 'optional V8 fixture check requires node')
    def test_fill_only_keeps_customised_name_icon_and_art(self):
        custom = {'0': {'data': 'mine-grid', 'ext': 'png'}, '1': {'data': 'mine-hero', 'ext': 'jpg'}}
        steam = {'apps': [], 'compat_tools': {}, 'collections': [],
                 'shortcuts': [{'appid': 42, 'name': 'My Name', 'icon': '/mine.png', 'vr': True, 'artwork': dict(custom)}]}
        def evaluate(expression, timeout=20):
            nonlocal steam
            proc = subprocess.run(['node', str(ROOT / 'tests/fakeframe/rootfs/usr/local/lib/fakeframe/cef_shim.js')],
                                  input=json.dumps({'id': 1, 'expression': expression, 'awaitPromise': True,
                                                    'steam': steam}) + '\n',
                                  text=True, capture_output=True, timeout=10, check=True)
            reply = json.loads(proc.stdout)
            self.assertNotIn('exceptionDetails', reply['result'])
            steam = reply['steam']
            return reply['result']['result'].get('value')
        with tempfile.TemporaryDirectory() as home:
            grid = Path(home, '.local/share/Steam/userdata/1/config/grid')
            grid.mkdir(parents=True)
            (grid / '42p.png').write_bytes(b'mine')
            (grid / '42_hero.jpg').write_bytes(b'mine')
            with patch.dict(os.environ, {'HOME': home, 'USERPROFILE': home}), patch.object(shortcuts, 'evaluate', side_effect=evaluate):
                self.assertEqual(shortcuts.custom_art(42), {0, 1})
                shortcuts.configure(42, 'Generated', '/exe', '/dir', '/generated.png', False,
                                    {slot: str(FIXTURES / 'icon.png') for slot in art.SLOTS},
                                    {'category': 'Sideloaded', 'fill_only': True})
        s = steam['shortcuts'][0]
        self.assertEqual((s['name'], s['icon'], s['vr']), ('My Name', '/mine.png', True))
        self.assertEqual({k: s['artwork'][k] for k in ('0', '1')}, custom)
        self.assertEqual(set(s['artwork']), {'0', '1', '2', '3'})

    @unittest.skipUnless(__import__('shutil').which('node'), 'optional V8 fixture check requires node')
    def test_remove_without_collections_or_artwork_api_still_removes(self):
        steam = {'apps': [], 'shortcuts': [{'appid': 42, 'name': 'Game', 'exe': '"/home/steamos/devkit-game/G/g"',
                                            'start_dir': '/home/steamos/devkit-game/G'}], 'compat_tools': {}}
        def evaluate(expression, timeout=20):
            nonlocal steam
            expression = ('delete globalThis.collectionStore;'
                          'SteamClient.Apps.ClearCustomArtworkForApp = async () => { throw Error("busy"); };' + expression)
            proc = subprocess.run(['node', str(ROOT / 'tests/fakeframe/rootfs/usr/local/lib/fakeframe/cef_shim.js')],
                                  input=json.dumps({'id': 1, 'expression': expression, 'awaitPromise': True,
                                                    'steam': steam}) + '\n',
                                  text=True, capture_output=True, timeout=10, check=True)
            reply = json.loads(proc.stdout)
            self.assertNotIn('exceptionDetails', reply['result'])
            steam = reply['steam']
            return reply['result']['result'].get('value')
        with patch.object(shortcuts, 'evaluate', side_effect=evaluate):
            result = shortcuts.remove(42)
        self.assertEqual(steam['shortcuts'], [])
        self.assertEqual(len(result['warnings']), 5)


if __name__ == '__main__':
    unittest.main()
