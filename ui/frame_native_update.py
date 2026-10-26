"""The Frame's native Workspace viewer: is it the one this Mac app ships, and
Update Headset Viewer, which rebuilds it on the Frame from the bundled sources.

The viewer reports its source identity and capabilities (``--capabilities``).
This side computes the identity its own native sources would produce with the
same file list the build script hashes (scripts/native_source_identity.py).
Spatial Mode needs the roles, surfaces, wall slots and idle auxiliary panels;
an older viewer silently ignored them, so Spatial refuses to start on one.

The update sends the sources over the existing SSH link, and the Frame builds
them (``-j2``), runs the self-test and swaps the installed viewer and
dashboard, keeping the previous build (scripts/workspace-native-update-on-frame.py).
"""
import base64
import importlib.util
import io
import json
import subprocess
import tarfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPDATER = ROOT / "scripts" / "workspace-native-update-on-frame.py"
IDENTITY = ROOT / "scripts" / "native_source_identity.py"
# window-layout (FW-15): wall cells by occupancy, placeholders, user-set poses
# only and Arrange Windows; an older host would stack windows at stale poses.
SPATIAL_CAPABILITIES = {"roles", "surfaces", "wall-slots", "idle-auxiliary", "window-layout"}
PROTOCOL = 2
BASE = '"${XDG_CACHE_HOME:-$HOME/.cache}/frame-workspace/native-update"'
RUNNING = ("sending", "queued", "building", "testing", "installing")
STEP_NAMES = {"sending": "Sending Sources", "queued": "Starting", "building": "Building",
              "testing": "Testing", "installing": "Installing"}


def _identity_module(root=ROOT):
    spec = importlib.util.spec_from_file_location("native_source_identity", Path(root) / "scripts" / "native_source_identity.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source_archive(root=ROOT):
    """The bundled viewer sources plus the Frame-side updater, as a tar.gz."""
    root = Path(root)
    names = _identity_module(root).files(root) + ["scripts/workspace-native-update-on-frame.py"]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=9) as archive:
        for name in sorted(set(names)):
            data = (root / name).read_bytes()
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), 0, 0o755 if name.endswith((".sh", ".py")) else 0o644
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class ViewerError(Exception):
    pass


class HeadsetViewer:
    def __init__(self, run, root=ROOT):
        self.run = run  # the companion's ssh run(remote, stdin=, timeout=)
        self.root = Path(root)
        self.lock = threading.Lock()
        self.status = None   # the last successful check
        self.frame = None    # the Frame that check describes
        self.check_error = None
        self.update = {"state": "idle"}
        self.thread = None
        self._expected = None

    # ---- what this Mac app ships ----

    def expected(self):
        if self._expected is None:
            try:
                self._expected = _identity_module(self.root).identity(self.root)
            except (OSError, ImportError, AttributeError):
                self._expected = ""  # no bundled sources: nothing to compare or send
        return self._expected or None

    def revision(self):
        try:
            out = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"], capture_output=True,
                                 text=True, timeout=5)
            return out.stdout.strip() if out.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    # ---- what the Frame has ----

    def probe(self, frame):
        """Ask the Frame which viewer is installed. Raises ViewerError if it can't say."""
        try:
            out = self.run("python3 - status", stdin=UPDATER.read_text(encoding="utf-8"), timeout=20)
            lines = [line for line in (out or "").splitlines() if line.startswith("{")]
            data = json.loads(lines[-1])
            installed = data["installed"]
        except Exception as exc:  # noqa: BLE001 - unreachable, or an unexpected reply
            with self.lock:
                self.check_error = str(exc)[:200]
                if frame != self.frame:  # another headset: the last answer isn't about this one
                    self.status, self.frame = None, None
            raise ViewerError(f"Could not check the headset viewer: {exc}") from exc
        caps = installed.get("capabilities")
        protocol = installed.get("protocol")
        expected = self.expected()
        status = {
            "present": installed.get("present") is True,
            "identity": installed.get("identity") if isinstance(installed.get("identity"), str) else None,
            "expected": expected,
            "protocol": protocol if type(protocol) is int else None,
            "capabilities": [c for c in caps if isinstance(c, str)] if isinstance(caps, list) else None,
            "sessionActive": data.get("sessionActive") is not False,
            "job": data.get("job") if isinstance(data.get("job"), dict) else None,
            "checkedAt": time.time(),
        }
        status["spatial"] = (status["capabilities"] is not None and SPATIAL_CAPABILITIES <= set(status["capabilities"])
                             and (status["protocol"] or 0) >= PROTOCOL)
        status["matches"] = bool(expected) and status["identity"] == expected
        with self.lock:
            self.status, self.frame, self.check_error = status, frame, None
        return status

    def check(self, frame, force=False):
        """On connect (a new Frame) or when asked; never raises."""
        if not force and frame == self.frame and self.status is not None:
            return self.status
        try:
            return self.probe(frame)
        except ViewerError:
            return None

    def require_spatial(self, frame):
        """Before Spatial Mode changes anything on the Mac."""
        if self.updating():
            raise ViewerError("The headset viewer is updating. Start again when it finishes.")
        status = self.probe(frame)
        if not status["present"]:
            raise ViewerError("Install the native window host on the Frame before starting Spatial Mode. "
                              "Use Update Headset Viewer on the Mac page.")
        if not status["spatial"]:
            raise ViewerError("The headset viewer is too old for Spatial Mode. Use Update Headset Viewer on the "
                              "Mac page, then start again.")
        return status

    # ---- Update Headset Viewer ----

    def updating(self):
        with self.lock:
            return self.update.get("state") in RUNNING

    def start_update(self, frame, workspace_idle):
        """Start the update in the background. `workspace_idle` is the Mac side's
        own view; the Frame is asked too, and checks again before installing."""
        expected = self.expected()
        if not expected:
            raise ViewerError("This copy of the Mac app has no headset viewer sources to send.")
        with self.lock:
            if self.update.get("state") in RUNNING:
                raise ViewerError("The headset viewer is already updating.")
        if not workspace_idle:
            raise ViewerError("Stop the Workspace before updating the headset viewer.")
        status = self.probe(frame)
        if status["sessionActive"]:
            raise ViewerError("Stop the Workspace before updating the headset viewer.")
        if status["job"] and status["job"].get("state") in RUNNING:
            raise ViewerError("The headset viewer is already updating.")
        payload = base64.b64encode(source_archive(self.root)).decode()
        with self.lock:
            if self.update.get("state") in RUNNING:
                raise ViewerError("The headset viewer is already updating.")
            self.update = {"state": "sending", "message": "Sending the viewer sources.", "identity": expected,
                           "startedAt": time.time()}
        self.thread = threading.Thread(target=self._update, args=(frame, payload, expected), daemon=True)
        self.thread.start()
        return self.state()

    def _set_update(self, **values):
        with self.lock:
            self.update = {**self.update, **values, "updatedAt": time.time()}

    def _update(self, frame, payload, expected, poll=3, limit=3600):
        try:
            self.run("set -eu; umask 077; base=" + BASE + "; mkdir -p \"$base\"; "
                     "cat > \"$base/updater.py.tmp\" && mv -f \"$base/updater.py.tmp\" \"$base/updater.py\"",
                     stdin=UPDATER.read_text(encoding="utf-8"), timeout=30)
            out = self.run("python3 " + BASE + "/updater.py receive", stdin=payload, timeout=180)
            if "NATIVE_SOURCES_RECEIVED" not in (out or ""):
                raise ViewerError("The Frame did not confirm it received the viewer sources.")
            out = self.run("python3 " + BASE + "/updater.py start --identity " + expected +
                           " --revision '" + "".join(c for c in self.revision() if c.isalnum()) + "'", timeout=60)
            if "NATIVE_UPDATE_STARTED" not in (out or ""):
                raise ViewerError("The Frame did not start the update.")
            self._set_update(state="queued", message="Waiting to build.")
            deadline = time.monotonic() + limit
            while time.monotonic() < deadline:
                time.sleep(poll)
                try:
                    job = self.probe(frame).get("job") or {}
                except ViewerError:
                    continue  # a dropped link: the build carries on in its own unit
                state = job.get("state")
                if job.get("identity") not in (None, expected):
                    continue
                if state in ("queued", "building", "testing", "installing"):
                    self._set_update(state=state, message=str(job.get("message") or "")[:200])
                elif state == "installed":
                    status = self.status or {}
                    if status.get("identity") != expected:
                        raise ViewerError("The Frame finished, but its viewer still reports other sources.")
                    self._set_update(state="installed", message="The headset viewer is up to date.",
                                     finishedAt=time.time())
                    return
                elif state == "failed":
                    raise ViewerError(str(job.get("message") or "The update failed on the Frame.")[:300])
            raise ViewerError("The update did not finish within an hour.")
        except Exception as exc:  # noqa: BLE001 - shown on the Mac page
            message = str(exc).removeprefix("Could not check the headset viewer: ")
            self._set_update(state="failed", message=message[:300], finishedAt=time.time())

    # ---- for the Mac page ----

    def state(self):
        with self.lock:
            status, update, error = self.status, dict(self.update), self.check_error
        result = {"expected": self.expected(), "update": update}
        if update.get("state") in RUNNING:
            result.update(state="updating", label="Updating · " + STEP_NAMES.get(update["state"], "Working") + "…")
        elif status is None:
            result.update(state="unknown", label="Not Checked" if not error else "Couldn't Check")
        elif not status["present"]:
            result.update(state="missing", label="Not Installed")
        elif not status["spatial"]:
            result.update(state="incompatible", label="Too Old For Spatial Mode")
        elif not status["matches"]:
            result.update(state="outdated", label="Different From This Mac App")
        else:
            result.update(state="current", label="Up To Date")
        if status is not None:
            result.update(installed=status["identity"], sessionActive=status["sessionActive"],
                          checkedAt=status["checkedAt"])
        result["canUpdate"] = bool(self.expected()) and result["state"] in ("missing", "incompatible", "outdated", "unknown")
        if update.get("state") == "failed":
            result["canUpdate"] = bool(self.expected())
        return result
