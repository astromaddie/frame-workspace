# Spatial Mode, option B: the shared-display contract

FW-15 option B (chosen 2026-10-21): in Spatial Mode every Mac window gets its own panel in the
headset, but on the Mac they all live on one large hidden virtual display. This file is the
contract between the three parts that build it: the Mac helper (`mac/frame-mac-view`), the
companion (`ui/`) and the native headset host (`native/`, with its launcher). Measurements behind
it are in `SPATIAL.md` ("Option B spike" and "Option B spike 2"); the UI choices are in
`ISSUES.md` (FW-15: D1, S1, V1, focus confirmation, menu bar M1 and I1). Desktop Mode is not
changed by any of this.

## The shared display (helper)

- One `CGVirtualDisplay`, purpose `.spatialShared` (vendor `0xF0C2`, product `0x5E9D`),
  3600x2250 points at 2x (7200x4500 pixels, 60 Hz), max pixels exactly twice the size.
  `FRAME_SPATIAL_SHARED_SIZE=WxH` overrides it for experiments. 3840-wide modes never came online.
- Placed below the built-in with a 128-point shared edge (`CGConfigureDisplayOrigin`, app-only).
  No mirroring: the built-in stays main, blanked by the existing cover window.
- Every eligible window moves to the display's top-left, just under its menu bar, clamped to the
  usable rect (macOS caps heights at about 2119 points). Windows overlap; that's fine, each is
  captured on its own. The window's real frame (after macOS clamps it) is what's reported.
- Stop: end captures, re-fetch AX elements, restore every window, then remove the display and
  verify. `originals` are persisted to
  `~/Library/Application Support/Frame Control/spatial-originals.json`, and a helper that starts
  and finds that file restores from it (a crashed helper must not leave windows stacked).
- `FRAME_SPATIAL_ONLY_PIDS=pid,...` limits eligibility to those processes (fixtures and tests).

## Stream sources (helper `Capture.swift`)

| Key | What |
|---|---|
| `spatial:<wid>` | One window on the shared display: `desktopIndependentWindow`, `includeChildWindows = true` |
| `menubar` | The menu-bar strip of the display that shows the active app's menus, cropped to the app menus: `sourceRect = (0, 0, menusMaxX, menuBarHeight)` |
| `status` | The same strip's status items: `(statusMinX, 0, width - statusMinX, menuBarHeight)` |
| `menu:0` ... `menu:3` | Slots for open dropdown menus and status panels, each retargeted to the current surface's SCWindow, with a display-crop fallback when a window stream fails to start (CoreGraphics 1003 was seen once) |

- `spatial:` is a window source (still/live through `/streams/live`, counterpart keys, window id).
  `menubar`, `status` and `menu:*` are not; menu slots go still and live themselves as they hide
  and show.
- Frame rate: the focused `spatial:` window is captured at up to **60 fps**; other live windows at
  up to 30. Unchanging windows produce no frames.
- The split point between app menus and status items comes from the leftmost status item
  (layer 25); `SCKSource.updateCrop` moves it when it changes.
- A menu surface is: layer 101 with its top touching the menu bar, or Control Center at layer 22
  (cropped to its opaque bounds; its canvas is mostly transparent padding). Other surfaces are
  child windows already inside their window's capture. Surface IDs get reused, so discovery checks
  visibility and frame, not only new IDs.

## Helper HTTP API (authenticated as today)

- `POST /spatial/start` and `GET /spatial` return
  `{"state", "revision", "display": {"id", "bounds": [x, y, w, h], "scale": 2, "menuBarDisplay": id},
  "windows": [{"id", "pid", "app", "title", "bundleID", "x", "y", "w", "h"}],
  "skipped": [{"id", "reason"}], "menus": [{"slot", "id", "owner", "rect", "anchor"}],
  "focusedSrc"}`. `w`/`h` are the window's actual frame on the shared display; `skipped` lists
  windows that can't move (full screen, other Spaces).
- `POST /spatial/stop`: unchanged.
- `/ticket` and `/stream` accept the new keys; spatial admission is blocked outside Spatial Mode.
- `POST /focus?src=spatial:<wid>&u=&v=`: raise the window (AXRaise plus activating its app),
  restore its remembered cursor position, and move the cursor fence to it. Unknown source: 409.

## Focus and input

- When the native host's focus confirmation snaps onto a panel it does both: writes the existing
  request file (the companion still uses it for the live set), and sends WebSocket text
  `{"t":"focus","u":0.31,"v":0.12}` on that panel's stream. The helper's `Session` handles
  `"focus"` by raising, without waiting for the companion's poll. As a safety net a key-down or
  mouse-down on a `spatial:` session raises its window first if it isn't frontmost.
- Panel `(u, v)` maps to the window's current global frame on the shared display:
  `frame.origin + (u * w, v * h)`. Menu slots map through their surface `rect`, strips through
  their crop rects.
- Cursor fence (helper): an event tap on mouse-moved and dragged events keeps the Mac cursor
  inside the focused window's frame, the menu-bar rect and any visible menu surfaces, so the
  trackpad can't wander into the overlapping windows underneath. It re-enables itself after
  `tapDisabledByTimeout`. Windows sit right under the menu bar, so moving the cursor up out of a
  window reaches the menus.

## Surface messages (helper to native, WebSocket text on `menubar`, `status`, `menu:*`)

```json
{"t":"surface","v":1,"seq":7,"visible":true,"rect":[0,0,612,36],"anchor":"menubar",
 "anchorRect":[0,0,612,36],"menusWidth":612,"selectedRect":[100,0,40,36],"hot":true}
```

- `anchor` is `menubar`, `status` or `none`. `rect` and `anchorRect` are global points.
- `selectedRect`, on `menubar` only, is the selected app menu title's `[x,y,w,h]`
  in global points. It is absent when no app dropdown is open or no title can be
  identified. The helper chooses an intersecting `AXMenuBarItem` with `AXSelected`,
  falling back to the open dropdown's left edge matched to item frames. Status
  panels and context menus do not select a title. The host clips this rect to the
  strip picture and maps it at the picture's point scale plus its 6 mm inset;
  the pressed material follows the same curved region as the strip.
- `hot`: the Mac cursor is inside the menu-bar rect.
- The host shows a slot only after it has uploaded a picture that arrived after that `seq`, so a
  stale picture never flashes up.

## Native panels

- The panel spec gains `"role": "window" | "menubar" | "status" | "menu"` (default `window`); the
  launcher passes `--role ROLE`. Panel IDs: `menubar`, `status`, `menu0` to `menu3`.
- Non-window roles: placed by the host, no chrome, no handles, no saved pose; not focus targets,
  but they take gaze and laser hits (so looking at a menu doesn't move focus to the window behind).
- `menubar` (M1): a strip along the focused panel's top bezel. Slim by default (about 0.35 of the
  panel's width, 60% of the strip's height); it widens to the full `menusWidth` (at the window's
  metres per point) while `hot` is true or a menubar-anchored menu is open, and slims again 1 s
  after both clear.
- `status` (I1): at the right end of the dock shelf.
- `menu`: offset from its anchor by `(rect.minX - anchorRect.minX, rect.minY - anchorRect.maxY)`
  in metres per point, 1 cm in front. Menus anchored to `status` open above the shelf.

## Companion

- Spatial windows are keyed `spatial:<wid>` (Desktop's `separate:` stays as it is).
- Before reconciling windows, it launches the auxiliary panels (`menubar`, `status`, `menu:0..3`)
  with their roles, and stops them with the mode.
- The decoder budget counts auxiliary streams at their actual pixels; an idle menu slot with no
  picture costs nothing (today an unknown size reserves the whole budget).
- Auxiliary panels never appear in the switcher, dock, Last Session or profiles.
  `spatial-ui.json` and `scripts/workspace-spatial-on-frame.py` keep their format.
