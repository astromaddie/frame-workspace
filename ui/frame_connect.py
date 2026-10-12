"""Connect this computer to the Steam Frame: find it, create keys, add a `Host frame`
alias to ~/.ssh/config and get a key onto the headset. It first asks Valve's
SteamOS devkit service (port 32000) to pair, which needs only a tap on the
headset; if that service isn't there or says no, it copies the key over SSH,
asking for the Developer Mode password once. The Linux and Windows twin of
scripts/connect.sh (which the Mac app uses); same config block, so either can
re-run over the other. Idempotent.

Usage: python3 ui/frame_connect.py [--alias NAME] [HOST_OR_IP[:PORT]]
Env:   FRAME_USER (default steamos), FRAME_ALIAS (default frame; --alias wins, for
       terminals that don't pass the environment on, like Windows' `start`)
"""
import base64
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# The app runs this with python -I, which leaves the script's folder off sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import frame_host  # noqa: E402

FRAME_USER = os.environ.get("FRAME_USER", "steamos")
USER_FROM_ENV = "FRAME_USER" in os.environ
FRAME_ALIAS = os.environ.get("FRAME_ALIAS", "frame")
# $FRAME_CONTROL_SSH_DIR in tests, as frame_devices.ssh_dir(), so they never touch the real one.
SSH_DIR = Path(os.environ.get("FRAME_CONTROL_SSH_DIR") or Path.home() / ".ssh")
KEY = SSH_DIR / "id_ed25519_frame"
# The devkit service only accepts ssh-rsa keys (write_key in Valve's
# steamos-devkit-service), so pairing uses a second key next to the ed25519 one.
DEVKIT_KEY = SSH_DIR / "id_rsa_frame_devkit"
CONFIG = SSH_DIR / "config"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# Both go into ~/.ssh/config, so nothing that could add a line or a directive.
for _name, _value in (("FRAME_ALIAS", FRAME_ALIAS), ("FRAME_USER", FRAME_USER)):
    if not NAME_RE.fullmatch(_value):
        sys.exit(f"{_name} must be a plain name, not {_value!r}")
BEGIN = f"# >>> steam-frame ({FRAME_ALIAS}) >>>"
END = f"# <<< steam-frame ({FRAME_ALIAS}) <<<"

# Appends the key from stdin unless it's already there. base64 keeps it intact
# through Windows' command-line quoting.
ADD_KEY = """umask 077
mkdir -p ~/.ssh
k=$(cat)
grep -qxF "$k" ~/.ssh/authorized_keys 2>/dev/null || printf '%s\\n' "$k" >> ~/.ssh/authorized_keys
"""
ADD_KEY_CMD = 'sh -c "$(echo %s | base64 -d)"' % base64.b64encode(ADD_KEY.encode()).decode()


def say(msg):
    print(msg, flush=True)


# --- Valve's SteamOS devkit pairing (steamos-devkit-service on the headset). HTTP on
# port 32000: GET /properties.json names the user to log in as; POST /register with
# "ssh-rsa <key> <comment> <magic>" shows an approve prompt in the headset (the
# comment is what it displays, 30 s to answer), then installs the key and turns sshd on.
# The prompt only appears while Steam is on Settings > Developer > Pair new host;
# otherwise /register answers 403 "please put the Steam client in pairing mode".

DEVKIT_PORT = 32000
DEVKIT_SERVICE = "_steamos-devkit._tcp"
MAGIC_PHRASE = "900b919520e4cf601998a71eec318fec"  # fixed token Valve's client appends
REGISTER_TIMEOUT = 60
PAIRING_MODE_WAIT = 120  # seconds to keep asking while the user opens "Pair new host"
# A LAN host: never go through an HTTP(S)_PROXY from the environment.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def key_comment(node):
    """"frame-control@<short host name>" as one word: the headset splits the body on spaces."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", (node or "").split(".")[0]).strip("-.") or "computer"
    return f"frame-control@{name}"


def register_body(pub, comment):
    fields = pub.split()
    if len(fields) < 2 or fields[0] != "ssh-rsa":
        raise ValueError("the devkit service only takes ssh-rsa keys")
    return f"ssh-rsa {fields[1]} {comment} {MAGIC_PHRASE}\n"


def parse_login(raw):
    """The `login` from /properties.json if it's a plain user name, else None. "root"
    means several users are configured and Valve's client switches between them; we
    can't, so it counts as no answer."""
    props = json.loads(raw)
    if not isinstance(props, dict):
        raise ValueError("properties.json isn't a JSON object")
    login = props.get("login")
    if isinstance(login, str) and NAME_RE.fullmatch(login) and login != "root":
        return login
    return None


def devkit_error(status, raw):
    """A readable reason from a failed /register: its {"error": ...} JSON, or the text."""
    text = raw.decode("utf-8", "replace").strip()
    try:
        err = json.loads(text).get("error")
    except (ValueError, AttributeError):
        err = None
    return str(err or text or f"HTTP {status}")[:300]


def devkit_url(host, port, path):
    return f"http://[{host}]:{port}{path}" if ":" in host else f"http://{host}:{port}{path}"


def why(e):
    return str(getattr(e, "reason", None) or e)


def fetch_login(host, port=DEVKIT_PORT, timeout=5):
    """GET /properties.json. Raises OSError (HTTP errors included) or ValueError."""
    with _opener.open(devkit_url(host, port, "/properties.json"), timeout=timeout) as r:
        return parse_login(r.read())


def register(host, body, port=DEVKIT_PORT, timeout=REGISTER_TIMEOUT):
    """POST /register, which waits while someone answers the prompt. -> (ok, message)"""
    req = urllib.request.Request(devkit_url(host, port, "/register"), data=body.encode("ascii"),
                                 headers={"Content-Type": "text/plain"}, method="POST")
    try:
        with _opener.open(req, timeout=timeout) as r:
            return True, r.read().decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as e:
        with e:
            return False, devkit_error(e.code, e.read())
    except OSError as e:
        return False, f"no answer ({why(e)})"


def devkit_pair(host, pub, comment, port=DEVKIT_PORT, on_login=None):
    """The password-free route. -> None once paired, else the reason, which means: fall
    back to copying the key with the password. on_login(user) runs before the prompt
    with the login properties.json names, so the fallback uses that user too."""
    try:
        login = fetch_login(host, port)
    except (OSError, ValueError) as e:
        return f"devkit service not reachable on port {port}: {why(e)}"
    if login and on_login:
        on_login(login)
    say("    In the headset: Steam Settings > Developer > Pair new host, then approve the request")
    body = register_body(pub, comment)
    ok, msg = register(host, body, port)
    # The headset refuses at once unless Steam is on its "Pair new host" screen
    # (verified on a Frame, 2026-09-26), so keep asking while the user opens it.
    deadline = time.monotonic() + PAIRING_MODE_WAIT
    while not ok and "pairing mode" in msg and time.monotonic() < deadline:
        time.sleep(3)
        ok, msg = register(host, body, port)
    return None if ok else f"devkit pairing failed: {msg}"


def split_port(arg):
    """"host:2222" -> ("host", 2222); anything else (IPv6 too) keeps port 22."""
    host, sep, port = arg.rpartition(":")
    if sep and port.isdigit() and ":" not in host:
        return host, int(port)
    return arg, 22


def port_open(host, port=22):
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def reachable(host, port):
    """sshd, or the devkit service, which turns sshd on once a pairing is approved."""
    try:
        socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return port_open(host, port) or port_open(host, DEVKIT_PORT)


# --- mDNS. There's no stdlib client, so this borrows dns-sd (macOS; Bonjour for
# Windows) or avahi-browse (Linux) when present, with short timeouts.

def run_for(args, seconds):
    """What a command printed within `seconds`; dns-sd never exits by itself."""
    try:
        out = subprocess.run(args, capture_output=True, timeout=seconds).stdout
    except subprocess.TimeoutExpired as e:
        out = e.stdout
    except OSError:
        out = b""
    return (out or b"").decode("utf-8", "replace")


def parse_dns_sd_browse(text):
    """Instance names from `dns-sd -B _steamos-devkit._tcp`, deduplicated, in order."""
    pat = re.compile(r"\sAdd\s+\d+\s+\d+\s+\S+\s+" + re.escape(DEVKIT_SERVICE) + r"\.\s+(.+?)\s*$")
    names = []
    for line in text.splitlines():
        m = pat.search(line)
        if m and m.group(1) not in names:
            names.append(m.group(1))
    return names


def parse_dns_sd_resolve(text):
    """The target host from `dns-sd -L` ("... can be reached at frame.local.:32000")."""
    m = re.search(r"can be reached at (\S+?)\.?:\d+", text)
    return m.group(1) if m else None


def parse_avahi(text):
    """Host names, then IPv4 addresses, from `avahi-browse -rpt` resolved ("=") lines."""
    names, addrs = [], []
    for line in text.splitlines():
        f = line.split(";")
        if len(f) >= 9 and f[0] == "=" and f[2] == "IPv4":
            names.append(f[6])
            addrs.append(f[7])
    return list(dict.fromkeys(names + addrs))


def discover_devkit():
    if shutil.which("dns-sd"):
        hosts = []
        for name in parse_dns_sd_browse(run_for(["dns-sd", "-B", DEVKIT_SERVICE, "local."], 3))[:4]:
            host = parse_dns_sd_resolve(run_for(["dns-sd", "-L", name, DEVKIT_SERVICE, "local."], 2))
            if host and host not in hosts:
                hosts.append(host)
        return hosts
    if shutil.which("avahi-browse"):
        return parse_avahi(run_for(["avahi-browse", "-rpt", DEVKIT_SERVICE], 5))
    return []


HOST_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.:%-]*")


def pick_host(arg):
    if arg and not HOST_RE.fullmatch(arg):
        say(f"  - {arg!r} isn't a host name or IP address")
        return None
    for cand in [arg] if arg else [f"{FRAME_ALIAS}.local", FRAME_ALIAS]:
        host, port = split_port(cand)
        if reachable(host, port):
            return host, port
        say(f"  - {cand}: not resolvable, or ports {port} and {DEVKIT_PORT} closed")
    if arg:
        return None
    say(f"  - asking mDNS for {DEVKIT_SERVICE}")
    for host in discover_devkit():
        if HOST_RE.fullmatch(host) and reachable(host, 22):
            return host, 22
        say(f"  - {host}: advertised, but not reachable")
    return None


def make_ssh_dir():
    # Windows: no mode. Python 3.12.4+ turns 0o700 into an owner-only ACL, which locks
    # the user out if the folder's owner is Administrators; the profile's ACL suffices.
    if os.name == "nt":
        SSH_DIR.mkdir(exist_ok=True)
    else:
        SSH_DIR.mkdir(mode=0o700, exist_ok=True)


def make_key(path, kind, comment):
    if path.exists():
        say(f"    exists: {path}")
        return
    bits = ["-b", "3072"] if kind == "rsa" else []
    subprocess.run(["ssh-keygen", "-q", "-t", kind, *bits, "-N", "", "-C", comment, "-f", str(path)], check=True)
    say(f"    created {path}")


def config_block(host, port=22, user=FRAME_USER):
    return [BEGIN, f"Host {FRAME_ALIAS}", f"  HostName {host}", *([f"  Port {port}"] if port != 22 else []),
            f"  User {user}",
            "  IdentityFile ~/.ssh/id_ed25519_frame", "  IdentityFile ~/.ssh/id_rsa_frame_devkit",
            "  IdentitiesOnly yes", "  ServerAliveInterval 30", "Host *", END]


class config_lock:
    """The lock Frame Control takes to edit ~/.ssh/config (frame_devices.file_lock), so a
    running app and this setup never write over each other's change."""

    def __enter__(self):
        self.fh = open(SSH_DIR / "config.frame-control.lock", "a+")
        for _ in range(300):
            try:
                if os.name == "nt":
                    import msvcrt
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.lockf(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                time.sleep(0.1)
        return self  # 30 s: go ahead rather than fail the setup

    def __exit__(self, *exc):
        self.fh.close()  # closing releases the lock
        return False


def write_config(host, port=22, user=FRAME_USER):
    make_ssh_dir()
    with config_lock():
        _write_config(host, port, user)


def _write_config(host, port, user):
    """Replace our managed block and put it first: ssh uses the first value it sees per
    option. The trailing "Host *" returns the rest of the file to global scope."""
    old = CONFIG.read_text(encoding="utf-8") if CONFIG.exists() else ""
    kept, skip = [], False
    for line in old.splitlines():
        if line == BEGIN:
            skip = True
        elif line == END:
            skip = False
        elif not skip:
            kept.append(line)
    block = config_block(host, port, user)
    tmp = CONFIG.with_name(f"config.frame-control.{os.getpid()}.tmp")
    tmp.write_text("\n".join(block + kept) + "\n", encoding="utf-8")
    if not frame_host.make_private(tmp):
        say("    couldn't make ~/.ssh/config private; if ssh says \"Bad owner or permissions\", "
            "Frame Control repairs it when it next connects")
    # On Windows a running ssh.exe (Frame Control's own, say) keeps the config open
    # and locked, so the swap can fail for a moment; keep trying for a while.
    for attempt in range(60):
        try:
            os.replace(tmp, CONFIG)
            return
        except PermissionError:
            if attempt == 0:
                say("    ~/.ssh/config is in use by another ssh; waiting for it...")
            time.sleep(0.5)
    tmp.unlink(missing_ok=True)
    raise SystemExit("~/.ssh/config stayed locked by another program. Quit Frame Control "
                     "and any ssh windows, then run the setup again.")


def key_login_works():
    # accept-new: trust a first-seen host key (as the copy step does); a changed one still fails.
    return frame_host.run_ssh(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                               "-o", "StrictHostKeyChecking=accept-new", FRAME_ALIAS, "true"],
                              capture_output=True).returncode == 0


def configured_user():
    """The User in our managed block, so a re-run keeps one the headset named earlier."""
    if not CONFIG.exists():
        return None
    inside = False
    for line in CONFIG.read_text(encoding="utf-8").splitlines():
        if line in (BEGIN, END):
            inside = line == BEGIN
        elif inside and line.startswith("  User "):
            name = line[7:].strip()
            return name if NAME_RE.fullmatch(name) else None
    return None


def pair_with_devkit(host, port, user):
    """Try devkit pairing and confirm key login. -> (user, None) or (user, reason to fall back)."""
    say("==> Pairing through the headset's SteamOS devkit service (no password)")
    chosen = [user]

    def use_login(login):
        if login == chosen[0]:
            return
        if USER_FROM_ENV:
            say(f"    the headset logs in as '{login}'; keeping FRAME_USER={user}")
        else:
            chosen[0] = login
            say(f"    the headset logs in as '{login}'")
            write_config(host, port, login)

    try:
        pub = DEVKIT_KEY.with_suffix(".pub").read_text(encoding="utf-8")
    except OSError as e:
        return user, f"can't read the pairing key: {e}"
    reason = devkit_pair(host, pub, key_comment(platform.node()), on_login=use_login)
    if reason:
        return chosen[0], reason
    # The approval is what turns sshd on, so it may take a moment to answer.
    for _ in range(10):
        if key_login_works():
            return chosen[0], None
        time.sleep(1)
    return chosen[0], "paired, but key login still fails"


def use_alias(alias):
    """--alias: set up another headset under its own ~/.ssh/config alias (Devices tab)."""
    global FRAME_ALIAS, BEGIN, END
    if not NAME_RE.fullmatch(alias):
        sys.exit(f"--alias must be a plain name, not {alias!r}")
    FRAME_ALIAS = alias
    BEGIN = f"# >>> steam-frame ({FRAME_ALIAS}) >>>"
    END = f"# <<< steam-frame ({FRAME_ALIAS}) <<<"


def main(argv):
    if argv and argv[0] in ("-h", "--help"):
        sys.exit(__doc__)
    if len(argv) >= 2 and argv[0] == "--alias":
        use_alias(argv[1])
        argv = argv[2:]
    say("==> Looking for the Steam Frame")
    found = pick_host(argv[0] if argv else None)
    while not found:
        say("Could not reach the Frame over SSH.")
        say("Check: Developer Mode on and a user password set; same network; no client isolation.")
        try:
            typed = input("Type the Frame's IP address (Quick Settings shows it), or press Enter to quit: ").strip()
        except EOFError:
            typed = ""
        if not typed:
            return 1
        found = pick_host(typed)
    host, port = found
    say(f"    found: {host}" + (f" port {port}" if port != 22 else ""))

    say("==> SSH keys")
    make_ssh_dir()
    make_key(KEY, "ed25519", f"{platform.node() or 'computer'}->steam-frame")
    make_key(DEVKIT_KEY, "rsa", key_comment(platform.node()))

    user = FRAME_USER if USER_FROM_ENV else (configured_user() or FRAME_USER)
    say(f"==> ~/.ssh/config alias '{FRAME_ALIAS}' -> {host}")
    write_config(host, port, user)

    say("==> Checking key login")
    if key_login_works():
        say("    key login already works")
    else:
        user, reason = pair_with_devkit(host, port, user)
        if not reason:
            say("    paired; key login OK")
        else:
            say(f"    {reason}; falling back to the password")
            say("    copying the key: enter the Developer Mode password when asked")
            pub = KEY.with_suffix(".pub").read_text(encoding="utf-8").strip()
            r = subprocess.run(["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "PubkeyAuthentication=no",
                                "-p", str(port), f"{user}@{host}", ADD_KEY_CMD], input=pub + "\n", text=True)
            if r.returncode != 0 or not key_login_works():
                say("Key login still isn't working. Check the password and run this again.")
                return 1
            say("    key login OK")
    say(f"\nDone. Frame Control can reach the Frame now. In a terminal: ssh {FRAME_ALIAS}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
