#include "gaze_bridge.hpp"
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-variable"
#include <cstdint>
namespace openvr_2_15_6 {
#include "vendor/openvr-2.15.6/openvr_capi.h"
}
#pragma GCC diagnostic pop
#include <dlfcn.h>
namespace fw {
using namespace openvr_2_15_6;
GazeBridge::GazeBridge(void *library) {
  if (!library) return;
  auto get = reinterpret_cast<intptr_t (*)(const char *, EVRInitError *)>(
      dlsym(library, "VR_GetGenericInterface"));
  if (!get) return;
  EVRInitError error = EVRInitError_VRInitError_None;
  auto table = reinterpret_cast<VR_IVRSystem_FnTable *>(get("FnTable:IVRSystem_026", &error));
  if (!error && table) system = table;
}
const char *GazeBridge::sample(EyeRay (&rays)[2]) {
  if (!system) return "system026-unavailable";
  auto table = static_cast<VR_IVRSystem_FnTable *>(system);
  HmdVector2_t ndc[2]{};
  if (!table->GetEyeTrackedFoveationCenter(&ndc[0], &ndc[1])) return "eye-unavailable";
  TrackedDevicePose_t pose{};
  table->GetDeviceToAbsoluteTrackingPose(ETrackingUniverseOrigin_TrackingUniverseStanding, 0, &pose, 1);
  if (!pose.bPoseIsValid || !pose.bDeviceIsConnected ||
      pose.eTrackingResult != ETrackingResult_TrackingResult_Running_OK) return "pose-invalid";
  for (int i=0;i<2;++i) {
    auto eye = i ? EVREye_Eye_Right : EVREye_Eye_Left;
    auto projection = table->GetProjectionMatrix(eye, .1f, 100.f);
    auto transform = table->GetEyeToHeadTransform(eye);
    if (!eyeRay(&projection.m[0][0], &transform.m[0][0], &pose.mDeviceToAbsoluteTracking.m[0][0],
                ndc[i].v[0], ndc[i].v[1], rays[i])) return "eye-invalid";
  }
  return "valid";
}
} // namespace fw
