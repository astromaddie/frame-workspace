#!/usr/bin/env python3
"""Real native backchannel while a WS message arrives without poll timeouts.

No video decoder or OpenVR is used. Run on Linux with DISPLAY=:0 (or Xvfb).
The independent reader timestamps each received gaze message during the write,
never after buffering the whole incoming test message. All UV data is discarded.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import queue
import socket
import struct
import subprocess
import tempfile
import threading
import time

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("binary", type=Path)
a = p.parse_args()
arrivals, ages, errors = [], [], []
window = []
chunk_gaps = []
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen(1)
listener.settimeout(8)


def send(c, obj):
    raw = json.dumps(obj).encode()
    c.sendall(bytes([0x81, len(raw)]) if len(raw) < 126 else bytes([0x81, 126]) + struct.pack("!H", len(raw)))
    c.sendall(raw)


def serve():
    c = None
    reader = None
    try:
        c, _ = listener.accept()
        c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        c.settimeout(6)
        def exact(n):
            data = b""
            while len(data) < n:
                part = c.recv(n - len(data))
                if not part:
                    raise EOFError()
                data += part
            return data
        request = b""
        while b"\r\n\r\n" not in request:
            request += exact(1)
        key = next(line.split(b":", 1)[1].strip() for line in request.split(b"\r\n")
                   if line.lower().startswith(b"sec-websocket-key:"))
        accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
        c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        send(c, {"t": "hello", "r": "test-reconnect", "foveation": 1, "gazeSession": "slow-read-test"})
        pings = queue.SimpleQueue()
        def receive():
            try:
                while True:
                    op, n = exact(2)
                    assert n & 128
                    length = n & 127
                    if length == 126:
                        length = struct.unpack("!H", exact(2))[0]
                    elif length == 127:
                        length = struct.unpack("!Q", exact(8))[0]
                    mask = exact(4)
                    raw = bytes(v ^ mask[i % 4] for i, v in enumerate(exact(length)))
                    if op & 15 != 1:
                        continue
                    m = json.loads(raw)
                    if m.get("t") == "ping":
                        pings.put(m["c"])
                    elif m.get("t") == "gaze" and m.get("valid"):
                        now = time.monotonic()
                        arrivals.append(now)
                        ages.append(now * 1000 - m["time"] / 1000)
            except EOFError:
                pass
            except Exception as e:
                errors.append(type(e).__name__)
        reader = threading.Thread(target=receive)
        reader.start()
        def idle(seconds):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                while not pings.empty():
                    send(c, {"t": "pong", "c": pings.get_nowait(), "a": time.monotonic() * 1e6})
                time.sleep(.002)
        idle(1)
        assert len(arrivals) >= 20, "Clock handshake/gaze did not start"
        body = b'{"t":"noop","padding":"' + b"x" * (128 * 1024) + b'"}'
        c.sendall(bytes([0x81, 127]) + struct.pack("!Q", len(body)))
        previous = time.monotonic()
        window.append(previous)
        for offset in range(0, len(body), 1024):
            time.sleep(max(0, previous + .004 - time.monotonic()))
            tick = time.monotonic()
            chunk_gaps.append((tick - previous) * 1000)
            previous = tick
            c.sendall(body[offset:offset + 1024])
        window.append(time.monotonic())
        idle(1)
        send(c, {"t": "close"})
        reader.join(3)
        assert not reader.is_alive(), "Reader did not exit"
    except Exception as e:
        errors.append(type(e).__name__ + ": " + str(e)[:100])
    finally:
        if c:
            c.close()
        if reader and reader.is_alive():
            reader.join(1)
        listener.close()


with tempfile.TemporaryDirectory(prefix="fw-gaze-slow-read-") as tmp:
    url = Path(tmp) / "url"
    url.write_text(f"http://127.0.0.1:{listener.getsockname()[1]}/view?src=test&t=test")
    thread = threading.Thread(target=serve)
    thread.start()
    result = subprocess.run([str(a.binary.resolve()), "--no-vr", "--foveation", "--diagnostic-gaze", "scan",
                             "--url-file", str(url), "--seconds", "5"], capture_output=True, text=True, timeout=10)
    thread.join(5)
assert not errors and not thread.is_alive(), errors
assert result.returncode == 1 and "decoded=0 uploaded=0" in result.stderr, result.stderr
assert len(window) == 2 and len(arrivals) >= 50
gaps = [(arrivals[i] - arrivals[i - 1]) * 1000 for i in range(1, len(arrivals))]
during = [gaps[i - 1] for i in range(1, len(arrivals))
          if arrivals[i] >= window[0] and arrivals[i - 1] <= window[1]]
report = {"gazeMessages": len(arrivals), "maxGapMs": max(gaps), "maxGapDuringPartialReadMs": max(during),
          "partialMessageMs": (window[1] - window[0]) * 1000, "maxChunkIntervalMs": max(chunk_gaps),
          "minHostAgeMs": min(ages), "maxHostAgeMs": max(ages),
          "coverage": "independent live reader; 128KiB WS message in 1KiB chunks; no decoder/OpenVR; no retained UVs"}
print(json.dumps(report))
assert max(chunk_gaps) < 8, "Test did not maintain continuous sub-poll-interval progress"
assert max(gaps) < 150, "Partial message starved the gaze backchannel"
