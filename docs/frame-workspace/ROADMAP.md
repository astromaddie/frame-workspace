# Frame Workspace — implementation backlog

Created 2026-10-18 (JST) for [`astromaddie/frame-workspace`](https://github.com/astromaddie/frame-workspace).

**Status:** Fork verified against the audited upstream commit. All **13 GitHub Issues** are now [published](https://github.com/astromaddie/frame-workspace/issues), with code-grounded acceptance criteria. The [original issue specifications](ISSUES.md) remain versioned alongside this roadmap.

**First-delivery status:** the requested scope is FW-01 through FW-04. Companion Start/Stop, real virtual-display capture, browser rendering and recovery now have off-head device evidence. Worn coding/readability acceptance and USB-C testing remain open; see the [validation report](VALIDATION.md).

**Readability follow-up:** the user confirmed the original letterboxed view was functional, then reported much clearer text after moving Workspace to its own ultrawide Gamescope/OpenVR viewer. Sharp now targets 4096×1152 at 30 fps for the 5120×1440 desktop. Full coding/IME acceptance and sustained 60 fps remain open; the [viewer findings](VALIDATION.md#ultrawide-viewer-follow-up) record the compositor and encoder limits.

**Native follow-up:** an ARM64 viewer now owns a direct OpenVR texture, with real
2880×2560 desktop capture and explicit Frame hardware-decode evidence. Its
head-motion behavior is not worn-verified and sustained 60 fps is not established.
See [native usage and evidence](NATIVE.md) and the separate
[full-screen feasibility gate](FULLSCREEN.md). Use 9:8 for unattended display
testing; 16:9 creation can open a macOS mirror/extend prompt on this Mac.

## Product thesis

Local-first Steam Frame workspace for an Apple Silicon (M4) MacBook: a *real* extended macOS virtual ultrawide display for coding, high text fidelity, optional independent Mac panels, and automatic detachment when the user uses the *native macOS green full-screen control*. No internet, cloud service, PC gaming host or purchased utility required for the core workflow. Prefer real macOS display and Space semantics over a new custom window manager.

## Source audit (upstream main `bcdd9d0830673ef53c6be9987a429dc43bef77e5`)

- MIT licensed: [LICENSE](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/LICENSE). Preserve its attribution.
- **Already works upstream:** Mac ScreenCaptureKit → VideoToolbox H.264 → SSH reverse tunnel → Steam Frame Chromium/WebCodecs → gamescope/SteamVR panel; native Bluetooth Mac keyboard/trackpad works on Mac side. [Architecture](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/docs/mac-in-headset.md).
- **Already works upstream:** per-window HiDPI `CGVirtualDisplay` instances, moving real macOS windows onto them, capturing each entire virtual display, and returning them on Stop. This uses a **private Apple API**, not a stable public system API. [Implementation](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/mac/frame-mac-view/Sources/Separate.swift) / [research and tests](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/docs/mac-window-separation.md).
- **Native macOS full-screen not solved:** `SeparateSource.start()` explicitly refuses an `AXFullScreen` window. [Code](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/mac/frame-mac-view/Sources/Separate.swift). The user's requested green-button behavior must be demonstrated in FW-06 before being promised.
- **Pixels constrain usefulness:** upstream Sharp video tops out at **2560 px long edge**, and the initial viewer panel is sized into 1920×1080. An ultrawide *logical* monitor alone is not a sharp ultrawide VR monitor. [Quality settings](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/ui/frame_macview.py). Tiling/cropping or an alternative rendering route may be needed.
- **Saved positions blocked:** dashboard-owned overlay transform setter returned error 12 (`PermissionDenied`), so persistence is conditional on testing a Frame Workspace-owned renderer. [Panel research](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/docs/panels.md); related [upstream issue #31](https://github.com/saphid/frame-control/issues/31).
- **Network not the only limit:** upstream tests show hardware encoder contention across multiple moving windows and Frame-side software decode; do not assume local bandwidth alone solves performance. [Research](https://github.com/saphid/frame-control/blob/bcdd9d0830673ef53c6be9987a429dc43bef77e5/docs/mac-window-separation.md).

## Mac-only virtual display proof (FW-03 / FW-04 first slice)

Build the CLI with `sh mac/frame-mac-view/build.sh`, then run `mac/bin/frame-mac-view workspace`. It creates one extended display named **Frame Workspace** at **5120×1440 logical points, 1×** by default. Use `--width 5120 --height 2160` for a taller display, or `--scale 2` for HiDPI; for example, `--width 2560 --height 1440 --scale 2`. `--duration seconds` is useful for a bounded probe. `--capture-probe` waits for one ScreenCaptureKit frame from the otherwise blank virtual display. This diagnostic uses the existing capture path capped at a 1920-pixel long edge; it does not measure full-resolution streaming or headset legibility. The `ready` JSON reports requested and actual logical/buffer sizes, global bounds, mirroring/main status and `display:<id>`; `frame-mac-view displays` lists the same source for the existing picker. The process owns the display until SIGINT, SIGTERM, optional `--exit-on-stdin-eof`, or the duration expires. Start does not move windows automatically; normal Stop returns eligible pre-start windows that were moved onto Workspace.

The command briefly requests a local wake at startup and holds macOS's idle-display-sleep assertion while it owns the display. This changes no persistent power setting. A forced sleep or lid-close can still defer WindowServer removal; the command reports `removed:false` and an error if the display remains online after its bounded wait.

On the user's M4 MacBook Pro (macOS 26.3.1), the opt-in `FRAME_MAC_VIEW_RUN_DISPLAY_TESTS=1 sh mac/frame-mac-view/test-workspace.sh` run verified four repeated 1× create/capture/remove cycles across **5120×1440** and **5120×2160**, one **2560×1440@2×** cycle, and separate SIGINT, SIGTERM and stdin-EOF cleanup. Each cycle produced a ScreenCaptureKit frame, appeared in `displays`, reported `main:false` and `mirrored:false`, preserved the pre-existing physical display layout, and ended with `removed:true`. The probe captures only the blank Workspace display. This establishes the Mac display and normal lifecycle path; headset readability, sustained streaming performance, and the broader FW-02/FW-05 benchmarks remain open.

The pre-existing Mac desktop was **1800×1169 logical points / 3600×2338 backing pixels**. The 5120×2160@1× workspace provides about **5.3× its logical desktop area**. Final validation passed 935 Python tests (one permission-dependent skip), 33 app Node tests and 8 site Node tests, plus the opt-in display suite; only the built-in display remained afterward. The compiled helper is kept in ignored `mac/bin/`; temporary test displays and processes were removed, and no bulky captures or logs were retained. That Mac-only baseline commit did not launch a headset test; the later companion/device checks are recorded below and in [VALIDATION.md](VALIDATION.md).

### Companion lifecycle integration (FW-04 implementation; off-head integration verified)

The Mac companion backend now exposes `POST /api/macview` actions `workspace_start` and `workspace_stop`. Start accepts a supported preset (`5120x1440`, `5120x2160`, `3440x1440`, `2560x2880`, `2880x2560`, or `3840x2160`), scale `1` or `2`, and the existing stream quality. Its public source key is always `workspace`; the companion keeps the current `display:<id>` only in memory and refreshes it whenever the owned helper starts. The successful state reports macOS logical and backing-buffer dimensions separately from the dimensions that the selected stream quality sends. For example, 5120×1440@1× at Sharp reports a 5120×1440 logical/buffer display and a 4096×1152 stream.

Start creates the foreground Swift owner, verifies its one-line ready record, and then opens the existing Frame panel. Choosing another size while running uses the same button to close the owned viewer and stream, confirm removal, and create and open the replacement. If removal is unconfirmed, a second display is not started. macOS may return windows to the laptop during a size or scale change; move them onto the new Workspace again if needed. If capture permission or panel launch fails, the companion closes that source and releases the display. Stop closes only the Workspace stream, sends EOF to its owner, and returns confirmed removal status plus the number of pre-existing visible windows it restored. When Accessibility is available, ordinary Stop restores only windows that were visible before Start and are still centered on the owned display; it does not move unrelated windows. No window positions or transient display IDs are written to disk.

The owner holds a per-user advisory lease while its display exists. A second live owner fails without touching the display. After an owner crash, the next Start or an explicit Stop can ask the CLI to request a short wake and wait for macOS to unregister the stale display; recovery never tries to release a `CGVirtualDisplay` it did not create. If the display remains after the bounded wait, the API reports removal as unconfirmed. A forced sleep/lid close can still defer WindowServer cleanup. Crash recovery has no saved window IDs or positions by design, so exact original window coordinates are restored on normal Stop only; macOS handles any rehoming after a process crash.

The approved inline controls offer the six desktop presets and 1×/2× scale, and show logical/backing dimensions beside the active stream size. Frame browser launchers use a `frame-workspace/mac-view` profile namespace; teardown targets only that namespace, waits for exit, and escalates from TERM to KILL when necessary.

The API and lifecycle tests use a fake helper for process ownership and rollback; Swift build and parser-only checks do not create a monitor. The integrated API and UI have not yet been tested against the live Frame companion while the FW-02 baseline is active. This does not establish worn readability, panel sharpness, or coding-session acceptance.

### Readable viewer and 60 fps follow-up

The dedicated Workspace OpenVR viewer now uses an aspect-matched fullscreen Chrome surface at **4096×1152** for the default 5120×1440 logical desktop. This removes the old browser letterbox; the user confirmed the new view is much more readable and clear. Sharp Workspace defaults to 30 fps, with an opt-in `FRAME_WORKSPACE_FPS=60` target. Initial encoder compatibility at 60 fps passed, but the live 60 fps scrolling trial did not sustain that rate at full resolution. Requested dimensions/rate, decoded canvas dimensions and physical headset presentation are separate evidence. See [VALIDATION.md](VALIDATION.md) for measurements and limits. FW-02/FW-04 still need the real coding, keyboard/trackpad/IME and sustained-use acceptance.

## Milestones and stop/go gates

1. **M0 — Verify reality:** FW-01, FW-02, FW-03, FW-06, FW-10. Produce live-device proof and architecture decisions before building a UI around uncertain capabilities.
2. **M1 — Useful workstation:** FW-04, FW-05, FW-08. One persistent Mac extended desktop in VR that has *demonstrably readable coding text*; use SteamVR's existing curvature affordance as the first option.
3. **M2 — Distinctive interaction:** FW-07, FW-09, FW-12. Native full-screen spatial detachment **only if FW-06 passes**; multi-panel performance and keyboard/pointer behavior.
4. **M3 — Durable product:** FW-11, FW-13. Named profiles, conditional room transform restore, privacy and failure recovery.

**MVP decision rule:** If a giant wide display destroys text legibility or panel positioning cannot be controlled, make multiple **real separated Mac windows** the default instead. Do not ship a giant blurry desktop for the sake of claiming a higher resolution.

## Issue index

| ID | Priority | Phase | Title | Depends on |
|---|---|---|---|---|
| [FW-01](https://github.com/astromaddie/frame-workspace/issues/1) | P0 | M0 | Establish the fork, upstream baseline, and scope boundaries | — |
| [FW-02](https://github.com/astromaddie/frame-workspace/issues/2) | P0 | M0 | Benchmark current Mac → Frame streaming and coding readability | [FW-01](https://github.com/astromaddie/frame-workspace/issues/1) |
| [FW-03](https://github.com/astromaddie/frame-workspace/issues/3) | P0 | M0 | Spike: independent HiDPI ultrawide macOS virtual display | [FW-01](https://github.com/astromaddie/frame-workspace/issues/1), [FW-02](https://github.com/astromaddie/frame-workspace/issues/2) |
| [FW-04](https://github.com/astromaddie/frame-workspace/issues/4) | P1 | M1 | Implement lifecycle-managed virtual ultrawide Workspace mode | [FW-03](https://github.com/astromaddie/frame-workspace/issues/3) |
| [FW-05](https://github.com/astromaddie/frame-workspace/issues/5) | P0 | M1 | Preserve readable text on ultrawide: evaluate tiled or viewport streaming | [FW-02](https://github.com/astromaddie/frame-workspace/issues/2), [FW-03](https://github.com/astromaddie/frame-workspace/issues/3) |
| [FW-06](https://github.com/astromaddie/frame-workspace/issues/6) | P0 | M0 | Spike: detect native macOS full-screen Spaces reliably | [FW-02](https://github.com/astromaddie/frame-workspace/issues/2) |
| [FW-07](https://github.com/astromaddie/frame-workspace/issues/7) | P1 | M2 | Automatic spatial detachment on native full-screen transitions | [FW-06](https://github.com/astromaddie/frame-workspace/issues/6), [FW-04](https://github.com/astromaddie/frame-workspace/issues/4) |
| [FW-08](https://github.com/astromaddie/frame-workspace/issues/8) | P1 | M1 | Frame-side curved workspace and low-friction controls | [FW-04](https://github.com/astromaddie/frame-workspace/issues/4), [FW-05](https://github.com/astromaddie/frame-workspace/issues/5) |
| [FW-09](https://github.com/astromaddie/frame-workspace/issues/9) | P1 | M2 | Prioritize active panel and benchmark multi-stream performance | [FW-02](https://github.com/astromaddie/frame-workspace/issues/2), [FW-05](https://github.com/astromaddie/frame-workspace/issues/5) |
| [FW-10](https://github.com/astromaddie/frame-workspace/issues/10) | P0 | M0 | Spike: owned VR overlays and saved spatial transform support | [FW-02](https://github.com/astromaddie/frame-workspace/issues/2) |
| [FW-11](https://github.com/astromaddie/frame-workspace/issues/11) | P2 | M3 | Saved logical workspace profiles and conditional spatial layouts | [FW-04](https://github.com/astromaddie/frame-workspace/issues/4), [FW-08](https://github.com/astromaddie/frame-workspace/issues/8), [FW-10](https://github.com/astromaddie/frame-workspace/issues/10) |
| [FW-12](https://github.com/astromaddie/frame-workspace/issues/12) | P1 | M2 | Keyboard, pointer, focus and multiple macOS Spaces correctness | [FW-04](https://github.com/astromaddie/frame-workspace/issues/4), [FW-06](https://github.com/astromaddie/frame-workspace/issues/6) |
| [FW-13](https://github.com/astromaddie/frame-workspace/issues/13) | P1 | M3 | Local-first security, failure recovery and end-to-end regression | [FW-04](https://github.com/astromaddie/frame-workspace/issues/4), [FW-07](https://github.com/astromaddie/frame-workspace/issues/7), [FW-08](https://github.com/astromaddie/frame-workspace/issues/8), [FW-09](https://github.com/astromaddie/frame-workspace/issues/9), [FW-11](https://github.com/astromaddie/frame-workspace/issues/11), [FW-12](https://github.com/astromaddie/frame-workspace/issues/12) |

## Backlog source

All 13 complete issue descriptions (scope, acceptance criteria, dependencies, references) are preserved in [ISSUES.md](ISSUES.md). The [13 live GitHub issues](https://github.com/astromaddie/frame-workspace/issues) are linked in the index above; their tracking IDs match GitHub issue numbers #1–#13.
