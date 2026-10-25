# Frame Workspace launchers

Frame Workspace has a separate Mac server app and a Steam library launcher on
the Frame. The Mac app opens the existing **Mac in the headset** controls.

## Daily use

1. Open **Frame Workspace** from Applications, Spotlight, or the Dock on the Mac.
   Allow its Screen Recording and Accessibility requests when macOS asks.
2. Open **Frame Workspace** from the Steam library on the Frame. It opens the
   Workspace dashboard and requests the last saved workspace.
3. In the headset, press the Steam button, open **Frame Workspace** from the
   SteamVR bar, then choose **Stop** on its Workspace card. This closes the
   Workspace-owned Frame viewers and returns to SteamVR when available; the Mac
   may finish restoring its windows and display after the local viewer closes.
   Stop is also available in the companion. Quit the Mac app to stop its server
   and close its owned viewers.

Closing the Mac window keeps the server available. Click the Dock icon to reopen
the controls. Saved Desktop/Spatial mode, size, scale, quality, and session
settings are retained. With Spatial mode selected, the launcher opens the saved
spatial workspace; select Desktop in the companion for a single monitor.

The Frame entry is a launch action: the streaming session is owned by the Mac
server. Opening the entry again asks the existing workspace to show itself again.
If the Mac is disconnected, the dashboard shows its existing connection message.
The launcher waits for a fresh companion update before sending a short-lived
request, so launching while offline does not queue a later display startup.

## Build and installation

On Apple Silicon macOS 14 or later:

```sh
cd app
npm ci
npm run dist:workspace:dir
```

Copy `app/dist/mac-arm64/Frame Workspace.app` to `/Applications`. The bundle
includes Python, the Swift Mac capture agent, the existing companion UI, and
its required helpers. macOS may request Screen Recording and Accessibility
permission when the capture controls are first used.

Install `scripts/workspace-launch-on-frame.py` and
`scripts/workspace-dash-on-frame.sh` together in
`~/.local/share/frame-workspace/launcher/` on the Frame and make them executable.
The Steam shortcut runs `workspace-launch-on-frame.py` directly as a VR entry.
It uses the native viewer and dashboard already installed in
`~/.local/share/frame-workspace/native/`.
Install `scripts/workspace-exit-on-frame.py` beside that dashboard with mode
`700` for local Stop while disconnected. The Mac companion also installs this
helper when it connects.

The Mac bundle uses `com.astromaddie.frame-workspace` and keeps its state in
`~/Library/Application Support/Frame Workspace`, cache in
`~/Library/Caches/Frame Workspace`, and server log in
`~/Library/Logs/Frame Workspace/server.log`. On first launch it copies only missing
Workspace settings from `Frame Workspace Preview`; it keeps the originals.
It uses native viewing, CPU conversion, and a 30 fps stream target. The desktop
monitor uses V4L2 decoding; independent window and Spatial menu/status panels
default to software decoding (`FRAME_WINDOW_DECODER`).
Upstream updates, telemetry, and Frame Control install-link registration are
disabled for this profile.

## Verification

The launcher tests exercise the production Python launcher and shell dashboard
consumer, including fresh connection updates, offline startup, concurrent and
repeated launches, and expired requests. Profile tests cover separate identities,
default settings, packaging requirements, and migration without overwriting data.

```sh
node --test app/test/*.test.js
python3 -m unittest discover -s tests -p 'test_workspace_launcher.py'
python3 -m unittest discover -s tests -p 'test_workspace_profiles.py'
node app/build/check-resources.js "/Applications/Frame Workspace.app/Contents/Resources" --workspace
```

Local package and device results are recorded in `launcher-validation.json`.

The Mac app and the Frame's Steam library entry are installed with the original
artwork. A launch from Steam reached the companion and started the saved Spatial
workspace. That run exposed hardware decoder errors for individual panels;
window panels now select software decoding before startup.

The installed Mac app opens from Finder. A Steam library launch opened a real
2880 × 2560 Desktop monitor; the Frame decoded its Mac capture and submitted the
texture. Stop through the installed dashboard closed that viewer, ended the Mac
stream, removed the virtual display, and restored Start. Desktop is selected
for the next single-monitor launch. The independent Spatial-window software
decoder policy still needs its own live run.

macOS can retain permission entries for an older signed app even when their
switches are on. If the app still reports denied access after granting it,
remove its stale entries from Screen & System Audio Recording and Accessibility,
then add the exact `/Applications/Frame Workspace.app` again and allow macOS to
quit and reopen it. This restored both grants for the installed app.

## Original artwork

The original **Open volume** icon has one centered floating window inside an
open room outline. The [SVG source](../../app/build/workspace/icon.svg),
[1024 px PNG](../../app/build/workspace/icon.png), macOS icon set, and matching
Steam library artwork are in `app/build/workspace/`. The geometry was created
for Workspace. Frame Control retains its own icon and packaging profile.

The asset manifest records SHA-256 hashes for the exports. Regenerate them on
macOS with:

```sh
cd app
npm run icon:workspace
```
