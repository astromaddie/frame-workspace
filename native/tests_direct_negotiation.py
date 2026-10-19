#!/usr/bin/env python3
"""The real native client must refuse an unavailable direct-capture path.

No video, OpenVR, Frame services or live gaze. Requires a local GL display
(--use-display) or Xvfb. The unsupported peer must not receive an ack or gaze.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("binary", type=Path)
p.add_argument("--use-display", action="store_true")
a = p.parse_args()
for capability in (None, 0, True, "1"):
    with tempfile.TemporaryDirectory(prefix="fw-direct-negotiation-") as folder:
        root = Path(folder)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(5)
        (root / "url").write_text(f"http://127.0.0.1:{listener.getsockname()[1]}/view?src=test&t=test")
        errors, messages = [], []

        def server():
            try:
                with listener.accept()[0] as c:
                    c.settimeout(5)
                    def exact(n):
                        out = b""
                        while len(out) < n:
                            part = c.recv(n - len(out))
                            if not part:
                                raise EOFError()
                            out += part
                        return out
                    request = b""
                    while b"\r\n\r\n" not in request:
                        request += exact(1)
                    assert b"&direct=1" in request and b"&atlas=" not in request
                    key = next(line.split(b":", 1)[1].strip() for line in request.split(b"\r\n")
                               if line.lower().startswith(b"sec-websocket-key:"))
                    accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
                    c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
                    hello = {"t": "hello", "r": "test-reconnect", "foveation": 1, "atlas": 1,
                             "gazeSession": "test-session"}
                    if capability is not None:
                        hello["direct"] = capability
                    raw = json.dumps(hello).encode()
                    c.sendall(bytes([0x81, 126]) + struct.pack("!H", len(raw)) + raw)
                    while True:
                        op, size = exact(2)
                        assert size & 128
                        length = size & 127
                        if length == 126:
                            length = struct.unpack("!H", exact(2))[0]
                        elif length == 127:
                            length = struct.unpack("!Q", exact(8))[0]
                        mask = exact(4)
                        raw = bytes(value ^ mask[i % 4] for i, value in enumerate(exact(length)))
                        if op & 15 == 1:
                            messages.append(json.loads(raw).get("t"))
            except (EOFError, ConnectionResetError):
                pass
            except Exception as e:
                errors.append(repr(e))
            finally:
                listener.close()

        thread = threading.Thread(target=server)
        thread.start()
        cmd = [str(a.binary.resolve()), "--no-vr", "--foveation", "--diagnostic-gaze", "scan",
               "--url-file", str(root / "url"), "--state-file", str(root / "state"), "--seconds", "2"]
        result = subprocess.run(cmd if a.use_display else ["xvfb-run", "-a", *cmd],
                                capture_output=True, text=True, timeout=8)
        thread.join(6)
        assert not errors and not thread.is_alive(), errors
        assert result.returncode == 1 and "exit reason=direct-capture-unavailable" in result.stderr, result.stderr
        state = json.loads((root / "state").read_text())
        assert state["decoded"] == 0 and state["uploaded"] == 0, state
        assert "ack" not in messages and "gaze" not in messages, messages
print(json.dumps({"result": "PASS", "rejectedCapabilities": 4,
                  "coverage": "real client; absent/zero/boolean/string direct; no ack, video or gaze fallback"}))
