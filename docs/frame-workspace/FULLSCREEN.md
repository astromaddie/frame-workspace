# FW-06 native full-screen feasibility

**Result:** detection, fresh capture after settled entry, and continuous capture
through entry/exit are demonstrated on one owned AppKit fixture. A confirmed
transient window-list omission previously ended the source; the close-recovery
policy fixes that observed failure. Keep FW-07 gated until inactive-Space capture
and the broader app/lifecycle matrix pass.

## Repaired production path

`SCKSource` now checks fresh ScreenCaptureKit content and a second window-list
lookup before declaring a window closed. Both checks preserve the original window
ID and known owner PID. Five seconds of confirmed absence are required; a live
match resets that grace. Asynchronous confirmations are single-flight, time out
for retry, and are invalidated by Stop. Pure production-policy tests cover
transient reappearance, bounded closure, mismatched identities and cancellation.

The repaired production source was then compiled into the diagnostic observer.
In one 240-second run, CUA clicked the fixture's native green button at
16:37:40 UTC and pressed Escape to leave at 16:39:52 UTC. The same window ID
survived. WindowInfo was absent at both Space notifications, yet capture stayed
live: **129 changing-content samples while full screen and 96 after exit**,
ending at **3,374 frames**, without capture failure or termination. The stream
updated its dimensions on entry and exit. This directly exercises the original
failure path; it does not prove capture from an inactive Space. See the compact
[transition evidence](evidence/fullscreen-recovery.json).

A separate live capture survived a brief native minimize/restore and resumed
changing content afterward. Closing that owned fixture ended capture about one
second later. CUA's state refresh restored the minimized window after about one
second, so sustained minimization is still untested. The initial diagnostic did
not stop its source on the first terminal callback and logged a later duplicate
close confirmation; it now mirrors the production consumer's cleanup. See the
[lifecycle record](evidence/fullscreen-lifecycle.json).

## Inactive-Space follow-up

A further 420-second run used the fixture's own production capture with existing
Screen Capture and Accessibility preflights. Native green-button entry again
retained the same window ID and continuous changing content. The diagnostic now
reports whether its target is the frontmost application, so an accidental
reactivation during automation can be distinguished from an inactive-Space test.

Computer-use targeting of macOS Space controls timed out before an away/back
transition was established. Only the full-screen entry Space notification was
recorded, and the fixture remained frontmost. This is another successful settled
full-screen capture, **not inactive-Space evidence** and not a capture failure.
The [compact attempt record](evidence/fullscreen-inactive-attempt.json) preserves
source/binary identity, counts, limits and reproduction details. FW-07 remains
gated; no private Spaces API or new permission grant was used.

## Earlier test record

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
| Historical pre-fix stream at entry | The source emitted `capture_ended: the window closed` at 16:16:45 UTC. No later frame or content samples arrived; the count remained 147. `SCShareableContent` still listed the same window ID at the transition. | The pre-fix source did not resume. The single missing `WindowInfo.find` result is consistent with a transient Space-transition gap, not proof that the window closed. |
| Fresh capture while full screen, concurrent repeat | Observer 3 started at 16:18:49 UTC with the same ID and observed AX full screen true. At 16:18:50 UTC it reported an interrupted application connection and produced no frames. Observer 2 from the same executable was still running. | Failed repeat with a known harness interference risk; does not negate the first successful capture. |
| Native exit | Escape left full screen at 16:19:12 UTC; AX changed true → false and `activeSpaceDidChange` fired. The immediate `WindowInfo` result was absent, while a `SCShareableContent` query two seconds later still listed ID 42121. Neither observer restarted capture. | Window identity survived the transition, but this initial pre-fix run did not establish recovery (the later repaired run above did). |
| Separate Space away/back | Calculator activation and Ctrl+Left did not produce another `activeSpaceDidChange` event. | Not tested successfully; do not infer inactive-Space capture. |
| Minimize, close, Stage Manager, two full-screen apps, Safari/Chrome | Not covered by this initial fixture run. The Mac locked during the first session and was subsequently unlocked by the user for further tests. | Untested in this record. |

## API and source findings

The probe used public `AXObserver` registration, `AXUIElementCopyAttributeValue`, `NSWorkspace.activeSpaceDidChangeNotification`, `CGWindowList`, and ScreenCaptureKit. Observer 2 registered 13 of 13 notifications successfully; observer 3 registered 18 of 18. The accepted notifications included window create/focus/main, move/resize/destroy, minimize, and restore. AX state was obtained by querying the dynamic string `AXFullScreen`; it is not a public AX attribute constant, and the public notification set has no dedicated full-screen-change notification. No private Spaces or `_AXUIElementGetWindow` API was used.

Production [Capture.swift](../../mac/frame-mac-view/Sources/Capture.swift)
creates a `SCContentFilter(desktopIndependentWindow:)` from the initial `SCWindow`.
The original one-second poll terminated on a single missing window-list result;
the repaired path uses the confirmation policy above. Size/title changes update
the configuration without rebuilding the filter. A genuine stream error is still
reported without automatic restart.

The logs show that a stable CGWindowID and a matching `SCWindow` can remain available while a stream has ended. Fresh capture of the first settled full-screen window worked. A single `WindowInfo.find` miss should not permanently mark the window gone without confirming absence through settled/repeated enumeration. If capture itself stops, a later experiment can re-enumerate `SCShareableContent` with `onScreenWindowsOnly: false` and rebuild the filter from a fresh `SCWindow`. Recovery still needs changing-content validation across entry, an actual Space away/back, and exit, with only one observer per executable.

| Gate | Status |
|---|---|
| FW-06 native entry detection on AppKit fixture | Demonstrated |
| FW-06 fresh capture after settled full-screen entry on AppKit fixture | Demonstrated once; concurrent repeat failed |
| FW-06 Path A continuity through entry and exit on AppKit fixture | Demonstrated after close-recovery repair |
| FW-06 Path A live capture from inactive Space | Not demonstrated |
| FW-06 app matrix and lifecycle matrix | Incomplete |
| FW-07 automatic detachment | Gated; do not implement or claim supported behavior yet |
