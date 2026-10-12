"""Mac in the headset (ui/frame_macview.py and the frame-mac-view agent).

The Python checks run anywhere. On macOS the agent is built and driven over
HTTP and WebSocket with its test pattern, which needs no Screen Recording
permission: status, the token, the viewer page, H.264 keyframes, pointer
input reaching the source, and closing.

Run: python3 -m unittest discover -s tests
"""
import sandbox  # noqa: F401  (first: keeps tests off real data and services)
import base64
import http.client
import json
import io
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "ui"))

import frame_macview  # noqa: E402


class Helpers(unittest.TestCase):
    def test_panel_id_is_stable_and_in_range(self):
        a = frame_macview.panel_id("window:123")
        self.assertEqual(a, frame_macview.panel_id("window:123"))
        self.assertNotEqual(a, frame_macview.panel_id("window:124"))
        self.assertTrue(2_001_000_000 <= a < 2_002_000_000)

    def test_fit_keeps_aspect_inside_the_panel(self):
        self.assertEqual(frame_macview.fit(2560, 1440), (1920, 1080))
        w, h = frame_macview.fit(800, 1600)
        self.assertEqual(h, 1080)
        self.assertAlmostEqual(w / h, 0.5, places=2)

    def test_the_tunnel_follows_the_headset(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        mv.retarget("frame", ["-o", "ControlPath=/tmp/x-%C", "-o", "HostName=192.0.2.1", "-o", "HostKeyAlias=frame-control-a"])
        self.assertEqual(mv.host_opts, ["-o", "HostName=192.0.2.1", "-o", "HostKeyAlias=frame-control-a"])

        class Tunnel:
            ended = False
            def poll(self): return None
            def terminate(self): Tunnel.ended = True
        mv.tunnel, mv.remote_port = Tunnel(), 47999
        mv.retarget("frame", ["-o", "HostName=192.0.2.9"])  # another address, same headset: keep it
        self.assertFalse(Tunnel.ended)
        mv.retarget("frame-2", ["-o", "HostName=192.0.2.2"])  # another headset: never the old one's tunnel
        self.assertTrue(Tunnel.ended)
        self.assertIsNone(mv.tunnel)
        self.assertEqual(mv.frame, "frame-2")

    def test_a_switch_just_before_publishing_drops_the_old_headsets_tunnel(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        mv.port = 47000
        ended = []

        class Proc:
            def poll(self): return None
            def terminate(self): ended.append(self)
            def wait(self): return 0
            stderr = io.StringIO("")

        def probe(port):
            mv.retarget("frame-2", ["-o", "HostName=192.0.2.2"])  # the app switches right now
            return True
        with mock.patch.object(frame_macview.subprocess, "Popen", return_value=Proc()), \
                mock.patch.object(frame_macview.time, "sleep"), mock.patch.object(mv, "_probe", probe):
            self.assertFalse(mv._open_tunnel([], [47001]))
        self.assertIsNone(mv.tunnel)
        self.assertEqual(len(ended), 1)  # the tunnel to the old headset was closed

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    @unittest.skipIf(os.name == "nt", "Windows' bash.exe is WSL's launcher, and runners have no distribution")
    def test_launch_script_parses(self):
        r = subprocess.run(["bash", "-n"], input=frame_macview.LAUNCH, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_show_checks_the_source_before_anything_else(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: self.fail("no ssh"), "frame")
        with self.assertRaises(frame_macview.MacViewError):
            mv.show("rm -rf /")

    def test_missing_browser_is_explained(self):
        calls = []

        def run(remote, stdin=None, timeout=30):
            calls.append(remote)
            e = RuntimeError("exit 3")
            e.stdout = "NO_BROWSER\n"
            raise e

        mv = frame_macview.MacView(["ssh"], run, "frame")
        mv.call = lambda path, **kw: {"screen": True, "ticket": "tk"}
        mv.ensure_tunnel = lambda **kw: None
        mv.remote_port = 47900
        with self.assertRaises(frame_macview.MacViewError) as cm:
            mv.show("window:5")
        self.assertIn("Chromium", str(cm.exception))
        self.assertTrue(calls[0].startswith("bash -s -- "))

    def test_separate_windows_need_accessibility(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: self.fail("no ssh"), "frame")
        mv.call = lambda path, **kw: {"screen": True, "accessibility": False}
        with self.assertRaises(frame_macview.MacViewError) as cm:
            mv.show("separate:42")
        self.assertIn("Accessibility", str(cm.exception))

    def test_screen_permission_is_checked_before_the_headset(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: self.fail("no ssh"), "frame")
        mv.call = lambda path, **kw: {"screen": False}
        with self.assertRaises(frame_macview.MacViewError) as cm:
            mv.show("display:1")
        self.assertIn("Screen Recording", str(cm.exception))

    def test_stop_all_ends_the_viewer_browser_unless_shown_again(self):
        calls = []
        mv = frame_macview.MacView(["ssh"], lambda remote, **kw: calls.append(remote) or "", "frame")
        mv.agent = mock.Mock(poll=lambda: None)
        mv.call = lambda path, **kw: {"closed": 1}
        mv.shown = {"window:5"}
        with mock.patch.object(frame_macview.time, "sleep"):
            gen = mv.shows
            mv.shown.clear()
            mv._end_viewer_browser(gen)
            self.assertEqual(len(calls), 1)
            self.assertIn("pkill -f '[f]rame-control/mac-view", calls[0])
            mv.shows += 1  # Show pressed during the wait: leave the new viewer alone
            mv._end_viewer_browser(gen)
            self.assertEqual(len(calls), 1)
            mv.launching = 1  # a Show replacing its own stream is still launching
            mv._end_viewer_browser(mv.shows)
            self.assertEqual(len(calls), 1)
        # A Show waits while the cleanup checks and runs pkill.
        mv.launching = 0
        in_pkill, release, entered = threading.Event(), threading.Event(), threading.Event()

        def blocking_pkill(remote, **kw):
            in_pkill.set()
            release.wait(5)
        mv.run = blocking_pkill
        mv._show = lambda *a: entered.set() or "shown"
        with mock.patch.object(frame_macview.time, "sleep"):
            cleanup = threading.Thread(target=mv._end_viewer_browser, args=(mv.shows,))
            cleanup.start()
            self.assertTrue(in_pkill.wait(2))
            shower = threading.Thread(target=mv.show, args=("window:5",))
            shower.start()
            self.assertFalse(entered.wait(0.3), "Show started while the cleanup held the lock")
            release.set()
            self.assertTrue(entered.wait(2))
            cleanup.join()
            shower.join()

    def test_tunnel_prefers_the_usb_c_network_when_plugged_in(self):
        mv = frame_macview.MacView(["ssh"], lambda remote, **kw: "13: usb0 inet 10.86.200.233/29 scope global", "frame")
        ssh_g = mock.Mock(stdout="user steamos\nhostname frame.example.ts.net\n")
        with mock.patch.object(frame_macview.socket, "create_connection") as conn, \
                mock.patch.object(frame_macview.subprocess, "run", return_value=ssh_g):
            self.assertEqual(mv._usb_route(), ["-o", "HostName=10.86.200.233", "-o", "HostKeyAlias=frame.example.ts.net"])
            conn.assert_called_once_with(("10.86.200.233", 22), timeout=1)
            ssh_g.stdout = "hostname frame.example.ts.net\nhostkeyalias paired-frame\n"  # a configured alias wins
            conn.side_effect = None
            self.assertIn("HostKeyAlias=paired-frame", mv._usb_route())
            conn.side_effect = OSError("unplugged")
            self.assertEqual(mv._usb_route(), [])
        mv.run = lambda remote, **kw: ""  # no usb0
        self.assertEqual(mv._usb_route(), [])
        mv.prefer_usb = False
        mv.run = lambda remote, **kw: self.fail("no ssh when USB is off")
        self.assertEqual(mv._usb_route(), [])

    def test_a_failed_usb_tunnel_falls_back_to_the_network(self):
        mv = frame_macview.MacView(["ssh"], lambda *a, **k: "", "frame")
        mv._usb_route = lambda: ["-o", "HostName=10.86.200.233"]
        tried = []

        def attempt(via, ports):
            tried.append(list(via))
            mv._last_tunnel_error = "Connection refused"
            return not via  # USB fails, the normal path works
        mv._open_tunnel = attempt
        mv.tunnel_up = lambda: False
        mv.ensure_tunnel()
        self.assertEqual(tried, [["-o", "HostName=10.86.200.233"], []])
        self.assertEqual(mv.route, "network")


class WS:
    """A minimal WebSocket client (masked frames out, plain frames in)."""

    def __init__(self, port, path):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.s.recv(1)
        self.status = int(head.split()[1])
        self.buf = b""

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.s.recv(65536)
            if not chunk:
                raise EOFError
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        b0, b1 = self._read(2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._read(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._read(8))[0]
        return b0 & 0x0F, self._read(n)

    def send_text(self, text):
        data, mask = text.encode(), os.urandom(4)
        head = bytes([0x81, 0x80 | len(data)]) if len(data) < 126 else bytes([0x81, 0xFE]) + struct.pack(">H", len(data))
        self.s.sendall(head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(data)))

    def close(self):
        self.s.close()


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "the agent is macOS-only")
class Agent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.bin = Path(cls.tmp) / "frame-mac-view"
        r = subprocess.run(["/bin/sh", str(ROOT / "mac" / "frame-mac-view" / "build.sh"), str(cls.bin)],
                           capture_output=True, text=True, timeout=600)
        if r.returncode:  # a real failure on a Mac with Xcode: don't hide it as a skip
            raise AssertionError("agent didn't build:\n" + (r.stderr or r.stdout)[-2000:])
        cls.token = "t0ken-" + os.urandom(6).hex()
        cls.proc = subprocess.Popen([str(cls.bin), "serve", "--port", "0", "--page", str(ROOT / "ui" / "mac-view.html"),
                                     "--exit-on-eof"], env={**os.environ, "FRAME_MAC_VIEW_TOKEN": cls.token},
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        line = cls.proc.stdout.readline()
        cls.port = int(line.rsplit(":", 1)[1])

    @classmethod
    def tearDownClass(cls):
        cls.proc.stdin.close()  # --exit-on-eof
        cls.proc.stdout.close()
        try:
            cls.proc.wait(5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path, method="GET"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(method, path)
        r = c.getresponse()
        return r.status, r.read()

    def test_token_is_required(self):
        self.assertEqual(self.get("/status")[0], 403)
        self.assertEqual(self.get("/status?k=wrong")[0], 403)
        status, body = self.get(f"/status?k={self.token}")
        self.assertEqual(status, 200)
        self.assertIn("screen", json.loads(body))

    def test_serves_the_viewer_page(self):
        status, body = self.get(f"/view?k={self.token}&src=test")
        self.assertEqual(status, 200)
        self.assertIn(b"VideoDecoder", body)

    def test_snapshot_needs_the_key(self):
        self.assertEqual(self.get("/snapshot?display=1")[0], 403)

    def test_separate_without_accessibility_explains(self):
        if json.loads(self.get(f"/status?k={self.token}")[1])["accessibility"]:
            self.skipTest("this Mac allows Accessibility here")
        ws = WS(self.port, f"/stream?k={self.token}&src=separate:1")
        self.assertEqual(ws.status, 101)
        for _ in range(5):
            op, data = ws.recv()
            msg = json.loads(data) if op == 1 else {}
            if msg.get("t") in ("error", "closed"):
                break
        self.assertIn("Accessibility", msg.get("message", msg.get("reason", "")))
        ws.close()

    def test_lists_displays(self):
        status, body = self.get(f"/displays?k={self.token}")
        self.assertEqual(status, 200)
        self.assertIsInstance(json.loads(body)["displays"], list)

    def ticket(self, src):
        status, body = self.get(f"/ticket?k={self.token}&src={src}", method="POST")
        self.assertEqual(status, 200)
        return json.loads(body)["ticket"]

    def test_ping_and_page_are_open_but_streams_are_not(self):
        self.assertEqual(self.get("/ping"), (200, b"frame-mac-view"))
        self.assertEqual(WS(self.port, "/stream?src=test").status, 403)
        self.assertEqual(self.get("/ticket?src=test", method="POST")[0], 403)

    def test_tickets_are_single_use_and_tied_to_one_source(self):
        t = self.ticket("test")
        self.assertEqual(WS(self.port, f"/stream?src=display:1&t={t}").status, 403)  # wrong source
        ws = WS(self.port, f"/stream?src=test&t={t}")
        self.assertEqual(ws.status, 101)
        hello = json.loads(ws.recv()[1])
        self.assertEqual(hello["t"], "hello")
        # Until the viewer acknowledges, a retry (the hello got lost) gets the
        # same key, and replaces the first viewer rather than adding one.
        retry = WS(self.port, f"/stream?src=test&t={t}")
        self.assertEqual(retry.status, 101)
        self.assertEqual(json.loads(retry.recv()[1])["r"], hello["r"])
        time.sleep(0.3)
        _, body = self.get(f"/status?k={self.token}")
        self.assertEqual(len(json.loads(body)["streams"]), 1)
        ws.close()
        ws = retry
        ws.send_text(json.dumps({"t": "ack"}))
        time.sleep(0.3)
        self.assertEqual(WS(self.port, f"/stream?src=test&t={t}").status, 403)  # spent
        again = WS(self.port, f"/stream?src=test&r={hello['r']}")  # the viewer reconnecting
        self.assertEqual(again.status, 101)
        again.close()
        ws.close()
        # Stop revokes the reconnect key.
        self.get(f"/close?k={self.token}&src=test", method="POST")
        self.assertEqual(WS(self.port, f"/stream?src=test&r={hello['r']}").status, 403)

    def test_stop_revokes_tickets_not_yet_used(self):
        t = self.ticket("test")
        self.get(f"/close?k={self.token}&src=test", method="POST")
        self.assertEqual(WS(self.port, f"/stream?src=test&t={t}").status, 403)

    def test_bad_frame_lengths_close_the_socket_not_the_agent(self):
        ws = WS(self.port, f"/stream?src=test&t={self.ticket('test')}")
        self.assertEqual(ws.status, 101)
        # A masked frame claiming 2^63 bytes.
        ws.s.sendall(bytes([0x81, 0xFF]) + struct.pack(">Q", 1 << 63) + os.urandom(4))
        with self.assertRaises((EOFError, OSError)):
            for _ in range(1000):
                ws.recv()
        ws.close()
        self.assertEqual(self.get(f"/status?k={self.token}")[0], 200)

    def test_stream_input_and_close(self):
        ws = WS(self.port, f"/stream?k={self.token}&src=test&codec=h264&fps=30&max=640")
        self.assertEqual(ws.status, 101)
        info = None
        key = None
        for _ in range(200):
            op, data = ws.recv()
            if op == 1:
                msg = json.loads(data)
                if msg["t"] == "hello":
                    continue
                if msg["t"] == "error":
                    self.skipTest("no H.264 encoder here: " + msg["message"])
                if msg["t"] == "info":
                    info = msg
            elif op == 2 and data[0] == 1:
                key = data
            if info and key:
                break
        self.assertEqual(info["src"], "test")
        self.assertTrue(info["input"])
        # A keyframe: flags, 8-byte timestamp, sequence number, input echoed,
        # then Annex B with the SPS first.
        self.assertEqual(key[17:21], b"\x00\x00\x00\x01")
        self.assertEqual(key[21] & 0x1F, 7)  # NAL type 7, SPS
        ws.send_text(json.dumps({"t": "m", "e": "down", "b": 0, "x": 0.5, "y": 0.5}))
        ws.send_text(json.dumps({"t": "key-frame"}))
        got_key = False
        for _ in range(200):
            op, data = ws.recv()
            if op == 2 and data[0] == 1:
                got_key = True
                break
        self.assertTrue(got_key, "no keyframe after asking for one")
        _, body = self.get(f"/status?k={self.token}")
        self.assertEqual([s["src"] for s in json.loads(body)["streams"]], ["test"])
        status, body = self.get(f"/close?k={self.token}", method="POST")
        self.assertEqual(json.loads(body)["closed"], 1)
        for _ in range(400):
            op, data = ws.recv()
            if op == 1 and json.loads(data)["t"] == "close":
                break
        self.assertEqual(json.loads(data)["t"], "close")
        # Without the viewer doing anything, the agent ends the stream itself.
        time.sleep(1)
        _, body = self.get(f"/status?k={self.token}")
        self.assertEqual(json.loads(body)["streams"], [])
        ws.close()

    def test_timing_reports_and_input_echo(self):
        # What a viewer does: sync clocks, report each frame, stamp input.
        ws = WS(self.port, f"/stream?k={self.token}&src=test&codec=h264&fps=30&max=640")
        self.assertEqual(ws.status, 101)
        ws.send_text(json.dumps({"t": "w"}))  # keep-warm filler: ignored
        ws.send_text(json.dumps({"t": "ping", "c": 1.5}))
        pong, echoed, seqs, info = None, None, [], None
        clicked = False
        deadline = time.time() + 10
        while time.time() < deadline and not (pong and echoed):
            op, data = ws.recv()
            if op == 1:
                msg = json.loads(data)
                if msg["t"] == "error":
                    self.skipTest("no H.264 encoder here: " + msg["message"])
                if msg["t"] == "pong":
                    pong = msg
                if msg["t"] == "info":
                    info = msg
            elif op == 2:
                seq, echo = struct.unpack(">II", data[9:17])
                seqs.append(seq)
                now = pong["a"] if pong else 0
                ws.send_text(json.dumps({"t": "rx", "s": seq, "r": now}))
                ws.send_text(json.dumps({"t": "fd", "f": [[seq, now + 1, now + 2, now + 3]], "drop": 0}))
                if echo == 7:
                    echoed = seq
                if len(seqs) == 3 and not clicked:
                    ws.send_text(json.dumps({"t": "m", "e": "down", "b": 0, "x": 0.5, "y": 0.5, "i": 7, "tv": now}))
                    clicked = True
        self.assertEqual(pong["c"], 1.5)
        self.assertGreater(pong["a"], 0)
        self.assertEqual(seqs[:3], sorted(seqs[:3]))
        self.assertIsNotNone(echoed, "no frame was tagged as the first reply to the click")
        time.sleep(1.7)  # frames are reported once the viewer has had time
        stream = json.loads(self.get(f"/stats?k={self.token}")[1])["streams"][0]
        first = stream["frames"][0]
        self.assertGreater(first["e1"], first["e0"])
        self.assertGreaterEqual(first["e0"], first["cap"])
        self.assertEqual(first["vs"] - first["rx"], 3)
        self.assertEqual([i["frame"] for i in stream["inputs"] if i["id"] == 7], [echoed])
        self.assertIn("total", stream["summary"])
        self.assertEqual(info["warm"], 0)  # off unless FRAME_MAC_VIEW_WARM is set
        live = json.loads(self.get(f"/status?k={self.token}")[1])["streams"][0]
        self.assertEqual(live["controller"]["tier"], 0)
        self.assertIn("fps", live["stats"])
        ws.close()


if __name__ == "__main__":
    unittest.main()
