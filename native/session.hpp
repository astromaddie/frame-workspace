#pragma once
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-variable"
#include "vendor/openvr/openvr_capi.h"
#pragma GCC diagnostic pop
#include "gaze_bridge.hpp"
#include "chrome.hpp"
#include "overlay_budget.hpp"
#include <SDL.h>
#define GL_GLEXT_PROTOTYPES 1
#include <SDL_opengl.h>
#include <dlfcn.h>
#include <memory>
#include <map>
#include <cstdio>
#include <stdexcept>
#include <string>

namespace fw {
// The host's sole GL context and OpenVR client. Panels own only their overlays,
// textures and stream workers, and must be destroyed before this session.
class OverlaySession {
  SDL_Window *window = nullptr;
  SDL_GLContext context = nullptr;
  void (*shutdown)() = nullptr;
  bool initialized = false;
  std::string manifestPath;
  OverlayBudget budget{nativeOverlayBudgetFromEnvironment(), dashboardOverlayReserve};
  std::map<std::string, WallTransitionLog> wallTransitionLogs;
  void printWallTransition(const std::string &key, const WallTransitionSummary &summary) {
    fprintf(stderr, "[native-host] frame walls %s key=%s overlays=%d/%d\n",
            summary.restored ? "restored" : "dropped", key.c_str(),
            summary.count, summary.budget);
  }
  void release() {
    flushWallTransitionLogs(true);
    if (cursorHandle && overlay) destroyOverlay(cursorHandle);
    if (cursorTexture) glDeleteTextures(1,&cursorTexture);
    if (initialized && shutdown) shutdown();
    if (context) SDL_GL_DeleteContext(context);
    if (window) SDL_DestroyWindow(window);
    SDL_Quit();
    if (library) dlclose(library);
  }
public:
  using GetInterface = intptr_t (*)(const char *, EVRInitError *);
  const bool noVR;
  bool sharedHost = false;
  EVROverlayError createOverlay(const std::string &key, const std::string &name,
                                VROverlayHandle_t *handle) {
    return budget.createRequired(key, name, handle, [&](const auto &k, const auto &n, auto *h) {
      return overlay->CreateOverlay(const_cast<char *>(k.c_str()), const_cast<char *>(n.c_str()), h);
    });
  }
  EVROverlayError createOptionalWallOverlay(const void *owner, const std::string &key,
                                            const std::string &name,
                                            VROverlayHandle_t *handle) {
    return budget.createOptional(owner, key, name, handle, [&](const auto &k, const auto &n, auto *h) {
      return overlay->CreateOverlay(const_cast<char *>(k.c_str()), const_cast<char *>(n.c_str()), h);
    });
  }
  bool destroyOverlay(VROverlayHandle_t handle) {
    return budget.destroy(handle, [&](auto h) { return overlay->DestroyOverlay(h); });
  }
  void updateOptionalWallPriorities(const std::vector<WallPriority> &priorities,
                                    const std::string &focusedKey) {
    budget.updateWallPriorities(priorities, focusedKey);
  }
  void recordWallTransition(const std::string &key, bool restored) {
    auto &log = wallTransitionLogs[key];
    WallTransitionSummary summary;
    if (log.record(restored, overlayCount(), overlayBudgetLimit(),
                   WallTransitionLog::Clock::now(), summary))
      printWallTransition(key, summary);
  }
  void flushWallTransitionLogs(bool force = false) {
    auto now = WallTransitionLog::Clock::now();
    for (auto &[key, log] : wallTransitionLogs) {
      WallTransitionSummary summary;
      if (log.flush(now, force, summary)) printWallTransition(key, summary);
    }
  }
  void registerOptionalWalls(const void *owner, std::string key,
                             std::function<bool()> drop, std::function<bool()> restore) {
    budget.registerWalls(owner, std::move(key), std::move(drop), std::move(restore));
  }
  void unregisterOptionalWalls(const void *owner) { budget.unregisterWalls(owner); }
  void updateOptionalWalls(const void *owner, float gazeAngle, uint64_t focusOrder, bool focused) {
    budget.updateWalls(owner, gazeAngle, focusOrder, focused);
  }
  void reconcileOptionalWalls() { budget.reconcileWalls(); }
  int overlayCount() const { return budget.diagnosticCount(); }
  int overlayBudgetLimit() const { return budget.totalBudget(); }
  int hostOverlayCount() const { return int(budget.count()); }
  int reservedOverlayCount() const { return budget.reserved(); }
  VROverlayHandle_t cursorHandle = 0;
  GLuint cursorTexture = 0;
  CursorOwnership cursor;
  void hideCursor(const void *owner) {
    if(!cursor.release(owner))return;
    if(cursorHandle)overlay->HideOverlay(cursorHandle);

  }
  void showCursor(const void *owner,HmdMatrix34_t pose,uint32_t order,bool focused) {
    if(!cursor.claim(owner,!sharedHost || focused)){hideCursor(owner);return;}
    if(!cursorHandle) {
      std::string key="com.frame-workspace.cursor."+std::to_string(getpid());
      if(createOverlay(key,"Frame Workspace Pointer",&cursorHandle))
        throw std::runtime_error("shared cursor overlay unavailable");
      VRTextureBounds_t bounds{0,1,1,0};overlay->SetOverlayTextureBounds(cursorHandle,&bounds);
      overlay->SetOverlayFlag(cursorHandle,VROverlayFlags_IsPremultiplied,true);
      overlay->SetOverlayInputMethod(cursorHandle,VROverlayInputMethod_None);
      overlay->SetOverlayWidthInMeters(cursorHandle,.014f);
      cursorTexture=createCursorTexture();glFinish();
      Texture_t t{reinterpret_cast<void *>(uintptr_t(cursorTexture)),ETextureType_TextureType_OpenGL,EColorSpace_ColorSpace_Gamma};
      overlay->SetOverlayTexture(cursorHandle,&t);
    }
    overlay->SetOverlaySortOrder(cursorHandle,order);
    overlay->SetOverlayTransformAbsolute(cursorHandle,ETrackingUniverseOrigin_TrackingUniverseStanding,&pose);
    overlay->ShowOverlay(cursorHandle);
  }
  void *library = nullptr;
  GetInterface get = nullptr;
  VR_IVROverlay_FnTable *overlay = nullptr;
  VR_IVRSystem_FnTable *system = nullptr;
  VR_IVRInput_FnTable *input = nullptr;
  VRActionSetHandle_t actionSet = 0;
  VRActionHandle_t aim = 0, grab = 0, squeeze = 0, switcher = 0, haptic = 0;
  VRInputValueHandle_t hands[2]{};
  struct Hand {
    bool valid = false, grab = false, squeeze = false, switcher = false;
    HmdMatrix34_t pose{};
    EyeRay ray() const {
      EyeRay r;
      for (int i = 0; i < 3; ++i) {
        r.origin[i] = pose.m[i][3]; r.direction[i] = -pose.m[i][2];
      }
      return r;
    }
  } hand[2];
  std::unique_ptr<GazeBridge> eyes;
  const char *exitReason = "running";
  // Counts SteamVR recenters (seated or standing zero pose resets), which move
  // the room under anything pinned in it.
  unsigned zeroPoseResets = 0;

  explicit OverlaySession(bool test) : noVR(test) {
    try {
      if (SDL_Init(SDL_INIT_VIDEO)) throw std::runtime_error("SDL video unavailable");
      SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION, 2);
      SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION, 1);
      SDL_GL_SetAttribute(SDL_GL_DOUBLEBUFFER, 0);
      window = SDL_CreateWindow("Frame Workspace Native", 0, 0, 32, 32,
                                SDL_WINDOW_OPENGL | SDL_WINDOW_HIDDEN);
      if (!window) throw std::runtime_error(std::string("GL window unavailable: ") + SDL_GetError());
      context = SDL_GL_CreateContext(window);
      if (!context) throw std::runtime_error("GL context unavailable");
      fprintf(stderr, "[native] GL vendor=%s renderer=%s\n", glGetString(GL_VENDOR), glGetString(GL_RENDERER));
      if (noVR) return;
      for (auto path : {"/opt/steamvr/bin/linuxarm64/libopenvr_api.so",
                        "/opt/steamvr/lib/linuxarm64/libopenvr_api.so", "libopenvr_api.so"}) {
        library = dlopen(path, RTLD_NOW | RTLD_LOCAL);
        if (library) break;
      }
      if (!library) throw std::runtime_error("OpenVR loader unavailable");
      auto init = reinterpret_cast<intptr_t (*)(EVRInitError *, EVRApplicationType)>(dlsym(library, "VR_InitInternal"));
      get = reinterpret_cast<GetInterface>(dlsym(library, "VR_GetGenericInterface"));
      shutdown = reinterpret_cast<void (*)()>(dlsym(library, "VR_ShutdownInternal"));
      if (!init || !get || !shutdown) throw std::runtime_error("OpenVR exports unavailable");
      EVRInitError error = EVRInitError_VRInitError_None;
      init(&error, EVRApplicationType_VRApplication_Overlay);
      if (error) throw std::runtime_error("OpenVR init failed code=" + std::to_string(int(error)));
      initialized = true;
      overlay = reinterpret_cast<VR_IVROverlay_FnTable *>(get("FnTable:IVROverlay_027", &error));
      if (error || !overlay) throw std::runtime_error("OpenVR overlay 027 unavailable");
      system = reinterpret_cast<VR_IVRSystem_FnTable *>(get("FnTable:IVRSystem_022", &error));
      if (error || !system) throw std::runtime_error("OpenVR system 022 unavailable");
      eyes = std::make_unique<GazeBridge>(library);
    } catch (...) { release(); throw; }
  }
  OverlaySession(const OverlaySession &) = delete;
  OverlaySession &operator=(const OverlaySession &) = delete;
  ~OverlaySession() { release(); }

  bool initInput(const std::string &manifest) {
    if (noVR || manifest.empty()) return false;
    if (input) return manifest == manifestPath;
    EVRInitError error = EVRInitError_VRInitError_None;
    auto table = reinterpret_cast<VR_IVRInput_FnTable *>(get("FnTable:IVRInput_010", &error));
    std::string path = manifest;
    if (error || !table || table->SetActionManifestPath(path.data()) ||
        table->GetActionSetHandle((char *)"/actions/workspace", &actionSet) ||
        table->GetActionHandle((char *)"/actions/workspace/in/aim", &aim) ||
        table->GetActionHandle((char *)"/actions/workspace/in/grab", &grab) ||
        table->GetActionHandle((char *)"/actions/workspace/in/squeeze", &squeeze) ||
        table->GetActionHandle((char *)"/actions/workspace/out/haptic", &haptic) ||
        table->GetInputSourceHandle((char *)"/user/hand/left", &hands[0]) ||
        table->GetInputSourceHandle((char *)"/user/hand/right", &hands[1])) return false;
    table->GetActionHandle((char *)"/actions/workspace/in/switcher", &switcher);
    input = table; manifestPath = manifest;
    return true;
  }
  // Pump the single runtime event queue and sample both hands once per host
  // frame. Every panel sees the same press/release and predicted pose.
  bool events() {
    SDL_Event s;
    while (SDL_PollEvent(&s)) if (s.type == SDL_QUIT) { exitReason = "sdl-quit"; return false; }
    if (noVR) return true;
    VREvent_t event{};
    while (system->PollNextEvent(&event, sizeof event))
      if (event.eventType == EVREventType_VREvent_Quit) {
        exitReason = "openvr-system-quit";
        system->AcknowledgeQuit_Exiting(); return false;
      } else if (event.eventType == EVREventType_VREvent_SeatedZeroPoseReset ||
                 event.eventType == EVREventType_VREvent_StandingZeroPoseReset) {
        ++zeroPoseResets;
      }
    for (auto &h : hand) h = {};
    if (!input) return true;
    VRActiveActionSet_t active{};
    active.ulActionSet = actionSet;
    active.nPriority = 0x01000000;
    if (input->UpdateActionState(&active, sizeof active, 1)) return true;
    for (int i = 0; i < 2; ++i) {
      InputPoseActionData_t pose{};
      InputDigitalActionData_t g{}, s{}, sw{};
      input->GetPoseActionDataForNextFrame(aim, ETrackingUniverseOrigin_TrackingUniverseStanding,
                                         &pose, sizeof pose, hands[i]);
      input->GetDigitalActionData(grab, &g, sizeof g, hands[i]);
      input->GetDigitalActionData(squeeze, &s, sizeof s, hands[i]);
      if(switcher) input->GetDigitalActionData(switcher, &sw, sizeof sw, hands[i]);
      hand[i].switcher = sw.bActive && sw.bState;
      hand[i].valid = pose.bActive && pose.pose.bPoseIsValid && pose.pose.bDeviceIsConnected;
      hand[i].pose = pose.pose.mDeviceToAbsoluteTracking;
      hand[i].grab = g.bActive && g.bState;
      hand[i].squeeze = s.bActive && s.bState;
    }
    return true;
  }
};
} // namespace fw
