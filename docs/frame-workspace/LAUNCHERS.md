# Frame Workspace launchers

Frame Workspace has a separate Mac server app and a Steam library launcher on
the Frame. The Mac app opens the existing **Mac in the headset** controls.

## Daily use

1. Open **Frame Workspace** from Applications, Spotlight, or the Dock on the Mac.
2. Open **Frame Workspace** from the Steam library on the Frame. It opens the
   Workspace dashboard and requests the last saved workspace.
3. Use **Stop Workspace** in the companion or the headset dashboard to close the
   monitor. Quit the Mac app to stop its server and close its owned viewers.

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

The Mac bundle uses `com.astromaddie.frame-workspace` and keeps its state in
`~/Library/Application Support/Frame Workspace`, cache in
`~/Library/Caches/Frame Workspace`, and server log in
`~/Library/Logs/Frame Workspace/server.log`. On first launch it copies only missing
Workspace settings from `Frame Workspace Preview`; it keeps the originals.
It uses native viewing, V4L2 decoding, CPU conversion, and a 30 fps stream target.
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

The Mac app is installed and its bundled server connects to the Frame. Both
launcher scripts are installed on the Frame. Steam library registration and a
live monitor launch are pending an idle headset: the device left standby during
the installation preflight, before Steam was restarted. No streaming readiness
claim is made by this installation record.

## Original icon proposals

The [icon preview](launcher-design/concepts.png) contains three original
directions: Spatial windows, Open volume, and Spatial W. Their vector sources
and 1024 px PNG exports are in `launcher-design/`. Open volume is the recommended
direction for a spatial computing identity. Selection is pending; the installed
Mac build still has the inherited icon. The earlier icon proposal has been
superseded. The companion interface continues to use its existing controls.

To regenerate the icon preview and PNG exports:

```sh
python3 docs/frame-workspace/launcher-design/make-concepts.py
env -u ELECTRON_RUN_AS_NODE app/node_modules/.bin/electron \
  docs/frame-workspace/launcher-design/render-concepts.cjs
```
