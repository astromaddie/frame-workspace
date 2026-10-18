# Native placement control review

[Review the two variations](evidence/native-controls-mockup.png) ([editable SVG](evidence/native-controls-mockup.svg)).

Both reuse the approved Workspace row, existing button/field styling and current
Show again behavior. Width and distance call the native owner's existing
control path, update without restarting the Mac display, and retain its validated
local pose persistence. No new image assets are required.

The authenticated companion backend now also has owner-scoped width/distance
updates and a curvature capability request with OpenVR readback. It leaves the
current curvature unchanged at startup and reports unsupported or unconfirmed
results without stopping the video owner. The real Frame accepted curvature
0.25 and read it back exactly with OpenVR error 0; the test restored 0.0 afterward. Width 2.8 m and distance 2.2 m were
also applied without changing the Mac display or native owner process. Rotation
was unchanged, translation followed the surface normal, and restoring 2.4 m /
2.0 m recovered the original matrix exactly. These are API/readback checks;
curved appearance and typing comfort still need worn acceptance. See
[device evidence](evidence/native-controls-live.json).

- **A: inline.** Width and distance remain beside Stop Workspace.
- **B: expandable.** A Placement button reveals the same compact adjustment row.

The user approved **A: inline** on 2026-10-19. The production interface now uses
the existing button styling in a compact wrapping row beside Stop Workspace.
No new assets were needed. Panel cycling remains separate capability work.

## Available backend controls

Use the companion's authenticated `POST /api/macview` route:

```json
{"action":"workspace_adjust","width":2.4,"distance":2.0}
{"action":"workspace_curvature","curvature":0.25}
```

Width is 0.4–8 m, distance 0.4–10 m, and curvature 0–1. Placement updates
preserve orientation and do not request recenter or typing focus. Curvature
requires a successful OpenVR readback; unsupported results leave video running.
Curvature is not persisted or changed on startup. The existing panel switcher
also lists the exact owned native Workspace and selects it through the owner;
selecting it follows Show again and explicitly recenters it. Other overlays
retain their existing dashboard behavior. The inline placement UI uses 0.2 m steps, clamps to those limits, and shows only
confirmed owner values. Pending updates disable conflicting Workspace actions;
failures show an inline error and clear unconfirmed values. Stopped, browser-owned,
and unready native owners expose no dimensions and cannot be adjusted. Normal
Workspace refresh reads the reserved owner through `frame_panels.py --native-owner`;
it does not poll per video frame. Width/distance changes do not request focus,
recenter, display recreation, or viewer restart.


## Inline control verification (local fixtures)

- `node tests/page/workspace_placement.mjs`: production renderer and event routing,
  confirmed-only values, increments/decrements, range clamping, busy/error handling,
  and stopped/unready owner gating passed.
- `python3 -m unittest discover -s tests -p test_macview.py`: 49 tests ran,
  one skipped; socket ResourceWarnings occurred.
- `test_panels.py`: 10 passed; `test_workspace_profiles.py`: 12 passed.
- Existing `desk_panels.mjs` and `macview_input.mjs` checks passed.
- `PLAYWRIGHT_MODULE=/path/to/playwright-core node tests/page/workspace_placement_browser.mjs`
  uses installed Google Chrome, production markup/styles/handlers, and isolated API
  fixtures. Actual increment/decrement clicks and no horizontal overflow passed
  at 1000 and 390 px. Reviewed captures:
  [desktop](evidence/native-controls-inline-1000.png),
  [narrow](evidence/native-controls-inline-390.png).

These fixture checks did not connect to the Frame or the running companion.
The temporary Playwright dependency and test bytecode caches were removed after
verification (13.7 MiB reclaimed).

## Production companion and Frame verification

After fresh off-head consent, the companion was restarted and the persistent
Coding profile restored at 2880×2560, 1×, Sharp. Actual clicks in the production
page increased width from 2.4 to 2.6 m and distance from 2.0 to 2.2 m. The native
readback confirmed both values while the same viewer process, Mac display and
orientation remained in place. Actual minus clicks restored 2.4 / 2.0 m and
the byte-exact original saved pose. The viewer remained connected with zero
errors. See [compact live evidence](evidence/native-controls-inline-live.json)
and [the activated controls](evidence/native-controls-inline-live.jpg).

These are off-head interaction/readback checks. Worn placement comfort and
typing acceptance remain open.
