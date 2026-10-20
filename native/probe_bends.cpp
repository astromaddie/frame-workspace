// Checks a running Workspace's bent surfaces against SteamVR's own geometry.
// For points across the window (and past its edges, on the chrome layers) it
// works out where the surface should be from the saved pose and bend.hpp,
// casts a ray at that point along the surface normal with
// ComputeOverlayIntersection, and reports how far SteamVR's overlay is from
// it. A pass is every point found within a few millimetres, on the window
// (z = 0), the bezel (1.5 cm) and the controls (3 cm). Run on the Frame with
// the Workspace up, after setting a bend (control file {"bend":{...}}):
//   c++ -std=c++17 -O1 -Inative native/probe_bends.cpp -o /tmp/probe_bends \
//     $(pkg-config --cflags --libs json-c) -ldl && /tmp/probe_bends
#include "bend.hpp"
#include <cstdio>
#include <dlfcn.h>
#include <string>

using namespace fw;

int main() {
  const std::string home = getenv("HOME") ? getenv("HOME") : "";
  const std::string runtime = getenv("XDG_RUNTIME_DIR") ? getenv("XDG_RUNTIME_DIR") : "/run/user/1000";
  Placement p;
  if (!loadPlacement(home + "/.local/share/frame-workspace/workspace-pose.json", p)) {
    printf("no saved Workspace pose\n");
    return 1;
  }
  auto state = json(readFile(runtime + "/frame-workspace/state.json", 1 << 20));
  const double tw = num(state, "textureWidth", 0), th = num(state, "textureHeight", 0);
  if (tw <= 0 || th <= 0) {
    printf("no Workspace texture size\n");
    return 1;
  }
  // state.json reports the window's own size (not the turned texture).
  const float height = p.width * float(th / tw);
  const bool verbose = getenv("PROBE_VERBOSE") != nullptr;
  HandleLayout l;
  l.width = p.width;
  l.height = height;
  const BendShape b = bendShape(p, height);
  printf("window %.3f x %.3f m, bend %s a=%.3f b=%.3f (%d overlay%s)\n", p.width, height,
         b.vertical ? "top-to-bottom" : "side-to-side", b.amount[0], b.amount[1], b.segments(),
         b.segments() == 2 ? "s" : "");

  void *lib = dlopen("/opt/steamvr/bin/linuxarm64/libopenvr_api.so", RTLD_NOW);
  auto init = reinterpret_cast<intptr_t (*)(EVRInitError *, EVRApplicationType)>(dlsym(lib, "VR_InitInternal"));
  auto get = reinterpret_cast<intptr_t (*)(const char *, EVRInitError *)>(dlsym(lib, "VR_GetGenericInterface"));
  auto shutdown = reinterpret_cast<void (*)()>(dlsym(lib, "VR_ShutdownInternal"));
  EVRInitError e = EVRInitError_VRInitError_None;
  init(&e, EVRApplicationType_VRApplication_Overlay);
  if (e) return 2;
  auto *ov = reinterpret_cast<VR_IVROverlay_FnTable *>(get("FnTable:IVROverlay_027", &e));
  if (!ov) return 2;
  const std::string base = "com.frame-workspace.native.1000.workspace";
  struct Layer { const char *name; std::string key; float z, width, height; } layers[3] = {
      {"window", base, 0, l.width, l.height},
      {"bezel", base + ".frame", 0.015f, l.frameWidth(), l.frameHeight()},
      {"controls", base + ".handles", 0.03f, l.chromeWidth(), l.chromeHeight()}};
  int failures = 0;
  for (auto &layer : layers) {
    VROverlayHandle_t h[2] = {0, 0};
    ov->FindOverlay(const_cast<char *>(layer.key.c_str()), &h[0]);
    std::string half = layer.key + ".half";
    ov->FindOverlay(const_cast<char *>(half.c_str()), &h[1]);
    float worst = 0;
    int missed = 0, points = 0;
    for (float fx : {-0.49f, -0.3f, -0.1f, 0.1f, 0.3f, 0.49f})
      for (float fy : {-0.45f, 0.f, 0.45f}) {
        const float x = fx * layer.width, y = fy * layer.height;
        // the window has no surface past its own edge
        if (layer.z == 0 && (std::fabs(x) > l.width / 2 || std::fabs(y) > l.height / 2)) continue;
        const HmdMatrix34_t m = multiply(p.matrix, bendPointXY(b, x, y, layer.z));
        VROverlayIntersectionParams_t q{};
        q.eOrigin = ETrackingUniverseOrigin_TrackingUniverseStanding;
        for (int r = 0; r < 3; r++) {
          q.vSource.v[r] = m.m[r][3] + m.m[r][2] * 0.5f;
          q.vDirection.v[r] = -m.m[r][2];
        }
        float best = 1e9f;
        for (int k = 0; k < 2; k++) {
          auto handle = h[k];
          if (!handle || !ov->IsOverlayVisible(handle)) continue;
          VROverlayIntersectionResults_t res{};
          if (ov->ComputeOverlayIntersection(handle, &q, &res)) {
            best = std::min(best, std::fabs(res.fDistance - 0.5f));
            if (verbose)
              printf("    %s x=%+.2f y=%+.2f overlay %d: off %.1f mm, uv %.3f %.3f\n", layer.name, x, y, k,
                     (res.fDistance - 0.5f) * 1000, res.vUVs.v[0], res.vUVs.v[1]);
          }
        }
        if (verbose && best > 1e8f) printf("    %s x=%+.2f y=%+.2f: missed\n", layer.name, x, y);
        points++;
        if (best > 1e8f) missed++;
        else worst = std::max(worst, best);
      }
    const bool ok = missed == 0 && worst < 0.006f;
    failures += !ok;
    printf("%-9s %d points: %s, worst %.1f mm off the expected surface%s\n", layer.name, points,
           missed ? (std::to_string(missed) + " missed").c_str() : "all found", worst * 1000,
           ok ? "" : "  <-- FAIL");
  }
  shutdown();
  printf(failures ? "FAIL\n" : "pass\n");
  return failures ? 3 : 0;
}
