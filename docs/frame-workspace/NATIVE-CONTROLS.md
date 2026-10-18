# Native placement control review

[Review the two variations](evidence/native-controls-mockup.png) ([editable SVG](evidence/native-controls-mockup.svg)).

Both reuse the approved Workspace row, existing button/field styling and current
Show again behavior. Width and distance would call the native owner's existing
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

This is a mockup, with reusable header, button, field and adjustment components
defined in the SVG. The production interface has not been constructed. The
user-provided working agreement requires mockup and asset approval before new UI
construction. Panel cycling remains separate capability work.

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
retain their existing dashboard behavior. New placement UI remains pending
mockup approval.
