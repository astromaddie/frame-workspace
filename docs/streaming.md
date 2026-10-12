# Screen and desktop streaming

This covers three directions, plus input:

- **A. Frame → Mac**: see and control the headset from the Mac.
- **B. Mac → Frame**: use the Mac's desktop inside the headset.
- **C. iPhone → Frame**: mirror the phone inside the headset.
- **PC VR from Linux**: [feasibility and options](linux-vr-streaming.md),
  including Valve's streaming and USB support. No Linux host tested yet.
- **Input**: type and point in the Frame from the Mac or iPhone.
- **Live view and Control**: watch a panel flat and tap on it to use it.

The confidence labels are the same as in [ssh.md](ssh.md).

## A. See and control the Frame from the Mac

| Option | What you get | Confidence | Notes |
|---|---|---|---|
| **Steam Link (macOS app) → `frame`** | A remote view of the headset | **Confirmed (Frame)**: Valve says to "use Steam Link on iOS, Android, or desktop to view the headset remotely by connecting to 'frame'" ([debugging](https://partner.steamgames.com/doc/steamhardware/steamframe/debugging)) | Steam Link for macOS exists ([Tom's Guide](https://www.tomsguide.com/news/macbook-gaming-just-got-a-killer-upgrade-with-steam-link-heres-how-it-looks)). It's the lowest-effort option. Whether you get the VR view or a flat mirror, and whether input works, is unverified. |
| **RDP to xrdp** | A separate Linux (Xorg) desktop session as `steamos` | **Confirmed (Frame)** for the server ([debugging](https://partner.steamgames.com/doc/steamhardware/steamframe/debugging)); **Inferred** for the Mac client | On the Mac, use Microsoft **Windows App** (the old "Microsoft Remote Desktop") from the App Store. Add PC `frame.local` (or the IP), user `steamos`, and the Developer Mode password. Valve says Xorg is the default session. This is a *separate* X session, not a mirror of what's in the headset. It's good for running GUI apps and supports clipboard sync. |
| **ADB + scrcpy (Lepton only)** | A mirror of the Android container | **Guess** | `brew install scrcpy android-platform-tools`, then `adb connect frame.local:5555` while Lepton Development is running ([adb_lepton](https://partner.steamgames.com/doc/steamhardware/steamframe/adb_lepton)), then `scrcpy`. This only shows Android apps, not SteamOS. |
| VNC server on the Frame (krfb / wayvnc) | A mirror of the Plasma desktop | **Inferred (SteamOS)** | Deck users run krfb in Desktop Mode ([one.vg](https://one.vg/blog/remote-control-your-steam-deck)). On the Frame, the in-headset desktop is a virtual screen, and krfb isn't known to be preinstalled. RDP and Steam Link cover this case, so it's not recommended. |

**Recommendation for A:** start with Steam Link for macOS, because Valve
documents it. Use Windows App (RDP) when you want a proper Linux desktop on the
Mac with keyboard, mouse, and clipboard.

**Verified 2026-09-30** (Frame BUILD_ID 20260925.6191901, Windows 11 25H2,
Remote Desktop Connection): signing in to xrdp as `steamos` with the Developer
Mode password opens a Plasma (X11) desktop within about 6 seconds.

- xrdp has no NLA, so the client shows a certificate warning (xrdp's own
  `www.xrdp.org` certificate) and then xrdp's own login box. Frame Control
  fills in `steamos` there on Windows, Remmina and FreeRDP.
- The desktop is a separate login session (Xorg on display `:10`), not the
  headset's view. It uses about 1.3 GB of the Frame's memory.
- Closing the client leaves the session running, and the next login
  reconnects to it. To end it over SSH, find it with `loginctl list-sessions`
  and run `loginctl terminate-session <id>`. That doesn't touch the headset's
  gamescope or SteamVR session.

## B. Show the Mac's desktop inside the Frame

The Frame's VR streaming uses **SteamVR** on the host. Linux hosts had
VR-streaming problems at launch
([Steam discussion](https://steamcommunity.com/app/4165890/discussions/0/528765047224280796/),
[gbl08ma](https://gbl08ma.com/posts/steam-frame-a-linux-machine-doesnt-support-linux/)).
Valve's later 2.17.8 notes explicitly describe Steam Link fixes on Linux and
initial USB streaming support (**documented**, not tested from a Linux host
here). See [the current comparison](linux-vr-streaming.md#a-valves-own-path--recommended-first).
**macOS isn't a supported SteamVR host**, so for the Mac we're only looking at
flat 2D desktop streaming into a window on the Frame's Linux desktop.

| Option | Setup | Confidence | Verdict |
|---|---|---|---|
| **Frame Control → Tools → Mac in the headset** | Nothing to install. Allow Screen Recording and Accessibility for Frame Control, then press **Show** next to any window or screen | **Verified 2026-09-28** on the Frame (panel in 1.5 s, measured with `scripts/macview-bench.py`: about 10–20 ms from the Mac drawing a frame to the viewer drawing it); laser input not yet tried while wearing it | **Recommended.** Hardware H.264 over an SSH tunnel, adapting to the link. Each window becomes its own panel you can place anywhere. Laser clicks and scrolls, and the Mac's keyboard types. See [mac-in-headset.md](mac-in-headset.md) |
| **macOS Screen Sharing (VNC) → Remmina on the Frame** | **Mac:** System Settings → General → Sharing → Screen Sharing on → (i) → enable "VNC viewers may control screen with password". **Frame:** `./scripts/install-apps.sh remmina` from the Mac, then open Remmina in the headset and connect to `vnc://<mac>.local` | **Verified 2026-09-27** (Frame BUILD_ID 20260925.6191901, macOS 27.0), in its own panel via `panel-on-frame.sh mac-screen`. Remmina is on Flathub for **aarch64** with VNC and RDP ([Flathub](https://flathub.org/apps/org.remmina.Remmina)). The Frame desktop runs Flatpaks ([UploadVR](https://www.uploadvr.com/flatpaks-open-source-steam-frame/)). macOS VNC is built in. | **Fallback** (whole screens only). Nothing to install on the Mac, and it's easy to set up. Noticeable lag, even at lower Remmina quality settings on a good 5 GHz link, where neither Wi-Fi nor the Frame's CPU was the bottleneck. Usable for reading and coding, but not for games. You'll type the Mac's hostname once in Remmina on the headset, then save the profile. To avoid even that, the script can pre-seed a Remmina profile over SSH (see below). |
| Sunshine (Mac) → Moonlight (Frame Flatpak) | `brew install` Sunshine on the Mac, then `./scripts/install-apps.sh moonlight` | Moonlight Flatpak supports **aarch64** ([Flathub](https://flathub.org/apps/com.moonlight_stream.Moonlight)). **Sunshine on macOS is poorly supported**: install problems on Apple Silicon/Sequoia, and no virtual gamepads ([LizardByte discussion #777](https://github.com/orgs/LizardByte/discussions/777)). | Try it if VNC is too laggy. Expect some friction. |
| Steam Remote Play with the Mac as host | Steam on the Mac, Steam Link/Remote Play on the Frame | macOS-hosted Remote Play is reported broken or flaky in 2024–2026 ([Steam discussion](https://steamcommunity.com/groups/homestream/discussions/1/574921459914429988/)) | Not recommended. It's only for games, if it works at all. |
| Immersed / Virtual Desktop | Vendor apps | Immersed has a Mac agent but no known Frame client. Virtual Desktop's developer said he'd "try" to port it ([NewsBreak](https://www.newsbreak.com/news/4892834783961-virtual-desktop-dev-says-he-ll-try-to-bring-the-app-to-steam-frame)). | Not available as of 2026-09-25. Check again later. |
| WiVRn / ALVR | VR streaming from a Linux or Windows PC | Irrelevant for a Mac host (no SteamVR/OpenXR runtime on macOS) | N/A |

For **local movies and stereo photos**, Frame Control's own OpenVR player
runs on the Frame; see [vr-video.md](vr-video.md). It currently renders a flat
stereo screen. VR180/360 projection is not implemented; the same page records
DeoVR only as an optional, independently installed alternative.

### Pre-seeding the Remmina profile (no typing in the headset)

`scripts/install-apps.sh remmina --vnc-host <your-mac>.local` writes
`~/.var/app/org.remmina.Remmina/data/remmina/mac-screen-sharing.remmina` on the Frame over
SSH. The profile then appears in Remmina's list, and you just click it. It
scales the Mac's desktop to fit the window (`scale=1`, `viewmode=1`). Without
that, Remmina shows a Retina Mac's native pixels 1:1, so you see a zoomed-in
corner. (Verified 2026-09-27.)

**Expect a Mac login prompt, not the VNC password.** macOS offers Apple's own
authentication (RFB security type 30) ahead of plain VNC auth (type 2), and
Remmina picks it. So Remmina asks for your **Mac account name and login
password**; the "VNC viewers may control screen" password isn't used. To store
the password without typing it in the headset, run on the Frame:

```sh
printf '%s' "$PASSWORD" | flatpak run org.remmina.Remmina \
  --update-profile ~/.var/app/org.remmina.Remmina/data/remmina/mac-screen-sharing.remmina \
  --set-option password
```

Remmina encrypts it into the profile with its own key, because there's no
secret service in the SSH session. (Verified 2026-09-27.)

### The Mac's cursor

The mirror doesn't show the Mac's pointer, with either `showcursor` value.
macOS keeps the pointer out of the picture it sends, and Remmina's cursor mode
draws the cursor shape only at the Frame's own pointer, which doesn't follow
the Mac trackpad. `scripts/mac-cursor-ring.lua` works around this: a
[Hammerspoon](https://www.hammerspoon.org/) script that draws a ring around the
Mac pointer as a real window, so it's part of the mirrored picture. Setup is in
its header. (Verified 2026-09-27.)

Going the other way, pointing a controller at the panel moves the Mac's mouse,
because Remmina forwards input (`viewonly=0`).

## First-party options, and why they do or don't fit

Checked 2026-09-28. The first-party way is usually the best one, so these are
listed first; the sections above and below explain the alternatives.

| Goal | First-party option | Fits? | Why, and what would make it easier |
|---|---|---|---|
| Type and point from the **iPhone** | **KDE Connect** (KDE; official [iOS app](https://apps.apple.com/app/kde-connect/id1580245991)) remote touchpad and keyboard, plus clipboard and files | **Best candidate, untested on the Frame** | The Frame doesn't have it (verified: no `kdeconnectd`), it isn't on Flathub, and the root is read-only, so it would have to run from `~` or a container. On Wayland it types through KWin, so it can only reach the desktop panel, not SteamVR or games (**inferred**). Steam Deck users report its remote input breaking after SteamOS updates ([SteamOS #1939](https://github.com/ValveSoftware/SteamOS/issues/1939)). If it works, Frame Control could install it and pair it for you. |
| Type and point from the **Mac** | KDE Connect for macOS (KDE builds) | Same as above | Its Mac app sends clipboard and files but has no keyboard/mouse sharing (**inferred**). |
| Either | **Bluetooth keyboard and mouse** paired in SteamOS (Valve) | Yes, with real hardware | Neither device can pretend to be one: iOS refuses the HID service ([Apple forums](https://developer.apple.com/forums/thread/733916)), and macOS has no built-in way. |
| Either | **xrdp** in Developer Mode (Valve) | No | Input goes into a *separate* desktop shown on the Mac, not into what you see in the headset. |
| **Mac screen** in the Frame | **Screen Sharing** (Apple's VNC server) + Remmina (already installed on this Frame, profile pre-seeded by `install-apps.sh`) | **Yes, closest to first-party** | Only the Mac side is first-party; Remmina is the client. Turn on System Settings → General → Sharing → Screen Sharing → (i) → "VNC viewers may control screen with password". Still to test in the headset (open question 11). |
| Mac screen | **Steam Remote Play** with the Mac as host (Valve) | Probably not | macOS isn't a SteamVR host, and Mac-hosted Remote Play is reported broken ([Steam forum](https://steamcommunity.com/groups/homestream/discussions/1/574921459914429988/)). One quick test is worth doing: Steam on the Mac, then Remote Play from the Frame's Steam. |
| Mac or **iPhone screen** | **AirPlay** (Apple) | Not officially | It's Apple's own mirroring for both, but Apple only licenses receivers to TV and speaker makers; nothing official runs on Linux. UxPlay (below) is the unofficial receiver. |

## C. Show the iPhone's screen inside the Frame

iOS only shares its screen two ways: **AirPlay** (Screen Mirroring in Control
Centre) or a **ReplayKit broadcast extension** in an app. Nothing else can
capture it.

| Option | What it takes | Confidence | Verdict |
|---|---|---|---|
| **UxPlay** (an open-source AirPlay receiver) on the Frame | Build it for aarch64 (no Flathub package; there's a Snap and distro packages), run it in `~` or a podman container, and advertise it over mDNS. The iPhone *and* the Mac then see "Frame" in Screen Mirroring, with nothing to install on either | **Inferred.** It runs on ARM64 Linux such as the Raspberry Pi ([UxPlay](https://github.com/FDH2/UxPlay)). Not tried on the Frame: needs mDNS registration and its ports (7000, 7001, 7100 and a UDP range) reachable | **Recommended to try first.** It's the only receiver-side option, and it covers the Mac too. The window shows in the Frame's Linux desktop panel |
| A broadcast extension in Frame Control | ReplayKit sends the screen to a small extension (50 MB memory limit), which encodes H.264 and sends it through the app's SSH tunnel to the page, shown the same way as the Frame's live view in reverse | **Inferred** from Apple's ReplayKit docs | Full control and no network setup, but several days' work, and the picture only shows where Frame Control's page is open in the headset |

## Input: type and point in the Frame from the Mac or iPhone

**Built: Home → Keyboard and trackpad**, in every version of Frame Control
(Mac, Windows, Linux, iPhone and iPad), with nothing to install on the device
you're holding. On a phone the panel is a trackpad (drag to move, tap to click,
two fingers to scroll, two-finger tap to right-click) plus a text field that
types on the Frame. On a computer, clicking the pad passes your mouse and
keyboard through to the Frame until you press Esc (⌘ is sent as Ctrl on a Mac).

It goes through **KDE Connect**, the first-party route (KDE makes the Frame's
desktop): Frame Control's server runs [`ui/frame_input_agent.py`](../ui/frame_input_agent.py)
on the Frame, which talks KDE Connect's own LAN protocol to the Frame's
`kdeconnectd` as if it were a phone. KDE Connect does the typing and clicking.

**Verified 2026-09-28** (SteamOS 0.4.1, build 20260925.6191901):

- KDE Connect isn't installed on the Frame, so **Frame Control ships it**:
  Valve's own build for the Frame (`kdeconnect` 24.02.2-1 from its `extra`
  repository) plus the five libraries it links that the Frame lacks
  (`kcontacts`, `kpeople`, `modemmanager-qt`, `pulseaudio-qt`, `libfakekey`),
  pinned by SHA-256 in [`frame/kdeconnect/packages.json`](../frame/kdeconnect/packages.json).
  The builds download them from the
  [kdeconnect-frame-24.02.2-1 release](https://github.com/saphid/frame-control/releases/tag/kdeconnect-frame-24.02.2-1)
  (`app/build/fetch-deps.js`, `frame/kdeconnect/fetch.py`). The address of
  Valve's repository for the Frame isn't to be shared, and Valve's public
  aarch64 preview repository has KDE Connect 25.08, built against newer KDE
  libraries than the Frame has.
- On first use, the computer copies them to the Frame over the SSH connection
  it already has. The iPhone app's bundle, already copied to the Frame, has
  them too. The agent checks each SHA-256 and unpacks them into
  `~/.local/share/frame-control/kdeconnect` (3.6 MB copied, 18 MB unpacked,
  about 2 s). There's no internet download on the Frame, no root, and nothing
  on the read-only system, so SteamOS updates leave it alone. A stamp there
  (`root/.frame-control-packages`) records which build it is; a newer Frame
  Control replaces it.
- `pacman -Sp kdeconnect …` also pulls in ModemManager, libqmi, libmbim,
  libqrtr-glib and ppp (packaging dependencies). `kdeconnectd` and its plugins
  don't link any of them (checked with `ldd` against the six packages alone),
  so Frame Control leaves them out.
- Licences: the packages are GPL and LGPL; Frame Control stays MIT because it
  only starts `kdeconnectd` and speaks its protocol. The notice, licence texts
  and complete source are in [`frame/kdeconnect`](../frame/kdeconnect/NOTICE.md),
  [`THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md) and the app's
  **About and licences** (Tools).
- It pairs by itself: the agent asks to pair and accepts on KDE Connect's side
  over D-Bus (`qdbus6 … acceptPairing`). It keeps its identity in
  `…/kdeconnect/bridge`, so later connections are already paired. (A pair
  request to a device that's already paired makes KDE Connect unpair it, so
  the agent only asks when it isn't paired.)
- Protocol version 7: whoever opens the TCP connection sends its identity line
  in plain text, then acts as the **TLS server** (KDE Connect's
  `lanlinkprovider.cpp`). Remote input is `kdeconnect.mousepad.request` with
  `dx`/`dy`, `singleclick`, `rightclick`, `singlehold`/`singlerelease`,
  `scroll`, `key` (any text) or `specialKey` (1 Backspace … 14 Escape,
  21–32 F1–F12) and modifier flags.
- **KDE Connect runs only while something uses the keyboard and trackpad.**
  Each device gets its own KDE Connect identity (KDE Connect keeps one
  connection per device, so a shared one would make a phone and a computer
  knock each other off). The last one to disconnect stops KDE Connect, so it
  isn't left running, or discoverable on your network, afterwards.
- KDE Connect 24.02 **hangs or crashes when asked to unpair a device that's
  offline** (seen twice: once spinning at 100% CPU with D-Bus unresponsive,
  once exiting). Frame Control never unpairs. If its copy stops answering, the
  agent restarts it once (tested by freezing it with `kill -STOP`).
- Moves from the iPhone app (Simulator) and the Mac's server moved the Frame's
  X pointer by exactly the amount sent, including with the bundled packages
  copied over SSH (2026-09-28).
- gamescope runs **two Xwayland displays**. `:0` holds Steam's VR bar and menus
  and ignores injected pointer motion; `:1` holds apps such as Chromium and
  takes it. KDE Connect runs on `:1`, so it reaches apps, not Steam's own menus.
  There's also a `gamescope-0-ei` (libei) socket.
- Typing through KDE Connect lands in a Chromium panel on `:1` (seen in the
  panel's own capture, 2026-09-29). It **can't reach panels on `:0`** (Frame
  Control's own panels, Steam's UI) and, since XTest positions are clamped to
  `:1`'s 1280×720 root, can't reach beyond that in a bigger window. Control on
  the live view (below) has neither limit.
- **Not yet tested:** whether it reaches the KDE desktop panel (Plasma is its
  own session).
- **Known limit:** keys and clicks typed while the link is reconnecting wait
  and are sent once it's back, but anything sent in the moment the Wi-Fi
  drops, before SSH notices, can be lost. Confirming every event would add a
  round trip to each pointer move.

## Live view and Control: watch a panel and tap on it

**Built: Home → Desktop / Headset view → Control.** The live view has two
sources:

- **Headset view**: what the lenses show (SteamVR's mirror, `/dev/video99`). It
  moves with the wearer's head, so Control makes the view a trackpad: drag to
  move the pointer, tap to click, press and hold to right-click, two fingers to
  scroll. With a mouse, moving over the view moves the pointer.
- **Desktop**: the app panel in use in the headset, from its own window, so it
  stays still however the wearer looks around. Control makes taps and clicks
  land exactly where you put them. Dragging is a mouse drag, press and hold is a
  right-click, two fingers scroll, and on a computer the mouse, wheel and
  keyboard work directly on it (⌘ is sent as Ctrl on a Mac). A picker shows any
  other panel, view only.

Below the view, a text field and key buttons type on the Frame from a phone.

How (**verified 2026-09-29**, SteamOS 0.4.1, build 20260925.6191901):

- **Input goes through gamescope's own injection.** gamescope serves an EIS
  socket (`/run/user/1000/gamescope-0-ei`; Steam feeds Remote Play input through
  it), and `libei` 1.4.1 is on the image. [`ui/frame_touch.py`](../ui/frame_touch.py)
  talks to it with `ctypes`: nothing to install. gamescope offers one device,
  "Gamescope Virtual Input", with relative and absolute pointer, buttons,
  scroll and keyboard (Linux key codes; no text capability, so the text field
  types printable ASCII on a US layout). Its absolute region is unbounded; the
  pointer uses the focused panel's display coordinates, and gamescope fits each
  window to its display, so a 1920×1080 window on the 1280×720 `:1` takes
  positions at two thirds scale. Taps on a 1280×720 page landed on the exact
  pixel.
- **It reaches the panel that has focus** (`GAMESCOPE_FOCUSED_WINDOW` on `:0`'s
  root), on either X display. In the OpenVR backend focus moves only on SteamVR
  overlay events (the controller's laser entering or clicking a panel), or to a
  new panel when none holds it (read from gamescope's `OpenVRBackend.cpp`, seen
  with `gamescopectl focus_info`, which writes to the journal). Neither
  `GAMESCOPECTRL_BASELAYER_WINDOW`/`_APPID` nor X focus moves it, and no
  gamescope command does. So Control follows the wearer: whatever they last
  used is what your taps reach. A window without a Steam app id (`STEAM_GAME`)
  gets a connector of its own and doesn't hold focus.
- **Keys in a burst can arrive out of order**, so the helper paces them (8 ms
  apart).
- **Known limit:** if focus moves to another panel in the middle of a drag, the
  release goes to the panel that has focus then. Whether gamescope hands it to
  the window that got the press isn't known yet. When the session ends, the
  helper lets go of every button and key it still holds.
- **The Desktop picture is the window's own pixels**: `ffmpeg -f x11grab
  -window_id <window> -i :<display>` works on gamescope's redirected windows,
  while grabbing the root gives black. It streams as H.264 like the headset view
  (about 30 fps at 720p).
- **When no panel is found**, `frame_touch.py panels` says why (`"why"`), and the
  Desktop view shows that instead of a bare "no app panel": X tools missing,
  gamescope listing windows that aren't X11 windows on any display, the desktop
  running but not listed, or nothing open. It reads gamescope's properties from
  `:0` first (one `xprop` call) and probes the other displays, 2 s each, only if `:0`
  has none. A whole run stops after 12 s, inside the server's 20 s, and says the headset
  was too slow. Added for
  [#73](https://github.com/saphid/frame-control/issues/73) (SteamOS 0.4.5, desktop
  open but not found). The likely cause there was the page itself: a second
  `loadPanels` (the panel switcher's) replaced the Desktop view's, fixed in #84.
- Tested from the iPhone app (Simulator): a tap on the Desktop view focused a
  text box in the panel and the text field typed into it; a trackpad move went
  exactly (+40, +25).

Our own `uinput` keyboard and mouse would also work (`steamos` is in the
`input` group and `/dev/uinput` is group-writable, verified 2026-09-27), and
remains the fallback if the bundled KDE Connect ever stops working on a new SteamOS.

| Other option | Mac | iPhone | Why not |
|---|---|---|---|
| **Bluetooth keyboard and mouse** | – | – | Needs real hardware, paired in SteamOS settings. The iPhone can't pretend to be a Bluetooth keyboard: iOS won't advertise the HID service ([Apple forums](https://developer.apple.com/forums/thread/733916)) |
| **Deskflow** (formerly Input Leap / Barrier) | ✓ | – | Moves the Mac's own mouse and keyboard onto the Frame's screen edge. Flathub has an aarch64 build ([Flathub](https://flathub.org/apps/org.deskflow.deskflow)); on Wayland it needs the InputCapture/libei portal, and only works while Plasma is running. No iPhone client |
| **Remmina / Steam Link / RDP** | ✓ | – | Input only reaches the streamed session, not the headset's own apps |

Other ways to get text in:

- **Clipboard from the Mac**: `scripts/paste-to-frame.sh`, or Frame Control's
  clipboard box (see [file-transfer.md](file-transfer.md#clipboard)). Needs
  the desktop panel open.
- **RDP session**: Windows App syncs the clipboard with xrdp, but only inside
  that RDP session.
