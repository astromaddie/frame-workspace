#!/usr/bin/env python3
"""Frame Control: a small local web UI for managing the Steam Frame from a computer.

Stdlib only; runs on macOS, Linux and Windows (differences live in frame_host.py).
Listens on 127.0.0.1 and talks to the headset through the `frame` SSH alias set
up by scripts/connect.sh or ui/frame_connect.py, or another headset picked on the
Devices tab: frame_link.py finds it at one of its addresses (frame_devices.py).

Usage: ui/server.py [--port 47810] [--exit-on-eof]   (normally started by the app)
Env:   FRAME_ALIAS (default frame)
       FRAME_LOCAL=1   run on the Frame itself (the iPhone app starts it there over SSH)
       FRAME_UI_KEY    required X-Frame-UI value (the iPhone app passes a fresh one)
       FRAME_DEVICE    what to call the device the page runs on (e.g. iPhone)
       FRAME_CLIENT    a stable id for that device (keyboard-and-trackpad pairing is kept per id)
"""
import argparse
import base64
import contextlib
import hashlib
import http.client
import json
import os
import queue
import re
import secrets
import shlex
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

# The app runs Python with -I, which leaves the script's own folder off
# sys.path, so add it for the sibling modules below.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import frame_agent  # noqa: E402
import frame_assistant  # noqa: E402
import frame_android  # noqa: E402
from apk_sources import search as apk_search, SourceError  # noqa: E402
from apk_sources import fdroid as apk_fdroid  # noqa: E402
import frame_apk_versions  # noqa: E402
import frame_catalog  # noqa: E402
import frame_devices  # noqa: E402
import frame_steamgriddb
import frame_comfort  # noqa: E402
import frame_contact  # noqa: E402
import frame_host  # noqa: E402
import frame_link  # noqa: E402
import frame_macview  # noqa: E402
import frame_media  # noqa: E402
import frame_panels  # noqa: E402
import frame_quest  # noqa: E402
import frame_meta  # noqa: E402
import frame_report  # noqa: E402
import frame_store  # noqa: E402
import frame_telemetry  # noqa: E402
import frame_titles  # noqa: E402
import frame_webinstall  # noqa: E402
import frame_vr  # noqa: E402
import frame_utilities  # noqa: E402
import frame_compat_db  # noqa: E402

frame_host.trust_bundled_cas()

HERE = Path(__file__).resolve().parent
# On the Frame itself, every `ssh frame COMMAND` the server and its helpers run
# goes to local-bin/ssh, which runs COMMAND here instead, so one code path serves
# both. Nothing listens beyond 127.0.0.1; the phone reaches it through SSH.
LOCAL = os.environ.get("FRAME_LOCAL") == "1"
if LOCAL:
    os.environ["PATH"] = f"{HERE / 'local-bin'}{os.pathsep}{os.environ.get('PATH', '')}"
UI_KEY = os.environ.get("FRAME_UI_KEY") or "1"
DEVICE = os.environ.get("FRAME_DEVICE") or "phone"
# What the Frame's KDE Connect calls this device (keyboard and trackpad).
INPUT_NAME = DEVICE if LOCAL else socket.gethostname().split(".")[0]
FRAME = os.environ.get("FRAME_ALIAS", "frame")
FRAME_FROM_ENV = "FRAME_ALIAS" in os.environ
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", FRAME):
    sys.exit(f"FRAME_ALIAS must be a plain host alias, not {FRAME!r}")
# Reuse one SSH connection for the frequent status/screenshot calls, where ssh
# supports it (not on Windows: there every command connects on its own).
# A private server (the MCP adapter starts one per session) keeps its own SSH masters, and
# uses the headsets without editing them.
PRIVATE = os.environ.get("FRAME_PRIVATE_SSH") == "1"
CONTROL = None if LOCAL else frame_host.control_path(private=PRIVATE)
# The ControlPath itself is per headset: the connector puts it in HOST_OPTS.
MUX = ["ssh", "-o", "BatchMode=yes"]
MUX_BASE = list(MUX)
# Commands use the master when it's up and connect directly when it isn't.
SSH_TAIL = [*(["-o", "ControlMaster=no"] if CONTROL else []), "-o", "ConnectTimeout=5"]
SSH = [*MUX, *SSH_TAIL]
# The address the connector picked (-o HostName=... and friends), in every ssh command.
HOST_OPTS = []

# Android helpers share the multiplexed connection when it's up.
frame_android.SSH_OPTS = SSH[1:]


_route_lock = threading.Lock()


def route(alias, host_opts):
    """Point every ssh, scp and rsync at `alias` with `host_opts` (frame_link calls this
    when it picks a headset and an address). The lists change in place, so code holding
    them follows; frame_titles reads frame_android.SSH_OPTS at call time."""
    global FRAME, HOST_OPTS
    with _route_lock:
        moved = alias != FRAME
        if moved:
            frame_catalog._env.clear()  # the SteamOS and Lepton builds reports record are per headset
        FRAME = frame_android.FRAME = alias
        HOST_OPTS = list(host_opts)
        MUX[:] = [*MUX_BASE, *HOST_OPTS]
        SSH[:] = [*MUX, *SSH_TAIL]
        frame_android.SSH_OPTS = SSH[1:]
    # Long-lived ssh processes started on the old route (outside the lock: they take their own).
    mv = globals().get("macview")
    if mv:
        mv.retarget(alias, host_opts)  # its tunnel is its own ssh: it must follow the headset too
    agent = globals().get("_input")
    if moved and agent:
        agent.stop()  # typing and pointing mustn't go on reaching the headset switched away from


LINK = None  # the connector (frame_link.Link); None on the Frame itself

# Installs and other changes in progress. Switching headsets waits for them: they
# read the ssh settings step by step, so a switch could send the rest (or a failed
# install's clean-up) to the other headset.
_work_lock = threading.Lock()
_work = [0]
NOT_HEADSET_WORK = {"/api/devices", "/api/contact", "/api/contact/prompt", "/api/queue"}


@contextlib.contextmanager
def working(meant=None):
    """Counts as running work. `meant`: the headset the page made this change for; if the
    app has switched away from it, refuse (checked together with counting, so a switch
    can't slip in between)."""
    with _work_lock:
        if LINK and meant and meant != LINK.active_device()["id"]:
            raise Failure("Frame Control switched headsets; try again on this one", 409)
        _work[0] += 1
    try:
        yield
    finally:
        with _work_lock:
            _work[0] -= 1


def busy_thread(fn, *args):
    """A thread that counts as work from before it starts until it ends."""
    with _work_lock:
        _work[0] += 1

    def run():
        try:
            fn(*args)
        finally:
            with _work_lock:
                _work[0] -= 1
    return threading.Thread(target=run, daemon=True)

APPID = re.compile(r"^\d{1,10}$")
FLATPAK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+){2,}$")
MAX_UPLOAD = 8 * 1024**3
MAX_JSON = 1024**2

# gamescope writes the PNG asynchronously; wait until its size stops changing.
SCREENSHOT = r"""
set -eu
f=$(mktemp /tmp/frame-ui-XXXXXX.png)
trap 'rm -f "$f"' EXIT
XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=gamescope-0 gamescopectl screenshot "$f" >/dev/null 2>&1
last=-1
for i in $(seq 1 50); do
  sleep 0.1
  size=$(stat -c %s "$f" 2>/dev/null || echo 0)
  if [ "$size" -gt 0 ] && [ "$size" = "$last" ]; then cat "$f"; exit 0; fi
  last=$size
done
echo "gamescope did not write a screenshot" >&2
exit 1
"""


class Failure(Exception):
    def __init__(self, message, status=502, apk=None):
        super().__init__(message)
        self.status = status
        self.apk = apk


# What ssh prints when it never reached the Frame, and what to tell the user
# instead. Only ssh's own wording is matched, so a command that ran on the Frame
# and failed keeps its real error.
UNREACHABLE = [
    (re.compile(r"Could not resolve hostname"),
     "Can't find the Frame on the network. Check it's on and connected, or run Set Up Connection."),
    (re.compile(r"port \d+: (Operation timed out|Connection timed out|Host is down|No route to host|"
                r"Network is unreachable)"),
     "The Frame isn't answering. It may be asleep, switched off, or on another network."),
    (re.compile(r"[Tt]imed out talking to "),
     "The Frame took too long to answer. It may be asleep or busy; try again."),
    (re.compile(r"port \d+: Connection refused"),
     "The Frame refused the connection. Check Developer Mode is still on."),
    (re.compile(r"Permission denied \(publickey"),
     "The Frame didn't accept this computer's SSH key. Set Up Connection pairs this computer with it "
     "again (Developer Mode must be on)."),
    # Before "Host key verification failed", which ssh prints after each of these.
    (re.compile(r"No \S+ host key is known for "),
     "The Frame's SSH identity isn't saved yet. Reconnecting saves it; or check and trust it on the Devices tab."),
    (re.compile(r"REMOTE HOST IDENTIFICATION HAS CHANGED|Host key verification failed"),
     "The Frame's SSH identity has changed: after SteamOS was reinstalled or reset, or a different device "
     "answering at its address. Check the new key on the Devices tab before trusting it."),
    (re.compile(r"kex_exchange_identification|Connection closed by .* port \d+|Connection reset by .* port \d+"),
     "The connection to the Frame dropped. Try again."),
]


def unreachable(message):
    """The plain-language reason the Frame couldn't be reached, or None if it was."""
    for pattern, friendly in UNREACHABLE:
        if pattern.search(message):
            return friendly
    return None


def error_body(message):
    """A JSON error body and status; SSH connection failures become one clear offline message."""
    friendly = unreachable(message)
    if friendly:
        return {"error": friendly, "offline": True, "detail": message}, 503
    return {"error": message}, None


# ---- Background jobs: installs that can outlast a request ------------------
#
# Flatpak and Android installs can take many minutes. The request starts the
# work and returns a job id at once; the page polls /api/job for the outcome.

JOB_TTL = 3600
_jobs_lock = threading.Lock()
_jobs = {}  # id -> {"label", "done", "error", "message", "result", "time"}


_backfill = {"running": False, "last": 0.0}
_backfill_lock = threading.Lock()


def backfill_art(apps=(), titles=()):
    """Apply art Frame Control couldn't at install time (Steam wasn't running), once Steam is up.

    Only entries marked art_pending at install; it fills empty Steam slots and never replaces art or
    names the user may have customised. Runs in the background, at most once every five minutes."""
    pkgs = [a["package"] for a in apps if a.get("art_pending")]
    gids = [t["id"] for t in titles if t.get("art_pending")]
    with _backfill_lock:
        if not (pkgs or gids) or _backfill["running"] or time.time() - _backfill["last"] < 300:
            return False
        _backfill.update(running=True, last=time.time())

    def run():
        try:
            frame_android.shortcut_tool("list")  # Steam isn't up: try again on a later listing
            for refresh, key in [(frame_android.refresh_art, p) for p in pkgs] + \
                                [(frame_titles.refresh_art, g) for g in gids]:
                try:
                    refresh(key, fill_only=True)
                except Exception as e:
                    print(f"artwork backfill for {key}: {e}", file=sys.stderr)
        except Exception:
            pass
        finally:
            with _backfill_lock:
                _backfill["running"] = False
    threading.Thread(target=run, daemon=True).start()
    return True


def _new_job_locked(label, **fields):
    now = time.time()
    for j in [j for j, v in _jobs.items() if v["done"] and now - v["time"] > JOB_TTL]:
        del _jobs[j]
    job = secrets.token_hex(8)
    _jobs[job] = {"label": label, "done": False, "error": None, "message": None, "result": None, "time": now, **fields}
    return job


def _run_job(job, label, work, progress):
    def report(stage, percent=None):
        with _jobs_lock:
            _jobs[job].update(stage=stage, percent=percent)

    fields = {}
    try:
        result = work(report) if progress else work()
        fields = {"message": result.get("message") or f"{label}: done", "result": result}
    except (Failure, frame_android.FrameError) as e:
        fields = {"error": unreachable(str(e)) or str(e)}
        frame_telemetry.diagnostic(f"job {label.split()[0]}", e)
    except SourceError as e:  # already user-readable, and about a store, not the Frame
        fields = {"error": str(e)}
    except Exception as e:
        fields = {"error": f"{type(e).__name__}: {e}"}
        frame_telemetry.diagnostic(f"job {label.split()[0]}", e)
    finally:
        with _jobs_lock:
            _jobs[job].update(fields, done=True, time=time.time())


def start_job(label, work, progress=False):
    """Run work() in the background. It returns a dict with a "message"."""
    with _jobs_lock:
        job = _new_job_locked(label)
    busy_thread(_run_job, job, label, work, progress).start()
    return {"message": f"{label}…", "job": job}


# ---- The install queue: Android games and apps, one after another ----------
#
# Installs from the Android store, Import from my Quest and My Meta library each copy
# a game into a Lepton instance, and each refuses to start while another is running.
# Queued, the page can line up several: one worker runs them in order, each as a
# normal job (/api/job) that reads "queued" until its turn. A queued install can be
# removed until it starts; each runs on the headset it was queued for, or not at all.

_queue = []  # [job, label, work, progress, headset id], in order; the running one is not here
_queue_state = {"worker": False, "running": None}


def _restage_locked():
    ahead = 1 if _queue_state["running"] else 0
    for i, (job, *_) in enumerate(_queue):
        n = ahead + i
        _jobs[job]["stage"] = f"Queued: {n} install{'' if n == 1 else 's'} before it" if n else "Queued: next"


def queue_job(label, work, progress=False):
    """Like start_job, but runs after every install queued before it."""
    device = LINK.active_device()["id"] if LINK else None
    with _jobs_lock:
        job = _new_job_locked(label, queued=True, stage=None, percent=None)
        _queue.append([job, label, work, progress, device])
        _restage_locked()
        ahead = len(_queue) - 1 + (1 if _queue_state["running"] else 0)
        start, _queue_state["worker"] = not _queue_state["worker"], True
    if start:
        threading.Thread(target=_drain_queue, daemon=True).start()
    return {"message": f"{label}: queued, {ahead} before it" if ahead else f"{label}…", "job": job, "queued": ahead}


def _drain_queue():
    while True:
        with _jobs_lock:
            if not _queue:
                _queue_state.update(worker=False, running=None)
                return
            job, label, work, progress, device = _queue.pop(0)
            _queue_state["running"] = job
            _jobs[job].update(queued=False, stage=None)
            _restage_locked()

        def guarded(*report, work=work, device=device):
            with working(device):  # refused if the app switched headsets since it was queued
                return work(*report)
        _run_job(job, label, guarded, progress)


def install_queue():
    """The running install and those waiting, in order."""
    with _jobs_lock:
        ids = ([_queue_state["running"]] if _queue_state["running"] else []) + [q[0] for q in _queue]
        return {"jobs": [{"job": j, "label": _jobs[j]["label"], "queued": bool(_jobs[j].get("queued")),
                          "stage": _jobs[j].get("stage"), "percent": _jobs[j].get("percent")}
                         for j in ids if j in _jobs]}


def queue_action(body):
    """{"action": "remove", "job"}: take a waiting install out of the queue."""
    if body.get("action") != "remove":
        raise Failure("unknown action", 400)
    job = body.get("job")
    with _jobs_lock:
        for i, q in enumerate(_queue):
            if q[0] == job:
                del _queue[i]
                _jobs[job].update(done=True, queued=False, cancelled=True, stage=None, time=time.time(),
                                  error="Removed from the install queue")
                _restage_locked()
                return {"message": f"Removed {_jobs[job]['label']} from the install queue"}
    raise Failure("that install isn't waiting in the queue (it may have started or finished)", 409)


def job_status(query):
    with _jobs_lock:
        job = _jobs.get((parse_qs(query).get("id") or [""])[0])
        snapshot = job and {k: v for k, v in job.items() if k != "time"}
    if not snapshot:
        raise Failure("no such job (the app may have restarted)", 404)
    return snapshot


def ensure_master():
    """Make sure the connection to the headset is up, or being tried (frame_link.Link.ensure)."""
    if LINK:
        LINK.ensure()


def ssh(remote, *, stdin=None, timeout=30, text=True):
    route_gen = LINK.gen if LINK else None  # which headset this command is for
    try:
        ensure_master()
        # Never let ssh inherit our stdin: under the app it's the pipe held open for
        # --exit-on-eof, and Windows' ssh.exe waits on it forever.
        feed = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        r = frame_host.run_ssh([*SSH, FRAME, remote], capture_output=True, **feed,
                               text=text, errors="replace" if text else None, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise frame_host.link_failure(Failure(f"Timed out talking to {FRAME}"))
    if r.returncode != 0:
        err = (r.stderr or r.stdout) if text else (r.stderr or r.stdout).decode(errors="replace")
        if r.returncode == 255 and repair_ssh_config(err):
            return ssh(remote, stdin=stdin, timeout=timeout, text=text)
        if r.returncode == 255 and LINK and unreachable(err):
            LINK.lost(err, route_gen)  # ssh itself failed: the connector reconnects
        failure = Failure(strip_ansi(err).strip() or f"ssh exited {r.returncode}")
        failure.stdout = r.stdout if text else r.stdout.decode(errors="replace")
        # ssh never reached the Frame (see frame_telemetry.diagnostic): judged on ssh's stderr only,
        # never on the command's output.
        stderr = (r.stderr if text else (r.stderr or b"").decode(errors="replace")) or ""
        if frame_host.ssh_link_failed(r.returncode, strip_ansi(stderr)):
            frame_host.link_failure(failure)
        raise failure
    return r.stdout


_config_repaired = False


def repair_ssh_config(err):
    """Windows' OpenSSH refused ~/.ssh/config for its ACL: give the file a private one,
    once per run. -> True if it did, so the command is worth retrying."""
    global _config_repaired
    if _config_repaired or not frame_host.WINDOWS or frame_host.BAD_PERMISSIONS not in err:
        return False
    # ssh doubles the backslashes: "C:\\Users\\me/.ssh/config"
    named = re.sub(r"[\\/]+", "/", err.split(frame_host.BAD_PERMISSIONS, 1)[1].splitlines()[0].strip())
    config = frame_devices.ssh_config()
    if not named.lower().endswith("/" + config.name.lower()):
        return False  # a key or another file: not ours to rewrite
    _config_repaired = True
    try:
        if frame_devices.repair_permissions(config):
            print(f"Gave {config} a private ACL: ssh refused it ({named})", file=sys.stderr)
            return True
    except OSError as e:
        print(f"Couldn't repair {config}'s permissions: {e}", file=sys.stderr)
    return False


def strip_ansi(s):
    return re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\r", "", s)


def terminal(argv):
    """Open a terminal window running argv (for anything needing a password)."""
    try:
        return frame_host.open_terminal(argv)
    except frame_host.HostError as e:
        raise Failure(str(e), 500)


# ---- actions ---------------------------------------------------------------

def status(_body):
    s = json.loads(ssh("python3 -", stdin=(HERE / "frame_status.py").read_text(), timeout=20))
    osr = s.get("os") if isinstance(s, dict) else None
    if isinstance(osr, dict):
        # The headset's hash is for usage statistics only; the page and MCP clients don't get it.
        frame_telemetry.frame_seen(osr.get("build"), osr.get("version"), osr.pop("headset", None),
                                   len(LINK.reg.devices()) if LINK else None)
        frame_report.frame.update(build=osr.get("build"), version=osr.get("version"))
    return s


COMFORT_FILES = ("frame_comfort.py", "frame_status.py", "frame_steam.py")
COMFORT_MISSING = {"comfort": "missing"}  # what COMFORT_RUN prints when the cached copy isn't complete
# Runs frame_comfort.py from ~/.cache/frame-control/comfort/<version>, first writing any of
# `files` that aren't there (none: just check they all are). Private, and each file written
# whole or not at all, so a copy cut short is finished by the next call.
COMFORT_RUN = """import json, os, pathlib, subprocess, sys
os.umask(0o077)
files = %r
root = pathlib.Path.home() / '.cache/frame-control/comfort' / %r
if files:
    root.mkdir(parents=True, exist_ok=True)
for name, source in files.items():
    path = root / name
    if not path.exists():
        tmp = root / (name + '.' + str(os.getpid()))
        tmp.write_text(source)
        tmp.replace(path)
if not all((root / name).is_file() for name in %r):
    print('{"comfort": "missing"}')
    sys.exit()
r = subprocess.run([sys.executable, str(root / 'frame_comfort.py'), %r], capture_output=True, text=True)
print(r.stdout, end='')
"""


def comfort(body):
    try:
        frame_comfort.validate(body)
    except ValueError as e:
        raise Failure(str(e), 400)
    # Content-addressed, user-only helper bundle. Desktop and phone use the same
    # on-headset state/lock; no listener, service registration or third-party app.
    # The page polls this every few seconds, so run the copy already on the headset and
    # send the sources only when that version isn't there yet.
    files = {name: (HERE / name).read_text() for name in COMFORT_FILES}
    version = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:16]
    out = json.loads(ssh("python3 -", stdin=COMFORT_RUN % ({}, version, COMFORT_FILES, json.dumps(body)), timeout=65))
    if out == COMFORT_MISSING:
        out = json.loads(ssh("python3 -", stdin=COMFORT_RUN % (files, version, COMFORT_FILES, json.dumps(body)),
                             timeout=65))
    if out.get("error") and "active" not in out:
        raise Failure(out["error"], 409)
    return out


def headset_view():
    """Both eyes as SteamVR composites them (see frame_vrshot.py); PNG bytes."""
    # Counts as work: the copy and clean-up must reach the headset that took the capture.
    with working():
        return _headset_view()


def _headset_view():
    # `timeout`: VR_Init can block if SteamVR is restarting.
    out = ssh("timeout 15 python3 -", stdin=(HERE / "frame_vrshot.py").read_text(), timeout=30)
    # SteamVR prints its own notices (e.g. about vrwebhelper) on stdout too, so
    # take the last line that is our result object.
    result = {"error": out.strip() or "no output"}
    for line in out.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and ("path" in obj or "error" in obj):
            result = obj
    if "error" in result:
        raise Failure(str(result["error"]))
    path = str(result["path"])
    if not re.fullmatch(r"/tmp/frame-vrcap/shot-\d+-vr\.png", path):
        raise Failure(f"unexpected capture path {path!r}")
    # Captures show whatever was on screen, so delete even if the copy fails.
    try:
        return ssh(f"cat {path}; rc=$?; rm -f {path}; exit $rc", timeout=20, text=False)
    finally:
        try:
            ssh(f"rm -f {path}", timeout=10)
        except Failure:
            pass  # frame_vrshot.py sweeps leftovers on the next capture


# Screenshots taken in the headset with Steam's shortcut. Steam files each under the app
# it was taken in: userdata/<account>/760/remote/<appid>/screenshots/<file>,
# with a smaller copy in screenshots/thumbnails/. A shot's id is
# "<account>/<appid>/<file>", checked here before it goes near a shell.
SHOT_ROOT = ".local/share/Steam/userdata"
SHOT_ID = re.compile(r"(\d{1,12})/(\d{1,20})/(\d{14}_\d{1,4}\.(?:jpg|png))")
SHOTS_DIR = Path.home() / "Pictures" / "SteamFrame"
LIST_SHOTS = f"""cd ~/{SHOT_ROOT} 2>/dev/null || exit 0
find . -mindepth 6 -maxdepth 6 -path './*/760/remote/*/screenshots/*' -type f \\
  \\( -name '*.jpg' -o -name '*.png' \\) -printf '%P\\t%s\\t%T@\\n'"""


def shot_path(shot_id, thumb=False):
    m = SHOT_ID.fullmatch(shot_id) if isinstance(shot_id, str) else None
    if not m:
        raise Failure("bad screenshot id", 400)
    return f"{SHOT_ROOT}/{m[1]}/760/remote/{m[2]}/screenshots/{'thumbnails/' if thumb else ''}{m[3]}"


def list_shots():
    shots = []
    for line in ssh(LIST_SHOTS, timeout=20).splitlines():
        rel, _, rest = line.partition("\t")
        parts = rel.split("/")  # account/760/remote/appid/screenshots/file
        size, _, mtime = rest.partition("\t")
        shot_id = f"{parts[0]}/{parts[3]}/{parts[-1]}" if len(parts) == 6 else ""
        if not SHOT_ID.fullmatch(shot_id) or not size.isdigit():
            continue
        try:
            when = float(mtime)
        except ValueError:
            continue
        local = SHOTS_DIR / parts[-1]
        shots.append({"id": shot_id, "appid": parts[3], "file": parts[-1], "size": int(size), "time": when,
                      "saved": local.exists() and local.stat().st_size == int(size)})
    shots.sort(key=lambda s: s["time"], reverse=True)
    return {"shots": shots, "folder": str(SHOTS_DIR)}


def shot_image(query):
    q = parse_qs(query)
    shot_id = (q.get("id") or [""])[0]
    full = shot_path(shot_id)
    if q.get("thumb") == ["1"]:
        # Steam writes the thumbnail a moment after the shot; fall back to the full image.
        thumb = shot_path(shot_id, thumb=True)
        remote = f"if [ -s {thumb} ]; then cat {thumb}; else cat {full}; fi"
    else:
        remote = f"cat {full}"
    ctype = "image/png" if shot_id.endswith(".png") else "image/jpeg"
    return ssh(remote, timeout=30, text=False), ctype


def save_shots(body):
    """Copy screenshots to ~/Pictures/SteamFrame, skipping ones already there."""
    ids = body.get("ids")
    if not isinstance(ids, list) or not 0 < len(ids) <= 1000:
        raise Failure("ids must be a list of 1-1000 screenshot ids", 400)
    paths = [shot_path(i) for i in ids]
    todo = [p for p in paths if not (SHOTS_DIR / p.rsplit("/", 1)[-1]).exists()]
    if todo:
        SHOTS_DIR.mkdir(parents=True, exist_ok=True)
        ensure_master()
        # Copy into a hidden folder and move complete files in, so a cut-off
        # copy never looks saved. -p keeps the time the shot was taken.
        incoming = Path(tempfile.mkdtemp(prefix=".incoming-", dir=SHOTS_DIR))
        try:
            try:
                r = frame_host.run_ssh(["scp", "-p", *SSH[1:], *(f"{FRAME}:{p}" for p in todo), str(incoming)],
                                       capture_output=True, stdin=subprocess.DEVNULL, text=True, timeout=300)
            except subprocess.TimeoutExpired:
                raise Failure("Copying screenshots timed out")
            if r.returncode != 0:
                raise Failure(strip_ansi(r.stderr).strip() or f"scp exited {r.returncode}")
            for f in incoming.iterdir():
                os.replace(f, SHOTS_DIR / f.name)
        finally:
            shutil.rmtree(incoming, ignore_errors=True)
    n, skipped = len(todo), len(ids) - len(todo)
    msg = f"Saved {n} screenshot{'s' * (n != 1)} to ~/Pictures/SteamFrame"
    return {"message": msg + (f" ({skipped} already there)" if skipped else ""), "saved": n}


# Live video of the headset view. SteamVR's steamvr-v4l2cam.service copies the
# headset view (one undistorted 1920x1080 image) into the v4l2loopback device
# /dev/video99. ffmpeg encodes it with x264 (the hardware encoder crashes
# ffmpeg) and the raw H.264 comes back over SSH for the page to decode with
# WebCodecs. An access unit delimiter starts every frame so the page can split
# the stream, and repeated SPS/PPS let it start at any keyframe. ffmpeg runs in
# the background while the shell waits for our stdin to close: when the local
# ssh goes, the channel closes and the shell kills ffmpeg, even one that has
# stopped writing (and so would never get SIGPIPE).
STREAM_DEVICE = "/dev/video99"
STREAM_HEIGHTS = (720, 1080)
STREAM_FPS = (30, 60)
STREAM_STALL = 10  # seconds without video before the stream is dropped
_stream_lock = threading.Lock()
_stream_proc = None


def stream_command(query):
    """The ffmpeg that streams H.264: the headset view, or one panel's own window (src=panel)."""
    q = parse_qs(query)
    try:
        height = int((q.get("h") or ["720"])[0])
        fps = int((q.get("fps") or ["30"])[0])
    except ValueError:
        raise Failure("h and fps must be integers", 400)
    if height not in STREAM_HEIGHTS or fps not in STREAM_FPS:
        raise Failure(f"h must be one of {STREAM_HEIGHTS} and fps one of {STREAM_FPS}", 400)
    rate = 3 if height == 720 else 6  # Mbit/s
    if q.get("src") == ["panel"]:
        # The panel's own pixels (x11grab of its window: gamescope keeps them), fitted
        # to the height asked for. It stays still however the wearer moves their head.
        window, display = panel_target(q)
        return (f"DISPLAY={display} ffmpeg -hide_banner -loglevel error -nostdin -f x11grab -framerate {fps} "
                f"-window_id {window} -i {display} "
                f"-vf \"scale=-2:'trunc(min({height},ih)/2)*2',format=yuv420p\" -c:v libx264 -preset ultrafast "
                f"-tune zerolatency -g {fps * 2} -bf 0 -b:v {rate}M -maxrate {rate}M -bufsize {rate // 2 or 1}M "
                f"-x264-params aud=1:repeat-headers=1 -f h264 - & p=$!; "
                f"exec >&-; cat >/dev/null; kill $p 2>/dev/null; wait $p")
    return (f"[ -e {STREAM_DEVICE} ] || {{ echo 'No headset view device ({STREAM_DEVICE}). Is SteamVR running?' >&2; exit 3; }}; "
            f"ffmpeg -hide_banner -loglevel error -nostdin -f v4l2 -video_size 1920x1080 -i {STREAM_DEVICE} "
            f"-vf fps={fps},scale=-2:{height},format=yuv420p -c:v libx264 -preset ultrafast -tune zerolatency "
            f"-g {fps * 2} -bf 0 -b:v {rate}M -maxrate {rate}M -bufsize {rate // 2 or 1}M "
            f"-x264-params aud=1:repeat-headers=1 -f h264 - & p=$!; "
            f"exec >&-; cat >/dev/null; kill $p 2>/dev/null; wait $p")


def launch(body):
    appid = str(body.get("appid", ""))
    if not APPID.match(appid):
        raise Failure("bad appid", 400)
    ssh(f"steam steam://rungameid/{appid} >/dev/null 2>&1 &")
    return {"message": f"Launching {appid}"}


def steam_frame(*args, timeout=40):
    """Run frame_steam.py on the Frame (it drives the Steam client) and return its JSON."""
    try:
        out = ssh("python3 - " + " ".join(map(shlex.quote, args)),
                  stdin=(HERE / "frame_steam.py").read_text(), timeout=timeout)
    except Failure as e:
        # frame_steam.py prints {"error": ...} on stdout when it fails, but ssh()
        # reports stderr instead if there was any, so look in both.
        for line in [*reversed(getattr(e, "stdout", "").splitlines()), *reversed(str(e).splitlines())]:
            try:
                raise Failure(json.loads(line)["error"]) from None
            except (ValueError, KeyError, TypeError):
                continue
        raise
    return json.loads(out)


def steam(body):
    """Steam games: install an owned game, or open its store page in the headset."""
    appid, action = str(body.get("appid", "")), body.get("action")
    if not APPID.match(appid):
        raise Failure("bad appid", 400)
    if action not in ("install", "store"):
        raise Failure("action must be install or store", 400)
    if action == "store":
        return steam_frame(action, appid)
    # Starts Steam's download; Steam reports the rest in the headset.
    try:
        res = steam_frame(action, appid)
    except Failure as e:
        frame_telemetry.install_finished("steam", False, error=e, steam_appid=appid)
        raise
    frame_telemetry.install_finished("steam", True, steam_appid=appid)
    return res


def vr(body):
    try:
        action = frame_vr.validate(body)
    except ValueError as e:
        raise Failure(str(e), 400)
    sources = {name: (HERE / (name + '.py')).read_text() for name in ('frame_status', 'frame_vr')}
    script = "import sys, types, json, os\n"
    for name, source in sources.items():
        script += f"m = types.ModuleType({name!r}); sys.modules[{name!r}] = m\nexec({source!r}, m.__dict__)\n"
    # Only the optional HUD needs files: its terminal child survives this SSH call.
    if action == 'hud-start':
        script += "m.ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)\n"
        for name, source in sources.items():
            script += f"p = m.ROOT / {name + '.py'!r}; t = p.with_suffix('.' + str(os.getpid()) + '.tmp')\nt.write_text({source!r}); os.replace(t, p)\n"
    script += f"try:\n print(json.dumps(m.dispatch({body!r})))\nexcept Exception as e:\n print(json.dumps({{'error': str(e)}}))\n"
    result = json.loads(ssh('python3 -', stdin=script, timeout=20))
    if result.get('error'):
        raise Failure(result['error'])
    return result


def steam_search(query):
    q = parse_qs(query)
    cc = (q.get("cc") or [""])[0].upper()
    if not re.fullmatch(r"[A-Z]{2}", cc):
        raise Failure("cc must be a two-letter country code", 400)
    try:
        return {"results": frame_store.search((q.get("q") or [""])[0], cc)}
    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException) as e:
        raise Failure(f"Steam store search failed: {e}")


def set_volume(body):
    # Validate everything before touching the headset.
    level = None
    if "level" in body:
        value = body["level"]
        if type(value) not in (int, float) or not 0 <= value <= 1:
            raise Failure("level must be 0..1", 400)
        level = float(value)
    if not body or set(body) - {"level", "muted"}:
        raise Failure("Provide level or muted", 400)
    if "muted" in body and type(body["muted"]) is not bool:
        raise Failure("muted must be true or false", 400)
    if "muted" in body:
        ssh(f"wpctl set-mute @DEFAULT_AUDIO_SINK@ {1 if body['muted'] else 0}")
    if level is not None:
        ssh(f"wpctl set-volume @DEFAULT_AUDIO_SINK@ {level:.2f}")
    return {"message": "Volume updated"}


# Runs on the Frame, clipboard text on stdin. Verified 2026-09-25 (SteamOS 0.3.0
# vr, build 20260922): the headset desktop is a nested Plasma Wayland session
# inside gamescope with its own D-Bus bus, and wl-copy/xclip are not installed.
# Klipper (org.kde.klipper, served by plasmashell) is reachable with qdbus6, so
# borrow plasmashell's bus address. Same as scripts/paste-to-frame.sh.
PASTE = r"""set -u
text=$(cat; printf x); text=${text%x}
pid=$(pgrep -u "$(id -u)" -x plasmashell | head -n 1)
if [ -z "$pid" ]; then
  echo "The headset's desktop isn't open (plasmashell is not running). Open the desktop in the headset, then try again." >&2
  exit 2
fi
bus=$(tr '\0' '\n' < "/proc/$pid/environ" | sed -n 's/^DBUS_SESSION_BUS_ADDRESS=//p')
if DBUS_SESSION_BUS_ADDRESS=$bus qdbus6 org.kde.klipper /klipper \
     org.kde.klipper.klipper.setClipboardContents "$text" >/dev/null; then
  echo "copied via Klipper (${#text} chars)"
else
  echo "Klipper call failed (bus: ${bus:-none})" >&2
  exit 2
fi
"""
DESKTOP_CLOSED = "plasmashell is not running"  # what PASTE says when the headset's desktop isn't open
# base64 keeps the script intact through every local shell's quoting rules.
PASTE_CMD = 'bash -c "$(echo %s | base64 -d)"' % base64.b64encode(PASTE.encode()).decode()


def clipboard(body):
    if body.get("fromMac") or body.get("fromComputer"):
        try:
            text = frame_host.clipboard_text()
        except frame_host.HostError as e:
            raise Failure(str(e), 500)
        if not text:
            raise Failure("The clipboard is empty (or holds something other than text)", 400)
    else:
        text = body.get("text")
        if not isinstance(text, str) or not text:
            raise Failure("nothing to send", 400)
    try:
        return {"message": ssh(PASTE_CMD, stdin=text, timeout=30).strip()}
    except Failure as e:
        if DESKTOP_CLOSED in str(e):
            # The headset's state, for the person to change, not a fault: a 409 isn't reported as one.
            raise Failure(str(e), 409) from None
        raise


# ---- keyboard and pointer (KDE Connect on the Frame, see frame_input_agent.py) ----

INPUT_FLAGS = ("singleclick", "doubleclick", "middleclick", "rightclick", "singlehold", "singlerelease",
               "scroll", "ctrl", "alt", "shift", "super")
INPUT_MOVE_LIMIT = 2000  # pixels per event
INPUT_TEXT_LIMIT = 500  # characters per event
INPUT_BATCH_LIMIT = 200  # events per request


def input_event(event):
    """A KDE Connect remote-input body with only the fields it knows, in range."""
    if not isinstance(event, dict):
        raise Failure("each input event must be an object", 400)
    out = {}
    for name in ("dx", "dy"):
        value = event.get(name)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            raise Failure(f"{name} must be a number", 400)
        out[name] = max(-INPUT_MOVE_LIMIT, min(INPUT_MOVE_LIMIT, round(float(value), 2)))
    for name in INPUT_FLAGS:
        if event.get(name) is True:
            out[name] = True
    key = event.get("key")
    if key is not None:
        if not isinstance(key, str) or not 0 < len(key) <= INPUT_TEXT_LIMIT:
            raise Failure(f"key must be text of 1 to {INPUT_TEXT_LIMIT} characters", 400)
        out["key"] = key
    special = event.get("specialKey")
    if special is not None:
        # KDE Connect's numbering: 1 Backspace … 14 Escape, 21-32 F1-F12.
        if isinstance(special, bool) or not isinstance(special, int) or not 1 <= special <= 32:
            raise Failure("specialKey must be a whole number from 1 to 32", 400)
        out["specialKey"] = special
    if not set(out) - {"ctrl", "alt", "shift", "super"}:
        raise Failure("input event has nothing to do", 400)
    return out


def input_client():
    """A stable id for this device, so KDE Connect on the Frame keeps its pairing apart.

    The iPhone app passes one (FRAME_CLIENT). A computer makes one the first time
    and keeps it: host names alone can clash (desk.home and desk.office).
    """
    if os.environ.get("FRAME_CLIENT"):
        return os.environ["FRAME_CLIENT"]
    if LOCAL:
        return DEVICE
    path = frame_host.data_dir("input-client-id")
    try:
        saved = path.read_text().strip()
        if re.fullmatch(r"[A-Za-z0-9_-]{4,64}", saved):
            return saved
    except OSError:
        pass
    made = (re.sub(r"[^A-Za-z0-9-]", "", INPUT_NAME)[:24] or "computer") + "-" + secrets.token_hex(4)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(made)
    except OSError:
        pass  # still works this time; it pairs again next time
    return made


# KDE Connect for the Frame, as Frame Control ships it (frame/kdeconnect/NOTICE.md).
KDECONNECT = HERE.parent / "frame" / "kdeconnect"
KDECONNECT_HOME = ".local/share/frame-control/kdeconnect"


def kdeconnect_packages():
    """[(file, sha256), ...] from frame/kdeconnect/packages.json; [] if it's missing."""
    try:
        manifest = json.loads((KDECONNECT / "packages.json").read_text())
        return [(p["file"], p["sha256"]) for p in manifest["packages"]]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def kdeconnect_stamp(packages):
    """What the agent writes once these are unpacked (frame_input_agent.stamp)."""
    return "".join(f"{sha}  {name}\n" for name, sha in packages)


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class InputAgent:
    """frame_input_agent.py running on the Frame, fed events over one long-lived ssh.

    Before starting it, copies KDE Connect to the Frame if it isn't there yet. The
    agent unpacks it, pairs, and reports its state ({"state": "off" | "installing" |
    "starting" | "pairing" | "ready" | "error"}).
    """

    def __init__(self, source=HERE / "frame_input_agent.py", packages=None):
        self.source, self.proc, self.lock, self.write_lock = source, None, threading.Lock(), threading.Lock()
        self.packages = kdeconnect_packages() if packages is None else packages
        # generation counts stop()s; launching is the generation a launch is under way for.
        self.status, self.launching, self.generation = {"state": "off"}, None, 0

    def command(self, folder=""):
        code = base64.b64encode(self.source.read_bytes()).decode()
        client = input_client()
        return ("python3 -u -c " + shlex.quote(
            f"import base64;exec(compile(base64.b64decode('{code}'),'frame_input_agent','exec'))")
            + f" {shlex.quote(client)} {shlex.quote(INPUT_NAME)} {shlex.quote(folder)}"
            + f" {shlex.quote(json.dumps(self.packages, separators=(',', ':')))}")

    def deliver(self, report, force=False):
        """Where the agent finds the packages on the Frame, copying them there first if needed.

        On the Frame itself (the iPhone app) they came with the bundle. Otherwise they
        go over the SSH connection, unless the Frame already has them unpacked (`force`:
        the agent found it didn't after all).
        """
        if LOCAL:
            return str(KDECONNECT / "packages")
        if not self.packages:
            return ""  # nothing to copy (the agent says so if it needed them)
        if not force:
            # Bytes, so Windows doesn't turn the stamp's line ends into CRLF.
            have = ssh(f"{{ test -x /usr/lib/kdeconnectd || cmp -s - {KDECONNECT_HOME}/root/.frame-control-packages; }}"
                       " && echo yes || true", stdin=kdeconnect_stamp(self.packages).encode(), text=False, timeout=20)
            if have.strip() == b"yes":
                return ""
        # A folder of its own: a cancelled start's agent may still be cleaning up another.
        folder = f"{KDECONNECT_HOME}/incoming/{secrets.token_hex(8)}"
        ssh(f"mkdir -p {folder}", timeout=20)
        try:
            for name, sha in self.packages:
                path = KDECONNECT / "packages" / name
                if not path.is_file() or file_sha256(path) != sha:
                    raise Failure(f"{name} is missing or damaged in this copy of Frame Control"
                                  " (a build runs app/build/fetch-deps.js to add it)", 500)
                report(f"Copying KDE Connect to the Frame ({name.rsplit('-', 3)[0]})")
                quoted = shlex.quote(name)
                ssh(f"cd {folder} && cat > {quoted}.part && mv {quoted}.part {quoted}",
                    stdin=path.read_bytes(), text=False, timeout=600)
        except (Failure, OSError):
            self.discard(folder)  # a partial copy is no use to anyone
            raise
        return f"~/{folder}"

    def discard(self, folder):
        """Remove a copy no agent will take over (best effort; agents tidy up old ones too)."""
        folder = folder.removeprefix("~/")
        if folder.startswith(f"{KDECONNECT_HOME}/incoming/") and not LOCAL:
            try:
                ssh(f"rm -rf {folder}", timeout=20)
            except (Failure, OSError):
                pass  # never let tidying up get in the way of reporting and retrying

    def start(self):
        with self.lock:
            if self.launching == self.generation or (self.proc and self.proc.poll() is None):
                return
            # (A launch from before a stop() may still be finishing; it ends itself.)
            self.launching, self.status = self.generation, {"state": "starting"}
            generation = self.generation
        threading.Thread(target=self._launch, args=(generation,), daemon=True).start()

    def _launch(self, generation, force=False):
        try:
            self._launch_once(generation, force)
        except Exception as e:  # whatever went wrong, never leave it stuck "starting"
            with self.lock:
                if self.launching == generation:
                    self.launching = None
                if self.generation == generation and self.status.get("state") in ("starting", "installing"):
                    self.status = {"state": "error", "message": f"Couldn't start the keyboard and trackpad: {e}"}

    def _launch_once(self, generation, force):
        def report(message):
            with self.lock:
                if self.generation == generation:
                    self.status = {"state": "installing", "message": message}
        folder = ""
        try:
            errors = tempfile.TemporaryFile()
            ensure_master()
            folder = self.deliver(report, force)
            with self.lock:
                stopped = self.generation != generation
            if stopped:  # turned off while copying
                raise Failure("stopped")
            proc = subprocess.Popen([*SSH, FRAME, self.command(folder)], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=errors)
        except (Failure, OSError) as e:
            self.discard(folder)  # no agent will take the copy over
            message = str(e)
            friendly = unreachable(message)
            with self.lock:
                if self.launching == generation:
                    self.launching = None
                if self.generation == generation:
                    self.status = {"state": "error", "message": friendly or message,
                                   **({"offline": True} if friendly else {})}
            return
        _live_tunnels.add(proc)
        with self.lock:
            if self.launching == generation:
                self.launching = None
            stale = self.generation != generation
            if not stale:
                self.proc = proc
        if stale:  # turned off meanwhile
            proc.terminate()
        wanted, heard = self._watch(proc, errors, retry=not force)
        if not heard:
            # The agent never started (or was stopped first), so it can't tidy the copy up.
            self.discard(folder)
        if wanted and not force:
            # It needed the packages after all (another device changed what's
            # installed after we looked): copy them and start once more.
            with self.lock:
                # Unless a start() already took over (it launches, and copies if still needed).
                if self.proc is not proc or self.generation != generation or self.launching is not None:
                    return
                self.proc, self.launching, self.status = None, generation, {"state": "starting"}
            self._launch(generation, force=True)

    def _watch(self, proc, errors, retry=False):
        """Follow the agent's status until it exits. Returns whether it asked for the
        packages (`retry`: the caller will send them, so that isn't an error yet), and
        whether it said anything at all (then it holds its copy and tidies it up)."""
        wanted = heard = False
        for line in proc.stdout:
            try:
                status = json.loads(line)
            except ValueError:
                continue
            if isinstance(status, dict) and isinstance(status.get("state"), str):
                heard = True
                if status["state"] == "need-packages":
                    wanted = True
                    continue
                with self.lock:
                    if self.proc is proc:
                        self.status = status
        proc.wait()
        _live_tunnels.discard(proc)
        try:
            errors.seek(0)
            detail = strip_ansi(errors.read().decode(errors="replace")).strip()
        except OSError:
            detail = ""
        with self.lock:
            if self.proc is proc and self.status.get("state") != "error" and not (wanted and retry):
                message = detail.splitlines()[-1] if detail else "The connection to the Frame ended"
                if wanted:
                    message = "KDE Connect didn't reach the Frame"
                friendly = unreachable(message)
                self.status = {"state": "error", "message": friendly or message, **({"offline": True} if friendly else {})}
        return wanted, heard

    def send(self, events):
        """Forward events if the agent is ready; start it if it isn't running.

        Returns the state, with "sent" saying whether the events went; if not,
        the page keeps them and sends them again once the state is "ready".
        """
        with self.lock:
            proc, ready = self.proc, self.status.get("state") == "ready"
        sent = False
        if not (proc and proc.poll() is None):
            self.start()
        elif ready and events:
            try:
                # One writer at a time: two devices sending at once mustn't tear a line.
                with self.write_lock:
                    proc.stdin.write((json.dumps(events) + "\n").encode())
                    proc.stdin.flush()
                sent = True
            except (BrokenPipeError, OSError, ValueError):
                pass  # _watch reports how it ended
        with self.lock:
            return {**self.status, "sent": sent}

    def stop(self):
        with self.lock:
            proc, self.proc, self.status = self.proc, None, {"state": "off"}
            self.generation += 1
        if proc and proc.poll() is None:
            proc.terminate()


_input = InputAgent()


def licenses():
    """The notices and licence texts for what Frame Control ships (the About dialog)."""
    found = [("Third-party notices", HERE.parent / "THIRD_PARTY_NOTICES.md"), ("KDE Connect for the Frame", KDECONNECT / "NOTICE.md"),
             ("Frame Control (MIT)", HERE.parent / "LICENSE")]
    found += [(f"{p.parent.name}: {p.stem}", p) for p in sorted((KDECONNECT / "LICENSES").glob("*/*.txt"))]
    notices = []
    for title, path in found:
        try:
            notices.append({"title": title, "text": path.read_text(errors="replace")})
        except OSError:
            pass
    return notices


def remote_input(body):
    """{"events": [...]} sends keyboard and pointer events; {} (or none yet) just starts the agent."""
    events = body.get("events", [])
    if not isinstance(events, list) or len(events) > INPUT_BATCH_LIMIT:
        raise Failure(f"events must be a list of at most {INPUT_BATCH_LIMIT}", 400)
    return _input.send([input_event(e) for e in events])


# ---- touch: the headset's panels, through gamescope's own input (frame_touch.py) ----

PANEL_WINDOW = re.compile(r"^\d{1,10}$")
PANEL_DISPLAY = re.compile(r"^:\d{1,2}$")
TOUCH_BUTTONS = ("left", "right", "middle")


def panels():
    """The headset's app panels (window, display, name, size) and which has focus."""
    return json.loads(ssh("python3 - panels", stdin=(HERE / "frame_touch.py").read_text(), timeout=20))


def panel_target(q):
    window, display = (q.get("window") or [""])[0], (q.get("display") or [""])[0]
    if not PANEL_WINDOW.match(window) or not PANEL_DISPLAY.match(display):
        raise Failure("window must be a window id and display an X display such as :1", 400)
    return window, display


def panel_capture(query):
    """One frame of a panel's own window, as PNG (its pixels, without the room around it)."""
    window, display = panel_target(parse_qs(query))
    return ssh(f"DISPLAY={display} timeout 10 ffmpeg -hide_banner -loglevel error -nostdin -f x11grab "
               f"-window_id {window} -i {display} -frames:v 1 -f image2pipe -c:v png -", timeout=20, text=False)


def touch_event(event):
    """A frame_touch.py event with only the fields it knows, in range."""
    if not isinstance(event, dict):
        raise Failure("each touch event must be an object", 400)

    def num(name, limit):
        value = event.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
            raise Failure(f"{name} must be a number", 400)
        return max(-limit, min(limit, round(float(value), 4)))
    out = {}
    if "fx" in event or "fy" in event:
        if "window" not in event:
            raise Failure("a position needs the panel's window and display", 400)
        out.update(fx=num("fx", 1), fy=num("fy", 1))
    # Any event can name the panel it's meant for; the Frame drops it if another has focus.
    if "window" in event:
        window, display = event.get("window"), event.get("display")
        if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
            raise Failure("window must be the panel's window id", 400)
        if not isinstance(display, str) or not PANEL_DISPLAY.match(display):
            raise Failure("display must be an X display such as :1", 400)
        out.update(window=window, display=display)
    for name in ("dx", "dy"):
        if name in event:
            out[name] = num(name, INPUT_MOVE_LIMIT)
    if "button" in event:
        if event["button"] not in TOUCH_BUTTONS:
            raise Failure(f"button must be one of {', '.join(TOUCH_BUTTONS)}", 400)
        out["button"], out["down"] = event["button"], event.get("down") is not False
    if "scroll" in event:
        sc = event["scroll"]
        if not isinstance(sc, list) or len(sc) != 2:
            raise Failure("scroll must be [dx, dy]", 400)
        out["scroll"] = [num_value(v, 5000) for v in sc]
    if "key" in event:
        key = event["key"]
        if isinstance(key, bool) or not isinstance(key, int) or not 0 < key < 768:
            raise Failure("key must be a Linux key code", 400)
        out["key"], out["down"] = key, event.get("down") is not False
    if "text" in event:
        text = event["text"]
        if not isinstance(text, str) or not 0 < len(text) <= INPUT_TEXT_LIMIT:
            raise Failure(f"text must be 1 to {INPUT_TEXT_LIMIT} characters", 400)
        out["text"] = text
    if not set(out) - {"window", "display"}:
        raise Failure("touch event has nothing to do", 400)
    return out


def num_value(value, limit):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise Failure("scroll values must be numbers", 400)
    return max(-limit, min(limit, round(float(value), 2)))


class TouchAgent(InputAgent):
    """frame_touch.py on the Frame, fed events over one long-lived ssh. Nothing to install:
    it uses gamescope's own input socket and the libei that's on the image."""

    def __init__(self):
        super().__init__(source=HERE / "frame_touch.py", packages=[])

    def deliver(self, report, force=False):
        return ""

    def command(self, folder=""):
        code = base64.b64encode(self.source.read_bytes()).decode()
        return "python3 -u -c " + shlex.quote(
            f"import base64;exec(compile(base64.b64decode('{code}'),'frame_touch','exec'))")


_touch = TouchAgent()


def remote_touch(body):
    """{"events": [...]} points, clicks, scrolls and types into the focused panel."""
    events = body.get("events", [])
    if not isinstance(events, list) or len(events) > INPUT_BATCH_LIMIT:
        raise Failure(f"events must be a list of at most {INPUT_BATCH_LIMIT}", 400)
    return _touch.send([touch_event(e) for e in events])


def flatpak(body):
    app, action = str(body.get("id", "")), body.get("action")
    if not FLATPAK_ID.match(app):
        raise Failure("bad Flatpak app ID", 400)
    if action == "install":
        def work():
            start = time.time()
            try:
                # Per-user, so it survives SteamOS updates and needs no sudo (as install-apps.sh).
                ssh("flatpak remote-add --user --if-not-exists flathub "
                    "https://dl.flathub.org/repo/flathub.flatpakrepo && "
                    f"flatpak install --user -y --noninteractive flathub {shlex.quote(app)}", timeout=1800)
            except Failure as e:
                frame_telemetry.install_finished("flatpak", False, time.time() - start, e, flatpak_id=app)
                raise
            frame_telemetry.install_finished("flatpak", True, time.time() - start, flatpak_id=app)
            return {"message": f"Installed {app}"}
        return start_job(f"Install {app}", work)
    if action == "uninstall":
        out = ssh(f"flatpak uninstall --user -y -- {shlex.quote(app)}", timeout=300)
        return {"message": strip_ansi(out).strip() or f"Removed {app}"}
    raise Failure("action must be install or uninstall", 400)


def power(what, password):
    """Sleep, restart or shut down from the Frame itself: sudo takes the Developer Mode password on stdin."""
    if not isinstance(password, str) or not password or "\n" in password:
        raise Failure("enter the Developer Mode password", 400)
    try:
        r = subprocess.run(["sudo", "-S", "-k", "-p", "", "systemctl", what], input=password + "\n",
                           capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise Failure(f"systemctl {what} didn't answer")
    if r.returncode != 0:
        err = r.stderr.strip()
        raise Failure("that password wasn't accepted" if "incorrect password" in err or "Sorry" in err
                      else err or f"systemctl {what} failed", 400)
    return {"message": {"suspend": "Going to sleep", "reboot": "Restarting", "poweroff": "Shutting down"}[what]}


def open_thing(body):
    what = body.get("what")
    if LOCAL:
        # Terminals, Steam Link and remote desktop open on the phone (its app does
        # that); what's left here is power, with the password the page asked for.
        if what in ("reboot", "poweroff", "suspend"):
            return power(what, body.get("password"))
        raise Failure("open that from the app", 400)
    # The headset and address in use, as every other command gets them (one snapshot).
    with _route_lock:
        alias, opts = LINK.named_route() if LINK else (FRAME, list(HOST_OPTS))
    host = next((o.split("=", 1)[1].replace("%%", "%") for o in opts if o.startswith("HostName=")), None)
    if what in ("terminal", "reboot", "poweroff", "suspend", "rdp", "sftp") and host and host.endswith(".invalid"):
        raise Failure("No headset address to use: add one on the Devices tab", 400)
    try:
        if what == "terminal":
            return {"message": f"Opened an SSH session in {terminal(['ssh', *opts, alias])}"}
        if what in ("reboot", "poweroff", "suspend"):
            # logind answers "challenge" over SSH, so sudo (and the password) is needed.
            where = terminal(["ssh", "-t", *opts, alias, "sudo", "systemctl", what])
            return {"message": f"Confirm with the Developer Mode password in {where} to {what}"}
        if what == "steamlink":
            return {"message": frame_host.open_steam_link()}
        if what == "rdp":
            return {"message": frame_host.open_rdp(alias, host)}
        if what == "sftp":
            return {"message": f"Opened an SFTP session in {terminal(['sftp', *opts, alias])}"}
        if what == "shots":
            SHOTS_DIR.mkdir(parents=True, exist_ok=True)
            frame_host.open_path(SHOTS_DIR)
            return {"message": f"Opened {SHOTS_DIR} in {frame_host.FILE_MANAGER}"}
        if what == "shot":
            saved = SHOTS_DIR / shot_path(body.get("id")).rsplit("/", 1)[-1]
            if not saved.exists():
                raise Failure("That screenshot isn't saved on this computer yet", 404)
            frame_host.reveal_path(saved)
            return {"message": f"Showed {saved.name} in {frame_host.FILE_MANAGER}"}
    except frame_host.Unreachable as e:
        raise Failure(str(e), 400)  # theirs to turn on; nothing failed here
    except frame_host.HostError as e:
        raise Failure(str(e), 500)
    raise Failure("unknown target", 400)


def apk_versions(query):
    args = parse_qs(query, keep_blank_values=True)
    packages, codes = args.get('package', []), args.get('code', [])
    if len(packages) != 1 or not frame_android.PKG_RE.match(packages[0]):
        raise Failure('invalid Android package id', 400)
    if codes and (len(codes) != 1 or not re.fullmatch(r'[0-9]{1,19}', codes[0])):
        raise Failure('invalid version code', 400)
    return frame_apk_versions.alternatives(packages[0], int(codes[0]) if codes else None)


def android(body):
    """Android apps, each in its own persistent Lepton instance (frame_android.py)."""
    action, pkg = body.get("action"), str(body.get("package", ""))
    ensure_master()
    try:
        if action == "install":
            url = body.get("url")
            if not url:
                frame_catalog.app(pkg)  # an unknown package fails now, not in the background

            def work():
                m = frame_apk_versions.install(pkg, url) if url else frame_catalog.install(pkg)
                message = f"Installed {m['label']}. It's in the Steam library; launching it opens its own panel."
                return {"message": f"{message} {frame_android.layer_note(m)}".strip(), "app": m}
            return queue_job(f"Install {pkg}", work)
        if action == "refresh-art":
            if not pkg and not body.get("all"):
                raise Failure('choose a package or all apps', 400)
            if not body.get('all'):
                return start_job('Refresh Steam artwork', lambda: {'apps': [frame_android.refresh_art(pkg)]})
            # Everything Frame Control sideloaded: Android apps and devkit titles.
            return start_job('Refresh Steam artwork', lambda: {
                'apps': frame_android.refresh_art(), 'titles': frame_titles.refresh_art()})
        if action in ("launch", "stop"):
            m = getattr(frame_android, action)(pkg)
            return {"message": f"{'Launching' if action == 'launch' else 'Stopped'} {m['label']}"}
        if action == "remove":
            m = frame_android.remove(pkg, keep_data=bool(body.get("keepData")))
            return {"message": f"Removed {m['label']}"}
        if action == "probe":
            r = frame_catalog.probe_and_report(pkg)
            word = {"runs": "runs", "crashes": "crashed", "instance_failed": "didn't start"}.get(r["result"], r["result"])
            return {"message": f"{pkg} {word}" + (f": {r['detail']}" if r.get("detail") else ""), "probe": r}
        if action == "report":
            # Any APK, not only catalogue or installed ones: package, did it work, how it was run.
            r = frame_catalog.add_report(pkg, body.get("version"), rating=body.get("rating"),
                                         notes=str(body.get("notes") or ""),
                                         runtime=body.get("runtime") or "instance",
                                         label=body.get("label"), source=body.get("source"))
            name = r.get("label") or pkg
            where = ("" if frame_catalog.compat_db.shared() else
                     " and shared it" if frame_telemetry.enabled("compat") else " on this computer")
            return {"message": f"Saved your report for {name}{where}", "report": r}
    except frame_android.FrameError as e:
        raise Failure(str(e)) from e  # from e: keeps frame_host.link_failure's mark for diagnostics
    raise Failure("unknown action", 400)


# ---- Import from my Quest (frame_quest.py, docs/quest-import.md) --------------

QUEST_ON_FRAME = "Import from my Quest runs on your computer, with the Quest plugged into it by USB."
QUEST_SERIAL = re.compile(r"^[A-Za-z0-9._:\[\]-]{1,128}$")


def quest_serial(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not QUEST_SERIAL.match(value):
        raise Failure("invalid Quest serial", 400)
    return value


def quest_status(query):
    """The Quest on this computer's USB (or what to do), and the apps on it."""
    if LOCAL:
        return {"quest": None, "devices": [], "guidance": QUEST_ON_FRAME, "apps": []}
    try:
        return frame_quest.status(quest_serial((parse_qs(query).get("serial") or [None])[0]))
    except frame_android.FrameError as e:
        raise Failure(str(e)) from e


def quest(body):
    """{"action": "import", "package", "serial"?, "data"?}: a background job with progress."""
    if LOCAL:
        raise Failure(QUEST_ON_FRAME, 400)
    if body.get("action") != "import":
        raise Failure("unknown action", 400)
    pkg = str(body.get("package", ""))
    if not frame_android.PKG_RE.match(pkg):
        raise Failure("invalid Android package id", 400)
    serial, data, older = quest_serial(body.get("serial")), body.get("data", False), body.get("allowDowngrade", False)
    if type(data) is not bool or type(older) is not bool:
        raise Failure("data and allowDowngrade must be true or false", 400)
    ensure_master()
    return queue_job(f"Import {pkg} from Quest",
                     lambda report: frame_quest.import_game(pkg, serial, data=data, progress=report,
                                                            allow_downgrade=older), progress=True)


# ---- My Meta library, Experimental (frame_meta.py, docs/meta-library.md) ------
#
# Never echoes the token: answers come from frame_meta.status() and _public(), and every
# error passes through frame_meta.redact().

META_APP_ID = re.compile(r"^[0-9]{1,25}$")


def _meta_failure(e):
    if isinstance(e, frame_meta.SignInCancelled):
        return Failure(frame_meta.redact(e, 600), 409)
    status = 400 if isinstance(e, frame_meta.TokenRejected) or "token" in str(e).lower() else 502
    return Failure(frame_meta.redact(e, 600), status)


def meta_sharing():
    """Whether test results reach the shared compatibility database (Privacy settings)."""
    blocked = frame_telemetry.blocked()
    return {"shared": bool(frame_catalog.compat_db.shared() or frame_telemetry.enabled("compat")),
            "blocked": blocked or None}


def meta_status(query):
    """The connected account (never its token) and, with library=1, the Quest games it owns
    with any community verdicts."""
    q = parse_qs(query)
    out = dict(frame_meta.status(), sharing=meta_sharing())
    if out["connected"] and q.get("library") == ["1"]:
        try:
            apps = frame_meta.with_verdicts(frame_meta.library(refresh=q.get("refresh") == ["1"]))
            out["apps"] = [frame_meta._public(a) for a in apps]
        except frame_android.FrameError as e:
            raise _meta_failure(e) from None
    return out


def meta(body):
    """{"action": "connect", "token", "acknowledged": true, "attempt"?} | {"action": "disconnect"} |
    {"action": "cancel-signin", "attempt"} |
    {"action": "install", "app", "test"?, "allowDowngrade"?} | {"action": "test", "package"}."""
    action = body.get("action")
    try:
        if action == "connect":
            if body.get("acknowledged") is not True:
                raise Failure("My Meta library is experimental: tick that you understand the warning first", 400)
            if not isinstance(body.get("token"), str):
                raise Failure("paste your Meta access token", 400)
            attempt = body.get("attempt")
            if attempt is not None and not (isinstance(attempt, str) and frame_meta.SIGNIN_ATTEMPT.fullmatch(attempt)):
                raise Failure("invalid sign-in attempt id", 400)
            return dict(frame_meta.connect(body["token"], attempt=attempt), sharing=meta_sharing(),
                        message="Connected your Meta account (Experimental)")
        if action == "cancel-signin":
            attempt = body.get("attempt")
            if not (isinstance(attempt, str) and frame_meta.SIGNIN_ATTEMPT.fullmatch(attempt)):
                raise Failure("invalid sign-in attempt id", 400)
            return dict(frame_meta.cancel_signin(attempt), sharing=meta_sharing(),
                        message="Cancelled that Sign in with Meta; it won't be saved")
        if action == "disconnect":
            return dict(frame_meta.disconnect(), sharing=meta_sharing(),
                        message="Disconnected your Meta account; its token was deleted from this computer")
        if action == "install":
            app_id, test, older = str(body.get("app", "")), body.get("test", False), body.get("allowDowngrade", False)
            if not META_APP_ID.match(app_id):
                raise Failure("invalid Meta app id", 400)
            if type(test) is not bool or type(older) is not bool:
                raise Failure("test and allowDowngrade must be true or false", 400)
            ensure_master()

            def work(report):
                try:
                    return frame_meta.install(app_id, test=test, progress=report, allow_downgrade=older)
                except frame_android.FrameError as e:
                    raise frame_meta.MetaError(frame_meta.redact(e, 1200)) from None
                except Exception as e:  # never let an unexpected error's text carry the token
                    raise frame_meta.MetaError(frame_meta.redact(f"{type(e).__name__}: {e}", 600)) from None
            return queue_job(f"{'Install and test' if test else 'Install'} {app_id} from Meta", work, progress=True)
        if action == "test":
            pkg = str(body.get("package", ""))
            if not frame_android.PKG_RE.match(pkg):
                raise Failure("invalid Android package id", 400)
            ensure_master()

            def test():
                # run_test's own errors are fixed texts; anything else could carry the game's
                # log (an ssh error is its output), so it is never shown, not even redacted.
                try:
                    return frame_meta.run_test(pkg)
                except frame_meta.MetaError:
                    raise
                except frame_android.FrameError:
                    raise frame_meta.MetaError(frame_meta.LOG_UNREADABLE) from None
                except Exception as e:
                    raise frame_meta.MetaError(f"The test stopped unexpectedly ({type(e).__name__}); "
                                               "try again.") from None
            return start_job(f"Test {pkg}", test)
    except frame_meta.MetaError as e:
        raise _meta_failure(e) from None
    raise Failure("unknown action", 400)


# Errors that are the APK's own fault, so they belong in the compatibility
# database as install_failed. Connection trouble and the like don't.
APK_FAULTS = {"android_installer", "apk_needs_newer_android", "apk_wrong_abi"}


def apk_installed(info, meta, error, seconds):
    """Every APK install (catalogue, dropped file, web link): usage analytics, and an
    install_failed report when the APK itself wouldn't install."""
    pkg = (info or {}).get("package")
    by_pkg = frame_catalog._cache.get("by_pkg") or {}
    in_catalog = bool(pkg) and pkg in by_pkg
    # Package names only for catalogue apps, which are public; a private APK's name stays here.
    # No version: a local rebuild can share a catalogue app's package name but carry anything in its version.
    # frame_android.install re-raises anything that isn't a FrameError, and whoever
    # catches it (a job, a request) reports it with its traceback: once is enough.
    frame_telemetry.install_finished("apk", error is None, seconds, error, catalog=in_catalog,
                                     diagnose=error is None or isinstance(error, frame_android.FrameError),
                                     package=pkg if in_catalog else None,
                                     xr_layer_missing=True if (info or {}).get("xr_layer_missing") else None)
    if error is not None and pkg and frame_telemetry.categorize(error)[0] in APK_FAULTS:
        frame_catalog.add_report(pkg, info.get("version"), result="install_failed", notes=str(error)[:300],
                                 via="install", label=info.get("label"))


frame_android.install_hooks.append(apk_installed)


# ---- Sideloaded titles (Linux/Windows builds as Steam Devkit Games) --------
#
# Installing is two steps: inspect (a dropped file is uploaded and a zip
# unpacked here, once) returns a token and the detected target and runtime for
# the page to confirm; install then runs in the background with progress the
# page polls. Unconfirmed uploads are dropped after STAGE_TTL.

STAGE_TTL = 3600
_titles_lock = threading.Lock()
_staged = {}  # token -> {"plan", "dir" (an upload's temp dir or None), "time"}
_title_jobs = {}  # token -> {"stage", "fraction", "done", "error", "title", "time"}


def _drop_staged(entry):
    frame_titles.discard(entry["plan"])
    if entry.get("dir"):
        shutil.rmtree(entry["dir"], ignore_errors=True)


def _purge_titles(now=None):
    now = now or time.time()
    with _titles_lock:
        stale = [_staged.pop(t) for t in [t for t, e in _staged.items() if now - e["time"] > STAGE_TTL]]
        for t in [t for t, j in _title_jobs.items() if j["done"] and now - j["time"] > STAGE_TTL]:
            del _title_jobs[t]
    for e in stale:
        _drop_staged(e)


def stage_title(path, temp_dir=None, name=None):
    """Inspect a .zip, folder or program and keep it for install; returns the plan and a token."""
    _purge_titles()
    try:
        plan = frame_titles.inspect(path, name)
    except BaseException as e:
        # Whatever went wrong, nothing will ever claim this upload.
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)
        if isinstance(e, frame_android.FrameError):
            raise Failure(str(e), 400)
        raise
    token = secrets.token_hex(12)
    with _titles_lock:
        _staged[token] = {"plan": plan, "dir": temp_dir, "time": time.time(),
                          "device": LINK.active_device()["id"] if LINK else None}
    return {"message": f"Read {plan['source']}: {plan['target']} with {plan['runtime_label']}",
            "token": token, "plan": frame_titles.public(plan)}


def _run_title_install(token, entry, name, exe, runtime):
    def update(**fields):  # the page reads jobs from other threads; change them under the lock
        with _titles_lock:
            _title_jobs[token].update(fields)

    start = time.time()
    try:
        m = frame_titles.install_plan(entry["plan"], name=name, exe=exe, runtime=runtime,
                                      progress=lambda stage, fraction: update(stage=stage, fraction=fraction))
        update(title=m, message=f"Installed {m['id']} in the Steam library ({m['runtime_label']})")
        frame_telemetry.install_finished("title", True, time.time() - start, runtime=m.get("runtime"))
    except frame_android.FrameError as e:
        update(error=str(e))
        frame_telemetry.install_finished("title", False, time.time() - start, e)
    except Exception as e:
        update(error=f"{type(e).__name__}: {e}")
        frame_telemetry.install_finished("title", False, time.time() - start, e)
    finally:
        _drop_staged(entry)
        update(done=True, time=time.time())


def titles(body):
    """Sideloaded titles (frame_titles.py): inspect a local path, install, discard, launch, remove."""
    action = body.get("action")
    if action == "inspect":
        # The app's page passes a dropped folder's path (Electron knows it); browsers upload instead.
        path = str(body.get("path") or "")
        if not os.path.isabs(path) or not os.path.exists(path):
            raise Failure("inspect needs the absolute path of a .zip, folder or program", 400)
        return stage_title(path, name=body.get("name") or None)
    if action in ("install", "discard"):
        token = str(body.get("token") or "")
        with _titles_lock:
            entry = _staged.pop(token, None)
        if not entry:
            raise Failure("that upload has expired; drop the file again", 400)
        if action == "discard":
            _drop_staged(entry)
            return {"message": "Discarded"}
        if LINK and entry.get("device") != LINK.active_device()["id"]:
            _drop_staged(entry)  # it was checked against the other headset's titles
            raise Failure("Frame Control switched headsets since this was read; drop the file again", 409)
        with _titles_lock:
            _title_jobs[token] = {"stage": "Starting", "fraction": 0, "done": False, "error": None,
                                  "message": None, "title": None, "time": time.time()}
        ensure_master()
        opt = lambda k: str(body.get(k) or "") or None  # noqa: E731
        busy_thread(_run_title_install, token, entry, opt("name"), opt("exe"), opt("runtime")).start()
        return {"message": f"Installing {entry['plan']['source']}", "job": token}
    if action not in ("launch", "remove", "refresh-art"):
        raise Failure("unknown action", 400)
    gid = str(body.get("id", ""))
    if not frame_titles.ID_RE.match(gid):
        raise Failure("bad title id", 400)
    ensure_master()
    if action == "refresh-art":
        return start_job(f"Steam artwork for {gid}", lambda: {"titles": [frame_titles.refresh_art(gid)]})
    try:
        m = getattr(frame_titles, action)(gid)
    except frame_android.FrameError as e:
        raise Failure(str(e)) from e  # from e: keeps frame_host.link_failure's mark for diagnostics
    return {"message": f"{'Launching' if action == 'launch' else 'Removed'} {m['id']}"}


def title_job(query):
    with _titles_lock:
        job = _title_jobs.get((parse_qs(query).get("token") or [""])[0])
        snapshot = job and {k: v for k, v in job.items() if k != "time"}
    if not snapshot:
        raise Failure("no such install", 404)
    return snapshot


# ---- Android display (wm size / wm density / font_scale over ADB) -----------
#
# Each running Lepton instance listens for ADB on the Frame (5555 is Lepton
# Development; own-instance apps get the next free port). ADB goes through a
# dedicated SSH forward that lives only for the request, like
# scripts/install-apk.sh, and is always torn down with an adb disconnect.

ADB_PORTS = range(5555, 5600)
SIZE_RE = re.compile(r"^(\d{3,4})x(\d{3,4})$")
DENSITY_RANGE = (120, 640)
WIDTH_RANGE, HEIGHT_RANGE = (640, 3840), (360, 2160)
FONT_RANGE = (0.5, 2.0)
KNOWN_LABELS = {"com.t3tools.t3code": "T3 Code", "org.fdroid.fdroid": "F-Droid"}
# One ADB session at a time: requests are rare, and it keeps adb's state simple.
_adb_lock = threading.Lock()
_live_tunnels = set()  # ssh processes (ADB forwards, live video) to kill if the server stops mid-request


def adb_path():
    try:
        return frame_host.adb()
    except frame_host.HostError as e:
        raise Failure(str(e), 500)


def adb(adb_bin, *args, timeout=20):
    try:
        r = subprocess.run([adb_bin, *args], capture_output=True, stdin=subprocess.DEVNULL, text=True,
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Failure(f"adb {' '.join(args[-2:])} timed out")
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        raise Failure(out or f"adb exited {r.returncode}")
    return out


def free_local_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AdbTunnel:
    """SSH forwards from local loopback to Frame ADB ports, plus adb connections.

    `with AdbTunnel([5555, 5557]) as t: t.shell(5555, "wm size")`. On exit it
    disconnects adb and kills the ssh process, whatever happened inside.
    """

    def __init__(self, ports):
        self.remote = list(ports)
        self.local = {}
        self.proc = None
        self.adb = adb_path()

    def __enter__(self):
        if not _adb_lock.acquire(timeout=60):
            raise Failure("another Android display request is still running; try again", 503)
        try:
            self._open()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _open(self):
        try:
            self._forward()
        except Failure:
            # A local port picked by free_local_port() can be taken before ssh
            # binds it (ExitOnForwardFailure turns that into an error): retry once.
            self._stop_ssh()
            self._forward()
        self.failed = {}
        for p in self.remote:
            out = adb(self.adb, "connect", self.serial(p), timeout=15)
            # adb connect exits 0 even when it fails; check what it says. Keep
            # going so one stuck port doesn't hide the healthy instances.
            if "connected to" not in out:
                self.failed[p] = f"adb couldn't connect to Frame port {p}: {out}"

    def _forward(self):
        self.local = {p: free_local_port() for p in self.remote}
        fwd = [a for p, lp in self.local.items() for a in ("-L", f"127.0.0.1:{lp}:127.0.0.1:{p}")]
        # Its own connection (ControlPath=none), so killing it drops the forwards.
        self.proc = _proc = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ControlPath=none", *HOST_OPTS,
             "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=5", "-N", *fwd, FRAME],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        _live_tunnels.add(_proc)
        deadline = time.time() + 12
        pending = set(self.local.values())
        while pending:
            if self.proc.poll() is not None:
                err = self.proc.stderr.read().decode(errors="replace").strip()
                raise Failure(f"ADB tunnel failed: {err or 'ssh exited ' + str(self.proc.returncode)}")
            if time.time() > deadline:
                raise Failure("ADB tunnel didn't come up within 12s")
            for lp in list(pending):
                try:
                    socket.create_connection(("127.0.0.1", lp), timeout=0.5).close()
                    pending.discard(lp)
                except OSError:
                    pass
            if pending:
                time.sleep(0.1)

    def serial(self, port):
        return f"127.0.0.1:{self.local[port]}"

    def shell(self, port, command, timeout=20):
        if port in getattr(self, "failed", {}):
            raise Failure(self.failed[port])
        return adb(self.adb, "-s", self.serial(port), "shell", command, timeout=timeout)

    def _stop_ssh(self):
        proc, self.proc = self.proc, None
        if not proc:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(5)
        except OSError:
            pass
        finally:
            _live_tunnels.discard(proc)
            if proc.stderr:
                proc.stderr.close()

    def __exit__(self, *exc):
        try:
            # Tunnel first: it's what could outlive us. `adb disconnect` only
            # talks to the local adb server, so it works without the tunnel.
            self._stop_ssh()
            for p in self.local:
                try:
                    subprocess.run([self.adb, "disconnect", self.serial(p)], capture_output=True, stdin=subprocess.DEVNULL, timeout=10)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        finally:
            _adb_lock.release()
        return False


class PodmanShell:
    """AdbTunnel's stand-in on the Frame itself: there's no adb there, but each
    instance is a podman container, so run Android's shell inside it."""

    def __init__(self, ports, containers):
        self.containers = containers

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def shell(self, port, command, timeout=20):
        ctr = self.containers.get(port)
        if not ctr:
            raise Failure(f"port {port} isn't a Lepton container this app can reach")
        return ssh(f"podman exec {shlex.quote(ctr)} /system/bin/sh -c {shlex.quote(command)}", timeout=timeout)


def android_shell(ports, containers):
    return PodmanShell(ports, containers) if LOCAL else AdbTunnel(ports)


DISPLAY_READ = "echo @@pkgs; pm list packages -3; echo @@size; wm size; echo @@density; wm density; " \
               "echo @@font; settings get system font_scale"


def parse_display(out):
    sec, parts = None, {}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("@@"):
            sec = line[2:]
            parts[sec] = []
        elif sec and line:
            parts[sec].append(line)

    def pick(lines, key):
        for line in lines:
            if line.lower().startswith(key) and ":" in line:
                return line.split(":", 1)[1].strip()
        return None

    size, density = parts.get("size", []), parts.get("density", [])
    font = (parts.get("font") or ["null"])[0]
    try:
        font_scale = None if font == "null" else float(font)
    except ValueError:
        font_scale = None
    phys_d, over_d = pick(density, "physical density"), pick(density, "override density")
    return {
        "packages": sorted(l.split(":", 1)[1] for l in parts.get("pkgs", []) if l.startswith("package:")),
        "physicalSize": pick(size, "physical size"),
        "overrideSize": pick(size, "override size"),
        "physicalDensity": int(phys_d) if phys_d and phys_d.isdigit() else None,
        "overrideDensity": int(over_d) if over_d and over_d.isdigit() else None,
        # null means never set, which Android treats as 1.0.
        "fontScale": font_scale,
    }


def lepton_ports():
    """Frame ADB ports (5555-5599) that are listening now, with container names and app labels."""
    out = ssh("ss -ltnH; echo @@podman; podman ps --format '{{.Names}} {{.Labels.adb_port}}' 2>/dev/null; "
              f"echo @@meta; for f in {frame_android.APPS_DIR}/*/meta.json; do [ -f \"$f\" ] && cat \"$f\" && echo @@m; done; true",
              timeout=20)
    listen, _, rest = out.partition("@@podman")
    podman, _, meta = rest.partition("@@meta")
    ports = set()
    for line in listen.splitlines():
        cols = line.split()
        if len(cols) >= 4:
            port = cols[3].rsplit(":", 1)[-1]
            if port.isdigit() and int(port) in ADB_PORTS:
                ports.add(int(port))
    containers = {}
    for line in podman.splitlines():
        cols = line.split()
        if len(cols) == 2 and cols[1].isdigit():
            containers[int(cols[1])] = cols[0]
    labels = dict(KNOWN_LABELS)
    for chunk in meta.split("@@m"):
        try:
            m = json.loads(chunk)
            labels[str(m["package"])] = str(m["label"])
        except (ValueError, KeyError, TypeError):
            pass
    return sorted(ports), containers, labels


def android_displays():
    ports, containers, labels = lepton_ports()
    if not ports:
        return {"instances": []}
    instances = []
    with android_shell(ports, containers) as t:
        for p in ports:
            item = {"port": p, "container": containers.get(p)}
            try:
                item.update(parse_display(t.shell(p, DISPLAY_READ)))
            except Failure as e:
                item["error"] = str(e)
            item["labels"] = {pkg: labels[pkg] for pkg in item.get("packages", []) if pkg in labels}
            instances.append(item)
    return {"instances": instances}


def android_display(body):
    port = body.get("port")
    if type(port) is not int or port not in ADB_PORTS:
        raise Failure(f"port must be an integer {ADB_PORTS.start}-{ADB_PORTS.stop - 1}", 400)
    cmds = []

    size = body.get("size")
    if size is not None:
        if size == "reset":
            cmds.append("wm size reset")
        else:
            m = SIZE_RE.fullmatch(size) if isinstance(size, str) else None
            if not m:
                raise Failure("size must be WIDTHxHEIGHT (e.g. 2560x1440) or \"reset\"", 400)
            w, h = int(m[1]), int(m[2])
            if not (WIDTH_RANGE[0] <= w <= WIDTH_RANGE[1] and HEIGHT_RANGE[0] <= h <= HEIGHT_RANGE[1]):
                raise Failure(f"size must be {WIDTH_RANGE[0]}-{WIDTH_RANGE[1]} wide and "
                              f"{HEIGHT_RANGE[0]}-{HEIGHT_RANGE[1]} high", 400)
            cmds.append(f"wm size {w}x{h}")

    density = body.get("density")
    if density is not None:
        if density == "reset":
            cmds.append("wm density reset")
        elif type(density) is int and DENSITY_RANGE[0] <= density <= DENSITY_RANGE[1]:
            cmds.append(f"wm density {density}")
        else:
            raise Failure(f"density must be an integer {DENSITY_RANGE[0]}-{DENSITY_RANGE[1]} or \"reset\"", 400)

    font = body.get("fontScale")
    if font is not None:
        if font == "reset":
            # Applying a config change (e.g. the wm resets just before) writes
            # font_scale=1.0 back asynchronously, so delete again once it settles.
            cmds.append("settings delete system font_scale; sleep 1; settings delete system font_scale")
        elif type(font) in (int, float) and FONT_RANGE[0] <= font <= FONT_RANGE[1]:
            cmds.append(f"settings put system font_scale {round(float(font), 3):g}")
        else:
            raise Failure(f"fontScale must be a number {FONT_RANGE[0]}-{FONT_RANGE[1]} or \"reset\"", 400)

    if not cmds:
        raise Failure("nothing to change: give density, size or fontScale", 400)
    ports, containers, _ = lepton_ports()
    if port not in ports:
        raise Failure(f"no Lepton instance is listening on Frame port {port}", 404)
    with android_shell([port], containers) as t:
        for c in cmds:
            out = t.shell(port, c)
            # wm prints usage or an exception on failure but may still exit 0.
            if re.search(r"exception|error|usage", out, re.I):
                raise Failure(f"{c}: {out}")
        now = parse_display(t.shell(port, DISPLAY_READ))
    now["port"] = port
    return {"message": f"Port {port}: " + "; ".join(c.split(";")[0] for c in cmds), "display": now}


# ---- install links from websites (frame-control://install, docs/web-install.md) ----
# The app hands the link to the page, which asks /check (fetches the manifest,
# downloads nothing), shows what it found and waits for the user's click before
# /start. A website can't call these itself: like all of /api/* they need the
# Host and X-Frame-UI checks in Handler.local_request.
_web_lock = threading.Lock()
_web_plans = {}   # id -> checked plan waiting for the user to confirm
_web_jobs = {}    # id -> progress of the confirmed install (only the latest is kept)
_web_workers = set()  # threads running an install, joined on shutdown
_web_closing = False  # set on shutdown; no new installs after that
MAX_WEB_PLANS = 8
WEB_TMP_PREFIX = "frame-webinstall-"  # then the server's PID, for sweep_tmp


def webinstall_check(body):
    manifest, url = body.get("manifest"), body.get("url")
    for v in (manifest, url):
        if v is not None and not isinstance(v, str):
            raise Failure("manifest and url must be strings", 400)
    try:
        plan = frame_webinstall.plan(manifest=manifest, url=url)
    except frame_webinstall.WebInstallError as e:
        raise Failure(str(e), 400)
    pid = secrets.token_urlsafe(16)
    with _web_lock:
        while len(_web_plans) >= MAX_WEB_PLANS:
            _web_plans.pop(next(iter(_web_plans)))
        _web_plans[pid] = plan
    shown = ("name", "file", "kind", "kindLabel", "host", "linkHost", "size", "source")
    return {"id": pid, **{k: plan[k] for k in shown}, "sha256": bool(plan["sha256"])}


def webinstall_start(body):
    pid = body.get("id")
    with _web_lock:
        if any(j["phase"] in ("download", "install") for j in _web_jobs.values()):
            raise Failure("another install from a link is still running", 409)
        # One use per check: the page can only install what it showed.
        plan = _web_plans.pop(pid, None) if isinstance(pid, str) else None
        if not plan:
            raise Failure("unknown or already used install id; open the link again", 400)
        job = {"phase": "download", "done": 0, "total": plan["size"], "detail": "", "message": None,
               "error": None, "cancel": False}
        if _web_closing:
            raise Failure("Frame Control is quitting", 503)
        _web_jobs.clear()
        _web_jobs[pid] = job
        # Started under the lock, so shutdown never sees a thread it can't join.
        worker = busy_thread(_webinstall_run, plan, job)
        _web_workers.add(worker)
        worker.start()
    return {"job": pid}


def _webinstall_run(plan, job):
    tmp = None
    try:
        tmp = tempfile.mkdtemp(prefix=f"{WEB_TMP_PREFIX}{os.getpid()}-")

        def progress(done, total):
            job["done"], job["total"] = done, total

        def detail(*args, **_kw):  # frame_titles may report its steps as text
            texts = [a for a in args if isinstance(a, str)]
            if texts:
                job["detail"] = texts[0][:200]

        def connected(conn):
            with _web_lock:
                job["_conn"] = conn
                stop = job["cancel"]  # cancelled before this connection existed
            if stop:
                frame_webinstall.abort(conn)

        path = frame_webinstall.download(plan, tmp, progress=progress, cancelled=lambda: job["cancel"],
                                         connected=connected)
        # Under the lock cancel uses, so a cancel it acknowledged is never followed by an install.
        with _web_lock:
            if job["cancel"]:
                raise frame_webinstall.Cancelled("download cancelled")
            job["phase"] = "install"
            job.pop("_conn", None)
        ensure_master()
        res = frame_webinstall.dispatch(path, name=plan["name"], exe=plan["exe"], progress=detail, source=plan["url"])
        job["message"], job["phase"] = res["message"], "done"
        if res.get("kind") != "apk":  # APKs are counted by apk_installed
            frame_telemetry.install_finished("web", True, kind_detail=res.get("kind"))
    except Exception as e:
        stage = job.get("phase")  # download or install, before it becomes "error"
        known = (frame_webinstall.WebInstallError, Failure, frame_android.FrameError)
        job["error"] = str(e) if isinstance(e, known) else f"{type(e).__name__}: {e}"
        job["phase"] = "error"
        # An APK that failed to install was counted by apk_installed.
        apk_install = stage == "install" and plan.get("kind") == "apk"
        if not isinstance(e, frame_webinstall.Cancelled) and not apk_install:
            frame_telemetry.install_finished("web", False, error=e, stage=stage, kind_detail=plan.get("kind"))
        elif apk_install and not isinstance(e, known):
            # Not a FrameError, so apk_installed left its diagnostic to whoever caught it: here.
            frame_telemetry.diagnostic("web install", e)
    finally:
        with _web_lock:
            job.pop("_conn", None)
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
        with _web_lock:
            _web_workers.discard(threading.current_thread())


def webinstall_job(query):
    job = _web_jobs.get((parse_qs(query).get("id") or [""])[0])
    if not job:
        raise Failure("unknown install job", 404)
    with _web_lock:  # the worker adds and drops _conn meanwhile
        return {k: v for k, v in job.items() if k != "cancel" and not k.startswith("_")}


def webinstall_cancel(body):
    jid = body.get("job")
    job = _web_jobs.get(jid) if isinstance(jid, str) else None
    if not job:
        raise Failure("unknown install job", 404)
    with _web_lock:
        if job["phase"] != "download":
            raise Failure("only the download can be cancelled", 409)
        job["cancel"] = True
        conn = job.get("_conn")
    if conn:
        frame_webinstall.abort(conn)
    return {"message": "Cancelling the download"}


def webinstall_shutdown():
    """Stop downloads and give workers a moment to delete their temporary files.

    An install already copying to the Frame may outlive this; sweep_tmp
    removes what it leaves on a later start.
    """
    global _web_closing
    with _web_lock:
        _web_closing = True
        conns = []
        for job in _web_jobs.values():
            job["cancel"] = True
            conns.append(job.get("_conn"))  # once: the worker may drop it any time
        workers = list(_web_workers)
    for conn in conns:
        if conn:
            frame_webinstall.abort(conn)
    deadline = time.time() + 4  # the app kills the server 5 s after asking it to stop
    for worker in workers:
        worker.join(max(0, deadline - time.time()))


def _pid_alive(pid):
    if frame_host.WINDOWS:
        # os.kill(pid, 0) would terminate the process there; ask the kernel instead.
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = ctypes.c_ulong()
            return not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists, owned by someone else
    return True


def _tmp_places():
    """Where each kind of staging folder goes, and its name before the server's PID."""
    tmp = Path(tempfile.gettempdir())
    return [(tmp, WEB_TMP_PREFIX), (tmp, frame_titles.TMP_PREFIX), (tmp, frame_android.TMP_PREFIX),
            (tmp, frame_agent.TMP_PREFIX), (frame_host.cache_dir("apk-sources"), apk_fdroid.TMP_PREFIX)]


def sweep_tmp(own=False):
    """Delete staging folders (downloads, unzipped titles, patched APKs, staged files,
    app-index downloads) left by a server that stopped mid-way.

    Folders carry the server's PID, so only a dead server's are taken; own=True (on
    the way out, see main) takes this server's too.
    """
    for folder, prefix in _tmp_places():
        try:
            found = list(folder.glob(f"{prefix}*"))
        except OSError:
            continue
        for d in found:
            _sweep_one(prefix, d, own)


def _sweep_one(prefix, d, own=False):
    m = re.fullmatch(re.escape(prefix) + r"(\d+)-.*", d.name)
    if not m:
        return
    pid = int(m[1])
    try:
        if (own if pid == os.getpid() else not _pid_alive(pid)) and d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


# ---- Panel switcher (same helper on the companion and in the headset) ----

def panels_action(body):
    action = body.get("action", "list")
    script = (HERE / "frame_panels.py").read_text()
    if action == "open":
        # Installed in the user account so the page can outlive this SSH call.
        remote = ('umask 077; mkdir -p ~/.local/share/frame-control/panels && '
                  'tmp=$(mktemp ~/.local/share/frame-control/panels/install.XXXXXX) && '
                  'cat > "$tmp" && mv "$tmp" ~/.local/share/frame-control/panels/switcher.py && '
                  'python3 ~/.local/share/frame-control/panels/switcher.py --open')
    elif action == "list":
        remote = "python3 -"
    elif action == "focus":
        key = body.get("key")
        if not isinstance(key, str) or not frame_panels.KEY.fullmatch(key):
            raise Failure("Choose an open panel.", 400)
        remote = "python3 - --focus " + shlex.quote(key)
    else:
        raise Failure("Unknown panel action", 400)
    result = json.loads(ssh(remote, stdin=script, timeout=45))
    if "error" in result:
        raise Failure(result["error"], 502)
    return result


# ---- Our Frame-side media player -----------------------------------------

_MEDIA_LOCK = threading.Lock()


def media(body):
    action = body.get("action")
    if action not in ("list", "status", "play", "stop"):
        raise Failure("Media action must be list, status, play or stop", 400)
    if action == "play":
        identity = body.get("id")
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}/[^/\\\x00]+", identity):
            raise Failure("Invalid media id", 400)
        if body.get("layout", "auto") not in frame_media.LAYOUTS:
            raise Failure("Invalid media layout", 400)
        if type(body.get("theatre", False)) is not bool:
            raise Failure("theatre must be true or false", 400)
    with _MEDIA_LOCK:
        # Ship only our small stdlib modules, atomically, to the user account.
        sources = {name: (HERE / name).read_text() for name in (
            "frame_media.py", "frame_media_player.py", "frame_media_remote.py", "frame_splat.py")}
        installer = """import json, os, pathlib, sys, tempfile
root = pathlib.Path.home()/'.local/share/frame-control/media'
root.mkdir(parents=True, exist_ok=True)
for name, source in json.load(sys.stdin).items():
    path = root/name
    fd, temp = tempfile.mkstemp(dir=root, prefix=name+'.')
    with os.fdopen(fd, 'w') as f:
        f.write(source)
    os.replace(temp, path)
"""
        ssh("python3 -c " + shlex.quote(installer), stdin=json.dumps(sources))
        try:
            out = ssh("python3 ~/.local/share/frame-control/media/frame_media_remote.py",
                      stdin=json.dumps(body), timeout=75)  # remote worst case: ffprobe 30 + reset-failed 10 + systemd-run 15 s
        except Failure as e:
            for line in reversed(getattr(e, "stdout", "").splitlines()):
                try:
                    detail = json.loads(line).get("error")
                except (ValueError, AttributeError):
                    continue
                if detail:
                    raise Failure(detail) from None
            raise
        return json.loads(out)


def push_media(path):
    # Validate the format, but leave layout selection until playback (ffprobe
    # can then read metadata on the Frame, where it is installed).
    name = Path(path).name
    frame_media.plan(name, "mono")
    if name.startswith(".") or "\\" in name:
        raise Failure("Rename the file: media names can't start with a dot or contain a backslash", 400)
    token = secrets.token_hex(16)
    dest = "Videos/FrameControl/" + token + "/"
    ssh("mkdir -p ~/" + dest)
    try:
        push_file(path, dest)
    except Exception:
        try:
            ssh("rm -rf ~/" + dest)
        except Exception:
            pass  # keep the copy error; an empty folder isn't listed as media
        raise
    return {"message": "Media sent. Choose its layout and press Play.",
            "id": token + "/" + name}


# ---- Mac in the headset (frame_macview.py) ----------------------------------

# The tunnel gets its own connection: the shared master's options would win
# over anything added after them.
macview = frame_macview.MacView(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8"],
                                lambda remote, stdin=None, timeout=30: ssh(remote, stdin=stdin, timeout=timeout),
                                FRAME, track=_live_tunnels.add)


def macview_state(query=None):
    if LOCAL:
        return {"available": False, "reason": "Runs on your computer, not the headset."}
    try:
        # Refresh restarts the helper, which picks up a permission just granted.
        if (query or {}).get("restart") == ["1"]:
            macview.restart_agent()
        return macview.state()
    except frame_macview.MacViewError as e:
        raise Failure(str(e), 500)


def macview_action(body):
    """{action: show|stop|permissions, src, quality, w, h}."""
    if LOCAL:
        raise Failure("Runs on your computer, not the headset.", 400)
    action = body.get("action")
    try:
        if action == "show":
            w, h = body.get("w"), body.get("h")
            return macview.show(str(body.get("src") or ""), str(body.get("quality") or "balanced"),
                                int(w) if w else None, int(h) if h else None)
        if action == "stop":
            return macview.stop(body.get("src") or None)
        if action == "permissions":
            return macview.request_permissions()
    except frame_macview.MacViewError as e:
        raise Failure(str(e), 502)
    raise Failure("unknown action", 400)


# ---- Report a problem (frame_report.py) --------------------------------------

def report_preview(body):
    """Exactly the diagnostics a report would include, for the dialog to show first."""
    return {"text": frame_report.diagnostics(body.get("activity") or (), include_logs=bool(body.get("includeLogs")))}


def report_send(body):
    try:
        return frame_report.send(body)
    except frame_report.ReportError as e:
        raise Failure(str(e))


def agent_call(body):
    return frame_agent.call(sys.modules[__name__], body)


def assistant_chat(body):
    return frame_assistant.chat(body, headset_view)


def agent_approval(body):
    return frame_agent.approvals.decide(body.get("confirmation"), body.get("accept"))


def source_text(body, key, optional=False):
    value = body.get(key)
    if optional and value in (None, ''):
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
        raise Failure('Provide a valid ' + key, 400)
    return value.strip()


def source_search(query):
    args = parse_qs(query)
    q = args.get('q', [''])[0]
    vr = args.get('vr', [''])[0]
    installable = args.get('installable', [''])[0]
    if len(q) > 500 or vr not in ('', 'true', 'false', '1', '0') or installable not in ('', 'true', 'false', '1', '0'):
        raise Failure('Invalid search filters', 400)
    try:
        return apk_search.search(q, vr=None if not vr else vr in ('true', '1'),
                                 source=args.get('source', [None])[0], installable=installable in ('true', '1'))
    except SourceError as e:
        raise Failure(str(e), 400)


def source_install(body):
    source, entry = source_text(body, 'source'), source_text(body, 'id')
    version = body.get('version_code')
    if version is not None and (type(version) is not int or version < 0):
        raise Failure('version_code must be a non-negative integer', 400)
    try:
        _, selected = apk_search.resolve(source)
        if not selected['enabled']:
            raise SourceError('This source is disabled')
    except SourceError as e:
        raise Failure(str(e), 400)
    return queue_job('Install ' + entry, lambda report: apk_search.install(source, entry, version, report), progress=True)


def source_manage(body):
    action = body.get('action')
    try:
        if action == 'enable':
            if type(body.get('enabled')) is not bool:
                raise Failure('enabled must be true or false', 400)
            return apk_search.set_enabled(source_text(body, 'source'), body['enabled'])
        if action == 'add':
            url = source_text(body, 'url')
            fingerprint, name = source_text(body, 'fingerprint', True), source_text(body, 'name', True)
            parts = urlparse(url)
            if parts.scheme not in ('https', 'fdroidrepos') or not parts.hostname or parts.username:
                raise Failure('Use an HTTPS or fdroidrepos:// repository URL without credentials', 400)
            apk_search.repo_module()  # fail now if this build can't manage repositories

            def add():  # downloads and verifies the whole index: a job, not a request
                source = apk_search.manage_repo('add_repo', url=url, fingerprint=fingerprint, name=name)
                message = 'Added ' + source['name']
                if source.get('trust_on_first_use'):
                    message += '. Trusted on first use: ' + source['fingerprint'].upper()
                return {'message': message, 'source': {k: source.get(k) for k in
                                                       ('id', 'name', 'fingerprint', 'trust_on_first_use')}}
            return start_job('Add repository', add)
        if action == 'game-data':
            package = source_text(body, 'package')
            return start_job('Add game data', lambda: apk_search.add_game_data(package))
        if action == 'remove':
            apk_search.manage_repo('remove_repo', source_id=source_text(body, 'source'))
            return {'message': 'Repository removed'}
        raise Failure('Unknown source action', 400)
    except SourceError as e:
        raise Failure(str(e), 400)


POST = {
    "/api/vr": vr,
    "/api/comfort": comfort, "/api/media": media, "/api/agent/call": agent_call,
    "/api/agent/approval": agent_approval, "/api/assistant/chat": assistant_chat,
    "/api/input": remote_input, "/api/touch": remote_touch,
    "/api/settings/artwork": frame_steamgriddb.save_settings,
    "/api/sources": source_manage, "/api/sources/install": source_install, "/api/android/display": android_display, "/api/android": android, "/api/quest": quest, "/api/meta": meta, "/api/queue": queue_action, "/api/titles": titles, "/api/launch": launch, "/api/steam": steam, "/api/volume": set_volume, "/api/clipboard": clipboard,
        "/api/flatpak": flatpak, "/api/open": open_thing, "/api/shots/save": save_shots,
        "/api/webinstall/check": webinstall_check, "/api/webinstall/start": webinstall_start,
        "/api/webinstall/cancel": webinstall_cancel,
        "/api/telemetry": frame_telemetry.update_settings, "/api/telemetry/event": frame_telemetry.page_event,
        "/api/contact": frame_contact.save, "/api/contact/prompt": frame_contact.prompt,
        "/api/report/preview": report_preview, "/api/report": report_send, "/api/macview": macview_action, "/api/panels": panels_action,
        "/api/devices": lambda body: devices_post(body)}




# ---- headsets and the connection (frame_devices.py, frame_link.py) ----------

def open_setup(alias, host=None):
    """Set Up Connection for `alias` in a terminal window, as the app's menu does."""
    if frame_host.MAC:
        argv = ["env", f"FRAME_ALIAS={alias}", "zsh", str(HERE.parent / "scripts" / "connect.sh")]
    else:
        argv = [sys.executable, str(HERE / "frame_connect.py"), "--alias", alias]
    return terminal(argv + ([host] if host else []))


def devices_post(body):
    if not LINK:
        raise Failure("Headsets are managed from the computer app", 400)
    if PRIVATE:
        raise Failure("Headsets are managed in the Frame Control app", 403)
    try:
        # Under the work lock: nothing can start on the old headset while it switches.
        with _work_lock:
            return frame_link.devices_action(LINK, body, open_setup, lambda: _work[0])
    except frame_devices.DeviceError as e:
        raise Failure(str(e), 400)


def devices_get(path, query):
    if not LINK:
        raise Failure("Headsets are managed from the computer app", 400)
    q = parse_qs(query)
    device = (q.get("id") or [None])[0]
    try:
        if path == "/api/devices":
            return dict(frame_link.devices_view(LINK), nextAlias=frame_link.next_alias(LINK))
        if path == "/api/devices/tailscale":
            return frame_link.tailscale_find(LINK, device)
        return frame_link.mdns_find(LINK, device)
    except frame_devices.DeviceError as e:
        raise Failure(str(e), 400)


def connection_state():
    if not LINK:  # on the Frame itself there's nothing to find
        return {"local": True, "phase": "connected", "version": 0}
    return LINK.snapshot()


# ---- HTTP ------------------------------------------------------------------

# POST reads the page repeats on a timer, as (path, action).
POLLS = {("/api/comfort", "status")}


def action_of(body):
    """The action a request asked for, for diagnostics: a short word, never user data."""
    a = body.get("action") if isinstance(body, dict) else None
    return a if isinstance(a, str) and re.fullmatch(r"[a-z]{1,20}", a) else ""


def _pipe_reader(pipe):
    """Chunks from a pipe via a thread; select() can't wait on pipes on Windows."""
    chunks = queue.Queue()  # unbounded: the pump never blocks, so it ends at EOF

    def pump():
        try:
            while True:
                chunk = pipe.read1(1 << 16) if hasattr(pipe, "read1") else os.read(pipe.fileno(), 1 << 16)
                chunks.put(chunk)
                if not chunk:
                    return
        except (OSError, ValueError):
            chunks.put(b"")

    threading.Thread(target=pump, daemon=True).start()
    return chunks


def _next_chunk(chunks, timeout):
    """The next chunk, or b"" at end of stream or after `timeout` seconds of silence."""
    try:
        return chunks.get(timeout=timeout)
    except queue.Empty:
        return b""


def push_file(path, dest="Downloads/"):
    """Copy a file to the Frame (as scripts/push.sh): rsync where both ends have it, else scp."""
    name = Path(path).name
    try:
        # Not on Windows: a Windows rsync (cwRsync, MSYS2) wouldn't take our POSIX -e quoting.
        if not frame_host.WINDOWS and shutil.which("rsync") and ssh("command -v rsync >/dev/null && echo yes || true").strip() == "yes":
            cmd = ["rsync", "-a", "-e", shlex.join(SSH), str(path), f"{FRAME}:{shlex.quote(dest)}"]
        else:
            # Modern scp uses SFTP, so the remote path isn't parsed by a shell.
            cmd = ["scp", *SSH[1:], "-r", str(path), f"{FRAME}:{dest}"]
        r = frame_host.run_ssh(cmd, capture_output=True, stdin=subprocess.DEVNULL, text=True, errors="replace", timeout=3600)
    except subprocess.TimeoutExpired:
        raise Failure(f"Copying {name} timed out")
    if r.returncode != 0:
        raise Failure(strip_ansi(r.stderr or r.stdout).strip() or f"copy exited {r.returncode}")
    return f"Sent {name} to ~/{dest}"


class ClientGone(Exception):
    """The page went away (a reload, the app quitting) before its reply was written:
    nobody to answer, and nothing went wrong here."""


class Handler(BaseHTTPRequestHandler):
    server_version = "FrameControl/1"
    timeout = 60  # per socket operation, so a stalled client can't hold a thread

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, fmt % args))

    def local_request(self):
        # Blocks DNS rebinding (Host) and cross-site form posts (custom header
        # forces a CORS preflight, which this server never approves).
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            self.send_json({"error": "forbidden host"}, 403)
            return False
        # All of /api/*, not just POST: an <img> on any website could otherwise
        # trigger a headset capture and display it.
        api = urlparse(self.path).path.startswith("/api/")
        if (self.command == "POST" or api) and not secrets.compare_digest(self.headers.get("X-Frame-UI") or "", UI_KEY):
            self.send_json({"error": "missing or wrong X-Frame-UI header"}, 403)
            return False
        return True

    def send_bytes(self, data, ctype, status=200, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # Nobody may frame the UI (clickjacking).
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        try:
            self.end_headers()
            self.wfile.write(data)
        except ConnectionError as e:  # Windows says ConnectionAbortedError, others BrokenPipeError
            raise ClientGone() from e

    def send_json(self, obj, status=200):
        self.send_bytes(json.dumps(obj).encode(), "application/json", status)

    def send_error_json(self, message, status, apk=None):
        body, offline_status = error_body(message)
        if apk is not None:
            body["apk"] = apk
        self.send_json(body, offline_status or status)

    def do_GET(self):
        if not self.local_request():
            return
        url = urlparse(self.path)
        path = url.path
        try:
            if path in ("/", "/index.html"):
                self.send_bytes((HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/settings/artwork":
                self.send_json(frame_steamgriddb.settings())
            elif path == "/artwork-settings.js":
                self.send_bytes((HERE / 'artwork-settings.js').read_bytes(), 'text/javascript; charset=utf-8')
            elif path == "/control-center.js":
                self.send_bytes((HERE / 'control-center.js').read_bytes(), 'text/javascript; charset=utf-8')
            elif path == "/control-center.css":
                self.send_bytes((HERE / 'control-center.css').read_bytes(), 'text/css; charset=utf-8')
            elif path == "/assistant":
                page = (HERE / "assistant.html").read_text().replace("__FRAME_KEY__", json.dumps(UI_KEY).replace("<", "\\u003c"))
                self.send_bytes(page.encode(), "text/html; charset=utf-8")
            elif path == "/api/agent/approval":
                token = (parse_qs(url.query).get("confirmation") or [""])[0]
                self.send_json(frame_agent.approvals.inspect(token))
            elif path == "/api/host":
                self.send_json({"os": "SteamOS", "fileManager": None, "computer": DEVICE, "mobile": True} if LOCAL else
                               {"os": frame_host.NAME, "fileManager": frame_host.FILE_MANAGER,
                                "computer": "Mac" if frame_host.MAC else "PC"})
            elif path == "/api/connection":
                self.send_json(connection_state())
            elif path == "/api/connection/events":
                self.connection_events()
            elif path in ("/api/devices", "/api/devices/tailscale", "/api/devices/mdns"):
                self.send_json(devices_get(path, url.query))
            elif path.startswith("/source-image/"):
                from apk_sources import _images
                try:
                    self.send_bytes(*_images.image(path.rsplit("/", 1)[-1]))
                except ClientGone:
                    raise
                except Exception:
                    self.send_json({"error": "Artwork unavailable"}, 404)
            elif path == "/api/sources/details":
                args = parse_qs(url.query)
                try:
                    self.send_json(apk_search.details(source_text({k: v[0] for k, v in args.items()}, "source"),
                                                     source_text({k: v[0] for k, v in args.items()}, "id")))
                except SourceError as e:
                    raise Failure(str(e), 400)
            elif path == "/api/sources":
                self.send_json({"sources": apk_search.sources()})
            elif path == "/api/search":
                self.send_json(source_search(url.query))
            elif path == "/api/apk-versions":
                self.send_json(apk_versions(url.query))
            elif path == "/api/android":
                ensure_master()
                apps = frame_android.list_apps()
                backfill_art(apps=apps)
                self.send_json({"apps": apps})
            elif path == "/api/quest":
                self.send_json(quest_status(url.query))
            elif path == "/api/meta":
                self.send_json(meta_status(url.query))
            elif path == "/api/titles":
                ensure_master()
                titles_list = frame_titles.list_titles()
                backfill_art(titles=titles_list)
                self.send_json({"titles": titles_list})
            elif path == "/api/titles/job":
                self.send_json(title_job(url.query))
            elif path == "/api/licenses":
                self.send_json({"notices": licenses()})
            elif path == "/api/panels":
                self.send_json(panels())
            elif path == "/api/touch":
                self.send_json(_touch.send([]) if parse_qs(url.query).get("start") == ["1"] else dict(_touch.status))
            elif path == "/api/input":
                self.send_json(_input.send([]) if parse_qs(url.query).get("start") == ["1"] else dict(_input.status))
            elif path == "/api/job":
                self.send_json(job_status(url.query))
            elif path == "/api/queue":
                self.send_json(install_queue())
            elif path == "/api/android/displays":
                self.send_json(android_displays())
            elif path == "/api/android/reports":
                self.send_json({"reports": frame_catalog.recent_reports(),
                                "shared": frame_catalog.compat_db.shared()})
            elif path == "/api/android/catalog":
                self.send_json({"apps": frame_catalog.catalog()})
            elif path == "/api/vr/utilities":
                self.send_json(frame_utilities.catalogue(steam_frame("utilities")["utilities"], frame_compat_db.load()))
            elif path == "/api/macview":
                self.send_json(macview_state(parse_qs(url.query)))
            elif path == "/api/telemetry":
                self.send_json(frame_telemetry.state())
            elif path == "/api/contact":
                self.send_json(frame_contact.state())
            elif path == "/api/computer/state":
                self.send_json(json.loads(ssh("python3 -", stdin=(HERE / "frame_computer.py").read_text(), timeout=20)))
            elif path == "/api/status":
                self.send_json(status({}))
            elif path == "/api/steam/owned":
                self.send_json(steam_frame("owned"))
            elif path == "/api/steam/search":
                self.send_json(steam_search(url.query))
            elif path == "/api/webinstall/job":
                self.send_json(webinstall_job(url.query))
            elif path == "/api/shots":
                self.send_json(list_shots())
            elif path == "/api/shots/image":
                self.send_bytes(*shot_image(url.query))
            elif path == "/api/stream":
                self.stream_video(url.query)
            elif path == "/api/screenshot" and parse_qs(url.query).get("view") == ["panel"]:
                self.send_bytes(panel_capture(url.query), "image/png", headers=[("X-Capture-Source", "panel")])
            elif path == "/api/screenshot" and parse_qs(url.query).get("view") == ["headset"]:
                self.send_bytes(headset_view(), "image/png", headers=[("X-Capture-Source", "steamvr")])
            elif path == "/api/screenshot":
                self.send_bytes(ssh(SCREENSHOT, timeout=20, text=False), "image/png",
                                headers=[("X-Capture-Source", "gamescope")])
            else:
                self.send_json({"error": "not found"}, 404)
        except ClientGone:
            raise
        except Failure as e:
            self.send_error_json(str(e), e.status, e.apk)
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except frame_android.FrameError as e:
            self.send_error_json(str(e), 502)
        except Exception as e:
            frame_telemetry.diagnostic(f"GET {path}", e)
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        if not self.local_request():
            return
        path = urlparse(self.path).path
        meant = self.headers.get("X-Frame-Device")
        body = None
        try:
            if path == "/api/upload":
                with working(meant):
                    self.send_json(self.upload())
                return
            handler = POST.get(path)
            if not handler:
                self.send_json({"error": "not found"}, 404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 <= length <= MAX_JSON:
                raise Failure("request body too large", 413)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise Failure("request body must be a JSON object", 400)
            # Not headset work: switching headsets mustn't wait for (or refuse) these.
            with (contextlib.nullcontext() if path in NOT_HEADSET_WORK else working(meant)):
                result = handler(body)
            self.send_json(result)
        except ClientGone:
            raise
        except Failure as e:
            # A poll that couldn't reach the headset is the headset asleep or away, which the page
            # already shows; like GET /api/status, not an error to report.
            polled = (path, action_of(body)) in POLLS and frame_telemetry.link_failed(e)
            if e.status >= 500 and not polled:
                frame_telemetry.diagnostic(f"POST {path} {action_of(body)}", e)
            self.send_error_json(str(e), e.status, e.apk)
        except (ValueError, TypeError) as e:
            self.send_json({"error": f"bad request: {e}"}, 400)
        except frame_android.FrameError as e:
            frame_telemetry.diagnostic(f"POST {path} {action_of(body)}", e)
            self.send_error_json(str(e), 502)
        except Exception as e:
            frame_telemetry.diagnostic(f"POST {path} {action_of(body)}", e)
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def connection_events(self):
        """Server-sent events: the connection state each time it changes, until the page goes.
        (The page reads it with fetch, which can send the X-Frame-UI header; EventSource can't.)"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.close_connection = True
        version = -1
        try:
            while True:
                snap = connection_state() if version < 0 else (LINK.wait(version, 15) if LINK else None)
                if snap is None:
                    if not LINK and version >= 0:
                        time.sleep(15)
                    self.wfile.write(b": still here\n\n")  # keeps proxies and the page's watchdog happy
                else:
                    version = max(snap["version"], 0)
                    self.wfile.write(b"data: " + json.dumps(snap).encode() + b"\n\n")
                self.wfile.flush()
        except OSError:
            pass  # the page went away

    def stream_video(self, query):
        """Raw H.264 of the headset view until the page disconnects (see stream_command)."""
        global _stream_proc
        remote = stream_command(query)
        ensure_master()
        # stderr goes to a file: nothing reads it while streaming, and a full
        # pipe would stall ffmpeg. It's only read if the stream fails to start.
        errors = tempfile.TemporaryFile()
        proc = subprocess.Popen([*SSH, FRAME, remote], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=errors)
        try:
            _live_tunnels.add(proc)
            # One viewer at a time: a new stream (another tab, a reload) ends the last one.
            with _stream_lock:
                old, _stream_proc = _stream_proc, proc
            if old and old.poll() is None:
                old.terminate()
            chunks = _pipe_reader(proc.stdout)
            # Nothing is sent until the first bytes arrive, so a failure to
            # start still comes back as a JSON error.
            first = _next_chunk(chunks, 20)
            if not first:
                proc.kill()
                proc.wait()
                errors.seek(0)
                err = strip_ansi(errors.read().decode(errors="replace")).strip()
                raise Failure(err or "The Frame sent no video for 20 s")
            chunk = first
            try:
                self.send_response(200)
                self.send_header("Content-Type", "video/h264")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
                self.end_headers()
                self.close_connection = True  # the body ends when the connection does
                while chunk:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    # A stalled headset view ends the stream rather than
                    # holding this thread (and the page) forever.
                    chunk = _next_chunk(chunks, STREAM_STALL)
            except OSError:
                pass  # the page stopped watching (or stopped reading); the body has started, so no JSON
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            for f in (proc.stdin, proc.stdout, errors):
                f.close()
            _live_tunnels.discard(proc)

    def upload(self):
        """Raw file body. X-Filename names it; X-Mode is 'push', 'apk' (install), 'apkinfo' (read only)
        or 'title' (a .zip or program to sideload: inspected and kept for /api/titles install)."""
        name = os.path.basename(unquote(self.headers.get("X-Filename", "")))
        mode = self.headers.get("X-Mode", "push")
        length = int(self.headers.get("Content-Length") or 0)
        if not name or name.startswith("."):
            raise Failure("missing filename", 400)
        if length <= 0 or length > MAX_UPLOAD:
            raise Failure("empty or too-large upload", 400)
        if mode in ("apk", "apkinfo") and not name.lower().endswith(".apk"):
            raise Failure("APK install needs a .apk file", 400)
        tmp = Path(tempfile.mkdtemp(prefix="frame-ui-"))
        keep = False
        try:
            dest = tmp / name
            with open(dest, "wb") as f:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(remaining, 1 << 20))
                    if not chunk:
                        raise Failure("upload interrupted", 400)
                    f.write(chunk)
                    remaining -= len(chunk)
            if mode == "media":
                return push_media(dest)
            if mode == "apkinfo":
                # Read an APK for a report without installing it.
                try:
                    info = frame_android.apk_info(str(dest))
                except frame_android.FrameError as e:
                    raise Failure(str(e), 400)
                info.pop("icon_png", None)
                try:
                    frame_android.check_installable(info)
                    info["blocker"] = None
                except frame_android.FrameError as e:
                    info["blocker"] = str(e)
                return {"message": f"Read {info['label']} {info['version']}", "apk": info}
            if mode == "title":
                keep = True  # stage_title owns tmp now, and removes it on failure
                return stage_title(str(dest), temp_dir=str(tmp))
            if mode == "apk":
                # Checked here, before install(), to hand the page a blocker it can offer
                # alternatives for; report these failures the way install() would have.
                start = time.time()
                try:
                    info = frame_android.apk_info(str(dest))
                except frame_android.FrameError as e:
                    frame_android._after_install(None, None, e, start)
                    raise Failure(str(e), 400)
                try:
                    frame_android.check_installable(info)
                except frame_android.FrameError as e:
                    frame_android._after_install(info, None, e, start)
                    raise Failure(str(e), 400, {"package": info["package"], "version_code": info.get("version_code"), "blocker": str(e)})
                ensure_master()
                try:
                    display = self.headers.get("X-APK-Display", "auto")
                    if display not in ("auto", "flat", "vr"):
                        raise frame_android.FrameError("invalid APK display mode")
                    m = frame_android.install(str(dest), source=name,
                                              flatscreen=None if display == "auto" else display == "flat")
                except frame_android.FrameError as e:
                    raise Failure(str(e), 400)
                kind = "VR app" if not m['flatscreen'] else "app"
                notes = " ".join(m.get("vr_issues", []))
                return {"message": f"Installed {m['label']} as its own {kind} in the Steam library. {notes}".strip(), "app": m}
            return {"message": push_file(dest)}
        finally:
            if not keep:
                shutil.rmtree(tmp, ignore_errors=True)


class LoopbackServer(ThreadingHTTPServer):
    def server_bind(self):
        # HTTPServer.server_bind resolves socket.getfqdn(host), a reverse-DNS
        # lookup that can stall for seconds (verified on GitHub's macOS runners).
        # Loopback needs no hostname.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], ClientGone):
            super().handle_error(request, client_address)


_ONE_SERVER = None


def one_server():
    """Only one Frame Control server per user: two would each connect, reconnect and
    edit the headsets on their own, and could move each other's installs to another
    headset. Held until this process exits. (FRAME_CONTROL_DATA_DIR gives a second,
    separate one, as the tests do. A private server, the MCP adapter's, runs alongside:
    it can't add, remove or switch headsets.)"""
    lock = frame_devices.file_lock(frame_host.data_dir("server.lock"), timeout=float(os.environ.get("FRAME_CONTROL_SERVER_WAIT") or 20))  # while the app restarts it
    try:
        lock.__enter__()
    except OSError:
        sys.exit("Frame Control is already running on this computer (the app, or a server started "
                 "from a terminal). Quit it, then try again.")
    return lock


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 47810)))
    ap.add_argument("--exit-on-eof", action="store_true",
                    help="stop cleanly when stdin closes (the app closes it on quit; "
                         "Windows has no SIGTERM to catch)")
    args = ap.parse_args()
    httpd = LoopbackServer(("127.0.0.1", args.port), Handler)
    sweep_tmp()
    threading.Thread(target=apk_search.warm, daemon=True).start()  # big indexes download before the first search
    frame_telemetry.start()
    frame_contact.start()
    global LINK, _ONE_SERVER
    if not LOCAL:
        if not PRIVATE:  # a private server only uses the headsets (see one_server)
            _ONE_SERVER = one_server()
        LINK = frame_link.Link(frame_devices.Registry(), env_alias=FRAME if FRAME_FROM_ENV else None,
                               mux_base=MUX_BASE, control=CONTROL, apply=route, explain=unreachable)
        LINK.work_lock, LINK.work = _work_lock, lambda: _work[0]
        frame_report.link = LINK  # its connection summary goes in every report's diagnostics
        frame_report.start_ssh_check()  # ...with which ssh this is, found once in the background
        LINK.start()
    if not frame_host.WINDOWS:
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    if args.exit_on_eof:
        def watch_stdin():
            # os.read, not sys.stdin.buffer.read: a buffered read holds stdin's lock,
            # and if a signal stops the server first, Python aborts (SIGABRT) at exit
            # when it can't take that lock back from this thread.
            while os.read(0, 4096):
                pass
            threading.Thread(target=httpd.shutdown, daemon=True).start()
        threading.Thread(target=watch_stdin, daemon=True).start()
    try:
        # The real port, which --port 0 leaves to the system (the iPhone app reads it from here).
        print(f"Frame Control on http://127.0.0.1:{httpd.server_address[1]}  (alias: {FRAME}; Ctrl-C to stop)", flush=True)
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # The app both closes stdin and sends SIGTERM on quit; a second signal
        # mid-cleanup would abort it and leave the SSH master running.
        if not frame_host.WINDOWS:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        webinstall_shutdown()
        macview.shutdown()  # close the headset's viewers before the agent goes
        # The master was started with -N, so it stays up until told to exit.
        if LINK:
            LINK.stop()
        for proc in list(_live_tunnels):  # ADB forwards and video streams cut off mid-way
            if proc.poll() is None:
                proc.terminate()
        _purge_titles(now=float("inf"))  # unconfirmed title uploads
        # Staging folders of work still running (a patched APK mid-copy, an index
        # download): leaving below skips the cleanup their TemporaryDirectory would
        # get at interpreter exit. sweep_tmp at the next start catches any missed.
        sweep_tmp(own=True)
    # Stopped as asked, and everything above is cleaned up. Leave now, without
    # Python's interpreter teardown: daemon threads are still running (app index
    # downloads, the stdin watcher, telemetry, the headset link, request handlers)
    # and none can be stopped promptly. Tearing the interpreter down under them
    # occasionally crashed the process (SIGSEGV, seen on Python 3.13 on Linux):
    # OpenSSL's exit cleanup freed state those threads were using. That teardown
    # also runs atexit handlers and weakref finalizers; nothing here registers
    # atexit work, the only finalizers are TemporaryDirectory cleanups (swept
    # above), and the OS frees the one-server lock with the process.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
