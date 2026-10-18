# FW-05 surface decision

The current prototype uses **one aspect-matched native surface**, with the user's
2880×2560, 1× Workspace as the unattended test preset. That path has a 2880×2560
desktop buffer, encoded picture and submitted OpenVR texture. It avoids the
browser/theater letterbox and does not downsample that desktop to fit a 1080p
container. Native width and distance change angular size independently of video
resolution.

| Quantity | Current 9:8 test path | What it establishes |
|---|---|---|
| Logical desktop | 2880×2560 points | Available macOS layout space |
| Desktop backing buffer | 2880×2560 pixels at 1× | Pixels available to capture |
| Encoded picture / native texture | 2880×2560 pixels | No source downsampling for this preset |
| Final pixels seen through the lenses | Not measured | Requires worn size, distance and readability checks |

This is a pixel-preservation decision, not proof that every desktop pixel maps
to one physical headset pixel. The user's readability confirmation applies to
the earlier browser view; the native view still needs worn acceptance.

## Why tiles are deferred

The 5120×1440 ultrawide preset still sends 4096×1152 at Sharp. Two 2560×1440
tiles could preserve its full backing resolution, but would require two
encoders and independent viewer/decoder paths, alignment and grouping, and
validated input across the seam. The current native launcher owns one reserved
Workspace service. It cannot yet supply two native tiles, and overlapping
hardware-decoder diagnostics previously timed out when another decoder was
active. That is a reason to measure a multi-decoder design, not a claimed
hardware session limit.

ScreenCaptureKit display cropping exists internally in `SCKSource`, including
display-local capture bounds and global pointer coordinates. The current stream
API does not expose it as a tiled Workspace source. No tiled alignment or
seam-crossing behavior is claimed.

At 1×, the 9:8, portrait and 4K preset dimensions fit within the single-surface
stream limit; the current native desktop proof uses 9:8. Keep the 4096-pixel limit until a wider
single stream or grouped native tiles have their own decode, alignment and
input evidence. FW-09's browser-backed multi-window measurements are a separate
encoder/transport test, not proof of a native tiled renderer.

FW-05 remains open for worn coding readability and real pointer/selection/scroll
acceptance. The [native record](NATIVE.md) preserves dimensions and throughput;
the [input record](INPUT.md) distinguishes automated mapping checks from physical
input acceptance.
