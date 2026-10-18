# OpenVR overlay ABI

Unmodified `headers/openvr_capi.h` and `LICENSE` from ValveSoftware/openvr
v2.5.1, commit `ae46a8dd0172580648c8922658a100439115d3eb`.
Source: https://github.com/ValveSoftware/openvr/tree/ae46a8dd0172580648c8922658a100439115d3eb

The native viewer requests exactly `FnTable:IVROverlay_027`. Newer function tables must
not be cast to this layout. No OpenVR runtime binary is bundled or linked.
The viewer also requests exactly `FnTable:IVRSystem_022`.
The build metadata hashes both vendored files.

SHA-256:

- `openvr_capi.h`: `c80f5733e2381afb4012eb1625c603930388c9cc6030b8fe161ad044c35637c7`
- `LICENSE`: `f56ff606104d4ef18e617921a75c73ad73b5a1a1d70c69590c29de16919e04ad`
