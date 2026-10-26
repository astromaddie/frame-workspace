#!/usr/bin/env python3
"""Local production host integration; hidden GL, synthetic streams, no OpenVR.
Usage: python3 native/tests_host.py PATH/TO/frame-workspace-native
Do not use this test on an in-use headset. Temporary sources/state are removed.
"""
import base64
import hashlib
import json
import pathlib
import queue
import select
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time


def atomic_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    temporary.chmod(0o600)
    temporary.replace(path)


def send(connection, payload, opcode=1):
    header = bytes([0x80 | opcode])
    n = len(payload)
    header += (bytes([n]) if n < 126 else b"\x7e" + struct.pack("!H", n)
               if n < 65536 else b"\x7f" + struct.pack("!Q", n))
    connection.sendall(header + payload)


def receive(connection):
    def exact(size):
        data = b""
        while len(data) < size:
            part = connection.recv(size - len(data))
            if not part:
                raise EOFError()
            data += part
        return data
    first, length = exact(2)
    masked = length & 128
    length &= 127
    if length == 126:
        length = struct.unpack("!H", exact(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", exact(8))[0]
    assert masked, "viewer must mask WebSocket client frames"
    mask, data = exact(4), exact(length)
    return first & 15, bytes(value ^ mask[i % 4] for i, value in enumerate(data))


class Stream:
    # idle: like an empty menu slot, the helper sends a hidden surface and
    # `still`, and no picture. silent: connected, with no surface message.
    def __init__(self, encoded, idle=False, silent=False):
        self.encoded = encoded
        self.idle, self.silent = idle or silent, silent
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.commands = queue.Queue()
        self.stopped = threading.Event()
        self.connections = 0
        self.messages = queue.Queue()
        self.seq = 0
        self.failures = []
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                with connection:
                    connection.settimeout(1)
                    request = b""
                    while b"\r\n\r\n" not in request:
                        data = connection.recv(4096)
                        if not data:
                            raise EOFError()
                        request += data
                    headers = dict(line.split(": ", 1) for line in request.decode().split("\r\n")[1:] if ": " in line)
                    key = next(v for k, v in headers.items() if k.lower() == "sec-websocket-key")
                    accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                    connection.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                                        "Sec-WebSocket-Accept: " + accept + "\r\n\r\n").encode())
                    self.connections += 1
                    send(connection, b'{"t":"hello","r":"test-reconnect"}')
                    live, seq, last = not self.idle, 0, 0
                    if self.idle and not self.silent:
                        send(connection, b'{"t":"surface","v":1,"seq":1,"visible":false}')
                    if self.idle:
                        send(connection, b'{"t":"still"}')
                    while not self.stopped.is_set():
                        try:
                            command = self.commands.get_nowait()
                        except queue.Empty:
                            command = None
                        forced = None
                        if isinstance(command, tuple):
                            if command[0] == "pause":
                                live = False
                                command[1].set()
                            else:
                                forced = command[1]
                        elif isinstance(command, dict):
                            send(connection, json.dumps(command).encode())
                        elif command:
                            send(connection, json.dumps({"t": command}).encode())
                            if command in ("still", "live"):
                                live = command == "live"
                            if command == "close":
                                break
                        now = time.monotonic()
                        if forced is not None or (live and now - last >= 1 / 20):
                            seq = forced if forced is not None else seq + 1
                            self.seq = seq
                            packet = b"\x01" + b"\x00" * 8 + struct.pack("!I", seq) + b"\x00" * 4 + self.encoded
                            send(connection, packet, 2)
                            last = now
                        if select.select([connection], [], [], .005)[0]:
                            opcode, payload = receive(connection)
                            if opcode == 8:
                                break
                            message = json.loads(payload)
                            self.messages.put(message)
                            if message.get("t") == "ping":
                                send(connection, json.dumps({"t": "pong", "c": message["c"],
                                                             "a": time.monotonic() * 1e6}).encode())
            except (EOFError, ConnectionError, OSError):
                pass
            except Exception as error:
                self.failures.append(repr(error))

    def finish(self):
        self.stopped.set()
        self.listener.close()
        self.thread.join(3)
        assert not self.thread.is_alive(), "test stream did not stop"
        assert not self.failures, self.failures


def encode(color, width, height):
    return subprocess.check_output([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s={width}x{height}:r=20",
        "-frames:v", "1", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-pix_fmt", "yuv420p", "-f", "h264", "pipe:1"])


def run(binary):
    streams = [Stream(encode("red", 320, 180)), Stream(encode("blue", 400, 240))]
    try:
        with tempfile.TemporaryDirectory(prefix="fw-host-test-") as temporary:
            root = pathlib.Path(temporary)
            specs, requests = root / "specs", root / "requests"
            specs.mkdir(); requests.mkdir()
            panels = {}
            for name, stream in zip(("a", "b"), streams):
                folder = root / name
                folder.mkdir()
                url = folder / "view.url"
                url.write_text(f"http://127.0.0.1:{stream.port}/view?src=window%3A1&t=local")
                panels[name] = {"version": 1, "generation": "first-" + name,
                                "urlFile": str(url), "overlayKey": "test.panel." + name, "overlayName": name,
                                "controlFile": str(folder / "control.json"), "stateFile": str(folder / "state.json"),
                                "poseFile": str(folder / "pose.json"), "requestDir": str(requests),
                                "decoder": "software", "conversion": "gpu" if name == "b" else "cpu",
                                "slot": 0 if name == "a" else 8}
            with (root / "viewer.log").open("w+") as log:
                process = subprocess.Popen([str(binary), "--host-dir", str(specs), "--state-file", str(root / "host.json"),
                                            "--no-vr", "--action-manifest", "none", "--seconds", "60",
                                            "--spatial-state", str(root / "spatial-ui.json")],
                                           stdout=log, stderr=log)
                def state(name):
                    try:
                        return json.loads((root / name / "state.json").read_text())
                    except (OSError, ValueError):
                        return {}
                def wait(predicate, label):
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        assert process.poll() is None, "host exited during " + label
                        value = predicate()
                        if value:
                            return value
                        time.sleep(.03)
                    raise AssertionError("timed out: " + label)
                def ready(name, generation=None):
                    s = state(name)
                    return s if (s.get("ready") is True and s.get("hostGeneration") ==
                                 (generation or panels[name]["generation"])) else None
                try:
                    atomic_json(specs / "a.json", panels["a"])
                    a = wait(lambda: ready("a"), "first panel ready")
                    assert a["hostPid"] == process.pid and a["textureWidth"] == 320
                    assert a["wallSlot"] == 0 and abs(a["placement"]["width"] - .64) < 1e-5
                    assert not (root / "a/pose.json").exists(), "automatic wall pose is not a remembered hand pose"
                    atomic_json(specs / "b.json", panels["b"])
                    b = wait(lambda: ready("b"), "second panel ready")
                    assert b["hostPid"] == process.pid and b["textureWidth"] == 400
                    assert b["conversionActive"] == "gpu"
                    assert b["wallSlot"] == 8 and b["placement"]["matrix"][3] < -1
                    assert b["placement"]["matrix"][7] < 0, "later slot joins lower left wall"
                    assert not (root / "b/pose.json").exists()
                    # Invalid slot types never replace the active generation.
                    atomic_json(specs / "b.json", {**panels["b"], "generation": "bad-slot", "slot": True})
                    time.sleep(.2)
                    assert ready("b") and streams[1].connections == 1
                    atomic_json(specs / "b.json", panels["b"])
                    spatial = {"mode": "spatial", "apps": [
                        {"bundleID": "test.app", "name": "Terminal", "icon": "", "running": True,
                         "windows": [{"key": "test.panel.a", "title": "One"},
                                     {"key": "test.panel.b", "title": "Two"}]}],
                        "switcher": {"open": False, "index": 1, "seq": 8, "chosen": "test.panel.b"}}
                    spatial["apps"].append({"bundleID": "test.extra", "name": "Additional Windows",
                        "windows": [{"key": "test.extra." + str(i), "title": "A Window Title " * 18}
                                    for i in range(80)]})
                    assert len(json.dumps(spatial)) > 16384, "inventory exceeds small control-file limit"
                    spatial["switcher"].update(open=True, chosen=None, seq=7)
                    atomic_json(root / "spatial-ui.json", spatial)
                    def host_state():
                        return json.loads((root / "host.json").read_text())
                    wait(lambda: host_state().get("switcherOpen") and host_state().get("spatialTiles", 0) >= 3,
                         "live thumbnail strip uses running panel textures")
                    spatial["switcher"].update(open=False, chosen="test.panel.b", seq=8)
                    atomic_json(root / "spatial-ui.json", spatial)
                    def switches():
                        return [json.loads(p.read_text()) for p in sorted(requests.glob("spatial-*.json"))]
                    wait(lambda: len(switches()) == 1, "closed switcher chosen event")
                    assert switches() == [{"action": "switch", "key": "test.panel.b"}]
                    assert all(p.stat().st_mode & 0o777 == 0o600 for p in requests.glob("spatial-*.json"))
                    spatial["apps"][0]["name"] = "Renamed"
                    atomic_json(root / "spatial-ui.json", spatial)
                    time.sleep(.25)
                    assert len(switches()) == 1, "same chosen seq must not replay after state update"
                    spatial["switcher"].update(seq=9, chosen="test.panel.a", open=True, index=0)
                    atomic_json(root / "spatial-ui.json", spatial)
                    wait(lambda: len(switches()) == 2, "next exact key selection")
                    assert switches()[-1]["key"] == "test.panel.a"
                    wait(lambda: state("a").get("wallSlot") == -1, "recenter leaves wall")
                    release = [json.loads(p.read_text()) for p in requests.glob("wall-*.json")]
                    assert any(r.get("key") == "test.panel.a" and r.get("generation") == "first-a" for r in release)
                    spatial["switcher"].update(seq=10, chosen="missing.panel", open=False)
                    atomic_json(root / "spatial-ui.json", spatial)
                    time.sleep(.2)
                    assert len(switches()) == 2, "unavailable key must be ignored"
                    spatial["mode"] = "desktop"
                    spatial["switcher"].update(seq=11, chosen="test.panel.b")
                    atomic_json(root / "spatial-ui.json", spatial)
                    time.sleep(.2)
                    assert len(switches()) == 2, "desktop ignores spatial selection"
                    streams[0].commands.put("still")
                    a = wait(lambda: state("a") if state("a").get("streamStill") and
                             state("a").get("decoderSessionOpen") is False else None, "still releases decoder")
                    assert a["ready"] and a["decoderStillReleases"] == 1 and a["textureWidth"] == 320
                    uploads, other_uploads = a["uploaded"], state("b")["uploaded"]
                    wait(lambda: state("b").get("uploaded", 0) >= other_uploads + 10, "other panel continues while still")
                    assert state("a")["uploaded"] == uploads, "held picture must not be resubmitted each frame"
                    atomic_json(root / "a" / "control.json", {"revision": 1, "move": [.1, 0, 0],
                                                              "bend": {"vertical": True, "a": .1, "b": .3}})
                    wait(lambda: state("a").get("controlRevision") == 1, "independent still-panel controls")
                    pose = json.loads((root / "a" / "pose.json").read_text())
                    assert abs(pose["matrix"][3] - .1) < 1e-5 and pose["bend"]["vertical"]
                    bent_uploads, moving_uploads = state("a")["uploaded"], state("b")["uploaded"]
                    wait(lambda: state("b").get("uploaded", 0) > moving_uploads + 3, "other panel moves after still bend")
                    assert state("a")["uploaded"] == bent_uploads, "bent held picture must restage only once"
                    streams[0].commands.put("live")
                    a = wait(lambda: state("a") if state("a").get("decoderLiveOpens") == 1 and
                             state("a").get("uploaded", 0) > uploads else None, "live resumes keyframe")
                    assert not a["streamStill"] and a["decoderSessionOpen"] and a["errors"] == 0
                    # One panel's invalid URL produces a terminal generation; it
                    # never restarts the host, decoder, or another window.
                    bad = {**panels["a"], "generation": "bad", "urlFile": str(root / "missing.url"),
                           "overlayKey": "test.panel.bad", "stateFile": str(root / "bad-state.json"),
                           "controlFile": str(root / "bad-control.json"), "poseFile": str(root / "bad-pose.json")}
                    atomic_json(specs / "bad.json", bad)
                    def bad_state():
                        try:
                            return json.loads((root / "bad-state.json").read_text())
                        except (OSError, ValueError):
                            return {}
                    wait(lambda: bad_state().get("state") == "error", "failed stream isolation")
                    bad_time = (root / "bad-state.json").stat().st_mtime_ns
                    before = state("b")["uploaded"]
                    wait(lambda: state("b").get("uploaded", 0) > before + 5, "healthy stream survives failure")
                    assert (root / "bad-state.json").stat().st_mtime_ns == bad_time, "terminal spec must not restart"
                    (specs / "a.json").unlink()
                    wait(lambda: state("a").get("state") == "stopped", "individual panel removal")
                    assert ready("b"), "removing a panel must keep the other ready"
                    # Match the launcher: clear generation-scoped controls only
                    # after the prior owner has acknowledged its removal.
                    (root / "a" / "control.json").unlink()
                    # Re-add the same key, consuming the saved moved/bent pose.
                    replacement = {**panels["a"], "generation": "replacement"}
                    atomic_json(specs / "a.json", replacement)
                    a = wait(lambda: ready("a", "replacement"), "panel replacement")
                    assert abs(a["placement"]["matrix"][3] - .1) < 1e-5
                    assert a["placement"]["bend"]["vertical"] and a["hostPid"] == process.pid
                    assert a["wallSlot"] == -1, "saved pose takes precedence over spec slot"
                    streams[1].commands.put("close")
                    wait(lambda: state("b").get("exitReason") == "server-close", "server closes one panel")
                    assert ready("a", "replacement"), "server close must keep other panel alive"
                    for path in specs.glob("*.json"):
                        path.unlink()
                    wait(lambda: state("a").get("state") == "stopped", "last panel removal")
                    spatial["mode"] = "spatial"
                    spatial["switcher"].update(open=False, chosen=None, seq=12)
                    atomic_json(root / "spatial-ui.json", spatial)
                    wait(lambda: host_state().get("spatialMode") and host_state().get("runningPanels") == 0,
                         "empty host retains spatial app shelf")
                    process.terminate()
                    assert process.wait(5) == 0
                    log.seek(0)
                    output = log.read()
                    assert output.count("[native] GL vendor=") == 1, "host must create exactly one GL context"
                    assert streams[0].connections == 2 and streams[1].connections == 1
                    print("host integration passed: one GL context, two streams, still/live, controls/pose, add/remove/re-add, failure and close isolation, spatial live strip/chosen/seq/desktop/empty-host")
                except Exception:
                    log.seek(0)
                    print(log.read(), file=sys.stderr)
                    raise
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(5)
    finally:
        for stream in streams:
            stream.finish()


def run_surfaces(binary):
    roles = {"window": "window", "menubar": "menubar", "status": "status", "menu0": "menu", "menu1": "menu"}
    encoded = encode("gray", 400, 120)
    streams = {name: Stream(encoded) for name in roles}
    try:
        with tempfile.TemporaryDirectory(prefix="fw-surfaces-") as temporary:
            root = pathlib.Path(temporary)
            specs, requests = root / "specs", root / "requests"
            specs.mkdir(); requests.mkdir()
            for name, role in roles.items():
                folder = root / name; folder.mkdir()
                (folder / "url").write_text(f"http://127.0.0.1:{streams[name].port}/view?src={name}&t=local")
                atomic_json(specs / (name + ".json"), {
                    "version": 1, "role": role, "generation": "test", "urlFile": str(folder / "url"),
                    "overlayKey": "test." + name, "overlayName": name,
                    "controlFile": str(folder / "control.json"), "stateFile": str(folder / "state.json"),
                    "poseFile": str(folder / "pose.json"), "requestDir": str(requests)})
            spatial = {"mode": "spatial", "apps": [{"bundleID": "test", "name": "Fixture",
                "windows": [{"key": "test.window", "title": "Window"}]}],
                "switcher": {"seq": 1, "open": False}}
            atomic_json(root / "spatial.json", spatial)
            with (root / "viewer.log").open("w+") as log:
                process = subprocess.Popen([str(binary), "--host-dir", str(specs), "--no-vr",
                    "--action-manifest", "none", "--state-file", str(root / "host.json"),
                    "--spatial-state", str(root / "spatial.json"), "--seconds", "60"], stdout=log, stderr=log)
                def state(name):
                    try: return json.loads((root / name / "state.json").read_text())
                    except (OSError, ValueError): return {}
                def wait(predicate, label):
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        assert process.poll() is None, "host exited: " + label
                        result = predicate()
                        if result: return result
                        time.sleep(.025)
                    raise AssertionError("timed out: " + label)
                def metadata(name, seq, visible=True, anchor="menubar", hot=False, **extra):
                    message = {"t": "surface", "v": 1, "seq": seq, "visible": visible,
                        "rect": [20, 36, 200, 120] if name.startswith("menu") and name != "menubar" else [0, 0, 400, 36],
                        "anchorRect": [0, 0, 400, 36], "anchor": anchor, "hot": hot, "menusWidth": 400}
                    streams[name].commands.put({**message, **extra})
                try:
                    wait(lambda: all(state(name).get("ready") for name in roles), "five independent uploads")
                    assert all(not state(name)["surfaceVisible"] for name in roles if name != "window")
                    spatial["switcher"].update(seq=2, chosen="test.window")
                    atomic_json(root / "spatial.json", spatial)
                    def focused_message():
                        messages = []
                        while not streams["window"].messages.empty():
                            messages.append(streams["window"].messages.get_nowait())
                        return next((m for m in messages if m.get("t") == "focus"), None)
                    focus = wait(focused_message, "focus snap sends stream message")
                    assert focus == {"t": "focus", "u": .5, "v": .5}
                    wait(lambda: list(requests.glob("spatial-*.json")), "focus selection keeps companion request")
                    for name in roles:
                        if name == "window": continue
                        paused = threading.Event()
                        streams[name].commands.put(("pause", paused))
                        assert paused.wait(2)
                        wait(lambda: state(name).get("uploadedSeq") == streams[name].seq, "upload before metadata")
                        fence = streams[name].seq + 10
                        metadata(name, fence, anchor="status" if name == "menu1" else "menubar")
                        revision = wait(lambda: state(name).get("surfaceRevision", 0), "surface parsed")
                        assert not state(name)["surfaceVisible"], "old held picture stays hidden"
                        streams[name].commands.put(("frame", fence))
                        wait(lambda: state(name).get("uploadedSeq") == fence, "equal sequence uploaded")
                        assert not state(name)["surfaceVisible"], "equal sequence is stale"
                        streams[name].commands.put(("frame", fence+1))
                        wait(lambda: state(name).get("surfaceVisible"), "newer upload reveals surface")
                        assert state(name)["role"] == roles[name]
                        metadata(name, fence, anchor="status" if name == "menu1" else "menubar", hot=True)
                        wait(lambda: state(name).get("surfaceRevision", 0) > revision, "same-sequence hot update")
                        assert state(name)["surfaceVisible"], "hot-only update retains held picture"
                        assert not (root / name / "pose.json").exists(), "no auxiliary saved pose"
                        old_revision = state(name)["surfaceRevision"]
                        streams[name].commands.put({"t": "surface", "v": 1, "seq": fence-1, "visible": False})
                        time.sleep(1.1)
                        assert state(name)["surfaceRevision"] == old_revision, "older metadata ignored"
                        metadata(name, fence+1, visible=False)
                        wait(lambda: not state(name).get("surfaceVisible"), "hide immediately masks picture")
                        metadata(name, fence+1, anchor="status" if name == "menu1" else "menubar")
                        time.sleep(1.1)
                        assert not state(name)["surfaceVisible"], "re-show needs a fresh upload"
                        streams[name].commands.put(("frame", fence+2))
                        wait(lambda: state(name).get("surfaceVisible"), "fresh re-show")
                    before = state("menubar")["placement"]
                    atomic_json(root / "menubar/control.json", {"revision": 9, "move": [1,0,0], "focus": True, "recenter": True})
                    wait(lambda: state("menubar").get("controlRevision") == 9, "auxiliary ignores pose controls")
                    assert state("menubar")["placement"] == before
                    spatial["switcher"].update(seq=3, chosen="test.menubar")
                    atomic_json(root / "spatial.json", spatial)
                    time.sleep(1.1)
                    assert len(list(requests.glob("spatial-*.json"))) == 1, "auxiliary never a focus/switch target"
                    # Closing every window does not end the auxiliary-owned wall session.
                    def host(): return json.loads((root / "host.json").read_text())
                    wait(lambda: host().get("wallSession") == "test" and host().get("wallAnchored"), "session anchor")
                    window_spec = json.loads((specs / "window.json").read_text())
                    (specs / "window.json").unlink()
                    wait(lambda: state("window").get("state") == "stopped", "empty window wall")
                    assert host()["wallSession"] == "test" and host()["wallAnchored"]
                    atomic_json(specs / "window.json", {**window_spec, "generation": "later"})
                    wait(lambda: state("window").get("hostGeneration") == "later" and state("window").get("ready"), "later window")
                    assert host()["wallSession"] == "test"
                    # A new Spatial session resets even with a retained Desktop window.
                    menu_spec = json.loads((specs / "menubar.json").read_text())
                    atomic_json(specs / "menubar.json", {**menu_spec, "generation": "next-session"})
                    wait(lambda: host().get("wallSession") == "next-session", "new session anchor")
                    spatial["mode"] = "desktop"
                    atomic_json(root / "spatial.json", spatial)
                    wait(lambda: all(not state(n).get("surfaceVisible") for n in roles if n != "window"), "desktop hides auxiliaries")
                    assert state("window")["ready"], "desktop window path continues"
                    process.terminate(); assert process.wait(5) == 0
                    print("surface integration passed: four roles across five streams; show/hide/equal/newer/older/re-show/hot gating; focus coordinates/request; no pose/control/focus target; desktop isolation")
                except Exception:
                    log.seek(0); print(log.read(), file=sys.stderr); raise
                finally:
                    if process.poll() is None: process.terminate(); process.wait(5)
    finally:
        for stream in streams.values(): stream.finish()


def run_idle_auxiliary(binary):
    """Idle menu slots (no picture) register and never hold up window panels."""
    idle = {"menu0": Stream(b"", idle=True), "menu1": Stream(b"", idle=True),
            "menu2": Stream(b"", idle=True), "menu3": Stream(b"", silent=True)}
    window = Stream(encode("red", 320, 180))
    menubar = Stream(encode("gray", 400, 36))
    streams = {**idle, "window": window, "menubar": menubar}
    try:
        with tempfile.TemporaryDirectory(prefix="fw-idle-aux-") as temporary:
            root = pathlib.Path(temporary)
            specs, requests = root / "specs", root / "requests"
            specs.mkdir(); requests.mkdir()
            def spec(name, role, **extra):
                folder = root / name; folder.mkdir(exist_ok=True)
                (folder / "url").write_text(f"http://127.0.0.1:{streams[name].port}/view?src={name}&t=local")
                return {"version": 1, "role": role, "generation": "session", "urlFile": str(folder / "url"),
                        "overlayKey": "test." + name, "overlayName": name,
                        "controlFile": str(folder / "control.json"), "stateFile": str(folder / "state.json"),
                        "poseFile": str(folder / "pose.json"), "requestDir": str(requests), **extra}
            atomic_json(root / "spatial.json", {"mode": "spatial", "apps": [], "switcher": {"seq": 1, "open": False}})
            with (root / "viewer.log").open("w+") as log:
                process = subprocess.Popen([str(binary), "--host-dir", str(specs), "--no-vr",
                    "--action-manifest", "none", "--state-file", str(root / "host.json"),
                    "--spatial-state", str(root / "spatial.json"), "--seconds", "60"], stdout=log, stderr=log)
                def state(name):
                    try: return json.loads((root / name / "state.json").read_text())
                    except (OSError, ValueError): return {}
                def host():
                    try: return json.loads((root / "host.json").read_text())
                    except (OSError, ValueError): return {}
                def wait(predicate, label):
                    deadline = time.monotonic() + 8
                    while time.monotonic() < deadline:
                        assert process.poll() is None, "host exited: " + label
                        result = predicate()
                        if result: return result
                        time.sleep(.025)
                    raise AssertionError("timed out: " + label)
                try:
                    for name in idle:
                        atomic_json(specs / (name + ".json"), spec(name, "menu"))
                    for name in ("menu0", "menu1", "menu2"):
                        wait(lambda: state(name).get("registered") is True, name + " registers without a picture")
                        s = state(name)
                        assert s["ready"] is False and s["textureWidth"] == 0 and s["state"] == "idle", s
                        assert s["surfaceVisible"] is False and s["role"] == "menu"
                    assert idle["menu3"].connections == 1
                    assert state("menu3").get("registered") is False, "no surface message yet: not registered"
                    # A window launched while every menu slot idles still shows.
                    atomic_json(specs / "window.json", spec("window", "window", slot=0))
                    w = wait(lambda: state("window").get("ready") and state("window"), "window ready beside idle menus")
                    assert w["registered"] is True and w["textureWidth"] == 320 and w["wallSlot"] == 0
                    placement = w["placement"]
                    assert host().get("wallSession") == "", host()
                    # Menu Bar after the windows adopts their wall, not a new one.
                    atomic_json(specs / "menubar.json", spec("menubar", "menubar"))
                    wait(lambda: state("menubar").get("ready"), "menu bar ready")
                    assert host()["wallSession"] == "session" and host()["wallAnchored"]
                    assert state("window")["wallSlot"] == 0 and state("window")["placement"] == placement
                    assert all(state(n).get("registered") for n in ("menu0", "menu1", "menu2"))
                    process.terminate(); assert process.wait(5) == 0
                    print("idle auxiliary integration passed: idle menu slots register with no picture, a slot "
                          "with no surface message does not, windows launch beside them, Menu Bar adopts the wall")
                except Exception:
                    log.seek(0); print(log.read(), file=sys.stderr); raise
                finally:
                    if process.poll() is None: process.terminate(); process.wait(5)
    finally:
        for stream in streams.values(): stream.finish()


if __name__ == "__main__":
    binary = pathlib.Path(sys.argv[1]).resolve()
    run_idle_auxiliary(binary)
    run(binary)
    run_surfaces(binary)
