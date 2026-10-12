"""Owned media planning, eye isolation, decoder choice and fake-Frame ownership."""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import argparse
import io
import json
import os
import signal
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'ui'))
import frame_media as media
import frame_media_player as player
import frame_media_remote as remote
import frame_splat as splat
import server


def stop_now():
    """What systemd's SIGTERM does to the player, without signalling the test process."""
    handler = signal.getsignal(signal.SIGTERM)
    if callable(handler):
        handler(signal.SIGTERM, None)


class Media(unittest.TestCase):
    def test_layout_evidence_and_override(self):
        for name, layout in [('film_SBS.mp4', 'sbs'), ('film.OU.mkv', 'ou'),
                             ('film_FSBS.mp4', 'full-sbs'), ('photo_TB.png', 'ou')]:
            self.assertEqual(media.plan(name)['layout'], layout)
        self.assertEqual(media.plan('film_SBS_OU.mp4', 'mono')['source'], 'explicit')
        self.assertEqual(media.plan('film.mkv', metadata={'stereo_mode': 'top_bottom'})['layout'], 'full-ou')
        for name in ('film.mp4', 'film_SBS_OU.mp4', 'businessbs.mp4'):
            with self.assertRaises(ValueError):
                media.plan(name)
        with self.assertRaises(ValueError):
            media.plan('film.mkv', metadata={'stereo_mode': 'right_left'})

    def test_unsupported_containers_do_not_flatten_spatial_photos(self):
        for name in ('spatial.HEIC', 'stereo.mpo', 'cloud.ply', 'cloud.spz', 'app.exe'):
            with self.assertRaises(ValueError):
                media.plan(name, 'sbs')

    def test_ou_pixels_keep_each_eye_and_row(self):
        a, b, c, d = [bytes([n])*8 for n in (1, 2, 3, 4)]
        data, width, height = media.stereo_pixels(a+b+c+d, 2, 4, 'ou')
        self.assertEqual((data, width, height), (a+c+b+d, 4, 2))
        with self.assertRaises(ValueError):
            media.stereo_pixels(b'bad', 2, 4, 'sbs')
        self.assertEqual(media.geometry(3840, 2160, 'sbs'), (1920, 1080, 2))
        self.assertEqual(media.geometry(1920, 1080, 'ou'), (1920, 1080, .5))

    def test_decode_is_hardware_and_one_clock_for_audio(self):
        for codec, decoder in [('h264', 'h264_v4l2m2m'), ('hevc', 'hevc_v4l2m2m')]:
            cmd = player.decoder_command(Path('/tmp/a file.mp4'), {'codec_name': codec}, 1280, 720, True)
            self.assertIn(decoder, cmd)
            self.assertIn('-re', cmd)
            self.assertIn('pulse', cmd)
            self.assertIn(str(Path('/tmp/a file.mp4')), cmd)
        with self.assertRaises(ValueError):
            player.decoder_command(Path('x.webm'), {'codec_name': 'vp9'}, 640, 480, False)
        cmd = player.decoder_command(Path('x.png'), {'codec_name': 'png'}, 640, 480, False, True)
        self.assertNotIn('-re', cmd)
        self.assertIn('-frames:v', cmd)

    def play_with(self, name, busy=0, on_pixels=None, sleeps=None, info=None,
                  fail=None, layout='auto'):
        """Run the player against a fake OpenVR; returns (status, pixels calls).

        Handle 0 is the theatre surround and 1 the screen. `fail(handle, n)` makes
        the n-th upload busy; every status written is kept in self.writes."""
        calls = []
        self.writes = []
        write_status = player.write_status

        def record(path, **values):
            self.writes.append(values)
            write_status(path, **values)

        class FakeOverlay:
            def create(self, *a, **k):
                return len(calls)

            def call(self, *a):
                pass

            def pixels(self, handle, data, w, h):
                calls.append((handle, w, h))
                if on_pixels:
                    on_pixels(len(calls))
                if len(calls) <= busy or (fail and fail(handle, len(calls))):
                    raise player.OverlayBusy('standby')

            def close(self):
                # A Stop landing during cleanup must be ignored, not become an error.
                # Call the installed handler directly: a real SIGTERM kills Windows.
                stop_now()

        frame = bytes(4*2*4)
        proc = unittest.mock.MagicMock()
        proc.stdout = io.BytesIO(frame*4)
        proc.wait.return_value = 0
        proc.poll.return_value = 0
        old = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
        with tempfile.TemporaryDirectory() as d, \
                patch.object(player, 'Overlay', FakeOverlay), \
                patch.object(player, 'probe', return_value=(info or {'codec_name': 'h264', 'width': 4, 'height': 2}, False)), \
                patch.object(player.subprocess, 'Popen', return_value=proc), \
                patch.object(player, 'write_status', side_effect=record), \
                patch.object(player.frame_splat, 'render', return_value=(bytes(4*4*2), 4, 2)), \
                patch.object(player.time, 'sleep', side_effect=sleeps):
            path = Path(d)/name
            path.write_bytes(b'x')
            status = Path(d)/'status.json'
            try:
                player.play(argparse.Namespace(file=str(path), layout=layout, theatre=True, status=str(status)))
            finally:
                signal.signal(signal.SIGTERM, old[0])
                signal.signal(signal.SIGINT, old[1])
            return json.loads(status.read_text()), calls

    def test_video_survives_standby_and_stop_after_end_stays_ended(self):
        # Verified 2026-09-29: an unworn Frame enters standby within seconds and
        # SetOverlayRaw then returns RequestFailed (23) until it wakes.
        result, calls = self.play_with('clip_SBS.mp4', busy=3)
        self.assertEqual((result['state'], result['frames']), ('ended', 4))
        # Two video frames were dropped; the surround (handle 0) waited and was re-sent.
        self.assertEqual(result['dropped'], 2)
        self.assertIn((0, 1, 1), calls[3:])

    def test_stop_mid_video_reports_stopped(self):
        result, _ = self.play_with('clip_SBS.mp4', on_pixels=lambda n: n == 3 and stop_now())
        self.assertEqual(result['state'], 'stopped')

    def test_video_errors_when_steamvr_never_takes_frames(self):
        with patch.object(player, 'BUSY_LIMIT', -1), \
                self.assertRaisesRegex(RuntimeError, 'stopped accepting frames'):
            self.play_with('clip_SBS.mp4', busy=99)

    def test_still_waits_out_standby_without_a_limit(self):
        # Stills have no timeline: keep retrying (here past BUSY_LIMIT) until shown.
        ticks = iter(range(10))
        def sleep(_):
            if next(ticks) == 8:
                stop_now()
        with patch.object(player, 'BUSY_LIMIT', -1):
            result, calls = self.play_with('photo_SBS.png', busy=5, sleeps=sleep,
                                           info={'codec_name': 'png', 'width': 4, 'height': 2})
        self.assertEqual(result['state'], 'stopped')
        screen = [c for c in calls if c[0] == 1]
        self.assertGreater(len(screen), 1)  # retried through standby
        self.assertEqual(calls[-1], (0, 1, 1))  # surround drained once the screen took a frame

    def still_with_late_surround(self, name, **kw):
        # The screen takes its first frame while the surround is still refused
        # (its first upload, the drain right after the screen, and one retry).
        ticks = iter(range(10))
        def sleep(_):
            if next(ticks) == 5:
                stop_now()
        surround_tries = []
        def fail(handle, n):
            if handle == 0:
                surround_tries.append(n)
                return len(surround_tries) <= 3
            return False
        result, calls = self.play_with(name, sleeps=sleep, fail=fail, **kw)
        self.assertEqual(result['state'], 'stopped')
        screen = [c for c in calls if c[0] == 1]
        surround = [c for c in calls if c[0] == 0]
        self.assertEqual(len(screen), 1)  # shown once, not re-sent every second
        self.assertEqual(len(surround), 4)  # kept retrying after the screen, until it took
        self.assertEqual(calls[-1][0], 0)
        return calls

    def test_photo_surround_recovers_after_screen_is_shown(self):
        self.still_with_late_surround('photo_SBS.png',
                                      info={'codec_name': 'png', 'width': 4, 'height': 2})

    def test_splat_surround_recovers_after_screen_is_shown(self):
        self.still_with_late_surround('scene.splat')

    def test_video_standby_limit_is_five_minutes_without_an_accepted_frame(self):
        self.assertEqual(player.BUSY_LIMIT, 300)
        def run(times, busy):
            # Upload n happens at times[n] seconds on a fake clock; 1 is the surround.
            clock = [1000.0]
            def on_pixels(n):
                clock[0] = 1000.0 + times.get(n, times[max(times)])
            with patch.object(player.time, 'monotonic', side_effect=lambda: clock[0]):
                return self.play_with('clip_SBS.mp4', on_pixels=on_pixels,
                                      fail=lambda h, n: h == 1 and n in busy)
        # Busy for 299 s, then a frame lands: no error.
        result, _ = run({1: 0, 2: 0, 3: 299, 4: 299, 5: 299}, busy={2, 3})
        self.assertEqual((result['state'], result['dropped']), ('ended', 2))
        # An accepted frame resets the timer: 600 s busy in total, never 300 s in a row.
        result, _ = run({1: 0, 2: 0, 3: 200, 4: 250, 5: 450}, busy={2, 3, 5})
        self.assertEqual((result['state'], result['dropped']), ('ended', 3))
        # 301 s in a row without an accepted frame is an error.
        with self.assertRaisesRegex(RuntimeError, 'stopped accepting frames for 300 s'):
            run({1: 0, 2: 0, 3: 301}, busy={2, 3, 4, 5})

    def test_status_reports_where_the_layout_came_from(self):
        video = {'codec_name': 'h264', 'width': 4, 'height': 2}
        for name, layout, tags, expect in [
                ('clip_SBS.mp4', 'auto', None, ('sbs', 'filename')),
                ('clip.mkv', 'auto', {'stereo_mode': 'left_right'}, ('full-sbs', 'metadata')),
                ('clip_OU.mp4', 'sbs', None, ('sbs', 'explicit')),
                ('clip.mkv', 'mono', {'stereo_mode': 'left_right'}, ('mono', 'explicit'))]:
            with self.subTest(name=name, layout=layout):
                self.play_with(name, layout=layout, info=dict(video, tags=tags) if tags else video)
                playing = self.writes[0]
                self.assertEqual(playing['state'], 'playing')
                self.assertEqual((playing['layout'], playing['source']), expect)

    def test_fake_frame_library_and_traversal(self):
        with tempfile.TemporaryDirectory() as d, patch.object(remote, 'ROOT', Path(d)):
            identity = 'a'*32+'/space and quote\'.png'
            path = Path(d)/identity
            path.parent.mkdir()
            path.write_bytes(b'test')
            self.assertEqual(remote.media_path(identity), path.resolve())
            for bad in ('../../etc/passwd', '/etc/passwd', 'a'*32+'/..', 'a'*32+'/x/y', None):
                with self.assertRaises((ValueError, FileNotFoundError)):
                    remote.media_path(bad)
            link = path.parent/'link.png'
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                remote.media_path('a'*32+'/link.png')
            with patch.object(remote, 'status', return_value={'state': 'idle'}):
                files = remote.run({'action': 'list'})['files']
                self.assertEqual([f['id'] for f in files], [identity])

    def test_server_rejects_bad_actions_before_ssh(self):
        with patch.object(server, 'ssh') as ssh:
            for body in ({'action': 'delete'}, {'action': 'play', 'id': '../x'},
                         {'action': 'play', 'id': 'a'*32+'/x', 'layout': 'invalid'},
                         {'action': 'play', 'id': 'a'*32+'/x', 'theatre': 'false'}):
                with self.assertRaises(server.Failure):
                    server.media(body)
            ssh.assert_not_called()

    def test_fake_frame_stop_only_owns_our_unit(self):
        for rc in (0, 5):  # 5: already collected ("not loaded"), a no-op
            with patch.object(remote.subprocess, 'run') as run, patch.object(remote, 'status', return_value={'state': 'ended'}):
                run.return_value.returncode = rc
                self.assertEqual(remote.run({'action': 'stop'})['state'], 'ended')
                self.assertEqual(run.call_args.args[0], ['systemctl', '--user', 'stop', 'frame-control-media.service'])
        with patch.object(remote.subprocess, 'run') as run, patch.object(remote, 'status', return_value={}):
            run.return_value.returncode, run.return_value.stderr = 1, 'Access denied'
            with self.assertRaisesRegex(RuntimeError, 'Access denied'):
                remote.run({'action': 'stop'})

    def test_start_failure_reports_systemd_error(self):
        with tempfile.TemporaryDirectory() as d, patch.object(remote, 'ROOT', Path(d)), \
                patch.object(remote, 'STATUS', Path(d)/'status.json'), \
                patch.object(remote, 'active', return_value=False), \
                patch.object(remote.subprocess, 'run') as run:
            identity = 'b'*32+'/still_SBS.png'
            (Path(d)/identity).parent.mkdir()
            (Path(d)/identity).write_bytes(b'x')
            run.return_value.returncode, run.return_value.stderr = 1, 'Unit already exists'
            with patch.object(remote, 'probe', return_value=({}, False)), \
                    self.assertRaisesRegex(RuntimeError, 'Unit already exists'):
                remote.run({'action': 'play', 'id': identity})
            self.assertEqual(run.call_args.args[0][0], 'systemd-run')  # reset-failed's result is ignored

    def test_upload_rejects_unplayable_names_and_keeps_copy_error(self):
        with patch.object(server, 'ssh') as ssh, patch.object(server, 'push_file') as push:
            # On Windows a backslash is a separator, so such a name can't reach here.
            for name in ('.hidden.mp4',) + (('a\\b_SBS.mp4',) if os.sep == '/' else ()):
                with self.assertRaises(server.Failure):
                    server.push_media(Path('/tmp')/name)
            ssh.assert_not_called()
            push.side_effect = server.Failure('copy failed')
            ssh.side_effect = [None, server.Failure('link down')]
            with self.assertRaisesRegex(server.Failure, 'copy failed'):
                server.push_media(Path('/tmp/film_SBS.mp4'))

    def test_splat_invalid_records_and_stereo_parallax(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'small.splat'
            path.write_bytes(struct.pack('<6f8B', 0, 0, 0, .001, .001, .001,
                                         255, 0, 0, 255, 255, 128, 128, 128))
            data, w, h = splat.render(path, 64, 48)
            self.assertEqual((len(data), w, h), (128*48*4, 128, 48))
            def centroid(eye):
                weights = [(x, data[(y*w+x+eye*64)*4]) for y in range(h) for x in range(64)]
                return sum(x*v for x,v in weights)/sum(v for x,v in weights)
            self.assertGreater(centroid(0), centroid(1))
            for bad in (b'', b'bad', struct.pack('<6f8B', float('nan'), 0, 0, 1, 1, 1, *([128]*8))):
                path.write_bytes(bad)
                with self.assertRaises(ValueError):
                    splat.read(path)


if __name__ == '__main__':
    unittest.main()
