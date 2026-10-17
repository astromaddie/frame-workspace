# Frame Workspace — issue specifications

**Publication status (2026-10-18):** Saved in this repository. GitHub Issues are disabled; publish these 13 issues once the repository owner enables them in Settings → General → Features → Issues. Keep FW identifiers stable when GitHub assigns issue numbers.

---

# [FW-01] Establish the fork, upstream baseline, and scope boundaries

**Tracking ID:** FW-01  
**Priority:** P0  
**Phase:** M0 — Groundwork  
**Dependencies:** None

## Goal
Create a maintainable Frame Workspace branch of Frame Control without rewriting its functioning Frame management and Mac streaming features.

## Implementation / investigation
- Confirm the existing fork from `saphid/frame-control` retains original Git history and MIT attribution (already verified on 2026-10-18).
- Record upstream baseline commit `bcdd9d0830673ef53c6be9987a429dc43bef77e5` in `docs/frame-workspace/UPSTREAM.md`; add upstream remote instructions and conflict-avoidance rules.
- Keep the pre-existing ScreenCaptureKit + VideoToolbox + SSH + Chromium panel flow intact. Start the new workstation UX as a distinct, feature-gated module rather than stripping existing features.
- Identify/disable any inherited release publishing or update endpoints before making independent Frame Workspace releases; do not impersonate upstream artifacts.

## Acceptance criteria
- [x] Fork contains upstream Git history and original MIT license.
- [ ] Existing upstream tests pass unchanged; no rebrand or feature deletion is part of this task.
- [ ] A developer can synchronize upstream main using documented commands.
- [ ] README distinguishes upstream attribution from Frame Workspace-specific features.

## Risks / non-goals
- Do not add any CI workflow or release that publishes under the upstream author’s name.

## Verified upstream references
- [readme](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/README.md)
- [license](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/LICENSE)
- [python](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/ui/frame_macview.py)
- [tests](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/tests/test_macview.py)


---

# [FW-02] Benchmark current Mac → Frame streaming and coding readability

**Tracking ID:** FW-02  
**Priority:** P0  
**Phase:** M0 — Device proof  
**Dependencies:** FW-01

## Goal
Establish whether the existing implementation is good enough for sustained coding on an M4 MacBook before optimizing the wrong bottleneck.

## Implementation / investigation
- On the actual M4 MacBook and Steam Frame, test Frame Control → Tools → Mac in headset → Show for a display, ordinary window, and “Give each window its own display.”
- Measure Balanced (1920 long side) and Sharp (2560 long side), Wi-Fi and USB-C, with a worn headset. Use `scripts/macview-bench.py` and `/stats`; report capture→draw and input→draw p50/p95, observed fps and late frames.
- Measure practical readability at several coding font sizes; compare macOS native screen real estate with the headset’s usable apparent resolution, not just virtual display dimensions.
- Test keyboard/trackpad focus, Japanese IME, pointer accuracy, scroll, display sleep and 30-minute stability. Document OS/Frame firmware/headset optics settings.

## Acceptance criteria
- [ ] Results table includes M4 model, macOS/SteamOS versions, network path, quality settings, and measured data.
- [ ] A 30-minute real coding task establishes whether text is comfortably readable and identifies failure modes.
- [ ] Screenshots and timings distinguish rendered-pixel resolution from logical macOS display resolution.
- [ ] A reproducible benchmark script/config is committed with no private user data.

## Risks / non-goals
- Do not equate upstream measurements from its M5 Pro test machine with the user’s M4 or claim physical photon latency from unworn-headset benchmarks.

## Verified upstream references
- [stream](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/docs/mac-in-headset.md)
- [separation](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/docs/mac-window-separation.md)
- [python](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/ui/frame_macview.py)
- [tests](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/tests/test_macview.py)


---

# [FW-03] Spike: independent HiDPI ultrawide macOS virtual display

**Tracking ID:** FW-03  
**Priority:** P0  
**Phase:** M0 — Feasibility gate  
**Dependencies:** FW-01, FW-02

## Goal
Prove that a large, independently addressable Mac workspace can exist without a physical second monitor and be captured reliably.

## Implementation / investigation
- Reuse the existing `VirtualDisplay` implementation (`Separate.swift`), but prototype a persistent *workspace display*, separate from per-window “Separate” mode.
- Test 3440×1440 and 5120×1440 logical configurations where supported, macOS scaling/HiDPI enumeration, primary/secondary Spaces behavior and window placement.
- Capture via the existing ScreenCaptureKit display pipeline, measure VideoToolbox performance and Frame decode at multiple output sizes; explicitly account for the current 2560-pixel “Sharp” max long edge.
- Probe reconnect, display removal while asleep, wake, clamshell, Stage Manager, and accidental Mac window displacement. Compare a public-API / BetterDisplay fallback, but do not require a purchased dependency.

## Acceptance criteria
- [ ] An M4 creates and captures at least one usable virtual workspace display, or the issue records an explicit no-go result.
- [ ] Virtual monitor teardown leaves no stranded Windows or phantom displays following normal stop and crash recovery.
- [ ] Document raw logical resolution, video stream resolution, render resolution, and achieved legibility separately.
- [ ] An engineering decision identifies single-stream, tiled-stream or independent-window panels as the MVP approach.

## Risks / non-goals
- `CGVirtualDisplay` is a private Apple API already in upstream. It is unsupported and may regress with macOS upgrades.
- Do not claim a 5120-wide macOS desktop will look 5120 pixels sharp in the Frame without proving the entire rendering pipeline.

## Verified upstream references
- [sepcode](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/mac/frame-mac-view/Sources/Separate.swift)
- [header](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/mac/frame-mac-view/Sources/CGVirtualDisplay.h)
- [capture](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/mac/frame-mac-view/Sources/Capture.swift)
- [python](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/ui/frame_macview.py)
- [separation](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/docs/mac-window-separation.md)


---

# [FW-04] Implement lifecycle-managed virtual ultrawide Workspace mode

**Tracking ID:** FW-04  
**Priority:** P1  
**Phase:** M1 — Workspace MVP  
**Dependencies:** FW-03

## Goal
Give the user one large *real macOS desktop*, independent of the laptop’s built-in display, that can be used in the headset.

## Implementation / investigation
- Promote the FW-03 proof into a `WorkspaceDisplayManager` that creates and owns one HiDPI display, exposes stable workspace source IDs, and chooses safe defaults based on tested pixel density.
- Expose Start Workspace / Stop Workspace from the Mac companion; capture the virtual display using the existing agent transport and SteamVR panel route.
- Define explicit behavior for moving windows onto the workspace, default display choice, macOS menu bar and Dock, keyboard focus, sleep and reconnect. Preserve physical-display layout.
- On stop, restore window positions/display modes where possible, and detect/recover orphaned virtual displays after abnormal termination.

## Acceptance criteria
- [ ] Start launches a second macOS desktop into the Frame without moving or mirroring the MacBook’s built-in desktop by default.
- [ ] Stop restores the Mac to its earlier layout, including when started/stopped repeatedly.
- [ ] No Display/Window source IDs or frame-stream credentials are persisted as durable identity.
- [ ] At least one real coding session works with Mac keyboard and mouse while using the virtual display.

## Risks / non-goals
- MVP may expose “single large panel”; fine-grained VR geometry can be a later task if it needs a different overlay renderer.

## Verified upstream references
- [sepcode](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/mac/frame-mac-view/Sources/Separate.swift)
- [capture](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/mac/frame-mac-view/Sources/Capture.swift)
- [agent](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/mac/frame-mac-view/Sources/main.swift)
- [python](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/ui/frame_macview.py)
- [separation](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/docs/mac-window-separation.md)


---

# [FW-05] Preserve readable text on ultrawide: evaluate tiled or viewport streaming

**Tracking ID:** FW-05  
**Priority:** P0  
**Phase:** M1 — Workspace MVP  
**Dependencies:** FW-02, FW-03

## Goal
Avoid the fatal failure mode of a high-resolution ultrawide macOS desktop being squeezed into one low-detail video panel.

## Implementation / investigation
- Compare a single stream at native/near-native viewport resolution with 2–3 independently captured crops from a virtual display using ScreenCaptureKit `sourceRect`.
- If tiles are preferable, prototype a `workspace-tile` source with crop bounds and map pointer coordinates accurately back into global macOS display coordinates; preserve a continuous logical desktop.
- Render adjacent panels with consistent perspective, avoid visible seams and accidental independent scroll. Evaluate using SteamVR’s built-in Toggle Curvature first.
- Measure VideoToolbox encoder contention and Chromium decoding load under typing, scrolling, and largely static text; consider high-quality static refresh if appropriate.

## Acceptance criteria
- [ ] Code and logs show all streamed pixels and crops match their macOS coordinates without repeated or missing desktop regions.
- [ ] Code fonts remain comfortably legible with more usable horizontal workspace than the laptop display, verified on a worn Frame.
- [ ] Pointer, drag, selection, and scroll cross crop boundaries without coordinate errors.
- [ ] A documented decision explains whether ultrawide uses single-surface or tiled panels and why.

## Risks / non-goals
- SteamVR/dashboard-owned panels are not known to allow arbitrary programmable curved placement; prove layout controls before promising perfect tile alignment.

## Verified upstream references
- [python](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/ui/frame_macview.py)
- [capture](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/mac/frame-mac-view/Sources/Capture.swift)
- [viewer](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/ui/mac-view.html)
- [stream](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/docs/mac-in-headset.md)
- [panels](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/docs/panels.md)


---

# [FW-06] Spike: detect native macOS full-screen Spaces reliably

**Tracking ID:** FW-06  
**Priority:** P0  
**Phase:** M0 — Feasibility gate  
**Dependencies:** FW-02

## Goal
Determine how to respond when the user clicks an app’s normal green macOS Full Screen control (not a custom Frame Workspace shortcut).

## Implementation / investigation
- Instrument `AXFullScreen` transitions, app/window notifications via Accessibility, `NSWorkspace.activeSpaceDidChangeNotification`, and `CGWindowID` mapping. Avoid private Spaces manipulation unless separately reviewed.
- On Safari, Chrome, VS Code/Electron and a simple AppKit app, observe which windows/IDs/AX elements survive transitions, including inactive Spaces.
- Test Path A: capture the actual native full-screen app/Space to its own VR panel without moving it. Test Path B: relocate the app to its own virtual display and retain native full-screen semantics on that display.
- Measure detection delay, Space switching, wake/close/exit transitions; verify what happens to background windows and Mac keyboard focus.

## Acceptance criteria
- [ ] A state transition log proves a genuine click on macOS’s native green button is detected, without requiring a custom shortcut.
- [ ] For each tested app, record whether A or B captures a usable *live* full-screen surface after switching to another Space.
- [ ] Explicitly test entry, exit, window close, minimize, Space switch, Stage Manager, and two simultaneous full-screen apps.
- [ ] A go/no-go decision states which behaviors can be supported without private or destructive OS interference.

## Risks / non-goals
- Today `SeparateSource.start()` returns “take the window out of full screen first”; current implementation does not meet this requirement.
- Do not promise interception/replacement of the system’s green button. Observe the OS transition and respond only when proof establishes this is possible.

## Verified upstream references
- [sepcode](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/mac/frame-mac-view/Sources/Separate.swift)
- [capture](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/mac/frame-mac-view/Sources/Capture.swift)
- [separation](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/docs/mac-window-separation.md)
- [agent](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/mac/frame-mac-view/Sources/main.swift)


---

# [FW-07] Automatic spatial detachment on native full-screen transitions

**Tracking ID:** FW-07  
**Priority:** P1  
**Phase:** M2 — Native full-screen UX  
**Dependencies:** FW-06, FW-04

## Goal
During an active Frame Workspace session, clicking the ordinary macOS green Full Screen button causes that app to appear as a distinct Steam Frame panel automatically.

## Implementation / investigation
- Implement an event-driven full-screen lifecycle coordinator from the verified FW-06 strategy; do not require selection from a dropdown or a separate “Spatial Full Screen” command.
- Map app identity and window/Space transitions to an existing or new `separate` / `display` capture source; avoid panel duplicates, loops and racing OS animations.
- Preserve the native button’s effect and existing macOS Space semantics as far as the chosen strategy allows; announce unsupported apps rather than silently overriding them.
- On exit full-screen, close/reintegrate the detached panel according to a documented user preference and restore original window focus/placement.

## Acceptance criteria
- [ ] Green-button entry and exit are automatic for the FW-06 supported-app matrix while a Workspace session is active.
- [ ] No action occurs when Frame Workspace is inactive; other displays and ordinary Mac use remain untouched.
- [ ] Repeated full-screen toggling produces no duplicate streams, orphan displays, flicker loops or input confusion.
- [ ] Unsupported application classes fail predictably without moving, closing or losing an app window.

## Risks / non-goals
- This issue is gated. If FW-06 cannot prove a reliable approach, report the limitation instead of silently substituting a custom full-screen command.

## Verified upstream references
- [sepcode](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/mac/frame-mac-view/Sources/Separate.swift)
- [capture](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/mac/frame-mac-view/Sources/Capture.swift)
- [agent](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/mac/frame-mac-view/Sources/main.swift)
- [python](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/ui/frame_macview.py)
- [separation](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/docs/mac-window-separation.md)


---

# [FW-08] Frame-side curved workspace and low-friction controls

**Tracking ID:** FW-08  
**Priority:** P1  
**Phase:** M1 — Workspace MVP  
**Dependencies:** FW-04, FW-05

## Goal
Make the Mac workspace pleasant to use in the Steam Frame rather than exposing only raw streaming/debugging controls.

## Implementation / investigation
- Build a Frame-native control surface using the existing gamescope / Chromium / SteamVR panel infrastructure and panel switcher.
- For MVP, expose workspace start/show, focus, move/resize, Toggle Curvature (through Valve’s existing dashboard affordance), recenter instructions, and a quick return to SteamVR.
- Add a simple Mac menu-bar control and sane reconnect behavior; avoid requiring controllers for routine keyboard-first coding.
- When tiled view is selected, expose grouping and alignment that is truly supported; do not claim automatic placement until verified.

## Acceptance criteria
- [ ] A first-time user can start the workspace on the Mac and locate it in the headset with documented steps.
- [ ] Sizing and curvature operate through available SteamVR features without a separate VR game.
- [ ] Controls do not steal typing focus or require a physical headset controller for everyday app switching.
- [ ] UX explains unsupported automatic placement, where applicable.

## Risks / non-goals
- Current SteamVR dashboard owns overlay layout. Do not assume there is a supported API to programmatically recenter or curve arbitrary dashboard panels.

## Verified upstream references
- [panels](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/docs/panels.md)
- [panels_py](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/ui/frame_panels.py)
- [python](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/ui/frame_macview.py)
- [viewer](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/ui/mac-view.html)


---

# [FW-09] Prioritize active panel and benchmark multi-stream performance

**Tracking ID:** FW-09  
**Priority:** P1  
**Phase:** M2 — Quality and efficiency  
**Dependencies:** FW-02, FW-05

## Goal
Prevent three or more floating coding windows from needlessly competing for the Mac hardware encoder and the Frame decoder.

## Implementation / investigation
- Measure one, two, four and six simultaneous streams on the actual M4 and Frame: typing, scrolling, static code, active/inactive focus, Wi-Fi and USB-C.
- Extend the existing `RateController` policy to consider focus/visibility where reliably observable; lower background fps/resolution before penalizing foreground responsiveness.
- Evaluate sharp static text refresh (higher-quality keyframe or still overlay) only if evidence shows a real improvement at reasonable decode cost.
- Guard against too many large streams exhausting encoder sessions, memory, bandwidth or the SSH tunnel.

## Acceptance criteria
- [ ] Foreground streaming meets or materially improves the FW-02 established latency/legibility targets with at least three open panels.
- [ ] Background windows use measurably fewer resources without losing correctness or starving on focus change.
- [ ] Quality adaptations are reported with p50/p95, fps, encode/decode time, network throughput and failure incidence.
- [ ] Existing one-stream regression tests and quality presets continue to work.

## Risks / non-goals
- Upstream’s 2026-09-28 four-window tests showed increased hardware encoder latency. Do not rely on bandwidth alone as a solution.

## Verified upstream references
- [stream](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/docs/mac-in-headset.md)
- [python](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/ui/frame_macview.py)
- [tests](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/tests/test_macview.py)


---

# [FW-10] Spike: owned VR overlays and saved spatial transform support

**Tracking ID:** FW-10  
**Priority:** P0  
**Phase:** M0 — Feasibility gate  
**Dependencies:** FW-02

## Goal
Find a supported way to record and restore actual VR panel locations, or define a useful degradation if the platform prevents it.

## Implementation / investigation
- Reproduce upstream’s `SetOverlayTransformAbsolute` → PermissionDenied on the dashboard-owned `valve.steam.desktopgame.*` overlays.
- Prototype minimal Frame Workspace-owned OpenVR overlay or OpenXR quad-layer rendering, with visible placement, focus/input, transform read/write, and world locking on the Steam Frame.
- Test against SteamVR dashboard visibility, app multitasking, runtime reload and Steam game launch; avoid attempting to bypass Valve’s protected overlay ownership.
- Document whether curvature, multiple panels, and persisted transforms work in the alternate renderer, and which capabilities would be lost.

## Acceptance criteria
- [ ] Source-and-device-backed go/no-go table identifies whether independent overlay ownership can save/reload poses.
- [ ] A minimal proof restores a panel to the same world transform after its process restarts, or an explicit blocker is documented.
- [ ] No unsupported system compositor patches, privilege escalations or repeated focus-stealing workarounds are used.
- [ ] The roadmap clearly separates logical session restore from spatial pose restore.

## Risks / non-goals
- Upstream measured SteamVR error 12 (`PermissionDenied`) for setting transforms on gamescope-owned dashboard panels; saved spatial layouts must not be promised until this gate passes.

## Verified upstream references
- [panels](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/docs/panels.md)
- [panels_py](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/ui/frame_panels.py)


---

# [FW-11] Saved logical workspace profiles and conditional spatial layouts

**Tracking ID:** FW-11  
**Priority:** P2  
**Phase:** M3 — Session polish  
**Dependencies:** FW-04, FW-08, FW-10

## Goal
Restart a familiar work setup with named ultrawide and floating-window profiles, using spatial pose restoration only if FW-10 proves it.

## Implementation / investigation
- Save user-facing profiles for virtual screen resolution, named apps/window matching rules, panel sources and view preferences, but never transient window IDs, stream tickets or reconnect tokens.
- On reconnect or relaunch, enumerate fresh apps and windows, reacquire permissions, reopen captures and map the logical session to new window IDs.
- If FW-10 proves an owned overlay route, persist transform metadata against stable source rules; otherwise explicitly label placement as manual.
- Provide restore diagnostics, a safe Reset Workspace, and recovery from missing/renamed applications.

## Acceptance criteria
- [ ] A named “Coding” workspace can be stopped, restarted and recovered after a normal Frame reconnect without replaying stale credentials.
- [ ] Missing or retitled applications are skipped with actionable feedback; unrelated Mac windows are not rearranged.
- [ ] Spatial poses are restored only behind a proven implementation; otherwise users are not misled about saved positions.
- [ ] App/project settings are stored locally, documented, and versioned for future schema migration.

## Risks / non-goals
- Preserving source selections is not the same thing as persisting a compositor-owned room layout.

## Verified upstream references
- [panels](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/docs/panels.md)
- [python](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/ui/frame_macview.py)
- [sepcode](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/mac/frame-mac-view/Sources/Separate.swift)


---

# [FW-12] Keyboard, pointer, focus and multiple macOS Spaces correctness

**Tracking ID:** FW-12  
**Priority:** P1  
**Phase:** M2 — Interaction quality  
**Dependencies:** FW-04, FW-06

## Goal
Make the M4 MacBook’s real keyboard and trackpad the primary, reliable input devices when several VR Mac panels are open.

## Implementation / investigation
- Audit `CGEvent` coordinates and `AXUIElement` focus/raise behavior across plain window, separated virtual display, ultrawide crop and native full-screen Space sources.
- Verify clicking VR panels establishes input focus without unintentionally activating a different macOS Space or moving the visible Mac pointer to an unrelated surface.
- Test IME composition (Japanese and English), key modifiers, app switching, selection drag, scroll inertia, popovers, system dialogs and secure input.
- Add source-aware pointer mapping and fallback UI where macOS denies synthetic input.

## Acceptance criteria
- [ ] No reversed/misaligned clicks between physical display, virtual display and cropped VR panel.
- [ ] The Mac keyboard types into the most recently selected supported VR window, and a focus change is visible and predictable.
- [ ] Japanese IME and common keyboard shortcuts have documented behavior; secure-input restrictions are not bypassed.
- [ ] Automated coordinate tests plus a real-device multi-window checklist exist.

## Risks / non-goals
- macOS may intentionally restrict event injection into some secure fields or system UI.

## Verified upstream references
- [separation](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/docs/mac-window-separation.md)
- [capture](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/mac/frame-mac-view/Sources/Capture.swift)
- [sepcode](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/mac/frame-mac-view/Sources/Separate.swift)
- [viewer](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/ui/mac-view.html)


---

# [FW-13] Local-first security, failure recovery and end-to-end regression

**Tracking ID:** FW-13  
**Priority:** P1  
**Phase:** M3 — Reliability / ship gate  
**Dependencies:** FW-04, FW-07, FW-08, FW-09, FW-11, FW-12

## Goal
Ship a workstation tool that remains private and restores macOS reliably even when the headset, network or helper dies.

## Implementation / investigation
- Preserve upstream localhost-only Mac agent, SSH reverse tunnel, short-lived single-use viewer tickets, and explicit Screen Recording + Accessibility permissions.
- No internet service or account is needed for local use; document telemetry/update behavior inherited from Frame Control and default workspace-specific diagnostics to local only.
- Add fault tests for connection loss, USB-C unplugged midstream, Mac sleep and lid closure, app crash, Screen Recording/Accessibility revocation, Stage Manager, missing browsers, and SteamVR restart.
- Build on existing `tests/test_macview.py` and `scripts/macview-bench.py`; document a clean installer and rollback path.

## Acceptance criteria
- [ ] Sensitive screen capture and input remain inaccessible without the existing keys/permissions; tickets/tokens are not written to logs or settings.
- [ ] Every failure restores or safely recovers windows and owned virtual displays, without stranding the user on an invisible macOS desktop.
- [ ] 30-minute coding smoke test and resilience matrix pass on the user’s M4/Frame; known failures are documented.
- [ ] All inherited tests pass and feature flags allow disabling Workspace functionality without breaking Frame Control.

## Risks / non-goals
- Permission or ownership constraints should fail closed; do not introduce localhost API routes callable without existing auth.

## Verified upstream references
- [stream](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/docs/mac-in-headset.md)
- [separation](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/docs/mac-window-separation.md)
- [python](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/ui/frame_macview.py)
- [tests](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/tests/test_macview.py)
- [panels](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/docs/panels.md)