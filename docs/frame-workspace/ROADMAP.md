# Frame Workspace — implementation backlog

Created 2026-10-18 (JST) for [`astromaddie/frame-workspace`](https://github.com/astromaddie/frame-workspace).

**Status:** Fork verified from `saphid/frame-control` at the audited commit. GitHub Issues are currently disabled in this fork (HTTP 410), so the [13 fully specified issues](ISSUES.md) are committed as a reviewable backlog. When Issues are enabled, these can be published without rewriting the plan.

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

## Milestones and stop/go gates

1. **M0 — Verify reality:** FW-01, FW-02, FW-03, FW-06, FW-10. Produce live-device proof and architecture decisions before building a UI around uncertain capabilities.
2. **M1 — Useful workstation:** FW-04, FW-05, FW-08. One persistent Mac extended desktop in VR that has *demonstrably readable coding text*; use SteamVR's existing curvature affordance as the first option.
3. **M2 — Distinctive interaction:** FW-07, FW-09, FW-12. Native full-screen spatial detachment **only if FW-06 passes**; multi-panel performance and keyboard/pointer behavior.
4. **M3 — Durable product:** FW-11, FW-13. Named profiles, conditional room transform restore, privacy and failure recovery.

**MVP decision rule:** If a giant wide display destroys text legibility or panel positioning cannot be controlled, make multiple **real separated Mac windows** the default instead. Do not ship a giant blurry desktop for the sake of claiming a higher resolution.

## Issue index

| ID | Priority | Phase | Title | Depends on |
|---|---|---|---|---|
| FW-01 | P0 | M0 | Establish the fork, upstream baseline, and scope boundaries | — |
| FW-02 | P0 | M0 | Benchmark current Mac → Frame streaming and coding readability | FW-01 |
| FW-03 | P0 | M0 | Spike: independent HiDPI ultrawide macOS virtual display | FW-01, FW-02 |
| FW-04 | P1 | M1 | Implement lifecycle-managed virtual ultrawide Workspace mode | FW-03 |
| FW-05 | P0 | M1 | Preserve readable text on ultrawide: evaluate tiled or viewport streaming | FW-02, FW-03 |
| FW-06 | P0 | M0 | Spike: detect native macOS full-screen Spaces reliably | FW-02 |
| FW-07 | P1 | M2 | Automatic spatial detachment on native full-screen transitions | FW-06, FW-04 |
| FW-08 | P1 | M1 | Frame-side curved workspace and low-friction controls | FW-04, FW-05 |
| FW-09 | P1 | M2 | Prioritize active panel and benchmark multi-stream performance | FW-02, FW-05 |
| FW-10 | P0 | M0 | Spike: owned VR overlays and saved spatial transform support | FW-02 |
| FW-11 | P2 | M3 | Saved logical workspace profiles and conditional spatial layouts | FW-04, FW-08, FW-10 |
| FW-12 | P1 | M2 | Keyboard, pointer, focus and multiple macOS Spaces correctness | FW-04, FW-06 |
| FW-13 | P1 | M3 | Local-first security, failure recovery and end-to-end regression | FW-04, FW-07, FW-08, FW-09, FW-11, FW-12 |

## Backlog source

All 13 complete issue descriptions (scope, acceptance criteria, dependencies, references) are preserved in [ISSUES.md](ISSUES.md). The GitHub Issues tracker is not yet enabled; issue numbers and labels have not been assigned.
