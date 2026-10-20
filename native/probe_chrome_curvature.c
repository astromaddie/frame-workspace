// Casts rays from the running Workspace's centre of curvature through the
// window and its chrome layers with SteamVR's own ComputeOverlayIntersection:
// a pass is the bezel at r = R - 0.015 and the controls at R - 0.03 (in front,
// concentric), both still hit just past the window's edges, and u matching
// 0.5 + angle * R / (layer width in window meters). Needs a curved Workspace
// (curvature > 0). Build and run on the Frame:
//   cc -O1 -Inative/vendor/openvr native/probe_chrome_curvature.c -o /tmp/curveprobe -ldl -lm && /tmp/curveprobe
#include <dlfcn.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include "openvr_capi.h"
static HmdVector3_t apply(const HmdMatrix34_t *m, float x, float y, float z, int point) {
  HmdVector3_t v;
  for (int r = 0; r < 3; r++) v.v[r] = m->m[r][0] * x + m->m[r][1] * y + m->m[r][2] * z + (point ? m->m[r][3] : 0);
  return v;
}
int main(void) {
  void *lib = dlopen("/opt/steamvr/bin/linuxarm64/libopenvr_api.so", RTLD_NOW);
  intptr_t (*init)(EVRInitError *, EVRApplicationType) = dlsym(lib, "VR_InitInternal");
  intptr_t (*get)(const char *, EVRInitError *) = dlsym(lib, "VR_GetGenericInterface");
  void (*shutdown)(void) = dlsym(lib, "VR_ShutdownInternal");
  EVRInitError e = 0;
  init(&e, EVRApplicationType_VRApplication_Overlay);
  if (e) { printf("init %d\n", e); return 1; }
  struct VR_IVROverlay_FnTable *ov = (void *)get("FnTable:IVROverlay_027", &e);
  if (!ov) return 2;
  const char *names[3] = {"window", "bezel", "controls"};
  const char *keys[3] = {"com.frame-workspace.native.1000.workspace", "com.frame-workspace.native.1000.workspace.frame",
                         "com.frame-workspace.native.1000.workspace.handles"};
  VROverlayHandle_t h[3];
  float width[3], curve[3];
  for (int i = 0; i < 3; i++) {
    if (ov->FindOverlay((char *)keys[i], &h[i])) { printf("%s missing\n", names[i]); return 3; }
    ov->GetOverlayWidthInMeters(h[i], &width[i]);
    ov->GetOverlayCurvature(h[i], &curve[i]);
  }
  ETrackingUniverseOrigin o;
  HmdMatrix34_t m;
  ov->GetOverlayTransformAbsolute(h[0], &o, &m);
  const float R = width[0] / (2 * M_PI * curve[0]);
  printf("window %.3f m wide, curvature %.3f, radius %.3f m\n", width[0], curve[0], R);
  const float half = width[0] / 2 / R;
  // the centre, halfway out, and just past each edge, where the handles are
  const float edge[] = {-1.025f, -1.015f, -1.005f, -0.5f, 0, 0.5f, 1.005f, 1.015f, 1.025f};
  for (int j = 0; j < 9; j++) {
    const float a = half * edge[j];
    HmdVector3_t src = apply(&m, 0, 0, R, 1), dir = apply(&m, sinf(a), 0, -cosf(a), 0);
    printf("angle %+6.1f deg:", a * 180 / M_PI);
    for (int i = 0; i < 3; i++) {
      VROverlayIntersectionParams_t p = {src, dir, o};
      VROverlayIntersectionResults_t r;
      if (ov->ComputeOverlayIntersection(h[i], &p, &r))
        printf("  %s r=%.3f u=%.3f", names[i], r.fDistance, r.vUVs.v[0]);
      else
        printf("  %s miss", names[i]);
    }
    printf("\n");
  }
  shutdown();
  return 0;
}
