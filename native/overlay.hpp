#pragma once
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-variable"
#include "vendor/openvr/openvr_capi.h"
#pragma GCC diagnostic pop
#include "video.hpp"
#include <SDL.h>
#include <SDL_opengl.h>
#include <dlfcn.h>
#include <sys/stat.h>
namespace fw {
inline void atomicWrite(const std::string &path, const std::string &text) {
  if (path.empty())
    return;
  auto tmp = path + ".tmp";
  {
    std::ofstream f(tmp);
    if (!f || !(f << text << "\n"))
      throw std::runtime_error("state write failed");
  }
  chmod(tmp.c_str(), 0600);
  if (rename(tmp.c_str(), path.c_str()))
    throw std::runtime_error("state replace failed");
}
struct Placement {
  float width = 2.4f, distance = 2.0f;
  HmdMatrix34_t matrix{{{1, 0, 0, 0}, {0, 1, 0, 1.3f}, {0, 0, 1, -2}}};
};
inline std::string placementJSON(const Placement &p) {
  std::string s = "{\"version\":1,\"width\":" + std::to_string(p.width) +
                  ",\"distance\":" + std::to_string(p.distance) +
                  ",\"matrix\":[";
  for (int r = 0; r < 3; r++)
    for (int c = 0; c < 4; c++) {
      if (r || c)
        s += ",";
      s += std::to_string(p.matrix.m[r][c]);
    }
  return s + "]}";
}
inline bool loadPlacement(const std::string &path, Placement &p) {
  if (path.empty())
    return false;
  try {
    auto j = json(readFile(path));
    auto a = field(j, "matrix");
    if (num(j, "version") != 1 || !a || json_object_array_length(a) != 12)
      return false;
    Placement candidate;
    candidate.width = std::clamp(num(j, "width", 2.4), .4, 8.0);
    candidate.distance = std::clamp(num(j, "distance", 2), .4, 10.0);
    for (int r = 0; r < 3; r++)
      for (int c = 0; c < 4; c++) {
        double v =
            json_object_get_double(json_object_array_get_idx(a, r * 4 + c));
        if (!std::isfinite(v) || fabs(v) > 100)
          return false;
        candidate.matrix.m[r][c] = v;
      }
    for (int r = 0; r < 3; r++)
      for (int s = 0; s < 3; s++) {
        double dot = 0;
        for (int c = 0; c < 3; c++)
          dot += candidate.matrix.m[r][c] * candidate.matrix.m[s][c];
        if (fabs(dot - (r == s ? 1 : 0)) > .02)
          return false;
      }
    p = candidate;
    return true;
  } catch (...) {
    return false;
  }
}
class Overlay {
  SDL_Window *window = nullptr;
  SDL_GLContext context = nullptr;
  void *library = nullptr;
  void (*shutdown)() = nullptr;
  VR_IVROverlay_FnTable *overlay = nullptr;
  VR_IVRSystem_FnTable *system = nullptr;
  VROverlayHandle_t handle = 0;
  GLuint texture = 0;
  int width = 0, height = 0;
  bool noVR = false;
  void check(EVROverlayError error, const char *operation) {
    if (error)
      throw std::runtime_error(std::string(operation) +
                               " failed code=" + std::to_string(int(error)));
  }

public:
  Placement placement;
  std::string key;
  int lastSubmitError = 0;
  const char *exitReason = "running";
  double syncMs = 0, submitMs = 0;
  Overlay(bool test, const std::string &overlayKey)
      : noVR(test), key(overlayKey) {
    if (SDL_Init(SDL_INIT_VIDEO))
      throw std::runtime_error("SDL video unavailable");
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION, 2);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION, 1);
    SDL_GL_SetAttribute(SDL_GL_DOUBLEBUFFER, 0);
    window = SDL_CreateWindow("Frame Workspace Native", 0, 0, 32, 32,
                              SDL_WINDOW_OPENGL | SDL_WINDOW_HIDDEN);
    if (!window)
      throw std::runtime_error(std::string("GL window unavailable: ") +
                               SDL_GetError());
    context = SDL_GL_CreateContext(window);
    if (!context)
      throw std::runtime_error("GL context unavailable");
    fprintf(stderr, "[native] GL vendor=%s renderer=%s\n",
            glGetString(GL_VENDOR), glGetString(GL_RENDERER));
    if (noVR)
      return;
    const char *paths[] = {"/opt/steamvr/bin/linuxarm64/libopenvr_api.so",
                           "/opt/steamvr/lib/linuxarm64/libopenvr_api.so",
                           "libopenvr_api.so"};
    for (auto path : paths) {
      library = dlopen(path, RTLD_NOW | RTLD_LOCAL);
      if (library)
        break;
    }
    if (!library)
      throw std::runtime_error("OpenVR loader unavailable");
    auto init =
        reinterpret_cast<intptr_t (*)(EVRInitError *, EVRApplicationType)>(
            dlsym(library, "VR_InitInternal"));
    auto get = reinterpret_cast<intptr_t (*)(const char *, EVRInitError *)>(
        dlsym(library, "VR_GetGenericInterface"));
    shutdown =
        reinterpret_cast<void (*)()>(dlsym(library, "VR_ShutdownInternal"));
    if (!init || !get || !shutdown)
      throw std::runtime_error("OpenVR exports unavailable");
    EVRInitError error = EVRInitError_VRInitError_None;
    init(&error, EVRApplicationType_VRApplication_Overlay);
    if (error)
      throw std::runtime_error("OpenVR init failed code=" +
                               std::to_string(int(error)));
    overlay = reinterpret_cast<VR_IVROverlay_FnTable *>(
        get("FnTable:IVROverlay_027", &error));
    if (error || !overlay)
      throw std::runtime_error("OpenVR overlay 027 unavailable");
    system = reinterpret_cast<VR_IVRSystem_FnTable *>(
        get("FnTable:IVRSystem_022", &error));
    if (error || !system)
      throw std::runtime_error("OpenVR system 022 unavailable");
    char name[] = "Frame Workspace";
    check(overlay->CreateOverlay(key.data(), name, &handle), "CreateOverlay");
    check(overlay->SetOverlayTextureColorSpace(handle,
                                               EColorSpace_ColorSpace_Gamma),
          "color space");
    check(overlay->SetOverlayInputMethod(handle, VROverlayInputMethod_None),
          "input method");
    VRTextureBounds_t bounds{0, 0, 1, 1};
    check(overlay->SetOverlayTextureBounds(handle, &bounds), "texture bounds");
  }
  ~Overlay() {
    if (handle) {
      overlay->HideOverlay(handle);
      overlay->ClearOverlayTexture(handle);
      overlay->DestroyOverlay(handle);
    }
    if (shutdown)
      shutdown();
    if (texture)
      glDeleteTextures(1, &texture);
    if (context)
      SDL_GL_DeleteContext(context);
    if (window)
      SDL_DestroyWindow(window);
    SDL_Quit();
    if (library)
      dlclose(library);
  }
  bool recenter() {
    if (noVR)
      return true;
    TrackedDevicePose_t pose{};
    system->GetDeviceToAbsoluteTrackingPose(
        ETrackingUniverseOrigin_TrackingUniverseStanding, 0, &pose, 1);
    if (!pose.bPoseIsValid)
      return false;
    auto &m = pose.mDeviceToAbsoluteTracking;
    float x = m.m[0][2], z = m.m[2][2], length = std::hypot(x, z);
    if (length < .01)
      return false;
    x /= length;
    z /= length;
    placement.matrix = {{{z, 0, x, m.m[0][3] - x * placement.distance},
                         {0, 1, 0, m.m[1][3]},
                         {-x, 0, z, m.m[2][3] - z * placement.distance}}};
    return true;
  }
  void resize(float w, float d) {
    w = std::clamp(w, .4f, 8.f);
    d = std::clamp(d, .4f, 10.f);
    float delta = d - placement.distance;
    for (int r = 0; r < 3; r++)
      placement.matrix.m[r][3] -= placement.matrix.m[r][2] * delta;
    placement.width = w;
    placement.distance = d;
    apply();
  }
  void apply() {
    if (noVR)
      return;
    check(overlay->SetOverlayWidthInMeters(handle, placement.width), "width");
    check(overlay->SetOverlayTransformAbsolute(
              handle, ETrackingUniverseOrigin_TrackingUniverseStanding,
              &placement.matrix),
          "world transform");
  }
  void focus() {
    if (!noVR && texture)
      check(overlay->ShowOverlay(handle), "focus overlay");
  }
  bool events() {
    SDL_Event s;
    while (SDL_PollEvent(&s))
      if (s.type == SDL_QUIT) {
        exitReason = "sdl-quit";
        return false;
      }
    if (noVR)
      return true;
    VREvent_t e{};
    while (system->PollNextEvent(&e, sizeof(e)))
      if (e.eventType == EVREventType_VREvent_Quit) {
        exitReason = "openvr-system-quit";
        system->AcknowledgeQuit_Exiting();
        return false;
      }
    while (overlay->PollNextOverlayEvent(handle, &e, sizeof(e)))
      if (e.eventType == EVREventType_VREvent_Quit) {
        exitReason = "openvr-overlay-quit";
        return false;
      }
    return true;
  }
  bool upload(const Picture &p) {
    GLuint old = 0;
    if (!texture || p.width != width || p.height != height) {
      old = texture;
      glGenTextures(1, &texture);
      glBindTexture(GL_TEXTURE_2D, texture);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
      glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, p.width, p.height, 0, GL_RGBA,
                   GL_UNSIGNED_BYTE, nullptr);
      width = p.width;
      height = p.height;
      if (!noVR) {
        HmdVector2_t mouseScale{{float(width), float(height)}};
        check(overlay->SetOverlayMouseScale(handle, &mouseScale),
              "mouse scale");
      }
    } else
      glBindTexture(GL_TEXTURE_2D, texture);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, p.width, p.height, GL_RGBA,
                    GL_UNSIGNED_BYTE, p.rgba.data());
    // Complete producer work before SteamVR imports the texture. Frame Zink
    // has shown incomplete images with flush alone. This wait occurs only
    // for new desktop pictures, never at the compositor head-tracking rate.
    double syncStart = nowMs();
    glFlush();
    glFinish();
    syncMs = nowMs() - syncStart;
    if (glGetError() != GL_NO_ERROR)
      throw std::runtime_error("GL upload failed");
    bool success = true;
    double submitStart = nowMs();
    if (!noVR) {
      Texture_t t{reinterpret_cast<void *>(uintptr_t(texture)),
                  ETextureType_TextureType_OpenGL,
                  EColorSpace_ColorSpace_Gamma};
      lastSubmitError = int(overlay->SetOverlayTexture(handle, &t));
      success = lastSubmitError == 0;
      if (success)
        check(overlay->ShowOverlay(handle), "ShowOverlay");
    }
    submitMs = nowMs() - submitStart;
    if (old)
      glDeleteTextures(1, &old);
    return success;
  }
  void dump(const std::string &path) {
    std::vector<uint8_t> rgba(size_t(width) * height * 4);
    glBindTexture(GL_TEXTURE_2D, texture);
    glGetTexImage(GL_TEXTURE_2D, 0, GL_RGBA, GL_UNSIGNED_BYTE, rgba.data());
    if (glGetError() != GL_NO_ERROR)
      throw std::runtime_error("GPU readback failed");
    std::ofstream f(path, std::ios::binary);
    f << "P6\n" << width << " " << height << "\n255\n";
    for (size_t i = 0; i < rgba.size(); i += 4)
      f.write(reinterpret_cast<const char *>(rgba.data() + i), 3);
    if (!f)
      throw std::runtime_error("dump write failed");
  }
};
} // namespace fw
