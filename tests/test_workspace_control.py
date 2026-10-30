"""The production dedicated C++ WebSocket client against a real loopback peer.
Set FRAME_MOUSE_CONTROL_TEST_BIN to the task-local compiled driver.
"""
import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import threading
import time
import unittest


class WorkspaceControl(unittest.TestCase):
    def test_ordered_edges_latest_motion_and_heartbeat_loss(self):
        binary = os.environ.get("FRAME_MOUSE_CONTROL_TEST_BIN")
        if not binary:
            self.skipTest("compile native/tests_workspace_control.cpp and set FRAME_MOUSE_CONTROL_TEST_BIN")
        server = socket.socket()
        self.addCleanup(server.close)
        server.bind(("127.0.0.1", 0)); server.listen(1); server.settimeout(5)
        errors, observed = [], []
        def peer():
            try:
                sock, _ = server.accept()
                with sock:
                    sock.settimeout(5)
                    data = b""
                    while b"\r\n\r\n" not in data:
                        data += sock.recv(1)
                    self.assertTrue(data.startswith(b"GET /workspace/control?ticket=control-ticket "))
                    headers = dict(line.decode().split(": ", 1) for line in data.split(b"\r\n")[1:] if b": " in line)
                    key = next(value for name, value in headers.items() if name.lower() == "sec-websocket-key")
                    accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest())
                    sock.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
                    def send(value):
                        body = json.dumps(value).encode()
                        sock.sendall(b"\x81" + (bytes([len(body)]) if len(body) < 126 else b"\x7e" + struct.pack(">H", len(body))) + body)
                    def exact(n):
                        out = b""
                        while len(out) < n:
                            chunk = sock.recv(n-len(out))
                            if not chunk: raise EOFError()
                            out += chunk
                        return out
                    def receive():
                        header = exact(2); size = header[1] & 127
                        if size == 126: size = struct.unpack(">H", exact(2))[0]
                        if size == 127: size = struct.unpack(">Q", exact(8))[0]
                        mask = exact(4); body = exact(size)
                        return json.loads(bytes(c ^ mask[i % 4] for i, c in enumerate(body)))
                    send(dict(t="workspace-hello", session="session"))
                    ready = receive(); observed.append(ready)
                    self.assertEqual(ready["panels"][0]["generation"], "panel-1")
                    host = ready["host"]
                    send(dict(t="workspace-ready", session="session",host=host,bindingsQ=ready["bindingsQ"]))
                    send(dict(t="workspace-started",session="session",host=host))
                    def mouse(q, kind, x, y, **extra):
                        return dict(t="mouse",session="session",host=host,q=q,kind=kind,x=x,y=y,button=0,**extra)
                    for value in [mouse(1,"motion",10,1), mouse(2,"motion",20,2),mouse(3,"down",20,2),
                                  mouse(4,"motion",30,3),mouse(5,"wheel",30,3,dx=0,dy=8),mouse(6,"up",30,3)]: send(value)
                    send(mouse(2,"motion",999,999))  # stale cumulative snapshot cannot replay
                    # Deliberately withhold heartbeat ACK: the real client must
                    # cancel within its TTL despite an otherwise healthy socket.
                    started = time.monotonic()
                    while time.monotonic()-started < 3:
                        message = receive(); observed.append(message)
                        if message["t"] == "workspace-windows":
                            self.assertEqual(message["session"], "session")
                            self.assertEqual(message["host"], host)
                            send(dict(t="workspace-windows",session="session",host=host,windows=[dict(id=43,pid=123,stamp="explicit-window-stamp",app="Notes",title="Project")]))
                        elif message["t"] == "workspace-admit":
                            self.assertEqual(message["window"]["stamp"], "explicit-window-stamp")
                            self.assertEqual(message["window"]["id"], 43)
                            send(dict(t="workspace-admitted",session="session",host=host,ok=False,message="Window changed or closed. Refresh and choose again."))
                        elif message["t"] == "workspace-stop": break
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=peer); thread.start()
        url = f"http://127.0.0.1:{server.getsockname()[1]}/view?src=spatial%3A42&t=video-ticket&workspaceTicket=control-ticket&admissionStamp=stamp"
        result = subprocess.run([binary,url],capture_output=True,text=True,timeout=7)
        thread.join(6)
        self.assertFalse(thread.is_alive())
        if errors: raise errors[0]
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        records = [line.split() for line in result.stdout.splitlines() if line and line[0].isdigit()]
        edges = [r[1] for r in records if r[1] != "motion"]
        self.assertEqual(edges,["down","wheel","up"])
        self.assertEqual([r for r in records if r[1] == "up"][0][2:4],["30","3"])
        self.assertFalse(any("999" in r for r in records))
        self.assertIn("Window changed or closed", result.stdout)
        self.assertIn("workspace-admit",[v["t"] for v in observed])
        self.assertIn("workspace-heartbeat",[v["t"] for v in observed])
        self.assertIn("workspace-stop",[v["t"] for v in observed])
