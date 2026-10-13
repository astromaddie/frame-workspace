"""The connection to the active headset: which address to use, and every step of getting there.

A background thread (Link) keeps one SSH connection to the active headset open
and publishes what it's doing, stage by stage, for the page's connection pill:

  1. network   checking this computer's network (gateway, Wi-Fi, Tailscale)
  2. find      finding the headset: every address probed on port 22 at once
  3. ssh       opening SSH to the address that answered
  4. identity  checking the headset's identity (its pinned host key)
  5. login     logging in as the device's user
  then connected (network, address, round trip), or failed at a stage with a
  plain reason and a countdown to the next try.

Addresses go in the order frame_devices.order_addresses gives. All are probed
at once; the best-ranked one that answers wins, waiting a moment (PREFER) for a
better-ranked address that's still trying, happy-eyeballs style. If SSH to the
winner fails in a way another address could fix (a different device answered,
the link dropped), the next one that answered is tried.

The server hands in `apply(alias, host_opts)`, which points every ssh, scp and
rsync it runs at the alias with `-o HostName=<address>` and friends, so they all
follow. Where ssh can share one connection (not Windows), the master connection
lives here; it reconnects when it dies, when this computer changes networks, and
when the page asks.

Python stdlib only. Runs on this computer, never on the Frame.
"""
import copy
import hashlib
import ipaddress
import os
import queue
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time

import frame_devices
import frame_host
import frame_network
import frame_telemetry

PROBE_TIMEOUT = 4      # seconds for a TCP answer on port 22
RESOLVE_GRACE = 6      # ...after however long the name lookup took, up to this much
PREFER = 0.35          # how long an answer waits for a better-ranked address still trying
HANDSHAKE_TIMEOUT = 25
TICK = 2               # the loop's heartbeat
NETWORK_EVERY = 5      # how often the network fingerprint is read
TAILSCALE_EVERY = 30
RETRY = (5, 10, 20, 30)  # seconds before automatic retries after a failure
REQUEST_GAP = 5        # a request may start a new attempt this long after the last one
SCAN_TTL = 600         # a key shown for "Trust a new key" may be trusted this long after it was read

STAGES = [("network", "Checking this computer's network"), ("find", "Finding the headset"),
          ("ssh", "Opening SSH"), ("identity", "Checking the headset's identity"),
          ("login", "Logging in")]

# What ssh -v prints at each step (OpenSSH on macOS, Linux and Windows).
CONNECTING = re.compile(r"Connecting to (\S+) \[([^\]]+)\] port (\d+)")
ESTABLISHED = re.compile(r"Connection established")
HOSTKEY = re.compile(r"Server host key: (\S+) (\S+)")
KNOWN = re.compile(r"is known and matches")
ADDED = re.compile(r"Permanently added")
CHANGED = re.compile(r"REMOTE HOST IDENTIFICATION HAS CHANGED|Host key verification failed")
UNKNOWN = re.compile(r"No \S+ host key is known for")
AUTH_START = re.compile(r"Authentications that can continue|Next authentication method")
AUTHED = re.compile(r"Authenticated to |Authentication succeeded")
DENIED = re.compile(r"Permission denied")


def now():
    return time.time()


def ssh_g(alias):
    """(hostname, port, user, proxied) from `ssh -G ALIAS`, for a headset that's only an
    ssh alias. proxied: it goes through ProxyJump or ProxyCommand, so only ssh can reach it."""
    try:
        out = frame_host.run_ssh(["ssh", "-G", alias], capture_output=True, stdin=subprocess.DEVNULL, text=True,
                                 timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    got = {}
    for line in out.splitlines():
        k, _, v = line.partition(" ")
        if k in ("hostname", "port", "user", "proxyjump", "proxycommand") and k not in got:
            got[k] = v.strip()
    port = int(got["port"]) if got.get("port", "").isdigit() else 22
    proxied = any(got.get(k) not in (None, "", "none") for k in ("proxyjump", "proxycommand"))
    return got.get("hostname") or alias, port, got.get("user"), proxied


def proxy_route(alias):
    """How ~/.ssh/config sends ALIAS: (None, None) directly, ("jump", spec) through
    ProxyJump, or ("command", text) through a ProxyCommand."""
    try:
        out = frame_host.run_ssh(["ssh", "-G", alias], capture_output=True, stdin=subprocess.DEVNULL, text=True,
                                 timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    got = {}
    for line in out.splitlines():
        k, _, v = line.partition(" ")
        if k in ("proxyjump", "proxycommand") and k not in got:
            got[k] = v.strip()
    if got.get("proxyjump") not in (None, "", "none"):
        return "jump", got["proxyjump"]
    if got.get("proxycommand") not in (None, "", "none"):
        return "command", got["proxycommand"]
    return None, None


# One jump host, as ProxyJump names it: [user@]host[:port], nothing that could start an
# option, split a shell word or carry an ssh % token.
JUMP_RE = re.compile(r"(?:([A-Za-z0-9][A-Za-z0-9._-]{0,63})@)?"
                     r"(\[[0-9A-Fa-f:.]{2,45}\]|[A-Za-z0-9][A-Za-z0-9.-]{0,252})(?::(\d{1,5}))?")


def probe(host, port, timeout=PROBE_TIMEOUT, update=None):
    """Try a TCP connection to host:port. -> {"state", "detail", "ip", "rtt_ms"}.

    state: answered, unresolved, timeout, refused, unreachable or error. update(fields)
    reports progress (resolving, trying) as it happens."""
    update = update or (lambda **_: None)
    update(state="resolving", detail="Looking up the name")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as e:
        return {"state": "unresolved", "detail": "Can't find this name on the network", "error": str(e)}
    # The time out is for connecting: macOS can take 5 s to look up a .local name
    # (it waits for an IPv6 answer that never comes), which says nothing about the headset.
    deadline = now() + timeout
    last = None
    infos = infos[:4]
    for n, (family, kind, proto, _, addr) in enumerate(infos):
        ip = addr[0]
        if family == socket.AF_INET6 and len(addr) > 3 and addr[3] and "%" not in ip:
            try:  # a link-local IPv6 address only works with its interface
                # Windows' ssh takes only the number: its names ("wireless_32768") don't resolve.
                ip = f"{ip}%{addr[3] if frame_host.WINDOWS else socket.if_indextoname(addr[3])}"
            except (OSError, AttributeError):
                pass
        left = deadline - now()
        if left <= 0:
            break
        update(state="trying", detail=f"Trying {ip}", ip=ip)
        s = socket.socket(family, kind, proto)
        # Share the time out, so one address that never answers (a dead IPv6 route,
        # say) leaves the others their turn.
        s.settimeout(left / (len(infos) - n))
        t0 = time.monotonic()
        try:
            s.connect(addr)
            rtt = round((time.monotonic() - t0) * 1000, 1)
            return {"state": "answered", "detail": f"Answered in {rtt:g} ms", "ip": ip, "rtt_ms": rtt}
        except socket.timeout:
            last = {"state": "timeout", "detail": "No answer", "ip": ip}
        except ConnectionRefusedError:
            last = {"state": "refused", "detail": "Refused: SSH isn't on at this address", "ip": ip}
        except OSError as e:
            last = {"state": "unreachable", "detail": f"Can't get there ({e.strerror or e})", "ip": ip}
        finally:
            s.close()
    return last or {"state": "timeout", "detail": "No answer"}


def _ip_kind(text):
    """ipv4, ipv6, ipv6 link-local or tailscale for an IP address (zone allowed), else None."""
    try:
        ip = ipaddress.ip_address((text or "").strip("[]").split("%")[0])
    except ValueError:
        return None
    if frame_network.is_tailscale(str(ip)):
        return "tailscale"
    return "ipv4" if ip.version == 4 else "ipv6 link-local" if ip.is_link_local else "ipv6"


def address_kind(host, ip=None):
    """What sort of address a probe row is, for diagnostics, never the address itself:
    ".local", "ipv4", "ipv6", "ipv6 link-local", "tailscale" or "hostname", plus what a
    name resolved to (".local->ipv6 link-local") when the probe got that far."""
    h = (host or "").lower().rstrip(".")
    kind = _ip_kind(h) or (".local" if h.endswith(".local") else
                           "tailscale" if frame_network.is_tailscale(h) else "hostname")
    got = _ip_kind(ip) if ip and not _ip_kind(h) else None
    return f"{kind}->{got}" if got and got != kind else kind


def probes_summary(probes):
    """Each address tried, as its kind and how the probe went: "alias .local->ipv4 timeout"."""
    out = []
    for row in probes or ():
        lead = "alias " if row.get("label") == "from ~/.ssh/config" else ""
        out.append(f"{lead}{address_kind(row.get('host'), row.get('ip'))} {row.get('state') or '?'}")
    return "; ".join(out)


# Exactly the tokens the scrubbers write (here and frame_telemetry.scrub), nothing else in <...>.
PLACEHOLDER = re.compile(r"(<(?:host|user|email|ip|mac|steamid|hex|token|ssh-key|pem|url|redacted)>)")


def hide_hosts(text, hosts, keep=()):
    """text with each of `hosts` (the headset's own addresses and names) replaced by <host>,
    longest first and whatever the case, so a bare name like "steamdeck" that the scrubber
    can't recognise goes too."""
    keep = {k.lower() for k in keep if k} | KEYWORDS  # a headset called "host" mustn't eat ssh's wording
    names = sorted({h for h in hosts if h and h.lower() not in keep}, key=len, reverse=True)
    parts = PLACEHOLDER.split(text)  # never inside a scrubber's own <token> already there
    for i in range(0, len(parts), 2):
        for h in names:
            parts[i] = re.sub(r"(?<![\w.:-])%s(?![\w-]|[:.%%]\w)" % re.escape(h), "<host>", parts[i], flags=re.I)
    return "".join(parts)


# ssh names the host it was going to after these words ("Could not resolve hostname X",
# "connect to host X port 22", "Timed out talking to X"): whatever follows goes, known or not.
OPERAND = re.compile(r"(?i)\b(hostname|host|to(?:\s+host)?)\s+(?!<)([^\s:,;'\"()|]+)")
PLAIN_WORDS = {"answer", "the", "a", "an", "this", "it", "its", "be", "connect", "find", "work", "try"}
KEYWORDS = {"host", "hostname", "to", "port"} | PLAIN_WORDS


def hide_operands(text):
    def one(m):
        word = m.group(2).rstrip(".")
        dots = m.group(2)[len(word):]
        return m.group(0) if word.lower() in PLAIN_WORDS else f"{m.group(1)} <host>{dots}"
    return OPERAND.sub(one, text)


def scrub_failure(text, hosts=()):
    """Free text about a failed attempt, for the log: the attempt's own names, whole and
    longest first (case-insensitively, never ssh's own words), then anything ssh names as a
    host, then the shared scrubber (addresses, paths, user names)."""
    return frame_telemetry.scrub(hide_operands(hide_hosts(str(text or ""), hosts)), 600)


def failure_detail(error):
    """What an address that answered but failed SSH shows: the short reason when there is one
    (the whole message is the banner's headline already), else the message."""
    error = error or {}
    return error.get("short") or error.get("message") or "SSH failed"


def failure_category(message, raw=""):
    """The telemetry error category (frame_unreachable, frame_auth, ...) for a failure: a fixed
    word, never its text."""
    return frame_telemetry.categorize(f"{message or ''}\n{raw or ''}")[0]


_logged = {}            # failure line -> {"at": when last written, "repeats": since then}
LOG_REPEAT_EVERY = 300  # the same failure, retried every 30 s, goes in the log at most this often
LOG_REMEMBER = 32       # different failures remembered for that


def log_failure(stage, message, raw="", probes=(), hosts=()):
    """One failed connection attempt to stderr (the app's server.log), scrubbed when written,
    with the hosts of that attempt (so a later switch of headset can't let them through)."""
    hosts = list(hosts) + [r.get(k) for r in probes or () for k in ("host", "ip")]
    bits = [f"frame_link: {stage} failed: {scrub_failure(message, hosts)}"]
    last = [ln.strip() for ln in (raw or "").splitlines() if ln.strip()][-1:]
    if last and last[0] != message:
        bits.append(f"ssh said: {scrub_failure(last[0][:300], hosts)}")
    tried = probes_summary(probes)
    if tried:
        bits.append(f"addresses: {tried}")
    line = " | ".join(bits)[:900]
    t = time.monotonic()
    seen = _logged.get(line)
    if seen and t - seen["at"] < LOG_REPEAT_EVERY:
        seen["repeats"] += 1
        return
    more = f" (and {seen['repeats']} more times)" if seen and seen["repeats"] else ""
    _logged.pop(line, None)
    _logged[line] = {"at": t, "repeats": 0}
    while len(_logged) > LOG_REMEMBER:
        _logged.pop(next(iter(_logged)))  # the least recently written
    try:
        print(line + more, file=sys.stderr, flush=True)
    except (OSError, ValueError, AttributeError):
        pass  # no stderr (a closed pipe): the page still shows the failure


def ssh_target(host, ip):
    """Where ssh should go for an address whose probe answered from `ip`: that IP, so ssh
    doesn't look the name up again and try an address that didn't answer."""
    try:
        ipaddress.ip_address((ip or "").split("%")[0])
        return ip
    except ValueError:
        return host


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _scanned_key(path, name):
    """The first whole, valid known_hosts line for `name` that a key check wrote, or None.
    Only lines ssh has finished (ending in a newline): a half-written key mustn't count."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines(True):
        if line.endswith("\n"):
            entry = frame_devices.key_entry(line.strip(), name)
            if entry:
                return entry
    return None


def proxy_command(literal):
    """A ProxyCommand that runs `literal` (argv, ending with the jump host) with `-W` to the
    headset, each word exactly as given. ssh first expands its %-tokens in the line, so every
    % of ours is doubled; then it hands the line to a shell (`$SHELL -c` on macOS and Linux)
    or, on Windows, starts it as a command line, so every word is quoted for that: a path
    with spaces, $(...), backticks or quotes stays one plain word. The forward is
    [%h]:%p, so an IPv6 address (with a zone) reads as one host."""
    words = [w.replace("%", "%%") for w in literal]
    target = "[%h]:%p"
    if frame_host.WINDOWS:
        return subprocess.list2cmdline([*words[:-1], "-W", target, words[-1]])
    return " ".join(shlex.quote(w) for w in [*words[:-1], "-W", target, words[-1]])


def probe_raw(host, port, result):
    """ssh's own wording for a failed probe, so the server's UNREACHABLE table explains it."""
    return {"unresolved": f"ssh: Could not resolve hostname {host}: not found",
            "refused": f"ssh: connect to host {host} port {port}: Connection refused",
            "unreachable": f"ssh: connect to host {host} port {port}: No route to host",
            }.get(result["state"], f"ssh: connect to host {host} port {port}: Operation timed out")


class Link:
    def __init__(self, registry, *, env_alias, mux_base, control, apply, explain):
        self.reg = registry
        self.override = env_alias       # FRAME_ALIAS, if set: the headset this server starts on
        self.work_lock = threading.Lock()  # the server's: held, no install starts (see server.working)
        self.work = lambda: 0              # how many installs are running
        self.deferred = False              # a login change from ~/.ssh/config waiting for them
        self.session_alias = env_alias  # ...and stays selectable after switching away
        self.mux_base = list(mux_base)  # ["ssh", "-o", "BatchMode=yes", ControlPath...]
        self.control = control          # ControlPath, or None where ssh can't share connections
        self._apply = apply             # apply(alias, host_opts): point every ssh command at the headset
        self.applied = (None, [])       # what it was last given
        self.explain = explain          # ssh error text -> plain reason, or None
        self.cond = threading.Condition()
        self.version = 0
        self.stopped = False
        self.kicks = []                 # reasons someone asked for a (re)connect
        self.busy = False               # the loop is handling kicks
        self.state = {"phase": "idle", "reason": None, "device": None, "network": None, "stages": [],
                      "probes": [], "via": None, "error": None, "retry_at": None, "attempt": 0,
                      "started": None, "finished": None, "tests": {}, "devices_rev": 0}
        self.master = None              # the ssh ControlMaster process, if we started it
        self.opts = []                  # host options of the current connection
        self.alias = None
        self.fails = 0
        self.last_fp = None
        self.last_attempt = 0
        self.config_mtime = None
        self.thread = None
        self.attempt_gen = 0
        self.pending = None             # an ssh handshake still running
        self.gen = 0                    # bumped when the headset or its login changes: older attempts are void
        self.route_lock = threading.Lock()
        self.routed = None              # the device id every ssh command points at
        self.routed_device = None
        self.last_failure = None        # {"stage", "category", "at"}: for report diagnostics, no free text
        self.attempt_device = None      # the headset the current attempt is for
        self.scans = {}                 # device id -> the host key it showed for "Trust a new key"

    # ---- publishing ----
    def publish(self, **fields):
        with self.cond:
            self.state.update(fields)
            self.version += 1
            self.cond.notify_all()

    def snapshot(self):
        with self.cond:
            snap = copy.deepcopy(self.state)
            snap["version"] = self.version
        snap["now"] = now()
        return snap

    def wait(self, version, timeout):
        """The state once its version passes `version`, or None after `timeout` seconds."""
        with self.cond:
            if not self.cond.wait_for(lambda: self.version > version or self.stopped, timeout):
                return None
        return self.snapshot()

    def stage(self, sid, state, detail=None):
        """Move one stage along (pending -> active -> done or failed) and publish."""
        with self.cond:
            for s in self.state["stages"]:
                if s["id"] == sid:
                    if state == "active" and s["state"] != "active":
                        s["started"] = now()
                    if state in ("done", "failed", "skipped"):
                        s["ended"] = now()
                        s["started"] = s["started"] or s["ended"]
                    s["state"] = state
                    if detail is not None:
                        s["detail"] = detail
            self.version += 1
            self.cond.notify_all()

    def probe_update(self, index, attempt=None, **fields):
        with self.cond:
            if attempt is not None and attempt != self.state["attempt"]:
                return  # a probe from an earlier attempt, still finishing: not this one's row
            if index < len(self.state["probes"]):
                self.state["probes"][index].update(fields)
            self.version += 1
            self.cond.notify_all()

    def devices_changed(self):
        with self.cond:
            self.state["devices_rev"] += 1
            self.version += 1
            self.cond.notify_all()

    # ---- control from the server ----
    def start(self):
        # Route to the saved headset before the server takes requests: until the connector
        # has run, commands (an upload by scp, say) would otherwise go to the default alias.
        device = self.active_device()
        with self.route_lock:
            self.apply(device["alias"], self.first_route(device))
        self.thread = threading.Thread(target=self.run, name="frame-link", daemon=True)
        self.thread.start()

    def kick(self, reason):
        with self.cond:
            self.kicks.append(reason)
            self.cond.notify_all()

    def stop(self):
        with self.cond:
            self.stopped = True
            self.cond.notify_all()
        self.close_master()
        if self.thread:
            self.thread.join(5)  # an attempt in progress notices `stopped` and ends
        self.close_master()

    def alive(self):
        if self.state["phase"] != "connected" or self.kicks or self.busy:
            return False  # a reconnect is queued or starting: don't begin anything on this connection
        if not self.control:
            return True
        return self.master is None or self.master.poll() is None

    def ensure(self, wait=20):
        """Called before a command: make sure a connection is up, or being tried.

        Waits (up to `wait` s) for an attempt already running, or starts one if the
        last ended a while ago. Never raises: if the headset can't be reached, the
        command runs anyway and fails with ssh's own error, as it always has."""
        with self.cond:
            if self.stopped or self.alive():
                return
            if self.state["phase"] != "connecting" and not self.kicks and now() - self.last_attempt > REQUEST_GAP:
                self.kicks.append("request")
            self.cond.wait_for(lambda: self.stopped or (not self.kicks and not self.busy and
                                                        self.state["phase"] != "connecting"), wait)

    def use(self, device_id):
        """Switch to another headset: one from the registry, or back to FRAME_ALIAS."""
        if self.session_alias and device_id == self.bare(self.session_alias)["id"] \
                and not self.reg.by_alias(self.session_alias):
            self.override = self.session_alias
        else:
            self.reg.set_active(device_id)
            self.override = None
        self.invalidate()

    def invalidate(self):
        """The headset in use, or how to log in to it, changed: from now on commands go to
        the one now selected (never the last one), any attempt still running is void,
        and ensure() waits for the connector to reach it."""
        device = self.active_device()
        with self.route_lock:
            self.gen += 1
            self.apply(device["alias"], self.first_route(device))
            self.routed = None  # the next attempt routes again
        with self.cond:
            # Say so at once: the page clears the old headset's panels when the device changes.
            self.state.update(phase="connecting", device=self.public_device(device), via=None, error=None,
                              retry_at=None, probes=[], stages=[])
            self.kicks.append("switch")
            self.version += 1
            self.cond.notify_all()
        self.devices_changed()

    def named_route(self):
        """(alias, options) for a terminal window: like every command's, but with the
        address by name, not the IP it answered from. A zone's % can't be passed through
        Windows' console, and ssh resolves the name itself."""
        device = self.active_device()
        routed = self.routed_device
        if self.routed is not None and routed and routed["alias"] == device["alias"]:
            device = routed  # as every command has it now (a frozen route, a login change deferred)
        via = self.state.get("via") if self.state.get("phase") == "connected" else None
        host = via["host"] if via else (device["addresses"][0]["host"] if device.get("addresses") else None)
        return device["alias"], self.host_opts(device, host)

    def first_route(self, device):
        """Where commands go before any address has answered: the first one, with the
        headset's own pinned identity, so nothing reaches another device meanwhile."""
        return self.host_opts(device, device["addresses"][0]["host"] if device["addresses"] else None)

    @staticmethod
    def route_key(device):
        return device["id"], device.get("user"), device.get("port"), tuple(device.get("frozen") or ())

    def lost(self, message, gen=None):
        """A command couldn't reach the headset (Windows has no master to watch). `gen`:
        the route it was sent on; one to a headset since switched away from says nothing
        about this one."""
        if gen is not None and gen != self.gen:
            return
        if self.state["phase"] == "connected":
            self.kick(f"lost: {message}")

    # ---- the device this server talks to ----
    def active_device(self):
        """The active headset from the registry, or a stand-in for a bare ssh alias."""
        if self.override:
            return self.reg.by_alias(self.override) or self.bare(self.override)
        want = self.reg.active()
        if want:
            try:
                return self.reg.get(want)
            except frame_devices.DeviceError:
                # Removed (by another server). Mid-install, fail closed: the rest of the
                # install, or its clean-up, mustn't land on whichever headset comes next.
                if self.work():
                    return self.NONE
        devices = self.reg.devices()
        if devices:
            return devices[0]
        if self.reg.emptied():
            return self.NONE  # every headset was removed: reach nothing until one is added
        return self.bare("frame")  # never set up here, or set up before the registry existed

    NONE = {"id": "none", "name": "No headset", "alias": "frame-control-no-headset", "user": None, "port": None,
            "addresses": [], "transient": True, "none": True, "identity_files": []}

    @staticmethod
    def bare(alias):
        """A headset that's only an ssh alias (no Set Up Connection block): ssh's config decides."""
        return {"id": f"alias-{alias}", "name": alias, "alias": alias, "user": None, "port": None,
                "addresses": [], "transient": True, "identity_files": []}

    @staticmethod
    def control_tag(device):
        """A short, path-safe name for the headset's ControlPath: its id, or for a bare
        alias a hash of it (an alias can be too long for a socket path)."""
        if device.get("transient"):
            return "a" + hashlib.sha1(device["alias"].encode()).hexdigest()[:8]
        return device["id"]

    def host_opts(self, device, host):
        """What every ssh command adds to reach DEVICE at HOST."""
        if device.get("none"):
            return ["-o", "HostName=no-headset.invalid"]  # fails at once, with ssh's own "can't resolve"
        # Each headset its own shared connection (see frame_host.control_path).
        mux = ["-o", f"ControlPath={frame_host.control_path(self.control_tag(device))}"] if self.control else []
        if device.get("transient"):
            return [*mux, *(device.get("frozen") or [])]  # what ~/.ssh/config said when it was routed
        if not host:  # a headset with no addresses: reach nothing, not whatever ~/.ssh/config says
            return ["-o", "HostName=no-address.invalid"]
        # Whatever ~/.ssh/config says for Host *, every command checks the headset's pinned key:
        # StrictHostKeyChecking=yes. Only a headset that never had an identity here (just
        # added, never connected) gets accept-new: the first contact saves its key, as Set Up
        # Connection and the connector's first handshake do, rather than every command failing
        # with "No host key is known" until the connector gets there. Once it had one, a pin
        # file that's gone, empty or unreadable fails closed (identity_established): accept-new
        # can't tell that from a new headset and would trust whatever answers.
        strict = self.strictness(device)
        if strict == "accept-new":
            try:
                frame_devices.pin_dir()  # ssh won't create the folder its known_hosts file goes in
            except OSError:
                pass  # ssh then says it couldn't save the key, and goes on
        return [*mux, "-o", f"StrictHostKeyChecking={strict}", "-o", f"HostName={frame_devices.ssh_host(host)}",
                "-o", f"HostKeyAlias={frame_devices.host_key_alias(device['id'])}",
                "-o", f"UserKnownHostsFile={frame_devices.known_hosts_opt(device['id'])}", "-o", "HashKnownHosts=no",
                "-o", f"User={device['user']}", "-o", f"Port={device['port']}"]

    def apply(self, alias, opts):
        self.applied = (alias, list(opts))
        self._apply(alias, opts)

    def strictness(self, device):
        """yes, or accept-new for a headset that never had an identity. Judged on what the
        registry says now, not only the copy handed in (a route frozen at connect, say, from
        before its first key was recorded). A key saved since is recorded on the way."""
        try:
            current = self.reg.get(device["id"])
        except frame_devices.DeviceError:
            current = {}
        if not (frame_devices.identity_established(device) or
                (current and frame_devices.identity_established(current))):
            return "accept-new"
        self.settle_pin(dict(device, **current) if current else device, tighten=False)
        return "yes"

    def settle_pin(self, device=None, tighten=True):
        """A key ssh saved on a first contact, whatever happened next (logged in or refused,
        by the handshake or any command): record it, make the file private, and from then on
        route every command strictly. Idempotent and cheap when there's nothing to do."""
        device = device or self.routed_device
        if not device or device.get("transient") or device.get("none") or not device.get("id"):
            return
        did = device["id"]
        if frame_devices.pinned(did):
            fps = [f for _, f in frame_devices.pinned_keys(did)]
            if fps and (device.get("identity") != fps or not device.get("identity_established")):
                try:
                    current = self.reg.get(did)
                except frame_devices.DeviceError:
                    return
                if current.get("identity") != fps or not current.get("identity_established"):
                    frame_host.make_private(frame_devices.known_hosts(did))  # ssh wrote it
                    self.reg.record_identity(did, fps)
        if not tighten:
            return
        with self.route_lock:
            alias, opts = self.applied
            if alias == device.get("alias") and "StrictHostKeyChecking=accept-new" in opts and \
                    frame_devices.identity_established(dict(device, **self._current(did))):
                self.opts = [("StrictHostKeyChecking=yes" if o == "StrictHostKeyChecking=accept-new" else o)
                             for o in opts]
                self.apply(alias, self.opts)

    def _current(self, device_id):
        try:
            return self.reg.get(device_id)
        except frame_devices.DeviceError:
            return {}

    def public_device(self, d):
        return {k: d.get(k) for k in ("id", "name", "alias", "user", "port", "transient")}

    # ---- the loop ----
    def run(self):
        self.kick("start")
        last_net = last_ts = 0
        while True:
            with self.cond:
                self.cond.wait_for(lambda: self.stopped or self.kicks, TICK)
                if self.stopped:
                    return
                reasons, self.kicks = self.kicks, []
                self.busy = bool(reasons)
            try:
                t = now()
                if t - last_net >= NETWORK_EVERY:
                    last_net = t
                    fp = frame_network.fingerprint()
                    if self.last_fp is not None and fp[::2] != self.last_fp[::2]:
                        reasons.append("network")
                    self.last_fp = fp
                    self.watch_config()
                    if self.state["phase"] == "connected" and self.control and self.master is None \
                            and not self.check(self.opts):
                        reasons.append("dropped")  # a master we found open, not one we started
                if t - last_ts >= TAILSCALE_EVERY and self.state["network"] and not reasons:
                    last_ts = t
                    self.refresh_network()
                if "StrictHostKeyChecking=accept-new" in self.applied[1]:
                    self.settle_pin()  # a command's first contact may have saved the key
                phase = self.state["phase"]
                if phase == "connected" and not self.alive():
                    reasons.append("dropped")
                if phase == "failed" and self.state["retry_at"] and now() >= self.state["retry_at"]:
                    reasons.append("retry")
                if reasons:
                    self.connect(reasons)
            except Exception as e:  # keep the loop alive whatever happens; say what went wrong
                message = f"{type(e).__name__}: {e}"
                self.note_failure("network", message, str(e))
                self.publish(phase="failed", error={"stage": "network", "message": message,
                                                    "raw": str(e)}, retry_at=now() + RETRY[-1])
            finally:
                with self.cond:
                    self.busy = False
                    self.cond.notify_all()

    def watch_config(self):
        """Set Up Connection may have added a headset or found a new address: pick it up."""
        try:
            mtime = frame_devices.ssh_config().stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self.config_mtime:
            self.config_mtime = mtime
            before = self.active_device()
            if before.get("transient") and not before.get("none") and self.routed is not None:
                # A bare alias is in use: a headset set up now doesn't take over by itself.
                self.override = self.override or before["alias"]
                self.session_alias = self.session_alias or before["alias"]  # and stays on the list
            if self.reg.sync_from_config():
                self.devices_changed()
                after = self.active_device()
                if (before.get("user"), before.get("port")) != (after.get("user"), after.get("port")):
                    self.deferred = True  # Set Up Connection changed the active headset's login
        if self.deferred:
            with self.work_lock:  # not while an install runs: it reads the route step by step
                if not self.work():
                    self.deferred = False
                    self.invalidate()

    def refresh_network(self):
        net = frame_network.current_network(self.last_fp)
        self.reg.record_network(net)
        net["name"] = self.reg.network_name(net)
        self.publish(network=net)

    # ---- one attempt ----
    def connect(self, reasons):
        why = self.describe(reasons)
        self.last_attempt = now()
        self.close_master()
        with self.work_lock, self.route_lock:
            gen = self.attempt_gen = self.gen
            device = self.active_device()
            if device.get("transient") and not device.get("none") and "frozen" not in device:
                # A bare alias: pin down where ~/.ssh/config sends it now, so an edit to that
                # file can't move the commands of an install that's running.
                h, p, u, proxied = ssh_g(device["alias"])
                device = dict(device, user=device.get("user") or u, port=p, frozen_host=h, proxied=proxied, frozen=[
                    "-o", f"HostName={frame_devices.ssh_host(h)}", "-o", f"Port={p}",
                    *(["-o", f"User={u}"] if u else [])])
            if self.routed and self.routed_device and self.route_key(device) != self.routed and self.work():
                # While an install runs, nothing moves it: not Set Up Connection changing this
                # headset in ~/.ssh/config, nor another Frame Control server adding, choosing
                # or removing headsets in devices.json. (Switching here is refused meanwhile.)
                # Reconnect as it started; the change applies once it's done (see watch_config).
                device = self.routed_device
                self.deferred = True
            if self.route_key(device) != self.routed:
                # Another headset, or a new user or port: nothing may go on using the old
                # route, even if this attempt fails.
                self.alias, self.opts = device["alias"], self.first_route(device)
                self.apply(self.alias, self.opts)
                self.routed = self.route_key(device)
                self.routed_device = device
        with self.cond:
            self.state.update(phase="connecting", reason=why, device=self.public_device(device), via=None,
                              error=None, retry_at=None, attempt=self.state["attempt"] + 1, started=now(),
                              finished=None, probes=[],
                              stages=[{"id": i, "label": label, "state": "pending", "detail": "",
                                       "started": None, "ended": None} for i, label in STAGES])
            self.version += 1
            self.cond.notify_all()
        ok = False
        try:
            self.attempt_device = device  # its names, for scrubbing this attempt's log lines
            ok = self.attempt(device)
        finally:
            self.finish(gen, ok, device)

    def finish(self, gen, ok, device):
        """Publish how an attempt ended (connected, or failed with a retry time)."""
        with self.cond:
            if gen != self.gen:
                # The headset changed meanwhile: this attempt's result is about the
                # old one. Leave "connecting"; the queued switch starts the next.
                self.state["phase"] = "connecting"
                self.version += 1
                self.cond.notify_all()
                ok = None
        if ok is None:
            self.close_master()
            return
        with self.cond:
            self.state["finished"] = now()
            if ok:
                self.fails = 0
                self.state.update(phase="connected", retry_at=None, error=None)
            else:
                self.fails += 1
                self.state.update(phase="failed", retry_at=None if device.get("none") or not (device.get("transient") or device["addresses"]) else
                                  now() + RETRY[min(self.fails, len(RETRY)) - 1])
                if not self.state["error"]:
                    self.state["error"] = {"stage": "find", "message": "Couldn't connect", "raw": ""}
                    self.note_failure("find", "Couldn't connect", "", self.state["probes"])
            self.version += 1
            self.cond.notify_all()

    @staticmethod
    def describe(reasons):
        for r in reasons:
            if r == "network":
                return "This computer changed networks"
            if r == "dropped" or r.startswith("lost"):
                return "The connection dropped"
            if r == "switch":
                return "Switched headset"
        if "retry" in reasons:
            return "Trying again"
        if "start" in reasons:
            return "Starting up"
        return "Connecting"

    def fail(self, sid, message, raw="", **more):
        """A stage failed. `more`: what the page can offer to fix it (hostkey, auth), and `short`:
        the reason in a few words, for the places a whole sentence doesn't fit (the header pill,
        the address that failed)."""
        self.stage(sid, "failed", message)
        with self.cond:
            self.state["error"] = dict({"stage": sid, "message": message, "raw": raw}, **more)
            probes = copy.deepcopy(self.state["probes"])
        self.note_failure(sid, message, raw, probes)

    def note_failure(self, stage, message, raw="", probes=()):
        """Keep a failure for report diagnostics as fixed values only (its stage and error
        category, decided now), and write it to the log scrubbed with this attempt's hosts."""
        self.last_failure = {"stage": stage, "category": failure_category(message, raw), "at": now()}
        hosts = []
        for d in (self.attempt_device, self.routed_device):  # the headset tried, and where commands go
            d = d or {}
            hosts += [a.get("host") for a in d.get("addresses") or ()]
            hosts += [d.get("frozen_host"), d.get("alias"), d.get("name")]
        log_failure(stage, message, raw, probes, hosts)

    def attempt(self, device):
        if device.get("none"):
            self.fail("find", "No headset is set up. Add one on the Devices tab.")
            return False
        if not device.get("transient") and not device["addresses"]:
            self.fail("find", "The active headset has no addresses. Add one on the Devices tab.")
            return False
        # 1. this computer's network
        self.stage("network", "active")
        net = frame_network.current_network(self.last_fp)
        self.reg.record_network(net)
        net["name"] = self.reg.network_name(net)
        ts = net.get("tailscale") or {}
        self.publish(network=net)
        bits = [net["name"]]
        if net.get("local_ip"):
            bits.append(f"this computer is {net['local_ip']}")
        bits.append("Tailscale on" if ts.get("up") else "Tailscale off" if ts.get("installed") else "no Tailscale")
        self.stage("network", "done" if net.get("gateway") or ts.get("up") else "failed", " · ".join(bits))
        if not net.get("gateway") and not ts.get("up"):
            with self.cond:
                self.state["error"] = {"stage": "network", "raw": "",
                                       "message": "This computer isn't connected to a network."}
            # Keep going anyway: a headset on a direct link or loopback could still answer.

        # 2. find the headset
        self.stage("find", "active")
        port = device.get("port") or 22
        if device.get("transient"):
            # Where the route was pinned (connect), so the probe checks what commands use.
            if "frozen_host" in device:
                host, port, user, proxied = device["frozen_host"], device["port"], device.get("user"), device["proxied"]
            else:
                host, port, user, proxied = ssh_g(device["alias"])
            if user and not device.get("user"):
                device["user"] = user
            a = {"host": host, "kind": frame_network.guess_kind(host), "label": "from ~/.ssh/config"}
            if proxied:
                # Reached through a jump host: only ssh itself can find it.
                with self.cond:
                    self.state["probes"] = [dict(a, why="through a jump host", state="answered", ip=None, rtt_ms=None,
                                                 detail="ssh's ProxyJump or ProxyCommand connects")]
                self.stage("find", "done", f"{device['alias']} goes through a jump host; ssh finds it")
                return self.handshake(device, a, {"ip": None, "rtt_ms": None}, device.get("user") or user) == "ok" \
                    and self.finish_bare(a, net)
            ranked = [(a, "from ~/.ssh/config")]
        else:
            ranked = frame_devices.order_addresses(device["addresses"], net.get("id"), bool(ts.get("up")))
            if ssh_g(device["alias"])[3]:
                # ~/.ssh/config sends this alias through a jump host: a direct probe says
                # nothing, so let ssh (through the jump host) try each address in turn.
                with self.cond:
                    self.state["probes"] = [dict(a, why=why, state="waiting", detail="Through a jump host", ip=None,
                                                 rtt_ms=None, label=a.get("label") or "") for a, why in ranked]
                self.stage("find", "done", f"{device['alias']} goes through a jump host; ssh finds it")
                for i, (a, why) in enumerate(ranked):
                    if i:
                        for sid in ("ssh", "identity", "login"):
                            self.stage(sid, "pending", "")
                    outcome = self.handshake(device, a, {"ip": None, "rtt_ms": None}, device.get("user"))
                    if outcome == "ok":
                        self.probe_update(i, state="answered", detail="Reached through the jump host")
                        self.publish(via={"host": a["host"], "kind": a["kind"], "ip": None, "rtt_ms": None,
                                          "why": "through a jump host", "network": net.get("id"),
                                          "network_name": net["name"]})
                        self.learn(device, a["host"], net, None)
                        return True
                    with self.cond:
                        why_not = failure_detail(self.state["error"])
                    self.probe_update(i, state="sshfailed", detail=why_not)
                    if outcome != "next":
                        return False
                return False
        with self.cond:
            self.state["probes"] = [{"host": a["host"], "kind": a["kind"], "label": a.get("label") or "",
                                     "why": why, "state": "waiting", "detail": "Waiting", "ip": None,
                                     "rtt_ms": None} for a, why in ranked]
        self.stage("find", "active", f"Trying {len(ranked)} address{'es' * (len(ranked) != 1)} at once")
        results = [None] * len(ranked)
        done = threading.Condition()

        attempt_no = self.state["attempt"]

        def run_probe(i, host):
            res = probe(host, port, update=lambda **f: self.probe_update(i, attempt_no, **f))
            res.setdefault("ip", None)
            res.setdefault("rtt_ms", None)
            self.probe_update(i, attempt_no, **{k: res[k] for k in ("state", "detail", "ip", "rtt_ms")})
            with done:
                if results[i] is None:  # not already given up on
                    results[i] = dict(res, t=time.monotonic())
                done.notify_all()

        for i, (a, _) in enumerate(ranked):
            threading.Thread(target=run_probe, args=(i, a["host"]), daemon=True).start()

        tried = set()
        user = device.get("user") or "the headset's user"
        deadline = time.monotonic() + PROBE_TIMEOUT + RESOLVE_GRACE
        while True:
            pick = self.pick(results, tried, done, deadline)
            if pick is None:
                break
            if tried:  # another address may do better (a different device answered, or it dropped)
                for sid in ("ssh", "identity", "login"):
                    self.stage(sid, "pending", "")
            tried.add(pick)
            a = ranked[pick][0]
            self.stage("find", "done", f"{a['host']} answered in {results[pick]['rtt_ms']:g} ms")
            outcome = self.handshake(device, a, results[pick], user)
            if outcome == "ok":
                via = {"host": a["host"], "kind": a["kind"], "ip": results[pick]["ip"],
                       "rtt_ms": results[pick]["rtt_ms"], "why": ranked[pick][1], "network": net.get("id"),
                       "network_name": net["name"]}
                self.publish(via=via)
                self.learn(device, a["host"], net, results[pick]["rtt_ms"])
                return True
            with self.cond:
                why_not = failure_detail(self.state["error"])
            self.probe_update(pick, state="sshfailed", detail=why_not)
            if outcome != "next":
                return False
        if tried:
            return False  # the last handshake already said why
        # Nothing answered: explain with the most useful failure.
        with self.cond:
            for row in self.state["probes"]:
                if row["state"] in ("waiting", "resolving", "trying"):
                    row.update(state="timeout", detail="No answer in time")
        states = [r["state"] for r in results if r]
        worst = next((s for s in ("refused", "timeout", "unreachable", "unresolved") if s in states), "timeout")
        i = states.index(worst) if worst in states else 0
        raw = probe_raw(ranked[i][0]["host"], port, results[i] or {"state": worst})
        message = self.explain(raw) or "The Frame isn't answering."
        self.fail("find", message, raw)
        return False

    def finish_bare(self, a, net):
        self.publish(via={"host": a["host"], "kind": a["kind"], "ip": None, "rtt_ms": None,
                          "why": "through a jump host", "network": net.get("id"), "network_name": net["name"]})
        return True

    @staticmethod
    def pick(results, tried, done, deadline=None):
        """The next address to use: the best-ranked answer once every better-ranked
        address has failed, or once it has waited PREFER seconds for them. None when
        nothing (else) answered. Probes still going at `deadline` (a name lookup can
        take longer than the connect timeout) count as no answer."""
        deadline = deadline or time.monotonic() + PROBE_TIMEOUT + RESOLVE_GRACE
        with done:
            while True:
                if time.monotonic() >= deadline:
                    for i, r in enumerate(results):
                        if r is None:
                            results[i] = {"state": "timeout", "detail": "No answer", "ip": None, "rtt_ms": None,
                                          "t": time.monotonic()}
                answered = [i for i, r in enumerate(results) if r and r["state"] == "answered" and i not in tried]
                pending = [i for i, r in enumerate(results) if r is None]
                if answered:
                    best = answered[0]
                    better = [i for i in pending if i < best]
                    waited = time.monotonic() - results[best]["t"]
                    if not better or waited >= PREFER:
                        return best
                    done.wait(PREFER - waited)
                elif not pending:
                    return None
                else:
                    done.wait(min(0.5, max(0.01, deadline - time.monotonic())))

    def learn(self, device, host, net, rtt):
        if device.get("transient"):
            return
        self.reg.record_success(device["id"], host, net.get("id"), rtt)
        self.reg.record_identity(device["id"], [f for _, f in frame_devices.pinned_keys(device["id"])])
        # Set Up Connection may have changed the block while this attempt ran: take that
        # in (and reconnect) rather than writing this attempt's older settings over it.
        self.watch_config()
        try:
            now_dev = self.reg.get(device["id"])
        except frame_devices.DeviceError:
            return
        if self.route_key(now_dev) != self.route_key(device):
            return
        # Terminal's `ssh ALIAS` and the helper scripts use ~/.ssh/config: point it here too,
        # unless the block changed since this attempt began (checked under the file lock).
        login = device.get("config_login") or [None, None]
        expect = {"hostname": device.get("config_host"), "user": login[0], "port": login[1]}
        try:
            if frame_devices.rewrite_block(device["alias"], hostname=host, user=device["user"],
                                           port=device["port"], expect=expect):
                self.config_mtime = frame_devices.ssh_config().stat().st_mtime
                self.reg.set_config_host(device["id"], host)
                self.reg.sync_from_config()  # records the login it now holds
        except OSError:
            pass  # not fatal: the app itself doesn't need the file
        self.devices_changed()

    # ---- SSH ----
    def check(self, opts, alias=None):
        """Is a master connection up for these options? (`ssh -O check`)"""
        if not self.control:
            return False
        try:
            return frame_host.run_ssh([*self.mux_base, *opts, "-O", "check", alias or self.alias], capture_output=True,
                                      stdin=subprocess.DEVNULL, timeout=5).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def close_master(self):
        proc, self.master = self.master, None
        pending, self.pending = self.pending, None
        if pending and pending.poll() is None:
            pending.kill()
        if self.control and self.alias:
            try:
                frame_host.run_ssh([*self.mux_base, *self.opts, "-O", "exit", self.alias], capture_output=True,
                                   stdin=subprocess.DEVNULL, timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def handshake(self, device, a, found, user):
        """SSH to one address, following ssh -v through stages 3-5.
        -> "ok", "next" (try another address) or "stop". A key ssh saved is recorded and
        protected however it ends: a login refused after the key was saved, a timeout, a stop."""
        try:
            return self._handshake(device, a, found, user)
        finally:
            if not device.get("transient"):
                with self.route_lock:
                    current = self.attempt_gen == self.gen
                self.settle_pin(device, tighten=current)

    def _handshake(self, device, a, found, user):
        # The IP that answered (with a link-local IPv6 address's zone), so ssh doesn't
        # look the name up again and stall on an address that didn't answer.
        opts = self.host_opts(device, ssh_target(a["host"], found.get("ip")))
        alias = device["alias"]
        with self.route_lock:
            if self.attempt_gen != self.gen:
                return "stop"  # the headset changed: don't route anything back to this one
            self.alias, self.opts = alias, opts
            self.apply(alias, opts)
        target = f"{a['host']}" + (f" ({found['ip']})" if found.get("ip") and found["ip"] != a["host"] else "")
        self.stage("ssh", "active", f"Opening SSH to {target}")
        if self.control and self.check(opts, alias):
            for sid in ("ssh", "identity", "login"):
                self.stage(sid, "done", "Reusing the SSH connection that's already open")
            return "ok"
        extra = []
        if not device.get("transient"):
            # A headset's first connection trusts what it shows (as Set Up Connection does) and
            # pins it; any later one checks the pin strictly (see host_opts).
            extra = ["-o", f"StrictHostKeyChecking={self.strictness(device)}"]
        if self.control:
            # No ConnectTimeout: with it, OpenSSH's master takes ~5s to open its socket.
            # `extra` first: ssh takes the first value of an option, and it may say accept-new.
            argv = [*self.mux_base, *extra, *opts, "-v", "-o", "ControlMaster=yes", "-o", "ServerAliveInterval=5",
                    "-o", "ServerAliveCountMax=2", "-N", alias]
        else:
            argv = [*self.mux_base, *extra, *opts, "-v", "-o", "ConnectTimeout=10", alias, "true"]
        lines = queue.Queue()
        collecting = [True]
        reader_done = threading.Event()
        errfile = None
        try:
            if self.control:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.PIPE, **frame_host.DETACHED)
            else:
                # No master (Windows): ssh runs `true` and exits. Its stderr goes to a file read
                # as it grows, not a pipe: Windows' OpenSSH can block writing to a redirected
                # pipe (see frame_host.run_ssh), and the handshake would never finish.
                fd_, errfile = tempfile.mkstemp(prefix="frame-ssh-")
                try:
                    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=fd_, **frame_host.DETACHED)
                finally:
                    os.close(fd_)
        except OSError as e:
            if errfile:
                _unlink(errfile)
            self.fail("ssh", f"Couldn't run ssh: {e}", str(e))
            return "stop"
        self.pending = proc  # so stop() can end it mid-handshake

        def read():
            for raw in iter(proc.stderr.readline, b""):
                if collecting[0]:
                    lines.put(raw.decode("utf-8", "replace").rstrip())
            reader_done.set()
            lines.put(None)
            proc.stderr.close()

        def tail():
            buf = b""
            try:
                with open(errfile, "rb") as fh:
                    while True:
                        chunk = fh.read()
                        if not chunk:
                            if proc.poll() is None:
                                time.sleep(0.05)
                                continue
                            chunk = fh.read()  # whatever it wrote just before exiting
                            if not chunk:
                                break
                        buf += chunk
                        *done, buf = buf.split(b"\n")
                        for raw in done:
                            lines.put(raw.decode("utf-8", "replace").rstrip())
            except OSError:
                pass
            if buf:
                lines.put(buf.decode("utf-8", "replace").rstrip())
            reader_done.set()
            lines.put(None)
            _unlink(errfile)
        threading.Thread(target=read if errfile is None else tail, daemon=True).start()

        step, said, authed = "ssh", [], False
        deadline = time.monotonic() + HANDSHAKE_TIMEOUT
        mismatch = False
        shown = None  # (type, fingerprint) of the key the headset showed
        exited = None  # when ssh was first seen to have exited
        while True:
            left = deadline - time.monotonic()
            if self.stopped:
                proc.kill()
                return "stop"
            if left <= 0:
                proc.kill()
                self.fail(step, self.explain(f"Timed out talking to {alias}") or "The headset took too long to answer.",
                          f"Timed out talking to {alias}")
                return "next"  # silence is about this address (or a jump host's forward to it)
            try:
                line = lines.get(timeout=min(left, 0.25))
            except queue.Empty:
                line = ""
                if authed and self.control and self.check(opts, alias):
                    break
                if proc.poll() is not None and lines.empty():
                    # ssh exited: its last words (Permission denied…) may still be on their way.
                    # The reader's end marker says when they're all in; a pipe that something
                    # else holds open (a ProxyCommand's child) gets a short grace instead.
                    exited = exited or time.monotonic()
                    if reader_done.is_set() and lines.empty() or time.monotonic() - exited > 2:
                        line = None
                    else:
                        continue
                else:
                    continue
            if line is None:  # ssh exited
                proc.wait()
                if not self.control and proc.returncode == 0:
                    break
                if authed and self.control and self.check(opts, alias):
                    break
                return self.failed(step, said, mismatch, alias, shown, a["host"], device)
            if not line.startswith("debug"):
                said.append(line)
            m = CONNECTING.search(line)
            if m:
                self.stage("ssh", "active", f"Opening SSH to {a['host']}" +
                           (f" ({m.group(2)})" if m.group(2) != a["host"] else "") + f", port {m.group(3)}")
            elif ESTABLISHED.search(line):
                self.stage("ssh", "done", f"Connected to {target}")
                step = "identity"
                self.stage("identity", "active", "Waiting for the headset's host key")
            elif HOSTKEY.search(line):
                m = HOSTKEY.search(line)
                shown = (m.group(1), m.group(2))
                self.stage("identity", "active", f"It shows {m.group(1)} key {m.group(2)[:20]}…")
            elif KNOWN.search(line):
                self.stage("identity", "done", "Matches the identity saved for this headset")
                step = "login"
                self.stage("login", "active", f"Logging in as {user}")
            elif ADDED.search(line):
                if not device.get("transient"):
                    self.settle_pin(device, tighten=False)  # now, before the login can fail
                self.stage("identity", "done", "First connection: saved this headset's identity")
                step = "login"
                self.stage("login", "active", f"Logging in as {user}")
            elif (CHANGED.search(line) or UNKNOWN.search(line)) and not device.get("transient"):
                mismatch = True  # a bare alias keeps ssh's per-address check, and its wording
            elif AUTH_START.search(line) and step != "login":
                self.stage("identity", "done")
                step = "login"
                self.stage("login", "active", f"Logging in as {user}")
            elif AUTHED.search(line) and self.is_target(line, opts, alias):
                authed = True
                if step != "login":
                    self.stage("identity", "done")
                self.stage("login", "done", f"Logged in as {user}")
                step = "connected"
        collecting[0] = False  # the master keeps printing mux debug lines: drop them
        self.pending = None
        if self.stopped:
            proc.kill()
            return "stop"
        for sid in ("ssh", "identity", "login"):
            with self.cond:
                pending = any(s["id"] == sid and s["state"] != "done" for s in self.state["stages"])
            if pending:
                self.stage(sid, "done")
        if self.control:
            self.master = proc
        return "ok"

    @staticmethod
    def is_target(line, opts, alias):
        """Whether an "Authenticated to X" line is about the headset, not a jump host
        (ssh -v passes its verbosity on to ProxyJump's own ssh)."""
        host = next((o.split("=", 1)[1].replace("%%", "%") for o in opts if o.startswith("HostName=")), alias)
        m = re.search(r"Authenticated to (\S+)", line)
        # OpenSSH lower-cases host names (FRAME.LOCAL logs as frame.local).
        return not m or m.group(1).lower() in (host.lower(), alias.lower()) or "Authentication succeeded" in line

    def failed(self, step, said, mismatch, alias, shown=None, host=None, device=None):
        text = "\n".join(said).strip()
        if mismatch:
            # A key that differs from the saved one, or (strict, pinned) none of the saved key's
            # type any more: either way not the identity saved. Never accepted here: the page
            # shows the new key's fingerprint and the person decides (Trust a new key).
            name = (device or {}).get("name") or alias
            lost = bool(device) and not frame_devices.pinned(device["id"]) and \
                frame_devices.identity_established(device)
            if lost:
                message = (f"The SSH identity saved for {name} is missing or can't be read, so Frame Control won't "
                           "trust whatever answers. Check its key and trust it on the Devices tab.")
            else:
                message = (f"This address answered with a different SSH identity from the one saved for {name}. "
                           "If SteamOS was reinstalled or reset, or this is a new headset, check its new key and trust "
                           "it on the Devices tab. If not, a different device may be answering here.")
            self.fail("identity", message, text,
                      short="Saved SSH identity missing" if lost else "SSH identity changed",
                      hostkey={"state": "missing" if lost else "changed", "type": shown[0] if shown else None,
                               "fingerprint": shown[1] if shown else None, "host": host})
            return "next"
        # A refused key is the same at every address: stop. (Judged by ssh's own words, not
        # the step: a jump host's progress lines look like the headset's.) A forward that
        # a jump host couldn't open is about this address only: try the next.
        forward = re.search(r"open failed|forwarding failed|Connection refused|Connection closed|timed out", text)
        if re.search(r"Permission denied", text) and not forward:
            # Not something a retry fixes: the headset needs this computer's key (Set Up Connection).
            self.fail("login", self.explain(text) or "The headset didn't accept this computer's key. Run Set Up "
                      "Connection: it pairs this computer with the headset again.", text, auth="denied",
                      short="This computer's key refused")
            return "stop"
        if step == "connected":
            self.fail("login", "Logged in, but the shared SSH connection didn't start.", text)
            return "next"
        self.fail(step, self.explain(text) or (text.splitlines()[-1] if text else "ssh stopped"), text)
        return "next"

    # ---- Test now ----
    def test(self, device_id):
        """Probe every address of a headset and check SSH on the ones that answer,
        without touching the live connection. Results stream into state["tests"]."""
        device = self.reg.get(device_id)
        started = now()
        rows = [{"host": a["host"], "kind": a["kind"], "state": "waiting", "detail": "Waiting", "ip": None,
                 "rtt_ms": None, "ssh": None} for a in device["addresses"]]

        def put(**fields):
            with self.cond:
                self.state["tests"][device_id] = dict({"started": started, "done": False, "rows": rows}, **fields)
                self.version += 1
                self.cond.notify_all()

        put()
        net = self.state["network"] or {}

        proxied = ssh_g(device["alias"])[3]

        def one(i, a):
            if proxied:  # through a jump host: a direct probe says nothing, ssh itself is the test
                res = {"state": "answered", "detail": "Through a jump host", "ip": None, "rtt_ms": None}
            else:
                res = probe(a["host"], device["port"], update=lambda **f: (rows[i].update(f), put()))
            rows[i].update({k: res.get(k) for k in ("state", "detail", "ip", "rtt_ms")})
            put()
            if res["state"] != "answered":
                return
            lead = f"Answered in {res['rtt_ms']:g} ms" if res.get("rtt_ms") is not None else "Through the jump host"
            rows[i]["ssh"] = "checking"
            put()
            # "yes" before host_opts (ssh takes an option's first value): a test only looks, so
            # it reports a key not saved yet rather than saving it as host_opts' accept-new would.
            argv = [*self.mux_base[:3], "-o", "ControlPath=none", "-o", "ConnectTimeout=8",
                    "-o", "StrictHostKeyChecking=yes",
                    *self.host_opts(device, ssh_target(a["host"], res.get("ip"))), device["alias"], "true"]
            try:
                r = frame_host.run_ssh(argv, capture_output=True, stdin=subprocess.DEVNULL, text=True,
                                       errors="replace", timeout=20)
                err = r.stderr.strip()
                if r.returncode == 0:
                    rows[i].update(ssh="ok", detail=f"{lead} · SSH works")
                    self.reg.record_success(device_id, a["host"], net.get("id"), res["rtt_ms"])
                elif UNKNOWN.search(err):
                    rows[i].update(ssh="unpinned", detail=f"{lead} · identity not saved yet")
                elif CHANGED.search(err):
                    rows[i].update(ssh="wrong", detail="Answered as a different headset")
                elif DENIED.search(err):
                    rows[i].update(ssh="denied", detail="Answered, but refused this computer's key")
                else:
                    rows[i].update(ssh="failed", detail=self.explain(err) or (err.splitlines() or ["SSH failed"])[-1])
            except (OSError, subprocess.TimeoutExpired):
                rows[i].update(ssh="failed", detail="SSH took too long")
            put()

        threads = [threading.Thread(target=one, args=(i, a), daemon=True) for i, a in enumerate(device["addresses"])]
        for t in threads:
            t.start()
        for t in threads:
            t.join(40)
        put(done=True, finished=now())
        self.devices_changed()

    # ---- Trust a new key ----
    # Every way a key check could do more than read the key: no login of any kind, no agent
    # (nor forwarding it), no X11, port or tunnel forwarding, no local or remote command, no
    # terminal, no shared connection, no learning keys. Given first, so ssh takes these over
    # anything ~/.ssh/config (or host_opts) says. -N: no command, even if a device accepts "none".
    SCAN_OPTS = ["BatchMode=yes", "ControlMaster=no", "ControlPath=none", "ConnectTimeout=8",
                 "StrictHostKeyChecking=accept-new", "UpdateHostKeys=no", "HashKnownHosts=no",
                 "PreferredAuthentications=none", "PubkeyAuthentication=no", "PasswordAuthentication=no",
                 "KbdInteractiveAuthentication=no", "HostbasedAuthentication=no", "IdentityAgent=none",
                 "ForwardAgent=no", "ForwardX11=no", "ForwardX11Trusted=no", "ClearAllForwardings=yes",
                 "Tunnel=no", "PermitLocalCommand=no", "RemoteCommand=none", "RequestTTY=no"]
    SCAN_TIMEOUT = 20

    def scan_identity(self, device_id):
        """Read the headset's host key without trusting it or logging in. ssh writes it to a
        throwaway known_hosts file during the key exchange, before any authentication, and is
        stopped as soon as it has (or when it gives up); SCAN_OPTS and -N rule out anything else.
        `-F none` too, unless ~/.ssh/config sends the alias through a jump host (then it's
        needed for that, and SCAN_OPTS still win). The address that showed the changed key goes
        first, then the rest in order. -> {"type", "fingerprint", "host", "saved", "recorded",
        "matches", "recorded_match"}; the key is kept (SCAN_TTL) for trust_identity, which pins
        exactly it."""
        device = self.reg.get(device_id)
        if not device["addresses"]:
            raise frame_devices.DeviceError("This headset has no addresses to ask")
        hosts = [a["host"] for a in device["addresses"]]
        with self.cond:
            err = self.state.get("error") or {}
            mine = (self.state.get("device") or {}).get("id") == device_id
        first = (err.get("hostkey") or {}).get("host") if mine else None
        if first in hosts:
            hosts.remove(first)
            hosts.insert(0, first)
        route = self.scan_route(device)
        reasons = []
        for host in hosts:
            key, err_text = self._scan_one(device, host, route)
            if key:
                saved = frame_devices.pinned_keys(device_id)
                recorded = [f for f in device.get("identity") or [] if isinstance(f, str)]
                scan = {"type": key[0], "blob": key[1], "fingerprint": key[2], "host": host,
                        "port": device["port"], "user": device["user"], "at": now()}
                with self.cond:
                    self.scans[device_id] = scan
                return {"type": key[0], "fingerprint": key[2], "host": host,
                        "saved": [{"type": t, "fingerprint": f} for t, f in saved], "recorded": recorded,
                        "matches": any(f == key[2] for _, f in saved), "recorded_match": key[2] in recorded}
            text = err_text.strip()
            reasons.append(f"{host}: " + (self.explain(text) or (text.splitlines() or ["no answer"])[-1]))
        raise frame_devices.DeviceError("Couldn't read the headset's key. " + "; ".join(reasons))

    # The jump host's own ssh (a ProxyCommand the scan builds itself): it may log in to the jump
    # host as configured (keys, the agent), but it never forwards the agent, X11 or ports, runs
    # no local or remote command, has no terminal, shares no connection and goes no further.
    HOP_OPTS = ["BatchMode=yes", "ForwardAgent=no", "ForwardX11=no", "ForwardX11Trusted=no",
                "ClearAllForwardings=yes", "Tunnel=no", "PermitLocalCommand=no", "RemoteCommand=none",
                "RequestTTY=no", "ControlMaster=no", "ControlPath=none", "ProxyCommand=none", "ProxyJump=none"]

    def scan_route(self, device):
        """The options that take a key check to the headset: `-F none` and nothing else when
        it's reached directly. Through a jump host (ProxyJump), `-F none` and a ProxyCommand
        built here, whose ssh gets HOP_OPTS ahead of ~/.ssh/config: config options can't be
        overridden for ProxyJump's own ssh, which inherits ForwardAgent, LocalCommand and the
        rest. Through a ProxyCommand from ~/.ssh/config, or several jump hosts, it refuses:
        those can't be made safe from here."""
        kind, spec = proxy_route(device["alias"])
        if kind is None:
            return ["-F", "none"]
        m = JUMP_RE.fullmatch(spec or "") if kind == "jump" else None
        if not m or (m.group(3) and not 1 <= int(m.group(3)) <= 65535):
            how = "a ProxyCommand" if kind == "command" else "this jump host setting"
            raise frame_devices.DeviceError(
                f"{device['name']} is reached through {how} in ~/.ssh/config, which Frame Control can't make safe "
                "for talking to a device it doesn't trust yet, so it won't read the key that way. Add an address "
                "it can reach directly (or through a single ProxyJump host) and check its key again; or compare "
                "the key by hand (on the headset, in Konsole: ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub)")
        user, host, port = m.groups()
        # The ssh this server runs, by its full path: the shell that runs a ProxyCommand may set
        # up another PATH.
        ssh = frame_host.which(self.mux_base[0])
        if not ssh:
            raise frame_devices.DeviceError(f"Couldn't find {self.mux_base[0]} to reach the jump host with")
        hop = [ssh, *(a for o in self.HOP_OPTS for a in ("-o", o)), *(["-l", user] if user else []),
               *(["-p", port] if port else []), host.strip("[]")]  # ssh takes a bare IPv6 address here
        return ["-F", "none", "-o", "ProxyCommand=" + proxy_command(hop)]

    def _scan_one(self, device, host, route):
        """-> ((type, blob, fingerprint) or None, what ssh said)."""
        name = frame_devices.host_key_alias(device["id"])
        path, opt = frame_devices.scan_file(device["id"])
        opts = [a for o in [*self.SCAN_OPTS, f"UserKnownHostsFile={opt}"] for a in ("-o", o)]
        argv = [self.mux_base[0], *route, *opts, *self.host_opts(device, host), "-N", device["alias"]]
        fd_, errfile = tempfile.mkstemp(prefix="frame-scan-")  # a file, not a pipe (see frame_host.run_ssh)
        key, proc = None, None
        try:
            try:
                proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=fd_,
                                        **frame_host.DETACHED)
            finally:
                os.close(fd_)
            deadline = time.monotonic() + self.SCAN_TIMEOUT
            while key is None:
                ended = proc.poll() is not None
                key = _scanned_key(path, name)
                if key or ended or self.stopped or time.monotonic() > deadline:
                    break
                time.sleep(0.05)
        except OSError as e:
            return None, str(e)
        finally:
            if proc and proc.poll() is None:
                proc.kill()  # it has the key (or gave up): nothing more to do with that device
            if proc:
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    pass
            try:
                with open(errfile, "rb") as fh:
                    said = fh.read().decode("utf-8", "replace")
            except OSError:
                said = ""
            _unlink(errfile)
            _unlink(path)
        if key is None and not said.strip() and proc and proc.returncode is not None and proc.returncode < 0:
            said = f"Timed out talking to {device['alias']}"
        return key, said

    def trust_identity(self, device_id, fingerprint):
        """Pin the key scan_identity read, and only if `fingerprint` is the one it showed (what
        the person saw and confirmed), replacing whatever was saved; and only while the headset
        is still reached as it was then (same port and user, the address still one of its own).
        -> the scan."""
        with self.cond:
            scan = self.scans.get(device_id)
            if not scan or now() - scan["at"] > SCAN_TTL:
                self.scans.pop(device_id, None)
                raise frame_devices.DeviceError("Check the headset's key again first: the one shown has expired")
            if not isinstance(fingerprint, str) or fingerprint != scan["fingerprint"]:
                raise frame_devices.DeviceError("That isn't the key the headset showed. Check its key again")
            self.scans.pop(device_id)
        d = self.reg.get(device_id)  # still a headset here
        if (d["port"], d["user"]) != (scan["port"], scan["user"]) or \
                scan["host"] not in [a["host"] for a in d["addresses"]]:
            raise frame_devices.DeviceError("The headset's address, port or user changed since its key was read. "
                                            "Check its key again")
        frame_devices.pin_key(device_id, scan["type"], scan["blob"])
        self.reg.record_identity(device_id, [scan["fingerprint"]])
        return scan


# ---- the page's API: /api/devices -------------------------------------------------

def devices_view(link):
    """Every headset with its addresses, the networks they worked on, and the current network."""
    snap = link.reg.snapshot()
    active = link.active_device()
    names = {nid: link.reg.network_name(dict(n, id=nid)) for nid, n in snap["networks"].items()}
    devices = []
    bare = link.bare(link.session_alias) if link.session_alias and not link.reg.by_alias(link.session_alias) else None
    for extra in ([active] if active.get("transient") and not active.get("none") else []) + \
            ([bare] if bare and bare["id"] != active["id"] else []):
        devices.append(dict(link.public_device(extra), active=extra["id"] == active["id"], addresses=[],
                            managed=False, pinned=False))
    for d in snap["devices"]:
        view = {k: v for k, v in d.items() if k not in ("config_host", "addresses")}
        view["active"] = d["id"] == active["id"]
        view["pinned"] = frame_devices.pinned(d["id"])
        view["keys"] = [{"type": t, "fingerprint": f} for t, f in frame_devices.pinned_keys(d["id"])]
        # It had an identity, but the saved key is gone or unusable: nothing is trusted until checked.
        view["identity_lost"] = not view["pinned"] and frame_devices.identity_established(d)
        view["addresses"] = [dict(a, network_names=[names.get(n, "an unnamed network") for n in a["networks"]])
                             for a in d["addresses"]]
        devices.append(view)
    return {"devices": devices, "active": active["id"], "network": link.state["network"],
            "networks": [dict(n, id=nid, display=names[nid]) for nid, n in snap["networks"].items()],
            "kinds": frame_devices.KIND_LABEL}


def login_change(reg, did, body):
    """Whether an update asks for another user or port (the page sends both every time)."""
    try:
        d = reg.get(did)
    except frame_devices.DeviceError:
        return False
    user, port = body.get("user"), body.get("port")
    try:
        port = int(port) if port is not None else None
    except (TypeError, ValueError):
        return True  # it will be refused anyway
    return (user is not None and user != d["user"]) or (port is not None and port != d["port"])


def devices_action(link, body, open_setup, busy=lambda: 0):
    """POST /api/devices {"action": ..., "id": device id, ...}. -> {"message", ...devices_view}.
    busy() counts installs in progress: nothing may move them to another headset."""
    reg = link.reg
    action = body.get("action")
    did = body.get("id")
    active = link.active_device()
    is_active = did == active["id"]
    moves = action == "use" or (action == "retry" and link.alive()) or (is_active and (
        # (a retry while connected would cut the install's connection)
        action in ("remove", "address-remove", "identity-trust")
        or (action == "update" and login_change(reg, did, body))
        or (action == "address-update" and body.get("newHost") not in (None, body.get("host")))))
    if moves and busy():
        raise frame_devices.DeviceError(
            f"Wait for what's running on {active['name']} to finish (see the activity bar), then try again")
    if action == "use":
        sa = link.session_alias
        d = link.bare(sa) if sa and did == link.bare(sa)["id"] and not reg.by_alias(sa) else reg.get(did)
        link.use(did)
        msg = f"Switched to {d['name']}"
    elif action == "update":
        before = reg.get(did)
        d = reg.update_device(did, name=body.get("name"), user=body.get("user"), port=body.get("port"))
        login_changed = (d["user"], d["port"]) != (before["user"], before["port"])
        if is_active and login_changed:
            link.invalidate()  # before anything else can fail: the old login mustn't stay in use
        msg = f"Saved {d['name']}"
        if is_active and not login_changed:
            link.publish(device=link.public_device(link.active_device()))  # a new name shows at once
        if login_changed:
            # Only what changed, and only if the block still says what it did: Set Up
            # Connection may have written a new login meanwhile, which then stands.
            try:
                if not frame_devices.rewrite_block(d["alias"], user=d["user"], port=d["port"],
                                                   expect={"user": before["user"], "port": before["port"]}) \
                        and any(b["alias"] == d["alias"] and (b["user"], b["port"]) != (d["user"], d["port"])
                                for b in frame_devices.parse_blocks(frame_devices.read_config())):
                    msg += "; ~/.ssh/config changed meanwhile, so it was left as it is"
            except OSError as e:
                raise frame_devices.DeviceError(f"Saved, but couldn't update ~/.ssh/config: {e}")
    elif action == "remove":
        if not body.get("config") and len(reg.devices()) == 1 and reg.get(did)["alias"] in {
                b["alias"] for b in frame_devices.parse_blocks(frame_devices.read_config())}:
            # Its ssh alias would stay, and the app would go on using it as a bare alias.
            raise frame_devices.DeviceError("This is your only headset. To remove it completely, also remove its "
                                            "entry from ~/.ssh/config (the box below)")
        d = reg.remove_device(did)
        if d["alias"] == link.session_alias:
            link.session_alias = None  # removed on purpose: not back as a bare alias
        if is_active:
            link.override = None
            link.invalidate()
        frame_devices.forget_pin(did)
        removed = False
        if body.get("config"):
            try:
                removed = frame_devices.remove_block(d["alias"])
            except OSError as e:
                raise frame_devices.DeviceError(f"Removed, but couldn't edit ~/.ssh/config: {e}")
        msg = f"Removed {d['name']}" + (f" and its '{d['alias']}' entry in ~/.ssh/config" if removed else "")
    elif action == "address-add":
        a = reg.add_address(did, body.get("host"), body.get("kind") or None, body.get("label") or "")
        if is_active and link.state["phase"] == "failed":
            link.kick("retry")
        msg = f"Added {a['host']}"
    elif action == "address-update":
        a = reg.update_address(did, body.get("host"), new_host=body.get("newHost"), kind=body.get("kind"),
                               label=body.get("label"))
        if is_active and a["host"] != body.get("host"):
            link.invalidate()  # the address in use may have moved
        msg = f"Saved {a['host']}"
    elif action == "address-remove":
        reg.remove_address(did, body.get("host"))
        if is_active:
            link.invalidate()  # it may be the address in use: stop using it now
        msg = f"Removed {body.get('host')}"
    elif action == "address-move":
        delta = body.get("delta")
        if delta not in (-1, 1):
            raise frame_devices.DeviceError("delta must be -1 or 1")
        reg.move_address(did, body.get("host"), delta)
        msg = "Moved"
    elif action == "test":
        d = reg.get(did)
        if not d["addresses"]:
            raise frame_devices.DeviceError("This headset has no addresses to test yet")
        threading.Thread(target=link.test, args=(did,), daemon=True).start()
        msg = f"Testing {len(d['addresses'])} address{'es' * (len(d['addresses']) != 1)}"
    elif action == "forget-identity":
        # Dropping the pin used to let the next contact save whatever key answered, unseen.
        # A new key is now always shown first: identity-scan, then identity-trust.
        reg.get(did)
        raise frame_devices.DeviceError("Forgetting a headset's identity is no longer supported: use Check its key "
                                        "on the Devices tab (identity-scan, then identity-trust), which shows the "
                                        "new key before saving it")
    elif action == "identity-scan":
        d = reg.get(did)
        scan = link.scan_identity(did)
        msg = (f"{d['name']} shows the key already saved for it" if scan["matches"]
               else f"{d['name']} shows a {scan['type']} key, {scan['fingerprint']}")
        link.devices_changed()
        return dict(devices_view(link), message=msg, scan=scan)
    elif action == "identity-trust":
        d = reg.get(did)
        link.trust_identity(did, body.get("fingerprint"))
        if is_active:
            link.kick("switch")  # reconnect with the key now saved
        msg = f"Saved {d['name']}'s new SSH identity; reconnecting"
    elif action == "name-network":
        reg.name_network(body.get("network"), body.get("name"))
        if link.state["network"]:
            link.refresh_network()
        msg = "Saved the network's name"
    elif action == "setup":
        alias = frame_devices.check_alias(body.get("alias"))
        host = frame_devices.check_host(body["host"]) if body.get("host") else None
        link.reg.undismiss(alias)
        where = open_setup(alias, host)
        msg = f"Opened Set Up Connection for '{alias}' in {where}"
    elif action == "retry":
        link.kick("retry")
        msg = "Connecting…"
    else:
        raise frame_devices.DeviceError("unknown action")
    link.devices_changed()
    return dict(devices_view(link), message=msg)


def next_alias(link):
    """A free alias for a new headset: not one Frame Control knows, nor any `Host` name
    already in ~/.ssh/config (Set Up Connection's block would shadow it)."""
    text = frame_devices.read_config()
    taken = {d["alias"] for d in link.reg.devices()} | {b["alias"] for b in frame_devices.parse_blocks(text)}
    taken |= {a for a in (link.session_alias, link.override, link.active_device().get("alias")) if a}
    for line in text.splitlines():
        f = line.split()
        if f and f[0].lower() == "host":
            taken |= {name for name in f[1:] if not any(c in name for c in "*?!")}
    if "frame" not in taken:
        return "frame"
    n = 2
    while f"frame-{n}" in taken:
        n += 1
    return f"frame-{n}"


LIKELY = re.compile(r"frame|steam", re.I)


def tailscale_find(link, device_id=None):
    """Tailscale peers that could be a headset, likely ones first, for "Find on Tailscale"."""
    ts = frame_network.tailscale_status()
    device = link.reg.get(device_id) if device_id else link.active_device()
    known = {a["host"].rstrip(".").lower() for a in device.get("addresses") or []}
    if not ts.get("installed"):
        return {"up": False, "peers": [], "message": "Tailscale isn't installed on this computer."}
    if not ts.get("up"):
        return {"up": False, "peers": [], "message": "Tailscale isn't running on this computer. Start it, then look again."}
    peers = []
    for p in ts["peers"]:
        ip = next((i for i in p["ips"] if "." in i), p["ips"][0] if p["ips"] else None)
        likely = p["os"] == "linux" and (LIKELY.search(p["name"]) or p["name"].lower() in (
            device["alias"].lower(), (device.get("name") or "").lower()))
        peers.append({"name": p["name"], "dns": p["dns"], "ip": ip, "os": p["os"], "online": p["online"],
                      "likely": bool(likely), "added": bool({p["dns"].lower(), (ip or "").lower()} & known)})
    peers.sort(key=lambda p: (not p["likely"], p["os"] != "linux", not p["online"], p["name"].lower()))
    return {"up": True, "peers": peers, "tailnet": ts.get("tailnet"), "message": None}


def mdns_find(link, device_id=None):
    """Headsets on this network: SteamOS devkit services (mDNS) and <alias>.local."""
    device = link.reg.get(device_id) if device_id else link.active_device()
    known = {a["host"].rstrip(".").lower() for a in device.get("addresses") or []}
    try:
        import frame_connect
        found = frame_connect.discover_devkit()
    except (ImportError, SystemExit, OSError):
        found = []
    names = list(dict.fromkeys([h.rstrip(".") for h in found] + [f"{device['alias']}.local", "frame.local"]))
    rows = [None] * len(names)

    def check(i, host):
        res = probe(host, 22, timeout=3)
        rows[i] = {"host": host, "state": res["state"], "ip": res.get("ip"), "rtt_ms": res.get("rtt_ms"),
                   "detail": res["detail"], "advertised": host in [h.rstrip(".") for h in found],
                   "added": host.lower() in known or (res.get("ip") or "").lower() in known}
    threads = [threading.Thread(target=check, args=(i, h), daemon=True) for i, h in enumerate(names)
               if frame_devices.HOST_RE.fullmatch(h)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(8)
    hosts = [r for r in rows if r and (r["advertised"] or r["state"] in ("answered", "refused"))]
    return {"hosts": hosts, "tool": bool(frame_host.which("dns-sd") or frame_host.which("avahi-browse"))}
