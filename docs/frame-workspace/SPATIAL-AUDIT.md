# Spatial Mode audit (2026-10-26)

An audit of FW-15 against main `7e7eca9`, after the first Spatial launch from the
installed Mac app and the Steam library entry failed (2026-10-26, 08:12–08:15 JST).

## What the user saw

1. The laptop screen went black.
2. The dock shelf couldn't be moved, jumped as the head turned, never sat straight
   ahead (about 15° off), and SteamVR recenter didn't help.
3. Only two "windows" appeared (AnyDesk, Xcode), each just a title or menu strip.
4. Selecting another app on the shelf (Claude) did nothing.
5. There is no way to reach Spotlight, Launchpad or Applications from the headset.

## Causes

**The Frame ran a stale viewer.** `~/.local/share/frame-workspace/native/frame-workspace-native`
was built 2026-10-21 00:48 from `6ffa755` or earlier. Its build metadata differed
from main for 18 native files. Option B (`c468154` onward: roles, surfaces, the L2
wall, the shelf status band, curved menu strips) never reached the headset. The
launcher release rebuilt only the dashboard (`launcher-validation.json`). The old
host ignores the `role`/`slot` fields and `surface` messages. So it showed the
menubar, status and menu strips as ordinary panels with their own saved poses. Those
were the "title bars" the user saw. Nothing checks compatibility: Spatial preflight
only tests that the binary is executable (`ui/frame_macview.py` near line 1998),
and the host spec stayed at `version:1`.

**Main has a startup blocker too.** Reconciliation launches menubar, status and all
four menu slots before any window (`_ensure_spatial_aux_panels`). An idle menu slot
sends no picture by design (`SpatialShared.swift` near line 332). But native
`ready` needs a submitted texture (`native/main.cpp` near line 1012), and the
launcher waits 40 s for `ready` and texture dimensions before it removes the panel
(`scripts/workspace-native-on-frame.sh` near line 469). The journal shows two 40 s
zero-frame panels (08:14:18–08:14:58, 08:15:02–08:15:42), and no window stream was
ever decoded. A local main build reproduced it: an idle `role=menu` stream stays
`ready:false`. The tests missed it because every fixture menu sends video right away.

**Shelf clicks queue behind startup.** App actions share the single FIFO worker with
reconciliation, so the Claude click was most likely still waiting. The path itself
works: native emits `{"action":"app","verb":"open"}`, the companion calls
`/apps/action`, and the helper calls `NSWorkspace.openApplication`. Errors are
swallowed with no headset feedback, and opening an app doesn't bring its panel forward.

**The shelf behaves as coded, also on main.** The anchor is the HMD's yaw at the
first pose. It snaps only once the head turns more than 0.8 rad (about 46°) away, so
any smaller offset, like the 15°, stays. There's no shelf handle, saved pose, recenter
handler (the session pumps Quit only), or Spatial recenter: dashboard Recenter
targets the Desktop Workspace key.

**The black screen is by design.** A floating black cover appears over the built-in
display as soon as the helper starts, before the headset shows anything. The cursor
fence and the Cmd-Tab capture are on from that point too.

**Launching apps:** the shelf can open pinned Dock apps and running apps, including
non-running pinned ones. There's no app search, Applications browser or Launchpad.
Spotlight, Notification Center and system panels are excluded from capture, so
Cmd-Space opens Spotlight under the cover, where the headset can't see it.

Stop worked on the Frame side. On the Mac, the display layout was back to the
built-in only afterwards.

## FW-15 status on main (source; none of it accepted on the device)

| Area | State |
|---|---|
| T1 toggle, Option B shared display, `spatial:` capture with children | Implemented |
| D1 shelf with real Dock icons, open, hide, quit | Implemented; no move, pin or recenter; snaps at 46° |
| S1 switcher, Cmd-Tab lease, V1 stills | Implemented |
| Gaze dwell, glow, cursor memory | Implemented; focus isn't confirmed by the Mac |
| M1 menu strip, I1 status | Capture and render implemented; no content clicks |
| L2 wall, new windows getting panels, restore on Stop | Implemented; saved poses override the wall (fixed after the first usable run: [SPATIAL.md](SPATIAL.md#window-layout-and-a-faster-start-2026-10-26)) |
| Move, curve and scale panels with controllers | Implemented; resize scales the panel, not the Mac window |
| Pointer, scroll and keys into window content from the native host | **Missing** (the browser viewer has it) |
| Drag between windows | Partial; blocked by the missing input and the cursor fence |
| Hide one window and bring it back | Partial; X hides with no way back; no close or minimise |
| App search, Applications, system UI | Not in the plan |
| Version handshake, matched native payload | Missing |
| End-to-end test (installed pair, idle menus, app launch) | Missing |

## Repeat the diagnosis

- Stale viewer: compare `source_sha256` in the Frame's `native/BUILD-METADATA.json`
  with `sha256` of the checkout's files.
- Startup blocker: build main with `scripts/build-workspace-native.sh` (on macOS,
  add a `gl.pc` with `Libs: -framework OpenGL`). Run the host with `--host-dir DIR
  --no-vr --action-manifest none --seconds 4` and one version-1 `role=menu` spec.
  Send the hello and `{"t":"surface","v":1,"seq":1,"visible":false}` with no video.
  The bug is present while the panel state shows `ready:false` and
  `textureWidth:0`; it's fixed once an idle menu registers and the windows launch.
- Run journal: `ssh frame journalctl --user --since ... | grep frame-workspace-native`
  shows the decoded sizes and `exit reason=host-remove` with 0 frames.
