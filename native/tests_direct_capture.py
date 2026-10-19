#!/usr/bin/env python3
"""Actual Mac direct-capture/VT sequence through native WS/decode/GL.

Generate the temporary sequence with the Mac direct-capture session test first.
Every packet must be mode3 with identical ordinary geometry. Compares the final
native texture with independently decoded pixels; no reconstruction oracle.
No OpenVR runtime, Frame services or live eye tracking are used.
"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import select
import socket
import struct
import subprocess
import tempfile
import threading
import time

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("binary", type=Path)
p.add_argument("sequence", type=Path)
p.add_argument("--conversion", choices=("cpu", "gpu"), default="gpu")
p.add_argument("--use-display", action="store_true")
a = p.parse_args()
data, offset, packets = a.sequence.read_bytes(), 0, []
while offset < len(data):
    length = struct.unpack_from("!I", data, offset)[0]
    offset += 4
    packet = bytearray(data[offset:offset + length])
    assert len(packet) == length and length > 37 and packet[0] & 128
    assert packet[17:21] == bytes([1, 3, 0, 0]), "Only direct captures are valid"
    offset += length
    packet[9:13] = struct.pack("!I", len(packets) + 1)
    packets.append(packet)
assert packets and packets[0][0] & 1
dimensions = {struct.unpack_from("!4H", packet, 21) for packet in packets}
assert len(dimensions) == 1
w, h, ew, eh = dimensions.pop()
assert (w, h) == (ew, eh)
bitstream = b"".join(packet[37:] for packet in packets)
selection = f"select=eq(n\\,{len(packets) - 1})"
cli_rgb = subprocess.check_output([
    "ffmpeg", "-v", "error", "-f", "h264", "-i", "pipe:0", "-vf",
    selection, "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"
], input=bitstream)
expected = cli_rgb
if a.conversion == "gpu":
    # The shader uses floating-point BT.709. FFmpeg's CLI RGB converter uses
    # libswscale integer approximations (observed up to 3 bytes darker on this
    # SCK fixture). Decode YUV independently and apply the BT.709 video-range
    # equations instead, as in tests_foveation.py. Keep the CLI deviation visible
    # separately; do not loosen the image-error bound to accommodate it.
    metadata = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-f", "h264", "-show_entries",
        "stream=color_space,color_range,pix_fmt,width,height", "-of", "json", "pipe:0"
    ], input=bitstream))["streams"][0]
    assert metadata == {"width": w, "height": h, "pix_fmt": "yuv420p", "color_range": "tv", "color_space": "bt709"}, metadata
    planes = subprocess.check_output([
        "ffmpeg", "-v", "error", "-f", "h264", "-i", "pipe:0", "-vf",
        selection, "-frames:v", "1", "-pix_fmt", "yuv420p", "-f", "rawvideo", "pipe:1"
    ], input=bitstream)
    assert len(planes) == w * h * 3 // 2
    expected = bytearray()
    for y in range(h):
        for x in range(w):
            lum = (planes[y * w + x] - 16) * 255 / 219
            index = (y // 2) * (w // 2) + x // 2
            cb = planes[w * h + index] - 128
            cr = planes[w * h + w * h // 4 + index] - 128
            for value in (lum + 1.792741 * cr, lum - .213249 * cb - .532909 * cr, lum + 2.112402 * cb):
                expected.append(min(255, max(0, round(value))))
assert len(expected) == w * h * 3
listener = socket.socket()
listener.bind(("127.0.0.1", 0))
listener.listen()
listener.settimeout(8)
errors, delivered = [], []

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
            def send(payload, op=1):
                raw = json.dumps(payload).encode() if isinstance(payload, dict) else payload
                n = len(raw)
                c.sendall(bytes([128 | op, n if n < 126 else 126 if n < 65536 else 127]) +
                          (b"" if n < 126 else struct.pack("!H", n) if n < 65536 else struct.pack("!Q", n)) + raw)
            request = b""
            while b"\r\n\r\n" not in request:
                request += exact(1)
            assert b"&direct=1" in request and b"&atlas=" not in request
            key = next(line.split(b":", 1)[1].strip() for line in request.split(b"\r\n")
                       if line.lower().startswith(b"sec-websocket-key:"))
            accept = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            send({"t": "hello", "r": "test-reconnect", "foveation": 1, "direct": 1, "gazeSession": "direct-test"})
            begin, seq = time.monotonic(), 0
            deadline = begin + len(packets) / 30 + 2
            while time.monotonic() < deadline:
                if seq < len(packets) and time.monotonic() >= begin + seq / 30:
                    send(packets[seq], 2)
                    seq += 1
                if select.select([c], [], [], .002)[0]:
                    op, size = exact(2)
                    length = size & 127
                    assert size & 128
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
                        send({"t": "pong", "c": m["c"], "a": time.monotonic() * 1e6})
                    if m.get("t") == "fd":
                        delivered.extend(row[0] for row in m["f"])
                if len(packets) in delivered:
                    break
            assert len(packets) in delivered, "Final picture was not uploaded"
            send({"t": "close"})
            try:
                while c.recv(4096):
                    pass
            except (ConnectionError, socket.timeout):
                pass
    except Exception as e:
        errors.append(repr(e))
    finally:
        listener.close()

with tempfile.TemporaryDirectory(prefix="fw-direct-native-") as directory:
    root = Path(directory)
    (root / "url").write_text(f"http://127.0.0.1:{listener.getsockname()[1]}/view?src=test&t=test")
    thread = threading.Thread(target=server)
    thread.start()
    cmd = [str(a.binary.resolve()), "--no-vr", "--foveation", "--conversion", a.conversion,
           "--url-file", str(root / "url"), "--dump", str(root / "out.ppm"),
           "--dump-seq", str(len(packets)), "--state-file", str(root / "state"),
           "--seconds", str(len(packets) / 30 + 3)]
    result = subprocess.run(cmd if a.use_display else ["xvfb-run", "-a", *cmd],
                            capture_output=True, text=True, timeout=len(packets) / 30 + 10)
    thread.join(6)
    assert result.returncode == 0 and not thread.is_alive() and not errors, (result.stderr, errors)
    state = json.loads((root / "state").read_text())
    assert state["errors"] == 0 and state["decoderFormatReopens"] == 0, state
    assert state["foveationActive"] and (state["encodedWidth"], state["encodedHeight"]) == (w, h), state
    with (root / "out.ppm").open("rb") as f:
        assert f.readline() == b"P6\n" and tuple(map(int, f.readline().split())) == (w, h)
        assert f.readline() == b"255\n"
        actual = f.read()
    assert len(actual) == len(expected)
    differences = [abs(x - y) for x, y in zip(actual, expected)]
    maximum, mean = max(differences), sum(differences) / len(differences)
    assert maximum <= (1 if a.conversion == "gpu" else 2), (maximum, mean)
    print(json.dumps({"result": "PASS", "conversion": a.conversion, "packets": len(packets),
                      "dimensions": [w, h], "formatReopens": 0, "errors": 0,
                      "maxRgbDifference": maximum, "meanRgbDifference": mean,
                      "oracle": "independent decoded BT709 video-range equations" if a.conversion == "gpu" else "FFmpeg CLI/libswscale",
                      "maxDifferenceVsFfmpegCli": max(abs(x - y) for x, y in zip(actual, cli_rgb)),
                      "coverage": "actual SCK/VT sequence through direct handshake, native decode and ordinary GL texture"}))
