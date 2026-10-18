# Frame Workspace validation — 2026-10-18

## Acceptance status

The first delivery scope is **FW-01 through FW-04**. FW-01 is complete. The Mac display, companion controls and off-head Frame transport now have device evidence. **FW-02 and FW-04 remain open for the user's worn readability, keyboard/trackpad/IME and 30-minute real coding session.** USB-C, physical lid closure and Stage Manager remain unverified. Do not describe the first four issues as fully accepted yet.

## Ultrawide viewer follow-up

The user's first worn trial confirmed the desktop worked, but reported substantial top/bottom letterboxing and insufficient magnification. After the viewer change below, the user reported it was **much more readable and clear**. This is worn readability feedback, not a completed 30-minute coding/IME acceptance or a sustained frame-rate measurement.

Workspace now launches an isolated **Gamescope OpenVR compositor and Chrome profile** in a transient `frame-workspace-viewer-<uid>.service`. Ordinary Mac window panels retain the inherited route. Chrome is fullscreen at the stream's aspect ratio; its initial physical overlay width is 5.34 m and SteamVR's placement controls remain available. No standalone app installer or startup service was installed.

| Wide desktop | Initial viewer | New Sharp viewer |
|---|---:|---:|
| macOS logical desktop | 5120×1440 | 5120×1440 |
| Decoded canvas | 2560×720 | 4096×1152 |
| Browser viewport, device scale 1 | 1920×1080 | 4096×1152 |
| Image inside browser | 1920×540, letterboxed | 4096×1152, no browser letterbox |
| Requested video rate | 60 fps | 30 fps |

Direct viewer inspection confirmed decoded frames, H.264 `avc1.640c32`, and zero decoder errors. Show again requested focus on the actual registered overlay key without changing display/stream identity. Stop removed the owned unit, overlays, browser processes and Mac virtual display; the original Gamescope PID remained running. The final normal launch omitted temporary browser diagnostics. Static-desktop FPS is not a throughput benchmark.

Two real limits were found and handled:

- Full-sized Gamescope output aborted at `uploadBufferData: size <= upload_buffer_size`. Valve's [OpenVR backend](https://github.com/ValveSoftware/gamescope/blob/master/src/Backends/OpenVRBackend.cpp) creates a black backing texture through a [fixed 1920×1080 RGBA staging buffer](https://github.com/ValveSoftware/gamescope/blob/master/src/rendervulkan.hpp). The launcher halves only the backing dimensions until they fit that capacity, retaining the full nested display and fullscreen Chrome surface. Forced window fullscreen is omitted because it shrinks the client to the backing size. The backend supports direct GPU layers, but its direct/composite choice was not instrumented; OpenVR's texture-size query returned 0×0 for GPU layers. Browser dimensions do not prove exact compositor texture dimensions or optical resolution. Exact correspondence between Valve master and Frame's packaged 3.16.30 source is unverified.
- This Mac's 5120×1440 H.264 encoder accepted submissions but returned no samples (`-19354`, then `-12915`). A fixture using production `Encoder.swift` produced 12/12 samples at 4096×1152@30, 4096×1728@30 and 4096×1152@60 within a three-second deadline. The 60 fps case is encoder compatibility only. Sharp therefore uses a 4096-pixel long edge; asynchronous encoder errors are now reported once per session rather than leaving a silent black viewer.

The launcher verifies nested window dimensions and overlay registration before reporting ready. Stop checks unit state and MainPID, with bounded TERM/KILL scoped to this service and a three-second timeout. A static service description and redacted diagnostics avoid logging viewer URLs. Explicit orphan recovery retains the live-owner lease check; unused Stop all does not start a display-recovery helper.

Checks: affected MacView suite passed 34 tests with one permission-dependent skip; subsequent lifecycle/idle-shutdown regressions, launcher checks and four benchmark tests passed. Swift build passed. Sustained 4096-wide streaming at 60 fps remains unverified.

## Initial environment and baseline method

M4 MacBook Pro Mac16,1, 10-core, 16 GB; macOS 26.3.1 (25D2128), arm64. Built-in desktop: **1800×1169 logical points / 3600×2338 backing pixels**. Steam Frame: SteamOS 0.5.6 build 20261015.6141207, SteamVR log version 2.18.2, Google Chrome user Flatpak 155.0.8059.84-1 (aarch64). Headset explicitly off-head and reserved with the shared device lock. The original installed Frame Control app and its Mac agent were preserved.

All runs below used LAN Wi-Fi, one viewer, 20-second measured scenarios after warm-up, no bandwidth/delay relay, and the inherited Balanced/Sharp quality definitions. The benchmark's 250 ms relay-buffer default is unused without a relay. USB `usb0` remained down; no cable result is claimed. Optical/IPD settings and perceived readability were not measured off-head.

Latency is clock-estimated **Mac composited frame → browser draw**, and **injected viewer event → first eligible drawn frame**. These are not physical input-to-photon measurements, and synthetic typing is not a real Mac keyboard/IME test. Sparse typing intentionally updates slowly, so its FPS and late percentage are ungraded. Individual Wi-Fi runs are not matched A/B comparisons.

The initial capture code/baseline was `b981bb3`; benchmark source was later committed as `70f75f4` (same patch as isolated-worktree commit `9ce9103`). Early JSON did not mark harness edits dirty; later runs include them. The lifecycle implementation accompanies this report. The benchmark now records logical/backing source metadata, uses a fresh removable Mac Chrome profile per scenario, selects the current Workspace source without falling back to a physical desktop, prevents overlapping benchmark invocations, samples the installed Flatpak viewer, and confirms browser cleanup before exiting.

## Existing transport baseline

Times are milliseconds, FPS is browser draw throughput, late is percent. All viewers reported **H.264 software decode**, ANGLE FD750 / OpenGL 4.6 and roughly 115–119 Hz browser RAF. That RAF does not establish optical presentation or worn frame rate.

| Source | Quality | Scenario | Encoded pixels | Content p50 / p95 | Input p50 / p95 | FPS | Late % |
|---|---|---|---|---:|---:|---:|---:|
| Separated window | Balanced | scroll | 1920x1290 | 45.4 / 110.9 | — | 47.4 | 18.77 |
| Separated window | Balanced | type | 1920x1290 | 22.1 / 110.2 | 91.2 / 240.6 | 3.2 | 98.55 |
| Separated window | Sharp | scroll | 2560x1720 | 43.9 / 108.0 | — | 56.0 | 9.73 |
| Separated window | Sharp | type | 2560x1720 | 25.4 / 98.2 | 72.6 / 151.8 | 3.2 | 97.1 |
| Ordinary window | Balanced | scroll | 1920x1230 | 44.7 / 118.5 | — | 45.4 | 19.87 |
| Ordinary window | Balanced | type | 1920x1230 | 18.4 / 89.9 | 66.8 / 138.9 | 4.2 | 84.27 |
| Ordinary window | Sharp | scroll | 2560x1640 | 44.5 / 109.1 | — | 55.8 | 9.66 |
| Ordinary window | Sharp | type | 2560x1640 | 24.8 / 86.2 | 62.1 / 127.4 | 4.0 | 89.53 |
| Built-in display | Balanced | scroll | 1920x1246 | 38.3 / 111.4 | — | 41.2 | 30.97 |
| Built-in display | Balanced | type | 1920x1246 | 22.5 / 76.0 | 55.3 / 139.7 | 3.7 | 88.61 |
| Built-in display | Sharp | scroll | 2560x1662 | 59.7 / 135.6 | — | 37.4 | 33.99 |
| Built-in display | Sharp | type | 2560x1662 | 31.0 / 95.3 | 65.2 / 135.3 | 9.9 | 41.15 |

Every scrolling configuration missed the inherited 100 ms content-p95 / 5% late-frame targets; whole-display scrolling also missed 45 FPS. Some typing paths met the 150 ms input-p95 target, while separated-window typing missed it. Preserve these negative results; they do not establish comfortable sustained coding. No performance improvement is claimed between these unmatched runs.

The ordinary test window was 1280×820 logical points. Separated-window runs predated source-dimension metadata, so their exact logical monitor dimensions were not recorded. Whole-display metadata confirms 1800×1169 logical / 3600×2338 backing; Balanced and Sharp reduce that to 1920×1246 and 2560×1662 encoded pixels. The first two viewer-CPU samples missed Chrome's Flatpak profile and reported zero: treat those as unavailable. Later scrolling viewer load was about 56–72% of one CPU core, not total-system utilization.

Excluded runs: an early physical-display suite failed browser relaunch and never produced a complete document; a briefly overlapping launch was interrupted. A later Sharp whole-display scrolling run overlapped synthetic unit tests, so the table uses its isolated repeat (which also failed targets). Browser profile-lock failures prompted fork-only profile isolation plus bounded TERM/KILL cleanup; two real launch/confirmed-shutdown cycles passed afterward.

## Virtual monitor and lifecycle evidence

- Repeated 5120×1440@1× and 5120×2160@1× create/capture/remove cycles passed, along with 2560×1440@2× and SIGINT, SIGTERM, parent-stdin EOF cleanup.
- Additional isolated probes passed 3440×1440@1×, 3440×1440@2× (6880×2880 backing), 5120×1440@2× (10240×2880), and 5120×2160@2× (10240×4320). Each captured a frame, remained extended/non-main/non-mirrored, and returned to the exact physical baseline.
- Normal Stop restored a pre-existing synthetic window moved onto Workspace to its exact original 320×212 frame. It reported one restored window and confirmed removal. Only pre-start windows still centered on the owned display are eligible; new windows and post-crash rehoming rely on macOS.
- A second live owner was rejected in 0.11 seconds without changing the live display. No-op orphan recovery exited in 1.66 seconds. A hardware check caught a windowless AppKit error-path hang; posting a wake event before stopping the run loop fixed it, and the regression now has an opt-in test.
- SIGKILL of the owned process while awake left the physical baseline intact. After forced **display** sleep (`pmset displaysleepnow`) and SIGKILL, safe recovery requested a wake, observed the stale display disappear, and confirmed the baseline. Fresh Start and EOF Stop succeeded afterward. This does not prove full-system sleep, clamshell or Stage Manager behavior.
- An isolated `--capture-probe` using the same executable as a running companion capture agent repeatedly failed with a ScreenCaptureKit connection-interrupted error. It rolled back correctly. Stopping the task companion made the same executable/mode pass; an independently built identical-source helper also passed. Run standalone capture probes with the companion stopped. Normal Workspace mode captures through the companion agent, and its live stream passed.

The public source identity is `workspace`. The companion keeps transient display IDs, window snapshots, stream tickets and reconnect keys in process memory rather than using them as durable settings or identity. A per-user lease prevents another live owner from being treated as an orphan. Recovery asks macOS to unregister stale displays; it never releases an unowned CGVirtualDisplay object.

## Companion and rendered pixels

The approved inline controls were exercised in the live localhost companion: Start created the real extended desktop and a Frame panel; Stop confirmed removal and returned to the built-in display. Presets were disabled while running and re-enabled after Stop. At the observed 354 px card width the controls wrapped without horizontal overflow.

For 5120×1440@1× at Sharp, direct viewer inspection measured **2560×720 canvas backing**, a **1920×1080 CSS viewport at device scale 1**, `object-fit: contain`, 32 drawn frames, H.264 `avc1.640c2a`, and no decoder errors. The image therefore occupies **1920×540** within the panel. These are browser pixels, not headset optical resolution. The logical workspace offers more coordinates, but 14-point text can be reduced to roughly 5.25 panel pixels in height before optics; increased logical area is not proof of increased usable coding area.

The live integration fault sequence also passed:

| Event | Observed result |
|---|---|
| SSH tunnel terminated | Tunnel and video reconnected automatically; the same virtual display remained. Viewer draw count advanced from 34 to 83 with no decoder error. |
| Capture agent SIGKILL | Companion started a fresh capture agent. Show again reopened decoded video on the same virtual display. This recovery requires Show again. |
| Workspace owner SIGKILL | Fresh Start mapped logical `workspace` to a new display ID and decoded frames. |
| Final Stop | Owned stream closed, removal confirmed, and only the built-in display remained. |

## Ultrawide stream measurements

Both runs used the same 5120×1440@1× virtual display, a synthetic scrolling Chrome window measured at 5120×1410 logical points, and the existing lab agent. No companion stream or unit suite ran alongside them. These independent Wi-Fi samples are functional/throughput evidence, not a matched performance comparison.

| Quality | Encoded pixels | Content p50 / p95 ms | Draw FPS | Late % | Max gap ms |
|---|---|---:|---:|---:|---:|
| Balanced | 1920x540 | 24.6 / 97.3 | 44.7 | 24.97 | 199.2 |
| Sharp | 2560x720 | 22.1 / 95.1 | 56.3 | 9.52 | 152.8 |

Both met the inherited content-latency threshold but missed the late-frame threshold; Balanced also narrowly missed 45 FPS. Direct viewer inspection confirmed 1920×540 and 2560×720 canvas buffers respectively, both fitted into a 1920×1080 viewport. Recorded frames decoded without errors. The actual image occupies 1920×540 for both at the observed panel size.

![Sharp ultrawide stream rendered in the Frame browser; synthetic content only](evidence/workspace-sharp-panel.png)

This is an off-head browser screenshot, not a photograph through the optics. The narrow text column belongs to the inherited benchmark page; it is not a workspace UI design or proof of a useful coding layout. [Balanced capture](evidence/workspace-balanced-panel.png) uses the same synthetic page.

## Engineering decision

**Go** for a lifecycle-managed real extended display and a single-panel transport proof. **No go yet** for calling that single giant surface a productive, readable ultrawide workstation. Keep independent separated-window panels as the existing fallback; FW-05 must evaluate viewport/tiled streaming or another rendering route before promising native-width text detail. FW-10 separately gates owned-overlay layout and world rotation.

The implementation reuses upstream's private `CGVirtualDisplay` API and public capture pipeline. [Apple's ScreenCaptureKit sample](https://developer.apple.com/documentation/screencapturekit/capturing-screen-content-in-macos) documents capturing existing displays/apps/windows. [BetterDisplay](https://github.com/waydabber/BetterDisplay) documents custom virtual screens and HiDPI as an optional external provider; it was not installed or required. Changing the display provider alone would not remove this viewer's measured stream/panel limits.

## Reproduction

Build with `sh mac/frame-mac-view/build.sh`. With the companion stopped, use `mac/bin/frame-mac-view workspace --width 5120 --height 1440 --capture-probe`; Ctrl-C removes the owned monitor. The full opt-in display checks are `FRAME_MAC_VIEW_RUN_DISPLAY_TESTS=1 sh mac/frame-mac-view/test-workspace.sh`; immediate termination/lease regressions use `FRAME_MAC_VIEW_RUN_TERMINATION_TESTS=1 sh mac/frame-mac-view/test-workspace.sh`.

For controlled crash recovery, start an owned `workspace --exit-on-stdin-eof` process and record **that child's PID**. Kill only that child, then run `workspace --recover-orphan` and compare fresh `displays` output with the physical baseline. The display-sleep case inserts `pmset displaysleepnow` before killing the child. No persistent power preference is changed.

Start the companion from this checkout with `FRAME_PRIVATE_SSH=1 FRAME_ALIAS=frame FRAME_CONTROL_TELEMETRY=0 python3 ui/server.py --port 47820`, then open `http://127.0.0.1:47820/#macview`. Private SSH mode preserves another running companion. Select size, scale and quality; Start Workspace; move desired windows onto Frame Workspace in macOS. Stop Workspace returns eligible windows and removes the monitor. The test server used a throwaway data directory and disabled unrelated catalogue warming.

Start the lab agent with `sh mac/frame-mac-view/lab.sh serve`. Check that its `/status` reports Screen Recording and Accessibility; an app launched through Finder and the same binary launched from an already-authorized terminal can have different permission attribution. Grant normal macOS permissions when needed; do not alter TCC databases. Run, for example:

```sh
python3 scripts/macview-bench.py run --scenario scroll,type --duration 20 --quality sharp --mode window --host frame.local --out /tmp/macview-window.json
python3 scripts/macview-bench.py run --scenario scroll,type --duration 20 --quality balanced --mode display --display main --host frame.local --out /tmp/macview-display.json
```

Use `--mode separate` for separated windows, or `--mode display --display workspace` while a foreground Workspace owner is active. Cable runs replace `--host frame.local` with `--usb`; never combine both route selectors. A missed inherited target intentionally exits 1 after writing results. Benchmarks own their viewer lifecycle; do not run them alongside an interactive companion stream.

![Approved inline Workspace controls, live companion](evidence/workspace-controls.jpg)

## Remaining human acceptance

Wear the Frame for a real 30-minute coding task using the Mac keyboard and trackpad. Compare comfortable font sizes (for example 14/18/22/28), usable columns versus the laptop, pointer alignment, scrolling, selection, app switching, English/Japanese IME, menu/dialog placement and sustained comfort. Record the chosen preset, quality, font size and optics settings, and whether the additional area is actually useful. Physical lid closure and Stage Manager need a separate controlled check. USB measurements need a live data link.

## Automated checks

Full Python suite: 946 tests passed, one permission-dependent skip. App Node tests: 33 passed; site Node tests: 8 passed; inline UI scripts passed syntax checks. After the final bounded browser-cleanup change, the affected MacView/benchmark suites passed 36 tests with one skip. Swift build and `git diff --check` passed. Inherited HTTP/TLS test-resource warnings were observed; no failing check was hidden or weakened.

## Cleanup

Preserved source, the current compiled helper, this compact report and three inspected screenshots. Removed the clean baseline worktree, obsolete temporary Swift helpers/fixture, temporary Mac Chrome profiles, raw test JSON/logs and superseded captures after summarizing their results: **95,895,552 allocated bytes (about 91.5 MiB)** measured with `du`. The original installed app, user files and existing Frame browser profile were preserved. The fork's current browser profile and small live prototype state are retained for the pending human test.
