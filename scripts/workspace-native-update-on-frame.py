#!/usr/bin/env python3
"""Builds and installs the native Workspace viewer on the Frame from the Mac
app's bundled sources (the companion's Update Headset Viewer). Runs on the Frame.

    status                       JSON: installed viewer, running session, update job
    receive                      unpack base64 tar.gz sources from stdin for the next start
    start --identity HEX         check the received sources, then build in a user unit
    worker --job DIR             (the unit) build with -j2, self-test, swap the install

Only the viewer, its dashboard, their input bindings and BUILD-METADATA.json
are replaced, never while a Workspace session runs. The previous build stays
in native-previous/. Poses and other state live outside these files.
"""
import base64
import fcntl
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import time

UID = os.getuid()
DATA = pathlib.Path(os.environ.get("XDG_DATA_HOME") or pathlib.Path.home() / ".local/share") / "frame-workspace"
INSTALL = DATA / "native"
PREVIOUS = DATA / "native-previous"
BASE = pathlib.Path(os.environ.get("XDG_CACHE_HOME") or pathlib.Path.home() / ".cache") / "frame-workspace" / "native-update"
UNIT = f"frame-workspace-native-update-{UID}.service"
DASH_UNIT = f"frame-workspace-dash-{UID}.service"
# Units that mean a Workspace session (not the bar panel or this update).
SESSION_UNITS = ("frame-workspace-host-", "frame-workspace-viewer-", "frame-workspace-panel-")
INSTALLED = ("input/actions.json", "input/bindings_frame_controller.json", "frame-workspace-dash",
             "BUILD-METADATA.json", "frame-workspace-native")  # the viewer last: it is the commit point
ACTIVE = ("queued", "building", "testing", "installing")
MAX_ARCHIVE = 8 * 1024 * 1024


def emit(value):
    print(json.dumps(value, separators=(",", ":"), sort_keys=True))


def write_json(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n")
    os.replace(temporary, path)


def read_json(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def lock(name, blocking=False):
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = open(BASE / name, "a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
    except BlockingIOError:
        handle.close()
        return None
    return handle


def capabilities(binary):
    if not binary.exists():
        return {"present": False}
    try:
        out = subprocess.run([str(binary), "--capabilities"], capture_output=True, text=True, timeout=10)
        reported = json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        reported = None
    meta = read_json(binary.with_name("BUILD-METADATA.json")) or {}
    result = {"present": True, "metadataIdentity": meta.get("source_identity"),
              "sourceRevision": meta.get("source_revision")}
    if not isinstance(reported, dict):
        return {**result, "capabilities": None}
    return {**result, "identity": reported.get("sourceIdentity"), "protocol": reported.get("protocol"),
            "capabilities": reported.get("capabilities")}


def session_units():
    try:
        out = subprocess.run(["systemctl", "--user", "list-units", "--plain", "--no-legend", "--all",
                              "frame-workspace-*"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    units = []
    for line in out.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 3 and fields[0].startswith(SESSION_UNITS) and fields[2] in ("active", "activating", "deactivating", "reloading"):
            units.append(fields[0])
    return units


def job_state():
    job = read_json(BASE / "job.json")
    if isinstance(job, dict) and job.get("state") in ACTIVE:
        # A worker that died (reboot, OOM) without writing an end state.
        try:
            active = subprocess.run(["systemctl", "--user", "is-active", "--quiet", UNIT], timeout=5).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            active = True
        if not active and time.time() - float(job.get("updatedAt") or 0) > 30:
            job = {**job, "state": "failed", "message": "The update stopped without finishing. Try again."}
    return job


def status():
    units = session_units()
    emit({"installed": capabilities(INSTALL / "frame-workspace-native"),
          "sessionActive": True if units is None else bool(units), "sessionUnits": units or [],
          "job": job_state()})


def receive():
    held = lock("sources.lock")
    if held is None:
        raise SystemExit("Another headset viewer update is receiving sources. Try again.")
    raw = base64.b64decode(sys.stdin.read(), validate=False)
    if len(raw) > MAX_ARCHIVE:
        raise SystemExit("The viewer sources are larger than expected.")
    incoming = BASE / "incoming.tmp"
    shutil.rmtree(incoming, ignore_errors=True)
    incoming.mkdir(mode=0o700, parents=True)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive.getmembers():
            parts = pathlib.PurePosixPath(member.name).parts
            if (not member.isfile() or member.name.startswith("/") or ".." in parts or
                    parts[0] not in ("native", "scripts")):
                raise SystemExit("The viewer sources contain an unexpected entry.")
        archive.extractall(incoming, filter="data") if hasattr(tarfile, "data_filter") else archive.extractall(incoming)
    shutil.rmtree(BASE / "incoming", ignore_errors=True)
    os.replace(incoming, BASE / "incoming")
    print("NATIVE_SOURCES_RECEIVED")


def start(identity, revision):
    if not identity or len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
        raise SystemExit("start needs --identity with a 64-character source hash.")
    sources = lock("sources.lock")
    build = lock("build.lock")
    if sources is None or build is None:
        raise SystemExit("A headset viewer update is already running.")
    current = job_state()
    if isinstance(current, dict) and current.get("state") in ACTIVE:
        raise SystemExit("A headset viewer update is already running.")
    units = session_units()
    if units is None or units:
        raise SystemExit("Stop the Workspace before updating the headset viewer.")
    incoming = BASE / "incoming"
    if not (incoming / "scripts" / "native_source_identity.py").exists():
        raise SystemExit("No viewer sources were received.")
    found = subprocess.run([sys.executable, str(incoming / "scripts" / "native_source_identity.py"), str(incoming)],
                           capture_output=True, text=True, timeout=60).stdout.strip()
    if found != identity:
        raise SystemExit("The received viewer sources do not match the Mac app's sources.")
    job = BASE / ("job-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.rmtree(job, ignore_errors=True)
    job.mkdir(mode=0o700, parents=True)
    os.replace(incoming, job / "src")
    for old in BASE.glob("job-*"):
        if old != job:
            shutil.rmtree(old, ignore_errors=True)
    write_json(BASE / "job.json", {"state": "queued", "identity": identity, "revision": revision, "job": str(job),
                                   "message": "Waiting to build.", "startedAt": time.time(), "updatedAt": time.time()})
    subprocess.run(["systemctl", "--user", "reset-failed", UNIT], capture_output=True, timeout=5)
    script = job / "src" / "scripts" / "workspace-native-update-on-frame.py"
    launched = subprocess.run(["systemd-run", "--user", f"--unit={UNIT}", "--collect",
                               "--description=Frame Workspace headset viewer update",
                               "--property=Type=exec", "--property=Nice=10", f"--setenv=HOME={pathlib.Path.home()}",
                               f"--setenv=XDG_DATA_HOME={DATA.parent}", f"--setenv=XDG_CACHE_HOME={BASE.parent.parent}",
                               f"--setenv=PATH={os.environ.get('PATH', '/usr/bin:/bin')}",
                               sys.executable, str(script), "worker", "--job", str(job)],
                              capture_output=True, text=True, timeout=30)
    if launched.returncode != 0:
        write_json(BASE / "job.json", {"state": "failed", "identity": identity, "job": str(job),
                                       "message": "Could not start the update on the Frame.", "updatedAt": time.time()})
        raise SystemExit("Could not start the headset viewer update: " + (launched.stderr or launched.stdout).strip()[-300:])
    print("NATIVE_UPDATE_STARTED")


def worker(job):
    job = pathlib.Path(job)
    held = lock("build.lock", blocking=True)
    state = read_json(BASE / "job.json") or {}
    identity = state.get("identity", "")

    def report(value, message, **extra):
        state.update(state=value, message=message, updatedAt=time.time(), **extra)
        write_json(BASE / "job.json", state)

    log = job / "build.log"
    try:
        report("building", "Building the headset viewer.")
        env = {**os.environ, "FRAME_NATIVE_SOURCE_REVISION": state.get("revision") or ""}
        with log.open("w") as output:
            built = subprocess.run(["bash", str(job / "src" / "scripts" / "build-workspace-native.sh"), "-j2",
                                    str(job / "out")], stdout=output, stderr=subprocess.STDOUT, env=env, timeout=3600)
        if built.returncode != 0:
            raise RuntimeError("The build failed: " + tail(log))
        report("testing", "Testing the new viewer.")
        test = subprocess.run([str(job / "out" / "frame-workspace-native"), "--self-test"],
                              capture_output=True, text=True, timeout=300)
        if test.returncode != 0:
            raise RuntimeError("The new viewer failed its self-test: " + (test.stderr or test.stdout).strip()[-300:])
        built_caps = capabilities(job / "out" / "frame-workspace-native")
        if built_caps.get("identity") != identity:
            raise RuntimeError("The new viewer reports different sources than the Mac app sent.")
        report("installing", "Installing the new viewer.")
        units = session_units()
        if units is None or units:
            raise RuntimeError("A Workspace session started during the update; the new viewer was not installed.")
        install(job)
        subprocess.run(["systemctl", "--user", "stop", DASH_UNIT], capture_output=True, timeout=10)
        installed = capabilities(INSTALL / "frame-workspace-native")
        if installed.get("identity") != identity:
            raise RuntimeError("The installed viewer does not report the new sources.")
        outcome = ("installed", "The headset viewer is up to date.")
    except Exception as exc:  # noqa: BLE001 - the companion shows the reason
        outcome = ("failed", str(exc)[:400])
    # Remove the build before reporting, so a finished update leaves only its log.
    shutil.rmtree(job / "out", ignore_errors=True)
    shutil.rmtree(job / "src", ignore_errors=True)
    report(*outcome, finishedAt=time.time())
    held.close()


def tail(path, lines=6):
    try:
        text = [line for line in path.read_text(errors="replace").splitlines() if line.strip()]
    except OSError:
        return "no build log"
    return " | ".join(text[-lines:])[-400:]


def install(job):
    new = {"frame-workspace-native": job / "out" / "frame-workspace-native",
           "frame-workspace-dash": job / "out" / "frame-workspace-dash",
           "BUILD-METADATA.json": job / "out" / "BUILD-METADATA.json",
           "input/actions.json": job / "src" / "native" / "input" / "actions.json",
           "input/bindings_frame_controller.json": job / "src" / "native" / "input" / "bindings_frame_controller.json"}
    for name, source in new.items():
        if not source.is_file():
            raise RuntimeError(f"The new build is missing {name}.")
    INSTALL.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Keep one previous build to roll back to.
    backup = DATA / "native-previous.tmp"
    shutil.rmtree(backup, ignore_errors=True)
    backup.mkdir(mode=0o700)
    for name in INSTALLED:
        if (INSTALL / name).exists():
            (backup / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(INSTALL / name, backup / name)
    shutil.rmtree(PREVIOUS, ignore_errors=True)
    os.replace(backup, PREVIOUS)
    # Copy onto the same file system first, so each replacement is atomic.
    staged = INSTALL / ".update-staged"
    shutil.rmtree(staged, ignore_errors=True)
    for name in INSTALLED:
        target = staged / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(new[name], target)
        os.chmod(target, 0o755 if name.startswith("frame-workspace-") else 0o644)
    replaced = []
    try:
        for name in INSTALLED:
            (INSTALL / name).parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged / name, INSTALL / name)
            replaced.append(name)
    except OSError:
        for name in replaced:  # put the previous build back
            if (PREVIOUS / name).exists():
                shutil.copy2(PREVIOUS / name, INSTALL / name)
        raise
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def main(args):
    command = args[0] if args else ""
    options = dict(zip(args[1::2], args[2::2]))
    if command == "status":
        status()
    elif command == "receive":
        receive()
    elif command == "start":
        start(options.get("--identity", ""), options.get("--revision", ""))
    elif command == "worker" and "--job" in options:
        worker(options["--job"])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
