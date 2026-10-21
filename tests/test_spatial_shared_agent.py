"""The shared-spatial helper API, driven through the real local server.

These checks create no displays and inject no input. Live fixture acceptance is
in Diagnostics/SharedSpatialFixtureRun.sh.
"""
import base64
import http.client
import json
import os
from pathlib import Path
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = Path("/private/tmp/claude-501/-Users-mads-code-frame-workspace/d6cfb87f-3bf9-45b7-bf70-3ae621c211ed/scratchpad/wp-a")

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


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "the helper is macOS-only")
class Agent(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        cls.tmp = tempfile.mkdtemp(prefix="shared-agent-", dir=SCRATCH)
        cls.addClassCleanup(shutil.rmtree, cls.tmp, ignore_errors=True)
        cls.bin = Path(os.environ.get("FRAME_SPATIAL_TEST_BINARY", Path(cls.tmp) / "frame-mac-view"))
        if "FRAME_SPATIAL_TEST_BINARY" not in os.environ:
            free = subprocess.check_output(["/usr/bin/memory_pressure"], text=True).splitlines()[-1]
            if int(free.rstrip("%").rsplit(" ", 1)[1]) < 15:
                raise RuntimeError("Free memory below 15%; run the fixture memory gate before retrying")
            result = subprocess.run(["sh", str(ROOT / "mac/frame-mac-view/build.sh"), str(cls.bin)],
                                    capture_output=True, text=True, timeout=600)
            if result.returncode or result.stderr:
                raise AssertionError("helper build failed or warned:\n" + result.stdout + result.stderr)
        cls.token = "spatial-test-" + os.urandom(12).hex()
        cls.proc = subprocess.Popen([str(cls.bin), "serve", "--port", "0", "--exit-on-eof"],
            env={**os.environ, "FRAME_MAC_VIEW_TOKEN": cls.token, "FRAME_SPATIAL_ONLY_PIDS": ""},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        cls.addClassCleanup(cls.stop_helper)
        if not select.select([cls.proc.stdout], [], [], 15)[0]:
            raise AssertionError("helper did not listen")
        cls.port = int(cls.proc.stdout.readline().rsplit(":", 1)[1])

    @classmethod
    def stop_helper(cls):
        if cls.proc.stdin and not cls.proc.stdin.closed:
            cls.proc.stdin.close()
        try:
            cls.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.proc.terminate()
            cls.proc.wait(timeout=10)
        cls.proc.stdout.close()
        cls.proc.stderr.close()

    def call(self, path, method="GET", authenticated=True):
        if authenticated:
            path += ("&" if "?" in path else "?") + "k=" + self.token
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def test_status_has_shared_contract_and_does_not_change_displays(self):
        before = self.call("/displays")[1]
        code, data = self.call("/spatial")
        self.assertEqual(code, 200)
        value = json.loads(data)
        self.assertEqual(value["state"], "stopped")
        self.assertEqual(value["windows"], [])
        self.assertEqual(value["skipped"], [])
        self.assertEqual(value["menus"], [])
        self.assertIsNone(value["display"])
        self.assertIsNone(value["focusedSrc"])
        self.assertIsInstance(value["revision"], int)
        self.assertEqual(self.call("/displays")[1], before)

    def test_spatial_keys_parse_but_admission_is_closed_outside_mode(self):
        for source in ("spatial:123", "menubar", "status", *(f"menu:{i}" for i in range(4))):
            with self.subTest(source=source):
                self.assertEqual(self.call(f"/ticket?src={source}", "POST")[0], 409)
                ws = WS(self.port, f"/stream?k={self.token}&src={source}")
                self.addCleanup(ws.close)
                self.assertEqual(ws.status, 403)

    def test_unknown_spatial_focus_conflicts_before_input(self):
        self.assertEqual(self.call("/focus?src=spatial:4294967294&u=.31&v=.12", "POST")[0], 409)

    def test_focus_rejects_invalid_coordinates(self):
        for u, v in (("nan", "0"), ("0", "inf"), ("1.01", "0"), ("0", "-.01")):
            self.assertEqual(self.call(f"/focus?src=spatial:123&u={u}&v={v}", "POST")[0], 400)

    def test_auxiliary_sources_are_not_window_live_set_members(self):
        for source in ("menubar", "status", "menu:0"):
            self.assertEqual(self.call(f"/streams/live?src={source}", "POST")[0], 400)
        code, data = self.call("/streams/live?src=spatial:123", "POST")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(data)["live"], ["spatial:123"])
        self.call("/streams/live?all=1", "POST")

    def test_bad_menu_keys_are_rejected(self):
        for source in ("menu:4", "menu:-1", "menu:x", "menubar:1", "status:1"):
            self.assertEqual(self.call(f"/ticket?src={source}", "POST")[0], 400)

    def test_spatial_routes_require_authentication(self):
        for path, method in (("/spatial", "GET"), ("/spatial/start", "POST"), ("/spatial/stop", "POST"),
                             ("/focus?src=spatial:123&u=0&v=0", "POST"), ("/ticket?src=menubar", "POST")):
            self.assertEqual(self.call(path, method, authenticated=False)[0], 403)

    def test_desktop_tickets_still_admit_the_existing_sources(self):
        for source in ("test", "window:123", "separate:123", "display:123"):
            self.assertEqual(self.call(f"/ticket?src={source}", "POST")[0], 200)
            self.call(f"/close?src={source}", "POST")


if __name__ == "__main__":
    unittest.main()
