# FW-06 native full-screen feasibility

**Result:** detection and a fresh capture of an already full-screen AppKit fixture are demonstrated. An existing production `SCKSource(.window)` stream terminated incorrectly during a later entry transition. Keep FW-07 gated until transition recovery and inactive-Space capture pass the app/lifecycle matrix.

## Test record

Host: M4 MacBook, macOS 26.3.1. The Mac-side native workspace was active at 2880×2560 (9:8). No Steam Frame spatial panel was used for these tests.

The diagnostic fixture was launched in a temporary app wrapper so CUA could target its standard macOS title bar. The probe did not move or resize its window. CUA clicked the native green full-screen control at 2026-10-18 16:16:45 UTC and used Escape to leave full screen at 16:19:12 UTC. The system title-bar controls then returned. These transitions also appear in the fixture's AppKit delegate log. The bundled app's Screen Capture and Accessibility preflights were false; the trusted external observer's were true. No TCC prompt or grant was requested.

The trusted external observer was run against the owned fixture by PID and `CGWindowID`:

```sh
scripts/fullscreen-probe.sh --pid 93217 --window-id 42121 --sample-content
```

An earlier fixture (PID 85501, window 42024) entered full screen at 15:58:03 UTC. A fresh trusted observer started at 16:00:10 UTC and ran for 600 seconds. Production capture stayed live and produced 594 one-second content samples, with changing content still present at the end and eight distinct sampled luma grids. This proves fresh capture after settled entry for this fixture, not continuity through a transition or capture from an inactive Space.

The later command was run a second time while window 42121 was already full screen, concurrently with the first observer from the same executable. That repeat failed; a similar same-executable ScreenCaptureKit interference was already observed in the Workspace capture probes. It cannot establish that full-screen windows are categorically uncapturable. The raw test logs were `/private/tmp/frame-workspace-fullscreen-observer.jsonl`, `observer2.jsonl`, `observer3.jsonl`, and `/private/tmp/frame-workspace-fullscreen-appkit-bundle.jsonl` (the abbreviated names share the `frame-workspace-fullscreen-` prefix). They contain fixture metadata, not image data or user-window inventories. This report preserves the results so generated logs can be removed.

| Scenario | Evidence | Result |
|---|---|---|
| Fresh capture after settled entry, first fixture | Observer 1 saw AX full screen true for window 42024, started production capture, and recorded 594 changing-content samples during a 600-second run without a capture failure. | Live full-screen capture demonstrated on one AppKit fixture. |
| Windowed capture before full screen | Observer 2 started at 16:16:32 UTC. Production `SCKSource(.window)` started; 147 frames arrived before the transition, with 10 one-second luma-grid samples and changing content. | Live capture demonstrated while windowed. |
| Native green-button entry | At 16:16:44–45 UTC, AppKit reported full screen and public `NSWorkspace.activeSpaceDidChangeNotification` fired. The dynamic AX `AXFullScreen` query changed false → true. PID/frame mapping retained `CGWindowID` 42121. | Entry detection demonstrated on the owned AppKit fixture. |
| Existing stream at entry | The source emitted `capture_ended: the window closed` at 16:16:45 UTC. No later frame or content samples arrived; the count remained 147. `SCShareableContent` still listed the same window ID at the transition. | Current source does not resume. The single missing `WindowInfo.find` result is consistent with a transient Space-transition gap, not proof that the window closed. |
| Fresh capture while full screen, concurrent repeat | Observer 3 started at 16:18:49 UTC with the same ID and observed AX full screen true. At 16:18:50 UTC it reported an interrupted application connection and produced no frames. Observer 2 from the same executable was still running. | Failed repeat with a known harness interference risk; does not negate the first successful capture. |
| Native exit | Escape left full screen at 16:19:12 UTC; AX changed true → false and `activeSpaceDidChange` fired. The immediate `WindowInfo` result was absent, while a `SCShareableContent` query two seconds later still listed ID 42121. Neither observer restarted capture. | Window identity survived the transition, but capture recovery is unproven. |
| Separate Space away/back | Calculator activation and Ctrl+Left did not produce another `activeSpaceDidChange` event. | Not tested successfully; do not infer inactive-Space capture. |
| Minimize, close, Stage Manager, two full-screen apps, Safari/Chrome | Not covered by this initial fixture run. The Mac locked during the first session and was subsequently unlocked by the user for further tests. | Untested in this record. |

## API and source findings

The probe used public `AXObserver` registration, `AXUIElementCopyAttributeValue`, `NSWorkspace.activeSpaceDidChangeNotification`, `CGWindowList`, and ScreenCaptureKit. Observer 2 registered 13 of 13 notifications successfully; observer 3 registered 18 of 18. The accepted notifications included window create/focus/main, move/resize/destroy, minimize, and restore. AX state was obtained by querying the dynamic string `AXFullScreen`; it is not a public AX attribute constant, and the public notification set has no dedicated full-screen-change notification. No private Spaces or `_AXUIElementGetWindow` API was used.

Production `Capture.swift` creates a `SCContentFilter(desktopIndependentWindow:)` from the initial `SCWindow` ([lines 158–169](../../mac/frame-mac-view/Sources/Capture.swift)). Its one-second window poll terminates the source on one missing `WindowInfo.find` result ([lines 233–244](../../mac/frame-mac-view/Sources/Capture.swift)); the size/title path updates configuration but does not rebuild the filter ([lines 245–256](../../mac/frame-mac-view/Sources/Capture.swift)). A stream error is reported without restart ([lines 306–308](../../mac/frame-mac-view/Sources/Capture.swift)).

The logs show that a stable CGWindowID and a matching `SCWindow` can remain available while a stream has ended. Fresh capture of the first settled full-screen window worked. A single `WindowInfo.find` miss should not permanently mark the window gone without confirming absence through settled/repeated enumeration. If capture itself stops, a later experiment can re-enumerate `SCShareableContent` with `onScreenWindowsOnly: false` and rebuild the filter from a fresh `SCWindow`. Recovery still needs changing-content validation across entry, an actual Space away/back, and exit, with only one observer per executable.

| Gate | Status |
|---|---|
| FW-06 native entry detection on AppKit fixture | Demonstrated |
| FW-06 fresh capture after settled full-screen entry on AppKit fixture | Demonstrated once; concurrent repeat failed |
| FW-06 Path A continuity through transitions and inactive Space | Not demonstrated; recovery verification required |
| FW-06 app matrix and lifecycle matrix | Incomplete |
| FW-07 automatic detachment | Gated; do not implement or claim supported behavior yet |
