OpenVR C API header, ValveSoftware/openvr tag v2.15.6, commit 0924064.
Source: https://github.com/ValveSoftware/openvr/blob/v2.15.6/headers/openvr_capi.h
SHA-256: 3d32d8eb5bcbe2e250610123aee8142c68f0e7d4da823f87513b6a2a333078d7

Included inside namespace openvr_2_15_6 ONLY by gaze_bridge.cpp for optional FnTable:IVRSystem_026. The existing
vendor/openvr header and System022/Overlay027 calls remain unchanged. Do not
include both SDKs in one translation unit or cast one version's table to another.
