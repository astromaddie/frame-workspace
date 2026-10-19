// Spike for FW-14 VR-first placement: does SteamVR on the Frame deliver laser
// hover/click events to a third-party world overlay, and with which device?
//
// Shows a translucent 0.6 m x 0.12 m strip just below the native Workspace
// (where the move pill will go), with mouse input and
// MakeOverlaysInteractiveIfVisible, then logs every overlay event and the
// pointing device's pose for SECONDS. Nothing else is changed; the strip is
// destroyed on exit. Build on the Frame:
//   cc -O1 -std=gnu11 -Inative/vendor/openvr native/spike_overlay_input.c -o /tmp/spike -ldl
// Run with the headset worn: /tmp/spike 30
#include <dlfcn.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "openvr_capi.h"

static double now(void) {
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return t.tv_sec + t.tv_nsec / 1e9;
}

int main(int argc, char **argv) {
  int seconds = argc > 1 ? atoi(argv[1]) : 30;
  void *lib = dlopen("/opt/steamvr/bin/linuxarm64/libopenvr_api.so", RTLD_NOW);
  if (!lib) { printf("no openvr: %s\n", dlerror()); return 1; }
  intptr_t (*init)(EVRInitError *, EVRApplicationType) = dlsym(lib, "VR_InitInternal");
  intptr_t (*get)(const char *, EVRInitError *) = dlsym(lib, "VR_GetGenericInterface");
  void (*shutdown)(void) = dlsym(lib, "VR_ShutdownInternal");
  EVRInitError e = EVRInitError_VRInitError_None;
  init(&e, EVRApplicationType_VRApplication_Overlay);
  if (e) { printf("init error %d\n", e); return 2; }
  struct VR_IVRSystem_FnTable *sys = (void *)get("FnTable:IVRSystem_022", &e);
  struct VR_IVROverlay_FnTable *ov = (void *)get("FnTable:IVROverlay_027", &e);
  if (!sys || !ov) { printf("interfaces unavailable\n"); return 3; }

  // Place the strip 25 cm below the Workspace centre, using the Workspace's
  // own transform when it exists, else 1.5 m ahead at a standing height.
  HmdMatrix34_t m = {{{1, 0, 0, 0}, {0, 1, 0, 1.2f}, {0, 0, 1, -1.5f}}};
  VROverlayHandle_t workspace = 0;
  if (ov->FindOverlay("com.frame-workspace.native.1000.workspace", &workspace) == EVROverlayError_VROverlayError_None) {
    ETrackingUniverseOrigin origin;
    if (ov->GetOverlayTransformAbsolute(workspace, &origin, &m) == EVROverlayError_VROverlayError_None) {
      float w = 2.4f, height;
      ov->GetOverlayWidthInMeters(workspace, &w);
      height = w * 2560.f / 2880.f; // 9:8 Workspace
      for (int r = 0; r < 3; r++) m.m[r][3] -= m.m[r][1] * (height / 2 + 0.12f);
      printf("placed below the Workspace (width %.2f m)\n", w);
    }
  }
  VROverlayHandle_t h = 0;
  char key[] = "com.frame-workspace.spike.pill", name[] = "Placement Spike";
  if (ov->CreateOverlay(key, name, &h)) { printf("CreateOverlay failed\n"); return 4; }
  enum { W = 256, H = 52 };
  static uint8_t px[W * H * 4];
  for (int y = 0; y < H; y++)
    for (int x = 0; x < W; x++) {
      float dx = fmaxf(0, fabsf(x - W / 2.f) - (W / 2.f - H / 2.f)), dy = y - H / 2.f;
      int inside = dx * dx + dy * dy <= (H / 2.f) * (H / 2.f);
      uint8_t *p = px + (y * W + x) * 4;
      p[0] = 220; p[1] = 222; p[2] = 223; p[3] = inside ? 170 : 0;
    }
  ov->SetOverlayRaw(h, px, W, H, 4);
  ov->SetOverlayWidthInMeters(h, 0.6f);
  ov->SetOverlayTransformAbsolute(h, ETrackingUniverseOrigin_TrackingUniverseStanding, &m);
  ov->SetOverlayInputMethod(h, VROverlayInputMethod_Mouse);
  HmdVector2_t scale = {{W, H}};
  ov->SetOverlayMouseScale(h, &scale);
  ov->SetOverlayFlag(h, VROverlayFlags_MakeOverlaysInteractiveIfVisible, true);
  ov->ShowOverlay(h);
  printf("strip shown; point the laser at it, hover, press and drag for %d s\n", seconds);
  fflush(stdout);

  double end = now() + seconds;
  int counts[400] = {0};
  while (now() < end) {
    struct VREvent_t ev;
    while (ov->PollNextOverlayEvent(h, &ev, sizeof ev)) {
      if (ev.eventType < 400) counts[ev.eventType]++;
      if (ev.eventType == EVREventType_VREvent_MouseMove && counts[ev.eventType] % 15 != 1) continue;
      TrackedDevicePose_t poses[16];
      sys->GetDeviceToAbsoluteTrackingPose(ETrackingUniverseOrigin_TrackingUniverseStanding, 0, poses, 16);
      unsigned d = ev.trackedDeviceIndex < 16 ? ev.trackedDeviceIndex : 0;
      char model[64] = "";
      ETrackedPropertyError pe;
      sys->GetStringTrackedDeviceProperty(d, ETrackedDeviceProperty_Prop_ModelNumber_String, model, sizeof model, &pe);
      printf("%7.2fs event %u device %u (%s, class %d) mouse (%.0f,%.0f) button %u  pose ok %d pos (%.2f,%.2f,%.2f)\n",
             seconds - (end - now()), ev.eventType, d, model, sys->GetTrackedDeviceClass(d),
             ev.data.mouse.x, ev.data.mouse.y, ev.data.mouse.button, poses[d].bPoseIsValid,
             poses[d].mDeviceToAbsoluteTracking.m[0][3], poses[d].mDeviceToAbsoluteTracking.m[1][3],
             poses[d].mDeviceToAbsoluteTracking.m[2][3]);
      fflush(stdout);
    }
    struct timespec t = {0, 5000000};
    nanosleep(&t, NULL);
  }
  printf("totals: move %d down %d up %d focus-enter %d focus-leave %d scroll %d\n",
         counts[EVREventType_VREvent_MouseMove], counts[EVREventType_VREvent_MouseButtonDown],
         counts[EVREventType_VREvent_MouseButtonUp], counts[EVREventType_VREvent_FocusEnter],
         counts[EVREventType_VREvent_FocusLeave], counts[EVREventType_VREvent_ScrollSmooth]);
  ov->DestroyOverlay(h);
  shutdown();
  return 0;
}
