# Spatial Mode spike (FW-15)

Labels as elsewhere: **verified** (run here, with date and versions) or **inferred**.

## Making it start reliably (stage 1, 2026-10-26)

Fixes for the first installed launch ([SPATIAL-AUDIT.md](SPATIAL-AUDIT.md)). Checked locally
(macOS build of the host, fake Frame for the companion); not yet run on the device.

- **Idle menus no longer block windows.** A Menu Bar, Status or menu panel counts as
  registered once its stream is connected and its first `surface` message has arrived, with
  no picture (host state `registered: true`, `state: "idle"`). Windows still need a submitted
  texture. Reconciliation launches window panels first, focused one first, and the auxiliary
  panels one per job once no window is waiting; a failing one retries after 5 s without
  holding anything up. Menu Bar then adopts the wall the windows already share.
- **Headset clicks have their own lane.** Shelf open/hide/quit, the switcher and focus run on
  an action queue, not behind Spatial startup. Failures are kept: the bar panel shows the
  latest one on its Workspace card for 20 s, and the Mac page lists recent problems. Opening
  an app that already has a panel also chooses that panel (`POST /switcher?verb=choose&key=`),
  so the host brings it forward. A Stop from the Mac page cancels startup and panel launches
  at their next stage.
- **The laptop goes dark only once the headset shows something.** The helper makes the cover
  at start but keeps it off screen until the companion calls `POST /spatial/cover?show=1`,
  after the first window panel has registered. It comes down when the last window panel goes
  or Spatial Mode stops. Cmd-Tab reaches macOS while no window has a panel.
- **The viewer must match.** Before Spatial Mode changes anything on the Mac, the companion
  asks the Frame's viewer for `--capabilities` and refuses to start if roles, surfaces, wall
  slots or idle auxiliary panels are missing. A capable viewer built from other sources still
  starts; the Mac page offers **Update Headset Viewer** (see [NATIVE.md](NATIVE.md)).

## The shelf: one row, and what a click does (2026-10-26)

After the first usable run: "it felt too tall ... just a big gray slab" and "clicking an app
on the dock didn't seem to do anything". Checked locally on macOS (native self-tests and
host integration, companion and helper tests, offline renders in `native/evidence/shelf-*.png`);
**not yet run on the device or worn**.

- **One row.** The Applications button, the app icons, a groove, then the status items (clock,
  Wi-Fi, battery, Control Center) at the right end. The backing hugs the icons (about 103 mm
  tall at full size, down from 167 mm); magnified icons rise past its top like the macOS Dock.
- **A click shows at once.** The icon shrinks while the trigger is held. An app that already has
  a panel has its most recently used panel brought in front of the user, as the switcher does,
  and its focus commits. An app with no panel yet hops until its panel appears or an error comes
  back (the amber notice also shows it); it gives up after 20 s, or 12 s once the Mac has opened
  it.
- **Where a click goes, and what each hop logs.** Every shelf request carries an id
  (`d<pid>-<n>`); grep it, or `dock:`, at each place:

  | Hop | Where | Look for |
  |---|---|---|
  | Headset host | the Frame's journal: `journalctl --user -b \| grep 'dock:'` | `dock: press`, `dock: click ... id=...`, `dock: ack id=... ok=...`, `dock: launch ended ...`, `dock: request not written` |
  | Request poller | the same journal, tag `frame-workspace` | `dock: poller picked up spatial-<pid>-<n>.json {...}`, `dock: poller forwarded ...` |
  | Companion | `~/Library/Logs/Frame Workspace/server.log` | `dock: companion received id=...`, `companion rejected id=...: <reason>`, `companion queued id=...`, `companion asking the helper`, `companion helper answered` / `refused`, `companion request poll failed` |
  | Mac helper | `~/Library/Logs/Frame Workspace/mac-helper.log` | `dock: action received id=... verb=... bundle=...`, `resolved <path> running=...`, `openApplication ok pid=... active=...` / `failed: ...`, `answered 200 ...` |

  The first line that is missing is where the click died. The helper's log used to be
  `~/Library/Logs/Frame Control/mac-helper.log`, truncated by every helper start; it now sits
  beside `server.log`, adds to the file under a header per helper start, and rolls over at 4 MB.
- **Found while tracing the path** (all fixed, each with a test): the poll for headset requests
  failed as a whole when the window list made the sync state too big (long titles written as
  escapes), so no click got through and nothing was logged; a panel the companion chose before
  its picture or the head pose was ready was consumed and lost; a request the host could not
  write, or one the companion dropped (Spatial Mode not running, a bundle not on the shelf), was
  silent; and `show_windows` used `NSRunningApplication.activate`, which macOS 14+ may refuse
  from a background process, where `open` goes through Launch Services.

To check it on the device: wear it, point at the shelf (it grows), click an app that has a
window: that window comes in front of you and gets the cursor. Click an app that is not
running: its icon hops until the window appears; quit it from the Mac and click it again with
the Mac asleep or the app missing to see the amber notice and the icon stop. After any click that
did nothing, read the four logs above for its id.

## Focus: eyes highlight, an action commits (2026-10-26)

After the first usable run (the cursor in several windows at once, no telling which window was
active, the typed-in window sometimes at the background rate). Checked locally on macOS (native
self-tests and host integration with a test eye ray, helper unit tests, companion tests, offline
chrome renders); **not yet run on the device or worn**.

- **Eyes only highlight.** A look lights the window's border, soft and pale, after a quarter
  second. Nothing moves on the Mac and the live set doesn't change.
- **An action makes a window active:** the laser moved onto it or its trigger pressed; a
  trackpad click while it's highlighted (the helper swallows that click, focuses the window and
  puts its cursor back); the switcher; or a 1.2 s stare. As a stare goes on, the soft glow
  gathers towards the active line, then snaps into it. No stare commits while the user typed in
  the last second, holds a button, points with the controller, has a menu, the launcher or the
  switcher open, or moves a window; it starts from zero afterwards. Typing never moves focus.
- **One cursor.** Only the active window's capture draws the Mac cursor; the menu bar strip and
  menus draw it while it's on them.
- **Clear active window.** A steady, crisp #1a9fff line round the active window; the others are
  dimmed to 85% (not under the launcher's own dim).
- **Full rate where you type.** The active window is first in the live set, stays live even
  before its size is known, and the helper makes it live at once when focus moves.

Protocol additions are in [spatial-shared-contract.md](spatial-shared-contract.md) ("Gaze
highlight and typing"); the host side is in [native/README.md](../../native/README.md).

To check it on the device: wear it with two overlapping windows. Look at the inactive one: its
border goes pale, nothing else changes, and typing still goes to the active one. Click the
trackpad while looking at it: that click only switches, the cursor appears where it last was in
that window, and only there. Type a little, then stare at another window for two seconds: it
must not switch until a second after you stop typing plus the stare. Scroll a video in the active
window and look elsewhere: it keeps its full frame rate.

## Window layout and a faster start (2026-10-26)

After the first usable run: windows appeared slowly one by one, it wasn't clear
where they were, and they stacked instead of tiling across the view. On the
Frame, saved pose files in `~/.local/share/frame-workspace/panels/w<ID>-pose.json`
won over the wall. Many were left by automated runs on earlier days under Mac
window IDs the user's real windows still had, so two windows came up at the same
pose about 100 degrees to the left, one about 3 m up. Panels launched one at a
time, each taking seconds. Checked locally (macOS host build, fake Frame for the
companion and launcher); not yet run on the device.

- **No stacking.** Windows without a position their user set get a free wall
  cell: never one another panel holds or a hand-placed panel covers. The L2 wall
  grows a top row and far columns, then rings further out. Panels share one
  scale from their Mac size in points, so text reads the same everywhere.
  Details in [native/README.md](../../native/README.md#spatial-wall-l2).
- **Only positions the user set are remembered.** A pose is saved only when the
  user moves, resizes, curves, recenters or arranges a panel, and records that
  (`userSet`) with the window's bundle ID and title. Every older pose file is
  ignored and moved into a dated `set-aside-YYYY-MM-DD` folder beside it on first
  run. Last Session and Profile Save keep only user-set window poses; a Last
  Session snapshot from before this puts its windows on the wall.
- **Arrange Windows**, on the bar's Workspace card and the Mac page in Spatial
  Mode, re-tiles every window panel in front of the user (focused one in the
  centre) and keeps that as the user's layout.
- **Fast start.** Window panels launch four at a time, focused first
  (`FRAME_SPATIAL_LAUNCH_PARALLEL`, 1–8); Menu Bar, Status and the menu slots
  still wait for the windows. The Frame launcher no longer holds the host lock
  while a panel waits for its picture. Every waiting window shows a placeholder
  straight away at its wall slot: the app icon and title on the Palladium face.
  The picture replaces it when it arrives. A failed launch shows "Couldn't open
  this window. Retrying…" on its placeholder until the retry 5 s later; Close on
  a placeholder stops that window opening.
- **The viewer must have it.** Spatial Mode now also asks for the `window-layout`
  capability, so an older headset viewer is updated first rather than stacking
  windows at stale poses.

Repeat the local checks: build as in [native/README.md](../../native/README.md)
(on macOS with a `gl.pc` giving `-framework OpenGL`), then
`python3 native/tests_host.py BUILD/frame-workspace-native`,
`PYTHONPATH=tests python3 -m unittest test_macview test_spatial_apps test_native_pose_launcher test_native_update test_workspace_profiles`
and `node tests/page/spatial_mode.mjs`. Renders: `native/evidence/spatial-wall-6.png`,
`-9`, `-12`, `-opening` and `-occupied`.

On the headset, still to check: a fresh Spatial start with 6 and with 12+ windows
(placeholders at once, no two panels overlapping, all in front), that the old
pose files went to `set-aside-2026-10-26/`, a window moved by hand coming back
there next session, a failed window's error placeholder, and Arrange Windows
from the bar and from the Mac page.

## Launcher A: the Applications grid (2026-10-26)

The approved launcher (FW-15 decision, `evidence/launcher-mockup.png` option A). Checked
locally on macOS (helper unit tests, companion tests with a fake Frame, native self-tests and
host integration, offline renders); **not yet run on the device or worn**.

- **Opening it.** The shelf gets an Applications button at its left end, magnified like the
  icons. Laser plus trigger opens a curved Palladium panel at eye level, 1.15 m in front of the
  head, with a backdrop that dims the windows behind. The button again, Esc on the Mac, a
  trigger aimed at nothing, or opening an app closes it. While it is open the windows behind
  take no clicks or gaze focus.
- **What it shows.** Search Applications, Recent Apps (Spotlight's `kMDItemLastUsedDate`, eight,
  newest first), then All Applications in pages of 8 × 3 with page dots. Folders open in place
  with a Back button. Pages turn with the dots, the Mac's Left/Right arrows (empty field), or a
  held icon at the panel's edge. Thumbstick paging is written (`LauncherStick`) but has no
  input yet: the stick reaches the host with the controller content-input work.
- **Where the apps come from.** `GET /launcher` in the helper (`LauncherCatalog.swift`) scans
  `/Applications`, `/System/Applications` (Utilities included) and `~/Applications` three
  folders deep, never inside a bundle, plus Finder. Agent and background-only apps
  (`LSUIElement`, `LSBackgroundOnly`) are left out, which also drops the launcher stubs for
  Siri, Mission Control, Time Machine and Apps. A bundle ID installed twice keeps Launch
  Services' default copy. Order and folders come from the old Launchpad database
  (`$(getconf DARWIN_USER_DIR)com.apple.dock.launchpad/db/db`): a private copy is opened
  read-only and immutable, then deleted. Apps it lists that are gone are dropped, empty folders
  too, and every other app follows by name. The user's own rearrangement lives in the Mac app's
  data folder (`spatial-launcher-layout.json`), never in macOS data.
- **Icons once.** The same 128 px icon pipeline as the shelf, cached by bundle ID and version
  (bundle version plus Info.plist time). The Frame keeps `icons/versions.json`; publishing the
  catalogue (`launcher.json`) reports which icons it lacks or has old versions of, and those
  go over in batches of up to 90 KB per SSH command, recent apps first.
- **Typing.** While it is open the helper lends the keyboard to the search field
  (`LauncherKeys.swift`, through the existing Spatial Mode event tap): printable keys, Delete,
  Esc, Return and the arrows are consumed and queued; Command and Control shortcuts, Tab and
  function keys pass. The companion polls `GET /launcher/keys?after=N` every 100 ms, which
  renews a 3 s lease. Closing, Desktop Mode, Stop, a helper restart and helper exit end it; a
  companion that stops polling lets it lapse. The up of a consumed key is consumed too, so no
  app sees half a keystroke. The filter (`ui/frame_spatial_launcher.py`) is case- and
  diacritic-insensitive: names starting with the query, then names with a word starting with
  it, then names containing it; recent apps first within each. Return opens the selected match
  (the first until the arrows move it).
- **Rearranging.** Hold the trigger on an icon for 500 ms to pick it up; it follows the laser
  and a blue bar marks the gap. Releasing between two icons moves it there (top level, or
  inside the open folder); onto an icon or off the panel nothing changes and no folder is made.
  The host shows the move at once and sends `{"action":"launcher","verb":"move","id":…,
  "before":…,"folder":…}`; the companion saves its layout file and republishes.

On this Mac (macOS 26.3.1, read-only probe, 2026-10-26): **167 apps** in 0.17 s (0.27 s with
Spotlight dates). The Launchpad database exists and **imported**: 119 app entries, 98 of them
still installed, three folders (Other 25, Games 3, SheepShaver (Other) 4); 138 top-level
items; catalogue JSON 21.6 KB. Recent Apps came back as Claude, Safari, WhatsApp, Frame
Workspace, AnyDesk, Terminal, System Settings, Xcode. `/Applications/Safari.app` is a hidden
symlink into the Safari cryptex, so the scan keeps hidden-flagged items (dot files are still
skipped).

Checks, all local:

```sh
cd mac/frame-mac-view
xcrun swiftc -swift-version 5 Sources/AppsState.swift Sources/LauncherCatalog.swift \
  Sources/LauncherKeys.swift tests/LauncherTests.swift -lsqlite3 -o "$scratch/LauncherTests"
"$scratch/LauncherTests"
cd ../.. && PYTHONPATH=tests python3 -m unittest test_spatial_apps
python3 native/tests_host.py "$build/frame-workspace-native"
```

Device checklist (not done yet): the button opens and closes the panel and lights while open;
the panel sits at eye level and wraps round the head; windows dim; laser hover, click, page dots
and folders; eyes plus trigger on an icon; Mac typing filters with no keystroke reaching the
focused window, and Command shortcuts still work; Esc closes and gives the keyboard back;
Return opens the match and brings its panel forward; pick-up, edge page turn and drop between
icons, kept after a companion restart; icons all arrive once and are not resent on the next
start; the shelf and status band unchanged; Cairo raster time for the 1440 × 893 panel on the
Frame.

## Apps and Cmd-Tab contract (stage 2)

The approved dock is **D1**, the floating shelf; the switcher is **S1**, the curved window
strip (FW-15 in `ISSUES.md`). The Mac helper and companion supply their data and actions;
this change adds no Mac page UI. The mode/session worker owns the mode toggle, automatic
panels, displays and mirroring. Until `MacView.mode` exists, this integration stays in Desktop Mode.

`Apps.swift` reads `com.apple.dock`'s `persistent-apps` preferences without writing them.
`GET /apps` returns pinned apps in Dock order, followed by other running regular apps, with
`bundleID`, `name`, `running`, `pinned`, `hidden` and `active`. The companion adds each icon
filename. The token-protected
`GET /apps/icon?bundleID=...` returns `{ "png": "<base64>" }`, a 128×128 PNG rendered from
NSWorkspace. `POST /apps/action?bundleID=...&verb=...` accepts only `open`, `hide`, `unhide`,
`quit` and `show_windows`; quit requests normal application termination. Apps can refuse a quit
or show a save prompt. `show_windows` activates and unhides the app.

`ui/frame_spatial_apps.py` joins that catalogue to the companion's owned native window panels.
Panel metadata and a current-window mapping retain app membership when a window is hidden.
Its `/windows?all=1` lookup includes offscreen windows for an already-hidden panel's first sync;
ordinary `/windows` callers keep the existing onscreen list.
The bar's existing sync loop sends state every second and wakes when the helper's switcher
changes (polled every 100 ms). `scripts/workspace-spatial-on-frame.py`, sent as Python on stdin,
validates the JSON and writes it atomically at
`$XDG_RUNTIME_DIR/frame-workspace/spatial-ui.json` **only in Spatial Mode**. Leaving that mode
removes the state. The existing bar sync still collects atomic request files, including when
the bar executable is missing in Spatial Mode.

The state contains Dock-ordered `apps`, each with `{key,title}` windows, and
`switcher: {open,index,seq,chosen,windows,tapActive}`. `switcher.windows` is an additive field:
the ordered panel keys in the strip, grouped by app in recent-use order. `index` selects a
window in **that** array, independent of Dock order. `chosen` is a panel key or null; `seq`
changes with the switcher's visible state. A tap failure is exposed as optional `switcher.error`.
MRU records activations observed since helper startup; its initial order is the frontmost app
followed by launch order. `chosen` stays null while highlighting and is committed on Cmd release.
Icons live in `frame-workspace/icons/<bundleID>.png`; only successful deliveries are remembered.
Failed icons retry without blocking state, and missing icons after a Frame runtime reset are
resent from the companion cache. Initial delivery is limited to four icons per sync.

The companion configures `POST /switcher?mode=spatial&entries=...` with JSON
`[{"key":"<owned panel key>","bundleID":"..."}]`, renewing this configuration each second.
`GET /switcher` reads the helper state. The helper intercepts Cmd-Tab / Cmd-Shift-Tab only in
Spatial Mode: each step highlights a window, the strip stays open while Cmd is held, and releasing
Cmd chooses its key. The strip order stays stable during a cycle. Controller requests use
`POST /switcher?mode=spatial&verb=open|close`; close cancels a highlight and acknowledges a
delivered choice, preventing it from replaying after another app activates. Desktop Mode removes
the event tap; helper exit also removes it. The spatial lease expires if companion updates stop, so an abandoned helper
does not keep intercepting Cmd-Tab.

Headset requests enter the existing ordered job queue:

- `app` forwards only the fixed verbs for apps in the published catalogue.
- `switch` accepts only an owned native window key and calls the existing `/focus` path;
  it closes the switcher after focus succeeds. The Frame brings the panel in front of the wearer.
  Its first cursor visit starts at the centre; later visits use the helper's per-window cursor memory.
- `switcher` accepts only `open` or `close`.

The Frame writer bounds state to 64 KiB, 128 apps and 256 window keys. It checks icon names,
128×128 PNG headers, bundle IDs, key ownership within state, switcher order/index and types,
and rejects symlink runtime/icon directories before writing. Requests for unknown apps,
stale/foreign panels or unsupported verbs are ignored. These checks do not grant an app access
to helper endpoints: the existing companion token is still required.

### Local verification and device acceptance

Reproduce without SSH, app actions or installing an event tap:

```sh
mac/frame-mac-view/build.sh mac/frame-mac-view/.fw15-apps-build/frame-mac-view
xcrun swiftc -swift-version 5 mac/frame-mac-view/Sources/AppsState.swift mac/frame-mac-view/tests/AppsStateTests.swift -o mac/frame-mac-view/.fw15-apps-build/AppsStateTests
mac/frame-mac-view/.fw15-apps-build/AppsStateTests
cd tests && python3 -m unittest test_macview test_server test_spatial_apps
```

The app/switcher unit tests use fakes and temporary runtime folders. The existing Mac helper
suite builds and runs an isolated helper with synthetic streams; it does not enable the new
spatial tap. A successful build and these checks do not establish keyboard interception,
NSWorkspace icon appearance, focus behaviour or headset interaction.

**Verified locally, 2026-10-20:** the command above ran **137 tests, one skipped, no failures**.
The skip was the missing-Accessibility rejection case because this Mac grants Accessibility;
the existing synthetic helper tests emitted unclosed-socket `ResourceWarning`s. The explicit
ARM64 helper build, pure Swift state checks, companion-to-Swift parser/release contract check
and `git diff --check` passed. App actions and transport were fakes; the running companion,
installed helper, user apps and Frame were untouched, and no event tap was installed.
Disposable build binaries and the test log were removed, recovering **1,528,298 file bytes**.

After integrating the mode/session and Frame UI work, verify on authorized devices:

1. Enter Spatial Mode and compare the shelf with the user's Dock: pinned order, running apps,
   correct icons and live active/hidden/running changes. Open an unlaunched pinned app, hide,
   unhide and show its windows; request a normal quit of a disposable app with unsaved content.
2. Hold Cmd and use Tab / Shift-Tab across several apps with multiple panels. Check MRU grouping,
   highlight order, no native Mac switcher, and the chosen panel's focus/front placement on Cmd
   release. Open and close the strip with the controller, then select with laser or gaze.
3. Open/close/hide windows while the strip is open; check no stale keys or jumping highlights.
   Reconnect or restart the helper/Frame and confirm icons and panel mappings recover.
4. Return to Desktop Mode and check native Cmd-Tab works again and spatial state is removed.
   Exit the helper and interrupt companion updates separately; check interception is removed
   promptly, including denied event-tap permission and macOS disabling a slow tap.

## How many windows the Frame can show (verified 2026-10-20)

User's M4 MacBook Pro (macOS 26.3.1), Steam Frame on the SteamOS build installed that day, the
Workspace (2880x2560) streaming alongside. Each test window was on a virtual display of its own
(`FRAME_WINDOW_DISPLAYS=1`, `Separate.swift`) and shown as a native panel (one viewer process and
one hardware H.264 decoder session each).

| Windows | Decoded size each | Total decoded, with the Workspace | Result |
|---|---|---|---|
| 3 (1280x800 windows, 2x) | 2560x1744 | 20.8 MP | all streaming |
| 4 | 2560x1744 | 25.2 MP | the 4th viewer exits: `h264_v4l2m2m` send failed -12 (Cannot allocate memory) |
| 9 (640x400 windows, 2x) | 1600x1200 | 24.6 MP | all streaming |
| 10 | 1600x1200 | 26.5 MP | the 10th exits the same way |

- **The limit is the hardware decoder's budget, about 21-25 megapixels decoded at once, not the
  number of windows** (4 sessions failed at 25.2 MP; 10 sessions passed at 24.6 MP).
- **Frame memory is the next limit.** About 1.6 GB was available with one panel, and each viewer
  process held about 280 MB (the Workspace's about 370 MB); with 9 panels about 0.6 GB was left.
- With all windows redrawing 60 times a second (the worst case), each window's delivered rate fell
  as windows were added (28 fps with one, about 17 with two, about 10-17 with three). The fixture's
  constant redrawing also kept the Mac's WindowServer at 60-98% CPU. Ordinary windows mostly sit
  still, which costs nothing to stream; the realistic moving case is one or two windows.
- Each window took 7-8.5 s to show (its display created, the window moved, the viewer started).

**Inferred design consequence:** one full-resolution live stream per window tops out at about five
typical windows. Spatial Mode needs (a) only the windows that matter to be live at full
resolution: the one you look at, plus ones that are changing; (b) others shown as their last frame
with no decoder session open, or at reduced resolution and rate, reopening when they change or you
look at them; and (c) one viewer process for all panels, to save memory and start panels faster.

## Repeat it

1. Start the companion with `FRAME_WINDOW_DISPLAYS=1` (port 60339) and the Workspace.
2. `swiftc -O mac/frame-mac-view/Diagnostics/SpatialFixture.swift -o /tmp/spatial-fixture` then
   `/tmp/spatial-fixture 12` (moving, 1280x800) or `/tmp/spatial-fixture 6 static small`.
3. `python3 scripts/spatial-capacity-sweep.py /tmp/sweep.log`, or show windows one by one with the
   `show` action and `separate:<window id>`.
4. A pass at a given count is every panel's unit active with `[vr-perf]` lines and no `Cannot
   allocate memory` in the user journal. Afterwards stop each `separate:` stream, quit the fixture,
   and remove the test windows' files in `~/.local/share/frame-workspace/panels/`.

## The laptop screen as if the lid were closed (verified 2026-10-20)

`mac/frame-mac-view/Diagnostics/SpatialMirrorProbe.swift` (build with `swiftc -O`, run while a
Workspace display exists) sets the built-in screen to mirror the Frame Workspace display with
`CGConfigureDisplayMirrorOfDisplay`, applied `.forAppOnly` so macOS undoes it when the probe exits,
then undoes it itself after 6 s. Result, with no errors:

- While mirroring, macOS listed **one** screen, the Frame Workspace: it became the main display
  (menu bar and Dock), at 0,0, 2880x2560; the built-in reported `active=0` and mirrored it. This is
  the "lid closed" desktop, through public API.
- After the undo, the layout was exactly as before (built-in main, 1800x1169 at 0,0; Workspace at
  1800,0), the Workspace stream kept running, and all 72 of the user's windows were still on the
  laptop screen.
- Not yet checked: whether the user's windows change size during a longer mirrored session, and
  whether the built-in's backlight turns off or only goes black.

## A drag between windows on different displays (verified 2026-10-20)

`SpatialDragProbe.swift` opens two text windows of its own, one on the Frame Workspace display and
one on the laptop screen, selects a word in the first and posts a mouse drag that jumps straight
from one display to the other (no path across their shared edge), then drops. **Pass:** the word
arrived in the second window. So a drag can follow the laser from one panel's display to another's.
Inferred, not tested: drags between different apps (the same pasteboard mechanism). The helper's
input now has a shared-pointer mode for window displays; cross-panel behaviour still needs a
device check after integration.

## New windows (verified 2026-10-20)

An Accessibility observer on an app saw `AXWindowCreated` (with the window's title) as a test
window opened. Its close was only reported when the app quit (the closed window object stayed
alive), so closes should also be confirmed from the window list, as `WindowCloseRecovery.swift`
already does.

## Mac foundation (built 2026-10-20)

The helper and companion now implement the Mac half of the
[shared spatial protocol](../../native/README.md#spatial-stream-and-focus-contract-fw-15).
This does not add the mode toggle, automatic window panels, or mirroring.

- `POST /streams/live?src=...` chooses live window streams. Other windows send `still`,
  stop encoding and sending pictures, and continue low-cadence content observation. They
  keep their last picture on the viewer. Resuming sends `live` before a forced keyframe
  containing SPS/PPS. Display streams, including the Workspace, stay live.
- `/status` exposes each stream's live pixel dimensions, live state and last content-change
  time (Unix seconds), plus the focused source and shared-pointer mode. Capture observations
  continue while still; decoder allocation cannot be inferred from this Mac-side status.
- With `FRAME_WINDOW_DISPLAYS=1`, the companion's existing one-second supervisor reserves
  running display streams first, then selects the focused window and recently changed
  windows, newest first. `FRAME_SPATIAL_DECODE_MP` defaults to **18 MP**. Optional windows
  need one second of observation before admission, remain eligible for five seconds after
  their last change, and get three seconds of live residency before a newer window can
  replace them. Focus and the capacity limit override residency. Disabling window displays
  restores all current and future window streams to live.
- Unknown window dimensions are not admitted to the live set. New streams deliver one initial
  picture before honoring still, so each panel has a texture; transient startup decoder demand
  needs device verification. If the Workspace plus focused window cannot
  fit, the focused window stays still until its stream size or the configured budget
  changes. The policy never admits optional pixels beyond the remaining capacity; it
  cannot reduce existing display streams that already exceed the budget by themselves.
- The companion accepts atomic `focus` request files with an owned window panel key and
  finite normalized `(u,v)`, coalesces a batch to its newest valid request and dispatches
  at most four per second. `/focus` raises the window and restores its cursor memory;
  `(u,v)` initializes the first visit. Repeated focus on the same window leaves its cursor
  in place. Shared pointer mode carries held mouse buttons across viewer sessions, while
  keys keep their original session ownership.

**Verified locally, 2026-10-20:** fake-agent policy and request tests, pure Swift state tests,
and the isolated synthetic-helper suite. From the repository root:

```sh
mac/frame-mac-view/build.sh mac/frame-mac-view/.fw15-build/frame-mac-view
xcrun swiftc -swift-version 5 mac/frame-mac-view/Sources/SpatialStreams.swift mac/frame-mac-view/tests/SpatialStreamsTests.swift -o mac/frame-mac-view/.fw15-build/SpatialStreamsTests
mac/frame-mac-view/.fw15-build/SpatialStreamsTests
cd tests && python3 -m unittest test_macview test_workspace_profiles test_server
```

The helper built successfully and the pure Swift checks passed. The Python suite ran **142
tests, with one skip and no failures**; the missing-Accessibility rejection test skipped
because this Mac grants Accessibility. The suite emitted socket `ResourceWarning`s. Pure checks
cover stale-frame epochs, resumed keyframe gating, shared button ownership, first-gaze
initialization after viewer focus, and cursor-coordinate round trips without drift. These
do not establish ScreenCaptureKit or headset behaviour. The helper build stayed inside
this worktree; the installed helper, running companion, displays, viewers and Frame were
untouched. Disposable build binaries were removed afterwards, recovering **2,116 KiB**;
the test fixture removed its isolated helper directories.

Device acceptance is still required after merging the Frame worker:

1. Show more windows than the simultaneous decoder limit with the Workspace running.
   Check `still` leaves the last texture visible and releases the decoder, and that `live`
   recreates it from SPS/PPS plus IDR without allocation failures or stale frames, including
   rapid transitions, resizing, opening windows and reconnecting. Check status dimensions
   against actual decoded sizes and confirm the total live pixels respect the budget.
2. Edit a still window, including a small text edit or badge change. Confirm it wakes,
   remains live briefly, and settles without repeatedly evicting other windows. Measure
   capture/encode load and focus latency with ordinary and continuously changing windows.
3. Wear the headset and look between panels. Confirm the intended window becomes key,
   the Mac cursor appears there, the first visit uses the gaze coordinates, and returning
   restores each window's last cursor position after mouse movement and window resizing.
4. Drag from one panel into another, release in the destination, then repeat between apps.
   Confirm the drag survives the panel boundary, keys remain independent, and closing or
   disconnecting the source during a drag releases buttons without stuck input.

## Shared host foundation (FW-15)

The opt-in Frame implementation is `FRAME_NATIVE_HOST=1`. The companion sends
window panels to one `frame-workspace-host-UID.service`; the Workspace can keep
its separate viewer. `frame-workspace-native --host-dir SPEC_FOLDER` watches
atomic per-panel spec files, keeps one GL context/OpenVR client, and reuses the
same per-panel overlay/chrome/placement loop as the standalone path. The default
launcher path is unchanged. See [native host protocol and lifecycle](../../native/README.md#shared-window-host-fw-15-opt-in).

Still/live and focus are the shared worker contracts:

- WebSocket text `{"t":"still"}` keeps the last completed picture and closes
  the decoder session after processing already accepted packets with a bounded
  final-output drain. `{"t":"live"}` recreates the session only from the next IDR with SPS/PPS. Per-panel state
  exposes `streamStill`, `decoderSessionOpen`, `decoderStillReleases` and
  `decoderLiveOpens`, so decoder ownership can be checked separately from a
  visible held texture.
- The host samples eye tracking through `GetEyeTrackedFoveationCenter`, tests
  the nearest front-facing window surface with the existing bend geometry, and
  now applies the 250 ms bezel confirmation described below, plus edge hysteresis.
  Uneven and vertical halves use
  original-window UV. Without eye tracking the controller laser picks the
  window. On a switch it writes an atomic mode-0600 request in the existing
  requests folder: `{"action":"focus","key":"OPENVR_KEY","u":0.0,"v":0.0}`
  (u right, v down, top-left origin). The Mac live-set,
  focus delivery, pointer mode and per-window cursor memory require the
  companion integration and end-to-end verification.

### Verification record (2026-10-20)

**Verified locally (Mac, Apple M4 GL):** the extended self-test, production
Pipeline still/live lifecycle, two-stream host integration, ordinary stream and
reconnect checks passed. All 16 YUV shader layout/range/matrix/pattern cases
matched the production swscale oracle to within 1 RGB level. A four-second
BT.601-limited GPU stream passed its existing image, control and timing
assertions with zero errors. The companion/launcher suite passed: 84 tests run,
1 skipped.

**Verified on the Frame:** an isolated AArch64 build using the device's system
libraries (FFmpeg 61.3.100), the build script's self-test and a separate explicit
`--self-test` passed. All 28 copied C/C++ source/header hashes matched the native
source checkpoint. `/tmp/fw-host-worker` was removed afterwards. No viewer,
OpenVR/GL session, hardware decoder, installed artifact, service or SteamVR
configuration was changed or exercised there.

**Known test harness limitation:** `tests_gpu_conversion.py` reaches a short
1.2-second stream test whose control observer deadline starts before process/GL
startup, while the first updated state is periodic. That revision-observer
assertion fails on both baseline `d877eb1` and this change; the same current
stream passes at four seconds. Assertions were preserved. The full short GPU
suite is therefore not a passing result.

Compact source identities, commands and outcomes are recorded in
`native/evidence/spatial-host-validation.json`. Synthetic streams and geometry
do not establish worn focus behavior, OpenVR host interaction, hardware
reclamation, or performance improvements.

Reproduction, on a development machine with the native build dependencies:

```sh
native/build/frame-workspace-native --self-test
python3 native/tests_still.py
python3 native/tests_host.py native/build/frame-workspace-native
python3 native/tests_stream.py native/build/frame-workspace-native --use-display
python3 native/tests_stream.py native/build/frame-workspace-native --use-display --reconnect
python3 native/tests_stream.py native/build/frame-workspace-native --use-display --conversion gpu --case bt601-limited --duration 4
cd tests && python3 -m unittest test_macview test_native_pose_launcher
```

`tests_host.py` uses a hidden GL window with `--no-vr`, two loopback H.264 streams,
CPU and GPU conversion, dynamic add/remove/re-add, still/live, independent
controls and saved bends, and per-stream failure/close isolation. It does not
contact a companion or initialize OpenVR. The tests remove their fixtures.
`--self-test` covers spec generations and terminal bookkeeping, nearest hits,
rotated/asymmetric/vertical surface UV, back-face/invalid/missed rays, dwell,
edge hysteresis and controller fallback source changes without initializing GL
or OpenVR.

### Device acceptance still required

With the integrator's Mac changes and an authorized device session:

- Start multiple host panels beside the separately running Workspace; check one
  host PID/GL/OpenVR session, independent keys and textures, concurrent launch,
  add/remove/replacement, stream close/error, reconnect and clean shutdown.
- Verify every existing chrome layer and half, controller hover/haptics,
  move/resize/bend (both directions and uneven halves), close, control-file
  commands and saved/profile pose restoration. Overlapping windows must route
  a hand to the nearest panel and retain a drag until release.
- Confirm a real V4L2 session closes on `still` while its last picture remains
  visible, another window can use the freed decoder budget, and repeated
  `live`/keyframe cycles reopen cleanly. Check rapid transitions, tunnel
  reconnect and dimension changes. The isolated build/self-test never opens a
  hardware decoder.
- Worn: validate eye-ray direction, nearest-window choice and top-left UV;
  settle on adjacent and overlapping panels without edge flicker; turn eye
  tracking off and verify controller fallback. Confirm focus requests are
  consumed once, the intended Mac window activates, cursor memory works, and
  looking away does not switch to a controller while gaze is valid.
- Measure the same window set with and without the host: resident memory,
  first-picture/startup latency and moving-stream throughput. The spike's
  per-process costs do not prove the host's actual saving or a larger decoder
  budget. No host runtime or worn test was performed in this worker session.

## Both foundations together (verified 2026-10-20)

Merged and run on the user's M4 and the Frame (not worn), with `FRAME_WINDOW_DISPLAYS=1` and
`FRAME_NATIVE_HOST=1`, the Workspace streaming, and eight still 1280x800 test windows (each
2560x1744 on its own display), twice what one-process-per-window could show:

- All eight showed, with no decoder allocation errors; the budget kept the Workspace and two
  windows live and six still.
- **One host process ran all eight panels in about 394 MB** (one viewer per window measured about
  280 MB each); the Frame had about 2.4 GB available, more than with a single panel before.
- A `focus` request for a still window (as gaze would send) made it live within 2 s, its decoder
  reopening at 2560x1744; the previously live windows, unchanged, went still.
- Rebuilding the Mac helper kept Screen Recording and Accessibility (the companion reported both).
- Showing a window still took 6.5-12 s, now mostly the Mac creating its display and moving it.

## Still to test

- A longer mirrored session, and the Mac's behaviour on sleep and wake while mirrored.

## Spatial Mode stage 2: companion and Mac helper

The companion owns `mode` (`desktop` or `spatial`) and remembers it with the
last start settings in `workspace/last-start.json`. The approved T1 segmented
switch is on the Mac page's Workspace card; the existing bar switch uses the
same lifecycle through `mode_desktop` and `mode_spatial` requests. These actions
also work through `POST /api/macview`; `workspace_start` uses the chosen mode
and optionally accepts an explicit `mode`. Choosing while stopped selects the
next start; choosing while running stops the old setup and starts the other.
Unconfirmed cleanup blocks replacement. Desktop keeps its existing display
and viewer behavior. Spatial forces window displays and the shared native host
without requiring the foundation's two environment flags.

The helper owns the hidden home, mirroring, window restoration and display
pool in the same `serve` process as the window captures. Its authenticated
API is `POST /spatial/start`, `GET /spatial`, and `POST /spatial/stop`.
The home is 1280x800 at 1x. Before creating it, the helper saves the physical
display modes, origins, mirror links, main display and eligible window frames.
It makes the home main and mirrors the built-in with an app-only transaction.
Window sizing uses those original frames rather than frames compressed by
mirroring. Eligibility requires a regular application's normal AX standard
window at least 120x80; helper/system windows, dialogs, minimized, hidden and
full-screen windows are excluded.

The pool starts with three ready 2x displays and refills toward three warm
spares as leases are consumed. Creation is serialized for background refills;
active windows can expand the pool up to a 128-slot safety ceiling. Closed or
stopped windows return leases for reuse without removing their displays.
A live window must first be parked on the home; failed parking retains the
lease for Stop to retry. The pure policy test admits 72 simultaneous distinct
leases, which establishes ownership logic rather than macOS display capacity.
Stop waits for capture shutdown, lease preparation and refill work, unmirrors,
restores physical layout and surviving windows, removes the pool/home, then
verifies restoration again. Failed or deferred cleanup retains its state and
blocks replacement; display enumeration failures cannot establish removal.

The companion reconciles the helper's eligible window list on its normal
one-second supervisor, opens native `separate:` panels off that loop, and
retires closed windows' panels. A panel launch cannot block budget/focus
observation; queued launches are invalidated by Stop or a mode switch. Panel
errors remain visible in API state and retry. Closing a panel explicitly
keeps it closed until that window disappears or the next Spatial start.

The bar state includes `workspace.mode`. `spatial-ui.json` has one writer, the
apps module (`ui/frame_spatial_apps.py`, see "Apps and Cmd-Tab contract" above),
which runs on the same one-second sync, sends icons, and clears the file when
Spatial Mode ends; it also handles the Frame's `app`, `switch` and `switcher`
requests (integration, 2026-10-21: the mode branch's own writer and request
hooks were dropped in favour of it).
Last Session is independent per mode; see [Last session](profiles.md#last-session-auto-saved).
Spatial matches durable window rules before automatic opening and supplies
saved poses to the first launch. It does not snapshot a half-open startup over
the previous complete session. This is locally tested behavior; room alignment
and device restoration still require acceptance below.

### Local verification (2026-10-20 and 2026-10-21)

The isolated helper build, pure `SpatialPolicyTests`, `SpatialStreamsTests` and
window-close recovery checks passed. The spatial policy fixture covers 72
distinct simultaneous leases, spare refill and failure bounds, eligibility,
layout ownership/restoration and cleanup gates. These are state checks; they
do not establish macOS display capacity, restoration or pool latency.

The first integrated Python run exposed seven HTTP timeouts: stopped Spatial
status was doing unnecessary Accessibility discovery on the Desktop status
path. Discovery is now restricted to starting/running Spatial setups; the
read-only stopped-status regression also requires an empty window list.

At source checkpoint `1d6dd35`, the required four-module suite passed **188
tests, with one skip and no failures**. The skip was the missing-Accessibility
rejection case because this Mac grants Accessibility. Five previously known
unclosed-WebSocket `ResourceWarning`s remain. The suite also built and ran an
isolated synthetic helper without creating displays or posting Mac input.
Both staged and worktree whitespace checks passed. The explicit helper build
and pure checks above passed; the installed helper and Frame were untouched.

Reproduce from this worktree's root, keeping helper output inside it:

```sh
mkdir -p mac/frame-mac-view/.fw15-build/tmp
mac/frame-mac-view/build.sh mac/frame-mac-view/.fw15-build/frame-mac-view
xcrun swiftc -swift-version 5 mac/frame-mac-view/Sources/SpatialPolicy.swift mac/frame-mac-view/tests/SpatialPolicyTests.swift -o mac/frame-mac-view/.fw15-build/SpatialPolicyTests
mac/frame-mac-view/.fw15-build/SpatialPolicyTests
xcrun swiftc -swift-version 5 mac/frame-mac-view/Sources/SpatialStreams.swift mac/frame-mac-view/tests/SpatialStreamsTests.swift -o mac/frame-mac-view/.fw15-build/SpatialStreamsTests
mac/frame-mac-view/.fw15-build/SpatialStreamsTests
TMPDIR="$PWD/mac/frame-mac-view/.fw15-build/tmp" /bin/sh mac/frame-mac-view/test-capture-recovery.sh
(cd tests && python3 -m unittest test_macview test_workspace_profiles test_server test_native_pose_launcher)
```

The Workspace card's production JavaScript is exercised by
`tests/page/spatial_mode.mjs`: both modes, control visibility, cleanup retry
and mode-button requests. No enabled browser surface was available for a
rendered page check in this session; inspect the switch on the device Mac page.

Cleanup retained source and this compact record, and removed the copied helper
checkout (33,516 KiB), disposable binaries/module cache/logs (33,484 KiB), and
generated bytecode (416 KiB): **67,416 KiB (65.8 MiB)** measured with `du -sk`.
The suite removed its isolated helper directory itself. No recovery copy or
superseded build was retained; rebuild with the commands above for integration.

### Device acceptance order

The implementation session did not create displays, mirror or move windows,
post Mac input, contact the Frame, or change the installed/running companion.
The integrator should perform these steps in an authorized device session:

1. Save work and record the starting physical layout and several fixture window
   frames. Use an isolated helper/companion build and one active companion owner.
   Keep an authenticated `workspace_stop` API command ready outside the headset.
   Verify the native host and bar are installed before changing the Mac layout.
2. With the headset off, verify Desktop Start/Stop first. Then choose Spatial
   while stopped and Start. Confirm there is no Frame Workspace display or
   Workspace viewer, the home is main, the built-in mirrors it, internal
   displays are absent from the picker, and every eligible normal window
   obtains a separate panel in one native host. Inspect both the Mac-page
   switch and bar state. Confirm permission/startup failures roll back cleanly.
3. Open, close and reopen fixture windows. Check no duplicate panels, no panels
   for helper/system/tiny/dialog windows, no leaked displays, safe placement
   of stopped windows before their displays are reused, and first-picture
   latency. Compare the same window on a ready pool lease and a newly created
   display; no pool speedup has been measured by this implementation session.
4. Move and bend panels, wait for a successful session snapshot, and switch to
   Desktop. Verify every moved window's original frame, physical layout and
   mirroring are restored and the home/pool are removed before Desktop starts.
   Give Desktop a distinct window set/poses; switch back and verify Spatial's
   own saved poses. Stop/restart the companion to check mode persistence and
   both Last Session files. A running Desktop's Show Again must still recenter.
5. Test Stop, companion quit and helper termination before sleep/wake cases.
   Confirm clean restoration on graceful exit and automatic unmirroring when
   the helper dies. While asleep, exercise deferred removal and retry Stop
   after wake: it must not report removal or start another setup prematurely.
   Check repeated open/close churn does not accumulate released displays.
6. Only after the off-head cleanup checks pass, wear the headset. Verify panel
   placement and controls, gaze focus/cursor memory, cross-panel drag, still/live
   decoder release and the existing pixel budget. Measure matched startup and
   steady-state behavior. Dock, app-switcher and Cmd-Tab acceptance follows
   integration of the other worker's modules.

## Option B spike: every window from one shared display (verified 2026-10-21, M4, macOS 26.3.1)

The user chose option B (`evidence/spatial-options-mockup.png`): every window its own panel, all of
them on one hidden display on the Mac, the looked-at window raised there for input.

- **Menus, popovers and sheets come along.** `SCContentFilter(desktopIndependentWindow:)` with
  `SCStreamConfiguration.includeChildWindows = true` captured a window's popover (including where it
  sticks out past the window), its sheet and its context menu; with it off, none of them
  (`ChildWindowCaptureProbe.swift` with `ChildWindowFixture.swift`). The app's menu bar is not part
  of any window, so it needs its own small panel (design to come).
- **Covered windows capture in full**: a window entirely under another window captured with none of
  the covering window in it.
- **Many captures are cheap**: 20 window streams at once (child windows included, 2x, up to 30 fps)
  took the capturing process about 7.5% CPU and 23 MB (`ManyWindowCaptureProbe.swift`).
  WindowServer went from about 60-68% (its load from other work on the Mac then) to about 78-88%.
- Not yet tested: one large hidden display hosting every window (the Workspace display already
  works as one), raising the looked-at window and its input, windows larger than the laptop screen.

## Option B spike 2: the shared display (2026-10-21)

Fixture-only Mac runs with `SharedDisplayProbe.swift` and `SharedDisplayFixture.swift` in
`mac/frame-mac-view/Diagnostics/`; build/run commands are in the probe's header. No mirroring,
system-setting changes or headset use. **Verified:** macOS 26.3.1; the built-in's live mode was
1800×1169 points / 3600×2338 backing pixels at 120 Hz, rather than the capsule's 1512×982 snapshot.
The separate-Spaces preference key was absent; **inferred:** the default was still in effect.
Screen Recording, Accessibility and event-posting preflights passed; no permission was requested.

- **Verified — display:** 3840×2400, 3840×2304, 3840×2250 and 3840×2160 points at 2× accepted
  settings but never appeared online or exposed modes within each 4.2-second readiness check.
  3600×2250 points / 7200×4500 pixels at 60 Hz worked repeatedly: creation/apply 0.47–0.76 s,
  HiDPI ready 0.56–0.79 s in the successful runs. This is the largest tested working mode, not a
  measured absolute limit. Its origin was (1672,1169), below the built-in with a 128-point shared
  edge. A one-point edge snapped to a corner; 128 points held. Main-display mode and mirroring
  stayed unchanged.
- **Verified — overlapping windows:** AX moved 20 owned windows to the same (1752,1259,640,422)
  point frame. Every stream used `desktopIndependentWindow`, child windows included, 2× BGRA,
  requested 30 fps, no cursor or shadow. Full 1280×844 frames arrived even for entirely covered
  windows: 104/104 distant identity-color samples matched across the 1/5/20 runs; the bottom
  window's saved frame was also visually checked. Short-run complete-frame rates and process
  measurements follow (CPU is percent of one core; memory is resident MiB, excluding GPU/surface
  allocations; CPU sampled over about 4.25 s after stream startup):

  | Windows | Complete frames/s per window | Probe CPU / MiB | Fixture CPU / MiB | WindowServer CPU / MiB |
  | --- | --- | --- | --- | --- |
  | 1 | 22.8 | 0.9% / 27.3 | 4.0% / 92.1 | 48.6% / 130.3 |
  | 5 | 21.8–22.5 | 3.1% / 27.7 | 6.4% / 86.7 | 53.9% / 114.4 |
  | 20 | 17.5–18.3 | 11.3% / 29.3 | 25.6% / 88.4 | 73.1% / 117.9 |

  **Verified limitation:** this was a loaded Mac (swap about 7.6–8.2 GiB during the load run),
  not an idle baseline or a sustained 30-fps result. These are capture rates, not encoded video,
  headset decode/render or worn performance. Swap was checked before each heavy phase; highest
  pre-step reading across the successful runs was 9864.94 MiB, below the 10240 MiB stop threshold.
- **Verified — large windows:** AX allowed an outer frame of 2400×1500 points; capture was
  4800×3000, 23.3 complete frames/s, four matching corner samples. A 3600×2200 request succeeded
  at the AX API but macOS clamped the actual frame to 3600×2119; capture followed that actual
  frame at 7200×4238, 20.6 frames/s, four matching samples. Exact 3600×2200 and the reason for
  the height cap remain unverified; accepting the requested AX size is insufficient.
- **Verified — raise/input:** with five windows fully overlapping, AXRaise returned success and
  activating the fixture reached frontmost-app plus focused-window readiness in 5.23 ms (one
  sample). The first logged click arrived 122.94 ms after raise, including an intentional 80 ms
  cursor-settle wait. Two mouse-down/up pairs, four delivered drag events (five posted; one
  coalesced), and `a`, `b` were logged only by window 0; the other 19 logged none. The real cursor
  moved to (2002,1484) on the virtual display, outside the built-in; **inferred:** it is absent
  from the user's real screen while there. The probe warped it back on teardown; physical-screen
  observation and repeated focus-latency statistics were not tested.
- **Verified — menus:** the hidden display showed the active fixture's menus and system status
  items. Display filtering with local `sourceRect=(0,0,3600,36)` captured a 7200×72 strip. Clicking
  `Probe` there opened fixture-owned SCWindow 16295, layer 101, frame (1879,1200,159,34); its own
  stream delivered 318×68. One earlier start failed with CoreGraphics 1003; two later starts
  worked, and a display crop also captured the dropdown. The fixture's `FW15` status menu was
  SCWindow 16302, same owner/layer, (4652,1200,153,34), streamed at 306×68. Clicking the actual
  Control Center item there opened SCWindow 16311, owner Control Center, layer 22,
  (4058,1169,1958,1556), independently streamed at 3916×3112. Visual inspection confirmed its
  controls with substantial surrounding transparent padding. Menu surfaces can reuse IDs;
  discovery must check current visibility/frame as well as new IDs. No status setting was changed.
- **Verified — teardown:** the final targeted run put all 20 fixture frames exactly back (20/20
  AX size/position calls succeeded), quit its fixture and removed the display. Rediscovering AX
  elements after ordering windows back in was necessary: references from before `orderOut` became
  invalid. Display layout/mode/mirroring matched exactly. All 228 pre-existing surfaces remained;
  227 frames matched exactly and only WindowServer's cursor surface (ID 4) differed. Earlier runs
  did show unrelated-window drift: a Finder frame changed during setup, and the load run saw a
  WhatsApp frame change from 500×500 to 64×64. **Unresolved:** the cause on this concurrently used
  Mac; none of the probe's AX mutations targeted those apps. Thus the final comparison passed for
  app windows, but these runs do not establish a general no-drift guarantee.

The load/input run took 47.3 s; the final menu/restoration run 16.1 s. Both completed, and the final
source rebuilds passed without warnings. Swap was 9904.94 MiB before that build and 11489.94 MiB
afterward; further runtime probing stopped at the 10 GiB limit. Temporary binaries, module cache,
snapshots, captures, logs, fixture preferences and crash reports were removed; cleanup recovered
103.07 MiB (155 scratch files and three fixture-generated files outside scratch). The diagnostic's
optional text drawing initially crashed in CoreText and was removed; color/animation and input
checks were retained. Long sessions, absolute display-size limits and production integration
remain untested.

**Inferred — building option B:** one shared display can host covered window captures, routed
input and separate menu/status surfaces. Use the verified 3600×2250 mode as a fallback, reserve a
real shared edge, size panels from confirmed AX frames, reacquire AX objects after hiding windows,
and retry transient menu streams with a display-crop fallback. Crop Control Center's padded canvas
before assigning a pixel budget. The 3600×2200 height cap and earlier non-fixture frame drift still
need controlled follow-up; preserve before/after inventories rather than promising exact restoration.

## First device run of Spatial Mode (2026-10-21, user's M4, not worn)

Full run with the user's 66 windows (34 on screen), after recording every window's frame and the
display layout (a recorder and a restore tool, `restore BEFORE.txt`, in the session's scratch).

- **Desktop Mode** still started and stopped cleanly (6.5 s, 0.8 s).
- **Pool displays never became ready**: they set their 2x mode once, before macOS had added
  them. Fixed: re-apply while waiting (a few times only; each is a display transaction).
- **Stop refused** ("the physical display layout changed while spatial mode was active") and left
  the built-in mirrored, the pool and home displays up and windows moved. Ending the helper
  undid the app-only mirror and removed its displays, and its exit path put windows back. Fixed:
  Stop now logs what differs and still undoes its own mirror.
- **Windows' streams failed**: first "the requested mode is not in the display mode list" (a pooled
  display's new modes weren't published yet); then, even with fresh displays sized to each window,
  leases took 2-147 s or never reached their mode: `CGCompleteDisplayConfiguration returned
  1001`, macOS picking 1840x1008@2x for a 920x504 window. **While the built-in is mirrored, macOS
  rejects or stalls the display configuration that each window's display needs** (verified
  repeatedly tonight). Without mirroring the same per-window displays work (the spike, and
  eight windows through the shared host on 2026-10-20).
- Start took 33-41 s standalone and 102 s through the companion; the companion's limit for
  `/spatial/` calls is now 120 s.
- After the run: the display layout was exactly as before, and 64 of 66 windows exactly back; two
  windows that were hidden off screen (a Terminal and an ExpressVPN window) moved.

**Decision (the user's, 2026-10-21): blank the laptop screen, don't mirror it.** By default
Spatial Mode now leaves the built-in as the main display behind a plain black cover window (level
just above normal windows, so the menu bar, Dock and system dialogs stay visible and usable); the
home display is only a hidden parking place. `FRAME_SPATIAL_MIRROR=1` keeps the mirror for
experiments. Retested without mirroring, window displays still stalled, so mirroring was not the
whole cause: Spatial window displays allowed up to 5120x3200 pixels (for reshaping pooled
displays), so macOS chose a larger default mode (1840x1008 for a 920x504 window). They are now
created like separated windows' displays (exactly twice the window's size, the same mode
selection). With that, the first window's display came up, the window moved and capture
started; the second display's HiDPI change still failed with 1001, and each display change took
about 30 s while the Mac's swap was at 4-7 GB from other sessions' work, so the next run should
start on an unloaded Mac (these runs were not conclusive). Stop worked every time with the
cover; after the runs the layout was exact and 64 of 66 windows exactly back (the same two hidden
windows as above moved).

Changes from this run: Stop always undoes its own mirror; per-window startup steps and display
mode failures are logged; the companion keeps the helper's log in
`mac-helper.log` beside the app's `server.log` (`~/Library/Logs/Frame Workspace/` in the Workspace app); spare pool displays are off
(`FRAME_SPATIAL_WARM_SPARES=0` from the companion). Next: rerun on an unloaded Mac, one window at a time, then
several; check whether display changes overlap (the second window started within a second of the
first) and serialize them if so.

## Spatial UI integration (2026-10-20)

The approved D1 shelf, S1 curved live-thumbnail strip and focus confirmation are
implemented in the shared native host. The exact companion JSON, icon location,
atomic action verbs, state sequencing and isolated diagnostic flags are in the
[native spatial contract](../../native/README.md#spatial-shelf-switcher-and-focus-fw-15).
The host keeps companion order, preserves still-frame appearance, samples
vertically transposed panel textures correctly, blocks underlying panel picks,
and applies one successful focus snap at a time. Desktop Mode hides these layers.

The current compact rendering evidence is
[spatial UI](../../native/evidence/spatial-ui-menu.png),
[held icon](../../native/evidence/spatial-ui-held.png) and
[focus confirmation](../../native/evidence/spatial-ui-focus.png). The UI image
uses installed app icons copied into an isolated fixture and asymmetric synthetic
thumbnail textures, alternating ordinary and vertical-bend storage. It is a
production-renderer diagnostic, not headset or Mac-window capture.

Local verification and exact commands are recorded in
[spatial-ui-validation.json](../../native/evidence/spatial-ui-validation.json).
**Verified locally:** Apple M4 GL build, self-test, production host integration
with synthetic streams and an inventory over 16 KiB, and renderer GL readback
covering late icons and live updates reusing the same texture. Self-tests cover
the uninterrupted focus dwell, foreground picking, layout, stable app targeting,
trigger/hold edges and grip traversal beyond seven visible cards.

**Verified on the Frame:** an isolated AArch64 build with
`scripts/build-workspace-native.sh` and an explicit second `--self-test` passed.
All 34 native code/input hashes and the build-script hash matched this checkout.
The build used system FFmpeg 61.3.100, SDL2 2.30.2 and Cairo 1.18.0. The temporary
`/tmp/fw-ui-worker` directory was removed. No running companion, installed viewer,
service, real runtime state, GL/OpenVR session or hardware decoder was touched.

Generated build/debug data and temporary fixtures/captures were removed after
verification; measured cleanup is recorded with the validation results. Compact
current screenshots, source and reproduction commands are retained.

Remaining acceptance: actual left-Y binding, eye tracking, focus latency and
short-glance behavior while worn; shelf height/yaw comfort; controller hold/menu
selection and left/right grip traversal; live and still thumbnail freshness in
SteamVR; foreground ordering at different depths; Cmd-Tab release selecting
exactly one intended Mac window; pose persistence and switching after real windows
close; and no unintended interactions through foreground UI. Local synthetic
checks do not establish those device behaviors or performance.
