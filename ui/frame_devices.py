"""The headsets Frame Control knows, and the addresses each can be reached at.

One headset can answer at several addresses: a LAN IP at home, another in the
office, its mDNS name (frame.local), its Tailscale IP or MagicDNS name. The
registry keeps them all, learns which worked on which network, and hands the
connector (frame_link.py) an order to try them in.

Stored as JSON in frame_host.data_dir("devices.json"). The format is plain so the
iPhone app can share it later; docs/devices.md describes it:

  {"version": 1, "active": "<device id>",
   "devices": [{"id", "name", "alias", "user", "port", "identity_files",
                "addresses": [{"host", "kind": lan|mdns|tailscale|manual, "label",
                               "networks": [network ids it worked on], "last_ok", "last_rtt_ms"}]}],
   "networks": {"<network id>": {"name", "ssid", "gateway", "gateway_mac", "last_seen"}}}

Headsets set up before this existed live only in ~/.ssh/config, in the managed
`# >>> steam-frame (ALIAS) >>>` blocks that scripts/connect.sh and
ui/frame_connect.py write; they're imported from there, so nobody has to add
them again. Each device keeps its alias: Terminal's `ssh frame` and the helper
scripts go on working, and the connector rewrites the block's HostName to the
last address that worked, so they follow it.

Host keys are pinned per headset, not per address: ssh gets
`-o HostKeyAlias=frame-control-<id>` and a known_hosts file of the headset's own
(~/.ssh/frame-control-hosts/<id>), so a different device answering at a
remembered IP is caught.

Python stdlib only.
"""
import base64
import contextlib
import copy
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import frame_host
import frame_network

VERSION = 1
# Everything here can end up in ssh arguments or ~/.ssh/config, so nothing that
# could start an option, add a line, or carry a directive.
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
HOST_RE = re.compile(r"[A-Za-z0-9:][A-Za-z0-9.:-]{0,252}(%[A-Za-z0-9._-]{1,32})?")
TEXT_MAX = 60
KINDS = ("lan", "mdns", "tailscale", "manual")
KIND_LABEL = {"lan": "Local network", "mdns": "mDNS (.local)", "tailscale": "Tailscale", "manual": "Other"}
DEFAULT_USER = "steamos"


class DeviceError(ValueError):
    """Bad input from the page; the server answers 400 with the message."""


def ssh_dir():
    """~/.ssh, or $FRAME_CONTROL_SSH_DIR in tests so they never touch the real one."""
    return Path(os.environ.get("FRAME_CONTROL_SSH_DIR") or Path.home() / ".ssh")


def ssh_config():
    return ssh_dir() / "config"


PIN_DIR = "frame-control-hosts"


def known_hosts(device_id):
    """The headset's own known_hosts file: one per headset, so saving or forgetting one
    headset's key (by ssh or by the app) can never touch another's."""
    return ssh_dir() / PIN_DIR / device_id


def known_hosts_opt(device_id):
    """How ssh is told about it. `~` rather than the full path when it's the usual
    place, so a home folder with a space in its name can't split the option."""
    if os.environ.get("FRAME_CONTROL_SSH_DIR"):
        return str(known_hosts(device_id))
    return f"~/.ssh/{PIN_DIR}/{device_id}"


def host_key_alias(device_id):
    return f"frame-control-{device_id}"


# ---- validation ----------------------------------------------------------------

def check_alias(alias):
    if not isinstance(alias, str) or not NAME_RE.fullmatch(alias):
        raise DeviceError("The SSH alias must be a plain name: letters, digits, dot, dash or underscore")
    return alias


def check_user(user):
    if not isinstance(user, str) or not NAME_RE.fullmatch(user):
        raise DeviceError("The user name must be letters, digits, dot, dash or underscore")
    return user


def check_host(host):
    host = host.strip() if isinstance(host, str) else host
    if (not isinstance(host, str) or not HOST_RE.fullmatch(host) or ".." in host
            or ("%" in host and ":" not in host.split("%")[0])):  # a zone only follows an IPv6 address
        raise DeviceError(f"{host!r} isn't a host name or IP address")
    return host


def check_port(port):
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise DeviceError("The port must be a number") from None
    if not 1 <= port <= 65535:
        raise DeviceError("The port must be between 1 and 65535")
    return port


def check_text(text, what):
    text = (text or "").strip() if isinstance(text, (str, type(None))) else None
    if text is None or len(text) > TEXT_MAX or re.search(r"[\x00-\x1f\x7f]", text):
        raise DeviceError(f"The {what} must be plain text of at most {TEXT_MAX} characters")
    return text


def check_kind(kind):
    if kind not in KINDS:
        raise DeviceError(f"The kind must be one of {', '.join(KINDS)}")
    return kind


def ssh_host(host):
    """A host for ssh's HostName, which expands %-tokens: an IPv6 zone's % is doubled."""
    return host.replace("%", "%%")


# ---- ~/.ssh/config's managed blocks ---------------------------------------------

BLOCK_RE = re.compile(r"# >>> steam-frame \((" + NAME_RE.pattern + r")\) >>>")


def begin_mark(alias):
    return f"# >>> steam-frame ({alias}) >>>"


def end_mark(alias):
    return f"# <<< steam-frame ({alias}) <<<"


def parse_blocks(text):
    """The managed blocks: [{"alias", "hostname", "user", "port", "identity_files"}]."""
    blocks, cur = [], None
    for line in text.splitlines():
        m = BLOCK_RE.fullmatch(line.strip())
        if m:
            cur = {"alias": m.group(1), "hostname": None, "user": None, "port": 22, "port_set": False,
                   "identity_files": []}
            continue
        if cur is None:
            continue
        if line.strip() == end_mark(cur["alias"]):
            blocks.append(cur)
            cur = None
            continue
        f = line.split(None, 1)
        if len(f) != 2:
            continue
        key, value = f[0].lower(), f[1].strip()
        if key == "hostname" and cur["hostname"] is None:
            cur["hostname"] = value.replace("%%", "%")
        elif key == "user" and cur["user"] is None:
            cur["user"] = value
        elif key == "port" and value.isdigit():
            cur["port"], cur["port_set"] = int(value), True
        elif key == "identityfile":
            cur["identity_files"].append(value)
    return blocks


def read_config(path=None):
    path = Path(path or ssh_config())
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


# One edit of ~/.ssh/config at a time: between this app's threads (_config_lock) and
# with Set Up Connection (frame_connect.py and scripts/connect.sh take the same lock
# file). _edit_config also notices any other program writing in between.
_config_lock = threading.Lock()
LOCK_NAME = "config.frame-control.lock"


@contextlib.contextmanager
def file_lock(path, timeout=30):
    """An exclusive lock on `path` (created if need be) shared with other processes:
    POSIX record locks (what zsh's `zsystem flock` takes), or msvcrt on Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                if frame_host.WINDOWS:
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.lockf(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise OSError(f"{path} stayed locked (is Set Up Connection running?)")
                time.sleep(0.1)
        yield
    finally:
        try:
            if frame_host.WINDOWS:
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.lockf(fh, fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def _write_config(path, text, expected):
    """Swap the file in whole (as frame_connect.write_config does), keeping it private.
    Returns False, writing nothing, if the file no longer holds `expected`."""
    fd_, tmp = tempfile.mkstemp(prefix="config.frame-control.", dir=str(path.parent))
    tmp = Path(tmp)
    try:
        with os.fdopen(fd_, "w", encoding="utf-8") as fh:
            fh.write(text)
        frame_host.make_private(tmp)  # best effort: an edit still beats none (repair_permissions insists)
        for attempt in range(20):  # Windows: a running ssh.exe can hold the file for a moment
            if read_config(path) != expected:
                return False
            try:
                os.replace(tmp, path)
                return True
            except PermissionError:
                time.sleep(0.25)
        raise OSError(f"{path} stayed locked by another program")
    finally:
        if tmp.exists():
            tmp.unlink()


def _edit_config(path, change):
    """Apply change(lines) -> new lines or None to the file, retrying if another program
    wrote it meanwhile. -> True if the file changed."""
    with _config_lock, file_lock(path.with_name(LOCK_NAME)):
        for _ in range(5):
            text = read_config(path)
            new = change(text.splitlines())
            if new is None:
                return False
            if _write_config(path, "\n".join(new) + "\n", text):
                return True
        raise OSError(f"{path} kept changing while Frame Control tried to update it")


def repair_permissions(path=None):
    """Give ~/.ssh/config make_private's ACL by swapping in a byte-for-byte copy: for a
    file Windows' OpenSSH refuses ("Bad owner or permissions"). -> True only if the copy
    got that ACL and replaced the file."""
    path = Path(path or ssh_config())
    with _config_lock, file_lock(path.with_name(LOCK_NAME)):
        try:
            data = path.read_bytes()
        except OSError:
            return False
        fd_, tmp = tempfile.mkstemp(prefix="config.frame-control.", dir=str(path.parent))
        tmp = Path(tmp)
        try:
            with os.fdopen(fd_, "wb") as fh:
                fh.write(data)
            if not frame_host.make_private(tmp):
                return False
            for attempt in range(20):  # a running ssh.exe can hold the file for a moment
                if path.read_bytes() != data:
                    return False
                try:
                    os.replace(tmp, path)
                    return True
                except PermissionError:
                    time.sleep(0.25)
            return False
        finally:
            if tmp.exists():
                tmp.unlink()


def rewrite_block(alias, path=None, hostname=None, user=None, port=None, expect=None):
    """Change HostName, User or Port inside ALIAS's managed block, leaving the rest of the
    file alone. -> True if the file changed. Does nothing if there's no such block, or
    if `expect` ({"hostname", "user", "port"}; None values match anything) no longer
    describes the block, checked under the lock: someone else changed it meanwhile."""
    def change(lines):
        if expect:
            block = next((b for b in parse_blocks("\n".join(lines)) if b["alias"] == alias), None)
            # A port the block doesn't set is inherited from elsewhere in the file: not compared.
            if not block or any(v is not None and block[k] != v and (k != "port" or block["port_set"])
                                for k, v in expect.items()):
                return None
        block = next((b for b in parse_blocks("\n".join(lines)) if b["alias"] == alias), None)
        # Port 22 needs no line, unless the block would otherwise inherit another port
        # from a later Host entry (which ssh would use).
        force = bool(port) and block is not None and not block["port_set"] and \
            effective_port(alias, config_path) != int(port)
        return _rewritten(lines, alias, hostname, user, port, force)
    config_path = Path(path or ssh_config())
    return _edit_config(config_path, change)


def _rewritten(lines, alias, hostname, user, port, force_port=False):
    begin, end = begin_mark(alias), end_mark(alias)
    if begin not in lines or end not in lines:
        return None
    i, j = lines.index(begin), lines.index(end)
    if j < i:
        return None
    block = lines[i:j]
    want = {"hostname": ssh_host(hostname) if hostname else None, "user": user,
            "port": str(port) if port else None}
    out, seen = [], set()
    for line in block:
        f = line.split(None, 1)
        key = f[0].lower() if f else ""
        if key in want and want[key] is not None and key not in seen:
            seen.add(key)
            # An existing Port line is kept, even for 22: dropping it could let a later
            # `Host *` Port apply to Terminal but not to the app.
            out.append(f"  {f[0]} {want[key]}")
        else:
            out.append(line)
    if want["port"] and (want["port"] != "22" or force_port) and "port" not in seen:
        at = next((n + 1 for n, line in enumerate(out) if line.split(None, 1)[:1] == ["HostName"]), 2)
        out.insert(at, f"  Port {want['port']}")
    new = lines[:i] + out + lines[j:]
    return None if new == lines else new


def remove_block(alias, path=None):
    def change(lines):
        begin, end = begin_mark(alias), end_mark(alias)
        if begin not in lines or end not in lines or lines.index(end) < lines.index(begin):
            return None
        return lines[:lines.index(begin)] + lines[lines.index(end) + 1:]
    return _edit_config(Path(path or ssh_config()), change)


def effective_port(alias, config):
    """The port ssh uses for ALIAS with this config file (`ssh -F FILE -G ALIAS`), else 22."""
    try:
        out = frame_host.run_ssh(["ssh", "-F", str(config), "-G", alias], capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return 22
    m = re.search(r"^port (\d+)$", out, re.M)
    port = int(m.group(1)) if m else 22
    return port if 1 <= port <= 65535 else 22


# ---- pinned host keys -------------------------------------------------------------

def _keygen(*args):
    try:
        return frame_host.run_ssh(["ssh-keygen", *args], capture_output=True, stdin=subprocess.DEVNULL, text=True,
                                  timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None


def pinned(device_id):
    """Whether a key is saved for the headset. Entries are plain text (ssh gets
    HashKnownHosts=no), but ask ssh-keygen too in case one was hashed."""
    target = known_hosts(device_id)
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    name = host_key_alias(device_id)
    if any(line.split(None, 1)[0].split(",").count(name) for line in lines
           if line.strip() and not line.startswith("#")):
        return True
    r = _keygen("-F", name, "-f", str(target))
    return bool(r and r.returncode == 0 and r.stdout.strip())


def seed_pin(device_id, hosts, port=22, sources=None):
    """Copy the host keys ssh already trusts for one of `hosts` into the headset's file
    under its alias, so moving to per-headset pinning asks nobody to trust anything again.
    -> True if a key is pinned."""
    if pinned(device_id):
        return True
    sources = sources or [ssh_dir() / "known_hosts", ssh_dir() / "known_hosts2"]
    name = host_key_alias(device_id)
    for host in hosts:
        wanted = host if port == 22 else f"[{host}]:{port}"
        keys = []
        for src in sources:
            if not Path(src).is_file():
                continue
            r = _keygen("-F", wanted, "-f", str(src))
            for line in (r.stdout if r else "").splitlines():
                f = line.split()
                if len(f) >= 3 and not line.startswith("#") and not f[0].startswith("@"):
                    keys.append(f"{name} {f[1]} {f[2]}")
        if keys:
            _write_pin(device_id, keys)
            return True
    return False


def pin_dir():
    """Where the per-headset files go, created private: ssh won't create it itself."""
    d = ssh_dir() / PIN_DIR
    d.mkdir(**({} if frame_host.WINDOWS else {"mode": 0o700}), parents=True, exist_ok=True)
    return d


def _write_pin(device_id, lines):
    target = known_hosts(device_id)
    fd_, tmp = tempfile.mkstemp(prefix=".seed-", dir=str(pin_dir()))
    with os.fdopen(fd_, "w", encoding="utf-8") as fh:
        fh.write("\n".join(dict.fromkeys(lines)) + "\n")
    frame_host.make_private(tmp)  # Windows: this user only, not the folder's inherited ACL
    os.replace(tmp, target)  # whole file at once: ssh never sees half of it


# The host key types OpenSSH writes to known_hosts.
KEY_TYPE_RE = re.compile(r"ssh-ed25519|ssh-rsa|ssh-dss|ecdsa-sha2-nistp(?:256|384|521)|"
                         r"sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com")


def fingerprint(blob):
    """OpenSSH's SHA256 fingerprint of a base64 key blob (what `ssh-keygen -l` and ssh -v print),
    or None if it isn't base64."""
    try:
        raw = base64.b64decode(blob, validate=True)
    except (ValueError, TypeError):
        return None
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def key_entry(line, name):
    """(type, blob, fingerprint) from a known_hosts line for `name`, or None."""
    f = (line or "").split()
    if len(f) < 3 or line.startswith("#") or name not in f[0].split(",") or not KEY_TYPE_RE.fullmatch(f[1]):
        return None
    fp = fingerprint(f[2])
    return (f[1], f[2], fp) if fp else None


def pinned_keys(device_id):
    """[(type, fingerprint)] of the keys saved for a headset (plain entries only)."""
    try:
        lines = known_hosts(device_id).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    name = host_key_alias(device_id)
    return [(e[0], e[2]) for e in (key_entry(line, name) for line in lines) if e]


def pin_key(device_id, key_type, blob):
    """Save one key as the headset's whole identity, replacing whatever was saved: what the
    person confirmed after seeing its fingerprint (see frame_link.Link.trust_identity)."""
    if not KEY_TYPE_RE.fullmatch(key_type or "") or not fingerprint(blob):
        raise DeviceError("That isn't an SSH host key")
    _write_pin(device_id, [f"{host_key_alias(device_id)} {key_type} {blob}"])


def scan_file(device_id):
    """(path, how ssh is told) for a throwaway known_hosts file that a key check writes the
    headset's key to, without touching the saved one. `~` as in known_hosts_opt."""
    name = f".scan-{device_id}-{secrets.token_hex(4)}"
    path = pin_dir() / name
    path.touch(**({} if frame_host.WINDOWS else {"mode": 0o600}))
    frame_host.make_private(path)
    return path, (str(path) if os.environ.get("FRAME_CONTROL_SSH_DIR") else f"~/.ssh/{PIN_DIR}/{name}")


def identity_established(device):
    """Whether this headset ever had an SSH identity here: the device's own flag (set once it
    had a key or a connection that worked, and never cleared, whatever happens to its
    addresses), a fingerprint recorded in devices.json, a pin file at all (even empty,
    unreadable or not parseable), or an address that worked. Then a missing or unusable pin
    fails closed, needing Check its key; only a headset with none of these may save the
    first key it meets (accept-new)."""
    if device.get("identity_established") or device.get("identity"):
        return True
    try:
        if os.path.lexists(known_hosts(device["id"])):
            return True
    except OSError:
        return True
    return any(a.get("last_ok") for a in device.get("addresses") or ())


def forget_pin(device_id):
    """Drop a headset's saved key (the headset was removed, or the API's forget-identity).
    The next connection trusts whatever key the headset shows, as a first connection does;
    the page's Trust a new key flow (frame_link) shows the key first instead."""
    try:
        known_hosts(device_id).unlink()
        return True
    except FileNotFoundError:
        return False


# ---- address order --------------------------------------------------------------------

def order_addresses(addresses, network_id, tailscale_up):
    """The order to try a device's addresses in, each with why it's there:
    known to work on this network, then mDNS, then Tailscale if it's up, then the rest
    (addresses that only ever worked elsewhere last). The user's order breaks ties."""
    def group(a):
        nets = a.get("networks") or []
        if network_id and network_id in nets:
            return 0, "worked on this network before"
        if a["kind"] == "mdns":
            return 1, "mDNS name"
        if a["kind"] == "tailscale":
            return (2, "Tailscale") if tailscale_up else (5, "Tailscale isn't running")
        if nets:
            return 4, "worked on another network"
        return 3, "not tried on this network yet"
    ranked = sorted(enumerate(addresses), key=lambda p: (group(p[1])[0], p[0]))
    return [(a, group(a)[1]) for _, a in ranked]


# ---- the registry ------------------------------------------------------------------------

def new_address(host, kind=None, label=""):
    host = check_host(host)
    return {"host": host, "kind": check_kind(kind) if kind else frame_network.guess_kind(host),
            "label": check_text(label, "label"), "networks": [], "last_ok": None, "last_rtt_ms": None}


class Registry:
    """devices.json, loaded once and saved on every change. Thread-safe."""

    def __init__(self, path=None, config=None):
        self.path = Path(path or frame_host.data_dir("devices.json"))
        self.config = Path(config) if config else None  # None: ssh_config() at call time
        self.lock = threading.RLock()
        self._depth = 0  # nested _changing() calls
        self._mtime = None  # devices.json as last loaded or saved
        self.data = {"version": VERSION, "active": None, "devices": [], "networks": {}}
        self.load()

    # -- storage --
    def load(self):
        with self.lock:
            try:
                self._mtime = self.path.stat().st_mtime_ns
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            if isinstance(data, dict) and isinstance(data.get("devices"), list):
                data.setdefault("networks", {})
                data.setdefault("active", None)
                data["devices"] = [d for d in data["devices"] if self._sane(d)]
                # The headset in use is this server's own choice: another server picking a
                # different one mustn't move commands (an install, say) under it. The file's
                # choice is only where a server starts.
                # Kept even if another server removed it, so the connector can see that
                # (and not quietly move to another headset in the middle of an install).
                mine = self.data.get("active")
                if mine:
                    data["active"] = mine
                self.data = data

    @staticmethod
    def _sane(d):
        try:
            check_alias(d["alias"])
            d["addresses"] = [a for a in d.get("addresses") or [] if isinstance(a, dict) and HOST_RE.fullmatch(a.get("host", ""))
                              and a.get("kind") in KINDS]
            for a in d["addresses"]:
                a.setdefault("networks", [])
                a.setdefault("label", "")
            if d.get("identity") or any(a.get("last_ok") for a in d["addresses"]):
                # Files from before the flag existed: it had an identity (or a connection that
                # worked, which saved one). Kept from now on, whatever happens to its addresses.
                d["identity_established"] = True
            return NAME_RE.fullmatch(d.get("id", "")) is not None
        except (KeyError, TypeError, DeviceError):
            return False

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
            self._mtime = self.path.stat().st_mtime_ns

    def _refresh(self):
        """Pick up what another Frame Control server saved (the app and a standalone
        server can share devices.json)."""
        with self.lock:
            try:
                if self.path.stat().st_mtime_ns != self._mtime:
                    self.load()
            except OSError:
                pass

    @contextlib.contextmanager
    def _changing(self):
        """Every change holds this registry's lock and a lock file shared with other
        processes, and starts from what's on disk, so no server saves over another's
        change. Nested calls (sync_from_config adding a device) share the outer one."""
        with self.lock:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            with file_lock(self.path.with_name(self.path.name + ".lock")):
                self.load()
                self._depth = 1
                try:
                    yield
                finally:
                    self._depth = 0

    def snapshot(self):
        self._refresh()
        with self.lock:
            return copy.deepcopy(self.data)

    # -- lookups --
    def devices(self):
        self._refresh()
        with self.lock:
            return copy.deepcopy(self.data["devices"])

    def _find(self, device_id):
        for d in self.data["devices"]:
            if d["id"] == device_id:
                return d
        raise DeviceError("No such headset (it may have been removed)")

    def get(self, device_id):
        self._refresh()
        with self.lock:
            return copy.deepcopy(self._find(device_id))

    def by_alias(self, alias):
        self._refresh()
        with self.lock:
            return next((copy.deepcopy(d) for d in self.data["devices"] if d["alias"] == alias), None)

    def active(self):
        self._refresh()
        with self.lock:
            return self.data.get("active")

    def emptied(self):
        self._refresh()
        with self.lock:
            return bool(self.data.get("emptied")) and not self.data["devices"]

    def set_active(self, device_id):
        with self._changing():
            self._find(device_id)
            self.data["active"] = device_id
            self.save()

    # -- devices --
    def add_device(self, alias, name=None, user=DEFAULT_USER, port=22, hosts=(), identity_files=()):
        with self._changing():
            check_alias(alias)
            if any(d["alias"] == alias for d in self.data["devices"]):
                raise DeviceError(f"There's already a headset with the alias {alias}")
            ids = {d["id"] for d in self.data["devices"]}
            device_id = secrets.token_hex(4)
            while device_id in ids:
                device_id = secrets.token_hex(4)
            d = {"id": device_id, "name": check_text(name or ("Steam Frame" if alias == "frame" else alias), "name"),
                 "alias": alias, "user": check_user(user or DEFAULT_USER), "port": check_port(port),
                 "identity_files": [str(f) for f in identity_files][:8], "addresses": [], "config_host": None,
                 "added": time.time()}
            for host in hosts:
                if host and not any(a["host"] == host for a in d["addresses"]):
                    d["addresses"].append(new_address(host))
            self.data["devices"].append(d)
            self.data.pop("emptied", None)
            if not self.data.get("active"):
                self.data["active"] = device_id
            self.save()
            return copy.deepcopy(d)

    def update_device(self, device_id, name=None, user=None, port=None):
        """-> the device after the change. The caller mirrors user and port into ~/.ssh/config."""
        with self._changing():
            d = self._find(device_id)
            # Check everything first: a rejected edit changes nothing.
            name = None if name is None else (check_text(name, "name") or d["alias"])
            user = None if user is None else check_user(user)
            port = None if port is None else check_port(port)
            d.update({k: v for k, v in (("name", name), ("user", user), ("port", port)) if v is not None})
            self.save()
            return copy.deepcopy(d)

    def remove_device(self, device_id):
        """Forget a headset. Its ~/.ssh/config block (if kept) isn't imported again
        unless Set Up Connection changes it."""
        with self._changing():
            d = self._find(device_id)
            self.data["devices"].remove(d)
            self.data.setdefault("dismissed", {})[d["alias"]] = d.get("config_host") or ""
            if not self.data["devices"]:
                self.data["emptied"] = True  # removed on purpose: don't fall back to the `frame` alias
            if self.data.get("active") == device_id:
                self.data["active"] = self.data["devices"][0]["id"] if self.data["devices"] else None
            self.save()
            return d

    # -- addresses --
    def _addr(self, d, host):
        for a in d["addresses"]:
            if a["host"] == host:
                return a
        raise DeviceError(f"{host} isn't one of this headset's addresses")

    def add_address(self, device_id, host, kind=None, label=""):
        with self._changing():
            d = self._find(device_id)
            a = new_address(host, kind, label)
            if any(x["host"] == a["host"] for x in d["addresses"]):
                raise DeviceError(f"{a['host']} is already on the list")
            if len(d["addresses"]) >= 32:
                raise DeviceError("That's enough addresses for one headset")
            d["addresses"].append(a)
            self.save()
            return copy.deepcopy(a)

    def update_address(self, device_id, host, new_host=None, kind=None, label=None):
        with self._changing():
            d = self._find(device_id)
            a = self._addr(d, host)
            # Check everything first: a rejected edit changes nothing.
            moved = new_host is not None and new_host != host
            if moved:
                new_host = check_host(new_host)
                if any(x["host"] == new_host for x in d["addresses"]):
                    raise DeviceError(f"{new_host} is already on the list")
            kind = None if kind is None else check_kind(kind)
            label = None if label is None else check_text(label, "label")
            if moved:
                a.update(host=new_host, networks=[], last_ok=None, last_rtt_ms=None)  # a new place: learn again
            if kind is not None:
                a["kind"] = kind
            if label is not None:
                a["label"] = label
            self.save()
            return copy.deepcopy(a)

    def remove_address(self, device_id, host):
        with self._changing():
            d = self._find(device_id)
            d["addresses"].remove(self._addr(d, host))
            self.save()

    def move_address(self, device_id, host, delta):
        with self._changing():
            d = self._find(device_id)
            a = self._addr(d, host)
            i = d["addresses"].index(a)
            j = max(0, min(len(d["addresses"]) - 1, i + int(delta)))
            d["addresses"].insert(j, d["addresses"].pop(i))
            self.save()

    def record_success(self, device_id, host, network_id, rtt_ms):
        """Learn: this address worked on this network."""
        with self._changing():
            try:
                a = self._addr(self._find(device_id), host)
            except DeviceError:
                return
            if network_id and network_id not in a["networks"]:
                a["networks"] = (a["networks"] + [network_id])[-16:]
            a["last_ok"] = time.time()
            a["last_rtt_ms"] = rtt_ms
            self._find(device_id)["identity_established"] = True  # survives editing the address away
            self.save()

    def record_identity(self, device_id, fingerprints):
        """Remember the fingerprints of the key saved for a headset, so that losing the pin
        file later can't turn into trusting whatever answers (identity_established)."""
        fingerprints = [f for f in fingerprints if isinstance(f, str) and f.startswith("SHA256:")][:8]
        if not fingerprints:
            return
        with self._changing():
            try:
                d = self._find(device_id)
            except DeviceError:
                return
            if d.get("identity") != fingerprints or not d.get("identity_established"):
                d["identity"] = fingerprints
                d["identity_established"] = True
                self.save()

    def undismiss(self, alias):
        """Set Up Connection is about to run for this alias: import its block again."""
        with self._changing():
            if self.data.get("dismissed", {}).pop(alias, None) is not None:
                self.save()

    def set_config_host(self, device_id, host):
        with self._changing():
            try:
                self._find(device_id)["config_host"] = host
            except DeviceError:
                return
            self.save()

    # -- networks --
    def record_network(self, net):
        """Remember a network we've seen (for naming it), keeping its user-given name."""
        if not net or not net.get("id"):
            return
        with self._changing():
            known = self.data["networks"].get(net["id"]) or {"name": ""}
            changed = (known.get("ssid") != (net.get("ssid") or known.get("ssid")) or
                       time.time() - (known.get("last_seen") or 0) > 3600 or "gateway" not in known)
            known.update(ssid=net.get("ssid") or known.get("ssid"), gateway=net.get("gateway"), wifi=net.get("wifi"),
                         gateway_mac=net.get("gateway_mac"), last_seen=time.time())
            self.data["networks"][net["id"]] = known
            if changed:
                self.save()

    def name_network(self, network_id, name):
        with self._changing():
            if network_id not in self.data["networks"]:
                raise DeviceError("That network hasn't been seen")
            self.data["networks"][network_id]["name"] = check_text(name, "network name")
            self.save()

    def network_name(self, net):
        """What to call a network: the name given to it, its Wi-Fi name, or its router."""
        if not net:
            return "No network"
        self._refresh()
        with self.lock:
            known = self.data["networks"].get(net.get("id") or "") or {}
        if known.get("name"):
            return known["name"]
        ssid = net.get("ssid") or known.get("ssid")
        if ssid:
            return ssid
        if net.get("gateway"):
            return f"{'Wi-Fi' if net.get('wifi') else 'Network'} via {net['gateway']}"
        return "No network"

    # -- ~/.ssh/config --
    def sync_from_config(self, seed=True):
        """Import managed blocks we don't know yet, and pick up a HostName that Set Up
        Connection changed since we last looked. -> True if anything changed."""
        blocks = parse_blocks(read_config(self.config))
        for b in blocks:
            if not b["port_set"]:
                # No Port in the block: another Host entry may give one (ssh uses the first).
                b["port"] = effective_port(b["alias"], self.config or ssh_config())
        changed = False
        with self._changing():
            first = not self.data["devices"] and not self.data.get("active")
            for b in blocks:
                host = b["hostname"] if b["hostname"] and HOST_RE.fullmatch(b["hostname"]) else None
                user = b["user"] if b["user"] and NAME_RE.fullmatch(b["user"]) else DEFAULT_USER
                d = next((x for x in self.data["devices"] if x["alias"] == b["alias"]), None)
                dismissed = self.data.get("dismissed", {})
                if d is None and b["alias"] in dismissed:
                    if dismissed[b["alias"]] == (host or ""):
                        continue  # removed on the Devices tab; unchanged since
                    del dismissed[b["alias"]]
                if d is None:
                    try:
                        d = self._find(self.add_device(b["alias"], user=user, port=b["port"],
                                                       identity_files=b["identity_files"])["id"])
                    except DeviceError:
                        continue
                    if host:
                        d["addresses"].append(dict(new_address(host), label="From Set Up Connection"))
                    d["config_host"] = host
                    changed = True
                    if seed and host and seed_pin(d["id"], [host], b["port"]):
                        d["identity"] = [f for _, f in pinned_keys(d["id"])][:8] or d.get("identity")
                        d["identity_established"] = True
                elif host and host != d.get("config_host"):
                    # Set Up Connection ran again and found the headset somewhere new.
                    d["config_host"] = host
                    if not any(a["host"] == host for a in d["addresses"]):
                        d["addresses"].insert(0, dict(new_address(host), label="From Set Up Connection"))
                    if seed and seed_pin(d["id"], [host], b["port"]) and not d.get("identity"):
                        d["identity"] = [f for _, f in pinned_keys(d["id"])][:8] or None
                        d["identity_established"] = True
                    changed = True
                if d.get("config_login") != [user, b["port"]]:
                    # Set Up Connection (or an edit) changed who to log in as, or the port.
                    if d.get("config_login") is not None and [d["user"], d["port"]] != [user, b["port"]]:
                        d["user"], d["port"] = user, b["port"] if 1 <= b["port"] <= 65535 else d["port"]
                    d["config_login"] = [user, b["port"]]
                    changed = True
                if d["identity_files"] != b["identity_files"] and b["identity_files"]:
                    d["identity_files"] = b["identity_files"][:8]
                    changed = True
                d["managed"] = True
            aliases = {b["alias"] for b in blocks}
            for d in self.data["devices"]:
                d["managed"] = d["alias"] in aliases
            if first:
                # First import: the headset the app used before is `frame`, even if Set Up
                # Connection put another block above it.
                frame = next((d for d in self.data["devices"] if d["alias"] == "frame"), None)
                if frame and self.data.get("active") != frame["id"]:
                    self.data["active"] = frame["id"]
                    changed = True
            if changed:
                self.save()
        return changed
