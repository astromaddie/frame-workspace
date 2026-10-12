# Headsets, addresses and the connection

Frame Control can manage more than one Steam Frame, and each headset can be
reached at more than one address: a LAN IP at home, another at the office, its
mDNS name (`frame.local`), its Tailscale IP or MagicDNS name. The **Devices**
tab (key 5) lists them, and the connection pill in the header shows what the
app is doing to reach the one in use, step by step, as it happens.

The code is in three modules, all stdlib-only Python on your computer:

| Module | What it does |
|---|---|
| `ui/frame_devices.py` | The registry: headsets, their addresses, networks; importing and updating `~/.ssh/config`; pinned host keys |
| `ui/frame_network.py` | Which network this computer is on, and Tailscale's state |
| `ui/frame_link.py` | The connector: finds the headset, keeps the SSH connection, publishes each stage; the Devices API |

## Headsets

Each headset keeps its own SSH alias, as Set Up Connection has always written
it: the first is `frame`, the next `frame-2`, and so on. Terminal's
`ssh frame-2` and the helper scripts (`FRAME_ALIAS=frame-2 scripts/push.sh …`)
work for each one.

- **Nothing to migrate by hand.** On first start, the app imports every
  `# >>> steam-frame (ALIAS) >>>` block in `~/.ssh/config` as a headset, with
  the block's HostName as its first address. It also copies the host key your
  `known_hosts` already trusts for that address into the headset's own
  known_hosts file, `~/.ssh/frame-control-hosts/<id>`, so nobody is asked to trust it again.
- **Add a headset** runs Set Up Connection (`scripts/connect.sh` on macOS,
  `ui/frame_connect.py --alias NAME` elsewhere) in a terminal with a new alias.
  When it writes its block, the app picks the headset up by itself. If Set Up
  Connection runs again and finds a headset somewhere new, that address is added
  at the top of its list.
- **Use this headset** (or the switcher in the header, or the app's
  **Frame → Headset** menu) moves the whole app to another headset; every panel
  reloads from it. From that moment no command goes to the previous headset, even
  if the new one never answers. It waits while an install is running, since an
  install reads the SSH settings step by step.
- **Remove** forgets a headset. Its `~/.ssh/config` block stays unless you tick
  the box; either way it isn't imported again unless Set Up Connection changes it.
- A plain `FRAME_ALIAS` that Set Up Connection never configured still works: the
  app shows it as not set up and lets ssh's own config decide where it goes.

## Addresses

Each address has a kind (LAN, mDNS, Tailscale or Other, guessed from the address
and changeable), an optional label, the networks it has worked on, and when it
last worked with its round-trip time.

When connecting, the app **tries all addresses at once** (TCP to the SSH port)
and ranks them:

1. addresses that worked on the network this computer is on now;
2. mDNS names;
3. Tailscale addresses, if Tailscale is running here;
4. addresses not tried on this network yet;
5. addresses that only ever worked on other networks;
6. Tailscale addresses while Tailscale is off.

Your order on the Devices tab breaks ties. The best-ranked address that answers
wins; one that answers first waits up to 0.35 s for a better-ranked one that is
still trying. If SSH to the winner fails in a way another address could fix
(a different device answered there, or the link dropped), the next one that
answered is tried. Every success records the network on that address, so next
time on that network it's tried first.

**Test now** probes every address and tries SSH on each one that answers, without
disturbing the connection in use: "SSH works", "answered as a different
headset", "refused this computer's key", or why it didn't answer. **Find on
Tailscale** lists your tailnet's devices (likely headsets first, from `tailscale
status --json`, including the Mac app's own CLI) with buttons to add their
MagicDNS name or IP. **Find on this network** asks mDNS for SteamOS devkit
services and checks `ALIAS.local` and `frame.local`.

## Networks

A network is told apart by its default gateway: the router's IP address plus its
hardware (MAC) address, read with `route`/`arp` (macOS), `ip route`/`ip neigh`
(Linux) or `route print`/`arp -a` (Windows). That works on wired networks, and
on macOS 14 and later, which hides the Wi-Fi name from apps without Location
permission. Where the system does share the Wi-Fi name, it's shown, and you can
name any network yourself ("Home Wi-Fi") on the Devices tab.

The app rereads the gateway every 5 seconds and Tailscale's state every
30 seconds. Changing networks reconnects.

## The connection, stage by stage

The connector runs in the server (`frame_link.Link`) and moves through:

1. **Checking this computer's network**: gateway, Wi-Fi, this computer's IP, Tailscale.
2. **Finding the headset**: each address resolving, trying, answered in N ms,
   no answer, refused, or can't be found.
3. **Opening SSH** to the address that answered.
4. **Checking the headset's identity**: the host key must match the one pinned
   for this headset.
5. **Logging in** as the headset's user.
6. **Connected** via network N, address A, round trip T; or **failed** at a stage
   with the reason in plain words and a countdown to the next try (5, 10, 20,
   then every 30 seconds). Retry now skips the wait.

Stages 3 to 5 come from following `ssh -v` as it runs. On macOS and Linux the
connection is an SSH ControlMaster that every command shares; when it dies (the
headset slept or left the network) the connector notices and starts again. On
Windows, where OpenSSH can't share a connection, the same handshake runs once
and each command then connects on its own; a command that can't reach the
headset makes the connector start again.

Once connected, every `ssh`, `scp` and `rsync` the app runs gets
`-o HostName=<address> -o HostKeyAlias=frame-control-<id>
-o UserKnownHostsFile=~/.ssh/frame-control-hosts/<id> -o HashKnownHosts=no -o User=… -o Port=…`. The
alias's block in `~/.ssh/config` is also updated to the last address that
worked (and to the user and port you set), so Terminal's `ssh frame` and the
scripts follow. Edits to `~/.ssh/config` take a lock file
(`~/.ssh/config.frame-control.lock`) that Set Up Connection takes too, and never
write over a change someone else made since the app last read the file.

**Host keys are pinned per headset, not per address.** Your own `known_hosts`
is keyed by address, so a different device answering at a remembered IP (a DHCP
lease that moved) would look like a new host there. The app keeps one known_hosts
file per headset instead, so saving or forgetting one headset's key never touches
another's: a different device answering at one of its
addresses is refused, and the pill says so. A headset's first connection trusts
the key it shows, as Set Up Connection does: until a key is saved, every ssh
command gets `StrictHostKeyChecking=accept-new` (the first contact saves it,
whichever command makes it), and from then on `StrictHostKeyChecking=yes`.
`accept-new` never replaces a saved key, so it can't hide a change.

**When the key changes** (SteamOS reinstalled or reset, a new headset at the
same address, or a different device answering there), nothing is accepted by
itself. The connection fails at *Checking the headset's identity*; the banner
and the headset on the Devices tab say so, with **Check the new key…** (also
**Check its key…** on the Devices tab at any time). That reads the key the
headset shows now, with ssh, a throwaway known_hosts file and no login
(`PreferredAuthentications=none`, so nothing of this computer's is offered to a
device that may not be the headset), and shows its SHA256 fingerprint next to
the saved one, with the command that prints it on the headset
(`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`, in Konsole on its desktop).
**This is my headset: trust its new key** then saves exactly that key, replacing
the old one, and reconnects. The server keeps the key it showed for 10 minutes
and saves it only if the page sends back the same fingerprint; it never saves
whatever answers next. Set Up Connection can't fix a changed key: it checks your
own `known_hosts`, not the headset's file.

**When the headset refuses this computer's key** (`Permission denied
(publickey,…)`), the connection stops at *Logging in*, and the banner and the
Devices tab offer **Set Up Connection…**, which pairs this computer again.

Neither is reported as a fault: the diagnostics report each kind at most once
a session ([privacy.md](privacy.md)).

## One server at a time

Only one Frame Control server runs per user (a lock file, `server.lock`, in the app's
data folder). Two would each connect, reconnect and edit the headsets on their own, and
one could move the other's install to a different headset. A second one, say
`scripts/frame-ui.sh` while the app is open, exits with "Frame Control is already
running". `FRAME_CONTROL_DATA_DIR` gives a separate one, with its own headsets.

## API

All under the usual `/api/` guards (loopback `Host`, `X-Frame-UI` header).

| Request | Returns |
|---|---|
| `GET /api/connection` | The connection state: `phase` (connecting, connected, failed), `device`, `network`, `stages`, `probes`, `via`, `error`, `retry_at`, `tests`, `version` |
| `GET /api/connection/events` | The same as server-sent events, one each time it changes (the page reads it with `fetch`, since `EventSource` can't send the header) |
| `GET /api/devices` | Headsets, the current network, known networks, the next free alias |
| `GET /api/devices/tailscale?id=` | Tailscale peers, likely headsets first |
| `GET /api/devices/mdns?id=` | Headsets found on this network |
| `POST /api/devices` | `{"action": ...}`: `use`, `update` (name, user, port), `remove`, `address-add`, `address-update`, `address-remove`, `address-move`, `test`, `forget-identity`, `identity-scan` (read the key the headset shows: `scan` with `type`, `fingerprint`, `host`, `saved`, `matches`), `identity-trust` (`fingerprint`: save the key `identity-scan` showed), `name-network`, `setup` (alias, optional host), `retry` |

Every host, alias and user is checked against strict patterns before it's
stored, because they end up in ssh arguments and `~/.ssh/config`; nothing goes
through a shell.

## The registry file

`devices.json` in the app's data folder (`~/Library/Application Support/Frame
Control` on macOS, `%APPDATA%\Frame Control` on Windows,
`~/.local/share/frame-control` on Linux). It's plain JSON so the iPhone app can
share the format later (it still connects to one host; see
[iphone.md](iphone.md)):

```json
{"version": 1, "active": "f67f8b7e",
 "devices": [{"id": "f67f8b7e", "name": "Steam Frame", "alias": "frame", "user": "steamos", "port": 22,
              "identity_files": ["~/.ssh/id_ed25519_frame"],
              "addresses": [{"host": "frame.local", "kind": "mdns", "label": "",
                             "networks": ["n-e0998baa61"], "last_ok": 1790593550.4, "last_rtt_ms": 0.9}]}],
 "networks": {"n-e0998baa61": {"name": "Home Wi-Fi", "ssid": null, "gateway": "192.168.1.1",
                               "gateway_mac": "b4:fb:e4:b5:67:55", "wifi": true, "last_seen": 1790593550.0}}}
```

A network id is `n-` and the first 10 hex digits of SHA-1 of `gateway|mac`.

## Tests

`tests/test_devices.py`, `tests/test_network.py` and `tests/test_link.py` run
with the other unit tests. They use a stand-in `ssh` (`tests/fakessh/ssh`) that
prints what `ssh -v` prints and plays a ControlMaster, real sockets on this
computer for the addresses, and temporary folders for `~/.ssh`
(`FRAME_CONTROL_SSH_DIR`) and the app data (`FRAME_CONTROL_DATA_DIR`), so they
never touch yours.
