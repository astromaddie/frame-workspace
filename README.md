<div align="center">

<img src="docs/img/icon.png" width="112" alt="Frame Control icon">

# Frame Control

**Manage your Valve Steam Frame from your computer.**<br>
See what the headset sees, install games and Android apps, move files and text across, and check battery and status, all over SSH.

[![Latest release](https://img.shields.io/github/v/release/saphid/steam-frame?label=release&color=1a9fff)](https://github.com/saphid/steam-frame/releases/latest)
[![Platforms](https://img.shields.io/badge/macOS%20%7C%20Windows%20%7C%20Linux-2a475e?label=runs%20on)](#install)
[![Checks](https://img.shields.io/github/actions/workflow/status/saphid/steam-frame/checks.yml?branch=main&label=checks)](https://github.com/saphid/steam-frame/actions/workflows/checks.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-66c0f4)](LICENSE)

[**Website**](https://frame-control.pages.dev) · [**Download**](#install) · [Trailer](#trailer) · [Features](#features) · [Set up the headset](#set-up-the-headset) · [Feedback](#feedback) · [Docs](#going-further)

<br>

<img src="docs/img/frame-control.png" alt="Frame Control's Games tab: installed games, sideloaded titles, and your Steam library with Frame ratings" width="900">

<a id="trailer"></a>
<a href="https://github.com/saphid/steam-frame/releases/download/trailer/frame-control-trailer.mp4"><img src="docs/img/trailer.jpg" alt="Watch the Frame Control trailer" width="900"></a>

<sub>The trailer: 66 seconds, with sound. Downloads the MP4 from the trailer release.</sub>

<sub>Unofficial hobby project, not affiliated with Valve. Free and open source.</sub>

</div>

---

## Features

<table>
<tr>
<td width="50%" valign="top">

**👓 Headset view and Desktop**<br>
Live video of what the lenses show, or of the app panel in use, flat and still however the wearer looks around. Turn on Control and tap or click right on it to use the Frame from your phone or computer.

</td>
<td width="50%" valign="top">

**🔋 Battery and status**<br>
Charge, charging watts and time left, storage, memory, temperature, Wi-Fi, and what's running.

</td>
</tr>
<tr>
<td valign="top">

**🎮 Steam games**<br>
Everything you own with its Steam Frame rating. Install onto the headset with live progress, and search the store.

</td>
<td valign="top">

**🤖 Android apps**<br>
About 4,500 F-Droid apps rated for the Frame. One click installs each as its own app in your Steam library.

</td>
</tr>
<tr>
<td valign="top">

**📁 Files, games and clipboard**<br>
Drag files onto the window to send them. Drop a game's .zip, folder or .exe to add it to the Steam library, with Proton or the Linux runtime picked for you. Send text or your clipboard straight to the headset's desktop.

</td>
<td valign="top">

**📸 Screenshots**<br>
Browse the shots you take in the headset and save them to your Pictures folder.

**⌨️ Keyboard and trackpad**<br>
Type and point in the Frame's apps from your computer or phone, through KDE Connect, which Frame Control brings along and sets up on the Frame. Nothing else to install, anywhere.

</td>
</tr>
<tr>
<td valign="top">

**🧩 Flatpaks and display**<br>
Install desktop apps like Moonlight or VLC, and set each Android app's resolution and text size.

</td>
<td valign="top">

**⚡ One-click tools**<br>
SSH, SFTP, Steam Link, remote desktop, volume, sleep, restart and shut down.

</td>
</tr>
</table>

The optional [Family and comfort](docs/family-comfort.md) card adds session
limits, breaks, local alerts and one-click casting. A session copies a small
Frame Control worker into your headset user account.

For the other features, nothing is installed on the Frame: the app uses what SteamOS
already ships (sideloading a game copies Valve's own devkit scripts to
`~/devkit-utils`, as Valve's Devkit Client does). The optional
[performance HUD](docs/vr-utilities.md) copies our own Python helpers into
`~/.local/share/frame-control/vr/`. [How each feature works](docs/frame-control.md).

## Install

| | Download | Needs |
|---|---|---|
| **macOS** (Apple Silicon) | [Frame-Control-mac-arm64.dmg](https://github.com/saphid/steam-frame/releases/latest/download/Frame-Control-mac-arm64.dmg) | Nothing extra |
| **Windows** 10 / 11 (x64) | [Frame-Control-Setup-x64.exe](https://github.com/saphid/steam-frame/releases/latest/download/Frame-Control-Setup-x64.exe) · [portable .zip](https://github.com/saphid/steam-frame/releases/latest/download/Frame-Control-win-x64.zip) | Nothing extra |
| **Linux** (x64) | [AppImage](https://github.com/saphid/steam-frame/releases/latest/download/Frame-Control-linux-x86_64.AppImage) · [.deb](https://github.com/saphid/steam-frame/releases/latest/download/Frame-Control-linux-amd64.deb) | `ssh` (most desktops have it) |
| **Linux** (arm64) | [AppImage](https://github.com/saphid/steam-frame/releases/latest/download/Frame-Control-linux-arm64.AppImage) · [.deb](https://github.com/saphid/steam-frame/releases/latest/download/Frame-Control-linux-arm64.deb) | `ssh`, and `adb` for Android apps (`sudo apt install adb`) |

**iPhone and iPad:** the same features from your phone, with nothing to install on
a computer. Build it from [`ios/`](ios) in Xcode; see [docs/iphone.md](docs/iphone.md).

The app brings its own Python and `adb`; SSH is built into macOS and Windows.
From 0.4 it updates itself: when a new version is published, a banner offers
**Update and restart**. It sends anonymous usage statistics, which you can turn
off. Sharing compatibility results and error details is opt-in. See
[docs/privacy.md](docs/privacy.md).
Google doesn't publish `adb` for arm64 Linux, so that build uses your
distribution's. If you already have `adb`, the app uses yours.

<details>
<summary><b>macOS: the app isn't notarized</b></summary>

There's no paid Apple developer account behind it, so macOS says the app is
damaged or can't be checked. Drag it to Applications, then clear the download
quarantine once:

```sh
xattr -dr com.apple.quarantine "/Applications/Frame Control.app"
```

The first time, macOS also asks to allow local network access (for SSH) and
control of Terminal (for the password prompts).
</details>

<details>
<summary><b>Windows: SmartScreen warning</b></summary>

The installer isn't code-signed, so Windows SmartScreen may say it protected
your PC. Choose **More info → Run anyway**. The portable `.zip` avoids the
installer: unzip it anywhere and run `Frame Control.exe`.
</details>

<details>
<summary><b>Linux: running the AppImage</b></summary>

```sh
chmod +x Frame-Control-linux-*.AppImage && ./Frame-Control-linux-*.AppImage
```

If it complains about FUSE, install `libfuse2` (Ubuntu 24.04+: `libfuse2t64`),
or run it with `--appimage-extract-and-run`.
</details>

## Set up the headset

You type one password on the headset, once. Everything else happens on your
computer.

1. **On the Frame:** Steam Settings → System → **Enable Developer Mode**, then
   in the Developer section, **Set User Password**. Pick something short:
   you'll type it once more on your computer and then never again.
2. **On your computer:** open Frame Control. It offers to **Set Up
   Connection**, which finds the headset, creates an SSH key, and asks for that
   password once in a terminal window. If it can't find the Frame, type the
   IP address from the Frame's Quick Settings.

   Before asking for the password it tries Valve's SteamOS devkit pairing: in
   the headset, open Steam Settings → Developer → **Pair new host** and approve
   the request, and no password is needed. (The service and the pairing-mode
   step are verified on a Frame; the approval itself isn't yet. See
   [SSH](docs/ssh.md#password-free-pairing-steamos-devkit-service).)
3. That's it. The app now reaches the headset whenever it's awake and on the
   same network. For anywhere else, see [Tailscale](docs/tailscale.md).

**What it changes:** only what you click. Installs go to your user account on
the Frame (`--user` Flatpaks, Lepton instances, Steam downloads, sideloaded
games in `~/devkit-game`), and nothing
needs `sudo` except the power buttons. On your computer it adds a `Host frame`
entry to `~/.ssh/config` and keys at `~/.ssh/id_ed25519_frame` and
`~/.ssh/id_rsa_frame_devkit` (the pairing service only takes RSA keys).

## Feedback

This is a first public test, so reports are really useful, especially from
Windows and Linux. The quickest way is **Report a problem** in the app (the
warning-sign button at the top, or **Help → Report a Problem…**). It adds
diagnostics with personal details removed, shows you exactly what's included,
and sends it privately to the maintainer; nothing is published. Without the app,
use the [feedback form](https://frame-control.pages.dev/feedback/). Please include:

- what you tried and what happened
- your computer's OS and your SteamOS build (Steam Settings → System)
- the server log: **Frame → Show Server Log** in the app

Issues and PRs opened directly on GitHub by new contributors are auto-closed
until a maintainer approves them; see [CONTRIBUTING.md](CONTRIBUTING.md).

## Going further

This repo also holds the scripts behind the app and field notes on how the
Frame's software fits together, all checked against a real headset and labelled
**verified** or **inferred**.

| | |
|---|---|
| [Frame Control in detail](docs/frame-control.md) | Every feature, how it works, per-platform notes, building |
| [Scripts and headset setup](docs/scripts.md) | The command-line helpers, minimum typing, streaming options, floating panels |
| [How the Frame works](docs/how-the-frame-works.md) | SteamVR → gamescope → Plasma, verified facts, debugging |
| [Android apps (Lepton)](docs/apks.md) | Sideloading, the rated F-Droid catalogue, per-app instances |
| [Import from my Quest](docs/quest-import.md) | Copy games you own off your Quest by USB; what runs, and why licence-checked games may not |
| [My Meta library](docs/meta-library.md) | Experimental: install and test Quest games your Meta account owns, with your own token |
| [Sideloading Linux and Windows games](docs/sideloading.md) | A .zip, folder or .exe as a Steam Devkit Game, runtime detection |
| [Install links for websites](docs/web-install.md) | `frame-control://install` links and manifests, the rules, a button to paste |
| [VR comfort and HUD](docs/vr-utilities.md) · [Steam games](docs/steam-games.md) · [VR video](docs/vr-video.md) · [WebXR in Chromium](docs/webxr-chromium.md) | Installing and buying, watching VR180/360, the Chromium build |
| [Mac in the headset](docs/mac-in-headset.md) | Mac windows and screens as panels in the Frame, with laser and keyboard input |
| [VR mods and custom songs](docs/mods.md) | Per-game feasibility, real-Frame results and blockers; no installer yet |
| [SSH](docs/ssh.md) · [Streaming](docs/streaming.md) · [Files](docs/file-transfer.md) · [Panels](docs/panels.md) · [Tailscale](docs/tailscale.md) | Topic notes |
| [Frame Control for iPhone](docs/iphone.md) | The iPhone and iPad app, how it runs the server on the Frame, pairing |
| [Recovery and OS images](docs/recovery-and-images.md) | Where to download the Frame's OS, what's inside, testing without the headset |
| [AI agents and assistant](docs/agents.md) | Key-free MCP tools, human approvals, and an opt-in assistant panel |
| [Testing](docs/testing.md) | Unit tests, end-to-end tests against a fake Frame in Docker, and the headset smoke test |
| [Open questions](docs/open-questions.md) | What's still unchecked |

<details>
<summary><b>Security notes</b></summary>

- With Developer Mode on, `sshd`, ADB and xrdp are all reachable on your LAN.
  Each running Lepton (Android) instance opens its own ADB port in 5555–5599,
  listening on `0.0.0.0` rather than only loopback. This was seen on the
  device on 2026-09-25, so anyone on the network can reach it. Use trusted
  networks only, and turn Developer Mode off when you don't need it.
- Frame Control reaches ADB and the Steam client's DevTools port (Frame
  loopback `127.0.0.1:8080`) only through SSH tunnels. The compatibility
  database key (maintainer-only) is never written to the repo.
- `steamos` has `sudo`, protected by the same Developer Mode password. Once
  you've switched to key auth, a short password still protects `sudo` and
  RDP, so pick one that isn't trivially guessable.
- Don't port-forward 22, 3389, or 5555–5599 from your router. For remote access,
  use Tailscale: `scripts/tailscale-on-frame.sh` (no sudo). In its userspace mode
  **every** Frame port is reachable from your tailnet, including Steam's DevTools
  on loopback 8080; see [docs/tailscale.md](docs/tailscale.md).
</details>

## Development

```sh
python3 -m unittest discover -s tests   # server tests; no headset needed
scripts/e2e.sh                          # end-to-end against a fake Frame (Linux with Docker)
cd app && npm install && npm start      # run the app from the checkout
```

The server is Python stdlib only; the app is Electron. GitHub Actions runs the
tests on macOS, Windows and Linux, and a `v*` tag builds all three installers
into a draft release, which reaches users once published. See
[building](docs/frame-control.md#building) and [releasing](docs/releasing.md).

## License

[MIT](LICENSE). The apps also ship other people's software under its own
licence, notably KDE Connect (GPL) for the keyboard and trackpad; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Steam, Steam Frame and SteamVR are trademarks of Valve
Corporation. This project isn't affiliated with or endorsed by Valve.
