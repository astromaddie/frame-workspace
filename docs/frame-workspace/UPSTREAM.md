# Frame Workspace — upstream maintenance

**Fork:** [astromaddie/frame-workspace](https://github.com/astromaddie/frame-workspace)  
**Upstream:** [saphid/frame-control](https://github.com/saphid/frame-control)  
**Audited baseline:** [bcdd9d0830673ef53c6be9987a429dc43bef77e5](https://github.com/saphid/frame-control/commit/bcdd9d0830673ef53c6be9987a429dc43bef77e5) on 2026-10-18. The fork's `main` was verified at exactly this SHA before adding Frame Workspace documentation.

## Contribution boundaries

This is an independent workstation-focused fork. Retain the original MIT [LICENSE](../../LICENSE) and `Copyright (c) 2026 saphid` notice. Preserve working upstream Frame management, macOS ScreenCaptureKit capture, VideoToolbox encoding, SSH reverse tunnels, Chromium/WebCodecs viewer and SteamVR/gamescope panel launching. Add experimental workspace features behind flags and keep Mac virtual-display cleanup safe.

**Important architecture constraints:** Upstream already supports per-window virtual HiDPI displays through **private** `CGVirtualDisplay`, but refuses native macOS full-screen windows. Sharp streams stop at a 2560 px long side. SteamVR dashboard-owned overlays currently reject attempts to set their spatial transforms, so persistent spatial layouts require separate technical proof. See [ROADMAP.md](ROADMAP.md) and [ISSUES.md](ISSUES.md).

## Synchronizing upstream

Run from a local checkout of the fork:

```bash
git remote -v
git remote add upstream https://github.com/saphid/frame-control.git  # if absent
git fetch upstream
git switch main
git merge upstream/main
```

Resolve upstream conflicts consciously; do not overwrite Frame Workspace-specific files. Run the existing upstream tests and the Frame Workspace test suite on supported hosts before pushing. Perform manual verification on the actual M4 MacBook and Steam Frame for all hardware-dependent behavior.

## Release safety

Before distributing an independent build, review inherited release/update settings, CI workflows, installer metadata, application identity, repository references, telemetry and URLs. Do not publish upstream-branded artifacts under the upstream maintainer's name or inadvertently point Frame Workspace's update path at unrelated upstream releases.

## Known deployment constraints

Local Wi-Fi and USB-C transport are already supported; high throughput alone does not guarantee text clarity or low latency. Private Apple APIs can change, codecs must decode on the Steam Frame, and multiple moving windows contend for encoding and rendering resources.
