# Frame Workspace — implementation backlog

Created 2026-10-18 (JST) for [`astromaddie/frame-workspace`](https://github.com/astromaddie/frame-workspace).

**Status:** Fork verified against the audited upstream commit. All **13 GitHub Issues** are now [published](https://github.com/astromaddie/frame-workspace/issues), with code-grounded acceptance criteria. The [original issue specifications](ISSUES.md) remain versioned alongside this roadmap.

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

Build the CLI with `sh mac/frame-mac-view/build.sh`, then run `mac/bin/frame-mac-view workspace`. It creates one extended display named **Frame Workspace** at **5120×1440 logical points, 1×** by default. Use `--width 5120 --height 2160` for a taller display, or `--scale 2` for HiDPI; for example, `--width 2560 --height 1440 --scale 2`. `--duration seconds` is useful for a bounded probe. `--capture-probe` waits for one ScreenCaptureKit frame from the otherwise blank virtual display. This diagnostic uses the existing capture path capped at a 1920-pixel long edge; it does not measure full-resolution streaming or headset legibility. The `ready` JSON reports requested and actual logical/buffer sizes, global bounds, mirroring/main status and `display:<id>`; `frame-mac-view displays` lists the same source for the existing picker. The process owns the display until SIGINT, SIGTERM, optional `--exit-on-stdin-eof`, or the duration expires. It never moves windows automatically.

The command briefly requests a local wake at startup and holds macOS's idle-display-sleep assertion while it owns the display. This changes no persistent power setting. A forced sleep or lid-close can still defer WindowServer removal; the command reports `removed:false` and an error if the display remains online after its bounded wait.

On the user's M4 MacBook Pro (macOS 26.3.1), the opt-in `FRAME_MAC_VIEW_RUN_DISPLAY_TESTS=1 sh mac/frame-mac-view/test-workspace.sh` run verified four repeated 1× create/capture/remove cycles across **5120×1440** and **5120×2160**, one **2560×1440@2×** cycle, and separate SIGINT, SIGTERM and stdin-EOF cleanup. Each cycle produced a ScreenCaptureKit frame, appeared in `displays`, reported `main:false` and `mirrored:false`, preserved the pre-existing physical display layout, and ended with `removed:true`. The probe captures only the blank Workspace display. This establishes the Mac display and normal lifecycle path; headset readability, sustained streaming performance, and the broader FW-02/FW-05 benchmarks remain open.

The pre-existing Mac desktop was **1800×1169 logical points / 3600×2338 backing pixels**. The 5120×2160@1× workspace provides about **5.3× its logical desktop area**. Final validation passed 935 Python tests (one permission-dependent skip), 33 app Node tests and 8 site Node tests, plus the opt-in display suite; only the built-in display remained afterward. The compiled helper is kept in ignored `mac/bin/`; temporary test displays and processes were removed, and no bulky captures or logs were retained. Frame SSH connectivity was verified, but no headset launch or test was performed.

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
