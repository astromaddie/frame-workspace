#pragma once
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-variable"
#include "vendor/openvr/openvr_capi.h"
#pragma GCC diagnostic pop
#include "video.hpp"
#include "gaze_bridge.hpp"
#include <SDL.h>
#define GL_GLEXT_PROTOTYPES 1
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
  // One output texture plus its own source planes per slot. With the fence
  // handoff, the GPU converts one picture while the CPU uploads the next; a
  // slot is reused only after SteamVR has copied a newer submitted texture
  // (vrclient blits each SetOverlayTexture source inside this GL context).
  struct Slot {
    GLuint texture = 0;
    GLuint yuv[3] = {};
    int yuvWidth = 0, yuvHeight = 0;
    Picture::Format yuvFormat = Picture::Format::RGBA;
    int width = 0, height = 0;
    GLsync fence = nullptr;
    std::shared_ptr<Picture> picture;
    double staged = 0;
  };
  static constexpr int maxSlots = 3;
  Slot slots[maxSlots];
  std::deque<int> inFlight; // oldest first
  int displayed = -1;
  std::vector<GLuint> retired; // previous-size textures, deleted after a submit
  GLuint texture = 0;          // the last submitted texture
  GLuint yuvFramebuffer = 0, yuvProgram = 0;
  int width = 0, height = 0;   // dimensions of the last submitted texture
  bool noVR = false;
  std::unique_ptr<GazeBridge> gazeBridge;
  // The render thread owns placement and texture dimensions. Gaze uses only
  // this snapshot, protected together with runtime geometry mutations and UV
  // calibration. Never hold this mutex across GL work or texture submission.
  std::mutex gazeGeometryMutex;
  Placement gazePlacement;
  int gazeWidth = 0, gazeHeight = 0;
  bool gazeTextureChanging = false;
  bool gazeCalibrated = false, gazeFlipX = false, gazeFlipY = false;
  double lastGazeCalibration = 0;
  bool gazeIntersection(const EyeRay &ray, float &u, float &v) {
    VROverlayIntersectionParams_t params{};
    params.eOrigin = ETrackingUniverseOrigin_TrackingUniverseStanding;
    for(int i=0;i<3;++i) { params.vSource.v[i]=ray.origin[i]; params.vDirection.v[i]=ray.direction[i]; }
    VROverlayIntersectionResults_t result{};
    if(!overlay->ComputeOverlayIntersection(handle,&params,&result)) return false;
    u=result.vUVs.v[0];v=result.vUVs.v[1];
    return std::isfinite(u) && std::isfinite(v) && u>=0 && u<=1 && v>=0 && v<=1 &&
        std::isfinite(result.fDistance) && result.fDistance>0;
  }
  bool calibrateGaze() {
    if(!gazeWidth || !gazeHeight) return false;
    const auto &placement = gazePlacement;
    const float h=placement.width * float(gazeHeight)/float(gazeWidth);
    float us[4]{},vs[4]{};
    const float positions[4][2]{{-.1f*placement.width,0},{.1f*placement.width,0},{0,.25f*h},{0,-.25f*h}};
    for(int i=0;i<4;++i) {
      EyeRay ray;
      for(int r=0;r<3;++r) {
        ray.origin[r]=placement.matrix.m[r][3]+placement.matrix.m[r][0]*positions[i][0]+
            placement.matrix.m[r][1]*positions[i][1]+placement.matrix.m[r][2];
        ray.direction[r]=-placement.matrix.m[r][2];
      }
      if(!gazeIntersection(ray,us[i],vs[i])) return false;
    }
    return uvOrientation(us[0],us[1],vs[2],vs[3],gazeFlipX,gazeFlipY);
  }
  void check(EVROverlayError error, const char *operation) {
    if (error)
      throw std::runtime_error(std::string(operation) +
                               " failed code=" + std::to_string(int(error)));
  }

  static GLuint compileShader(GLenum kind, const char *source) {
    GLuint shader = glCreateShader(kind);
    if (!shader)
      throw std::runtime_error("YUV shader allocation failed");
    glShaderSource(shader, 1, &source, nullptr);
    glCompileShader(shader);
    GLint ok = GL_FALSE;
    glGetShaderiv(shader, GL_COMPILE_STATUS, &ok);
    if (ok != GL_TRUE) {
      GLint length = 0;
      glGetShaderiv(shader, GL_INFO_LOG_LENGTH, &length);
      std::string log(size_t(std::max(length, 1)), '\0');
      glGetShaderInfoLog(shader, GLint(log.size()), nullptr, log.data());
      glDeleteShader(shader);
      throw std::runtime_error("YUV shader compilation failed: " + log);
    }
    return shader;
  }
  void ensureYuvProgram() {
    if (yuvProgram)
      return;
    static const char *vertex =
        "#version 120\n"
        "void main(){ gl_Position=gl_Vertex; gl_TexCoord[0]=gl_MultiTexCoord0; }\n";
    static const char *fragment =
        "#version 120\n"
        "uniform sampler2D yTexture; uniform sampler2D uTexture; "
        "uniform sampler2D vTexture; uniform int nv12;\n"
        "uniform int warpMode; uniform vec2 sourceSize; uniform vec2 encodedSize; "
        "uniform vec2 coreOrigin; uniform vec2 coreSize;\n"
        "uniform float yOffset; uniform float yScale; "
        "uniform float chromaOffset; uniform float rCr; "
        "uniform float gCb; uniform float gCr; uniform float bCb;\n"
        "float compressAxis(float x,float a,float c){ return x<a ? x*0.5 : "
        "(x<a+c ? a*0.5+x-a : a*0.5+c+(x-a-c)*0.5); }\n"
        "void main(){ vec2 q=gl_TexCoord[0].st; "
        "if(warpMode==2){ vec2 p=q*sourceSize; "
        "bool focal=all(greaterThanEqual(p,coreOrigin)) && all(lessThan(p,coreOrigin+coreSize)); "
        "vec2 tile=vec2(focal ? coreSize.x : 0.0,0.0); "
        "vec2 at=focal ? tile+p-coreOrigin : p*0.5; "
        "q=clamp(at,tile+vec2(0.5),tile+coreSize-vec2(0.5))/encodedSize; "
        "} else if(warpMode==1){ vec2 p=q*sourceSize; "
        "q=vec2(compressAxis(p.x,coreOrigin.x,coreSize.x), "
        "compressAxis(p.y,coreOrigin.y,coreSize.y))/encodedSize; } "
        "if(nv12<0){ gl_FragColor=texture2D(yTexture,q); return; } "
        "vec2 cq=q; if(warpMode==2){ "
        "vec2 tile=vec2(q.x<0.5 ? 0.0 : coreSize.x,0.0); "
        "cq=clamp(q*encodedSize,tile+vec2(1.0),tile+coreSize-vec2(1.0))/encodedSize; } "
        "float yy=(texture2D(yTexture,q).r-yOffset)*yScale; "
        "float cb; float cr; "
        "if(nv12!=0){ vec4 chroma=texture2D(uTexture,cq); "
        "cb=chroma.r-chromaOffset; cr=chroma.a-chromaOffset; } "
        "else { cb=texture2D(uTexture,cq).r-chromaOffset; "
        "cr=texture2D(vTexture,cq).r-chromaOffset; } "
        "gl_FragColor=vec4(yy+rCr*cr, yy+gCb*cb+gCr*cr, "
        "yy+bCb*cb, 1.0); }\n";
    GLuint vs = compileShader(GL_VERTEX_SHADER, vertex);
    GLuint fs = 0;
    try {
      fs = compileShader(GL_FRAGMENT_SHADER, fragment);
      yuvProgram = glCreateProgram();
      if (!yuvProgram)
        throw std::runtime_error("YUV program allocation failed");
      glAttachShader(yuvProgram, vs);
      glAttachShader(yuvProgram, fs);
      glLinkProgram(yuvProgram);
      GLint linked = GL_FALSE;
      glGetProgramiv(yuvProgram, GL_LINK_STATUS, &linked);
      if (linked != GL_TRUE) {
        GLint length = 0;
        glGetProgramiv(yuvProgram, GL_INFO_LOG_LENGTH, &length);
        std::string log(size_t(std::max(length, 1)), '\0');
        glGetProgramInfoLog(yuvProgram, GLint(log.size()), nullptr, log.data());
        throw std::runtime_error("YUV shader link failed: " + log);
      }
    } catch (...) {
      if (yuvProgram) {
        glDeleteProgram(yuvProgram);
        yuvProgram = 0;
      }
      if (fs)
        glDeleteShader(fs);
      glDeleteShader(vs);
      throw;
    }
    glDeleteShader(vs);
    glDeleteShader(fs);
    if (!yuvFramebuffer)
      glGenFramebuffers(1, &yuvFramebuffer);
    if (!yuvFramebuffer)
      throw std::runtime_error("YUV framebuffer allocation failed");
  }
  void ensureYuvTextures(Slot &s, const Picture &p) {
    ensureYuvProgram();
    if (s.yuvWidth == p.width && s.yuvHeight == p.height &&
        s.yuvFormat == p.format)
      return;
    if (!s.yuv[0])
      glGenTextures(3, s.yuv);
    if (!s.yuv[0] || !s.yuv[1] ||
        (p.format == Picture::Format::YUV420P && !s.yuv[2]))
      throw std::runtime_error("YUV texture allocation failed");
    const int cw = (p.width + 1) / 2, ch = (p.height + 1) / 2;
    auto allocate = [](GLuint id, int w, int h, GLenum format) {
      glBindTexture(GL_TEXTURE_2D, id);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
      glTexImage2D(GL_TEXTURE_2D, 0, format, w, h, 0, format,
                   GL_UNSIGNED_BYTE, nullptr);
    };
    allocate(s.yuv[0], p.width, p.height, p.format == Picture::Format::RGBA ? GL_RGBA : GL_LUMINANCE);
    if (p.format == Picture::Format::RGBA) {
      s.yuvWidth = p.width; s.yuvHeight = p.height; s.yuvFormat = p.format;
      return;
    }
    allocate(s.yuv[1], cw, ch,
             p.format == Picture::Format::NV12 ? GL_LUMINANCE_ALPHA
                                                : GL_LUMINANCE);
    if (p.format == Picture::Format::YUV420P)
      allocate(s.yuv[2], cw, ch, GL_LUMINANCE);
    glBindTexture(GL_TEXTURE_2D, s.yuv[1]);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    if (p.format == Picture::Format::YUV420P) {
      glBindTexture(GL_TEXTURE_2D, s.yuv[2]);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    }
    s.yuvWidth = p.width;
    s.yuvHeight = p.height;
    s.yuvFormat = p.format;
  }
  void convertYuv(Slot &s, const Picture &p) {
    ensureYuvTextures(s, p);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, s.yuv[0]);
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, p.width, p.height,
                    p.format == Picture::Format::RGBA ? GL_RGBA : GL_LUMINANCE, GL_UNSIGNED_BYTE,
                    p.format == Picture::Format::RGBA ? p.rgba.data() : p.y.data());
    glActiveTexture(GL_TEXTURE1);
    glBindTexture(GL_TEXTURE_2D, s.yuv[1]);
    if (p.format == Picture::Format::NV12) {
      glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, (p.width + 1) / 2,
                      (p.height + 1) / 2, GL_LUMINANCE_ALPHA,
                      GL_UNSIGNED_BYTE, p.uv.data());
    } else if (p.format == Picture::Format::YUV420P) {
      glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, (p.width + 1) / 2,
                      (p.height + 1) / 2, GL_LUMINANCE, GL_UNSIGNED_BYTE,
                      p.u.data());
      glActiveTexture(GL_TEXTURE2);
      glBindTexture(GL_TEXTURE_2D, s.yuv[2]);
      glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, (p.width + 1) / 2,
                      (p.height + 1) / 2, GL_LUMINANCE, GL_UNSIGNED_BYTE,
                      p.v.data());
    }
    glActiveTexture(GL_TEXTURE0);
    glBindFramebuffer(GL_FRAMEBUFFER, yuvFramebuffer);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D,
                           s.texture, 0);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE)
      throw std::runtime_error("YUV output framebuffer is incomplete");
    glViewport(0, 0, p.outputWidth(), p.outputHeight());
    glDisable(GL_BLEND);
    glDisable(GL_DEPTH_TEST);
    glDisable(GL_SCISSOR_TEST);
    glUseProgram(yuvProgram);
    auto uniform1i = [&](const char *name, int value) {
      GLint loc = glGetUniformLocation(yuvProgram, name);
      if (loc < 0)
        throw std::runtime_error(std::string("YUV uniform missing: ") + name);
      glUniform1i(loc, value);
    };
    auto uniform1f = [&](const char *name, float value) {
      GLint loc = glGetUniformLocation(yuvProgram, name);
      if (loc < 0)
        throw std::runtime_error(std::string("YUV uniform missing: ") + name);
      glUniform1f(loc, value);
    };
    uniform1i("yTexture", 0);
    uniform1i("uTexture", 1);
    uniform1i("vTexture", 2);
    uniform1i("nv12", p.format == Picture::Format::RGBA ? -1 : (p.format == Picture::Format::NV12 ? 1 : 0));
    uniform1i("warpMode", p.layout.atlas ? 2 : (p.layout.warped ? 1 : 0));
    auto uniform2f = [&](const char *name, float x, float y) {
      glUniform2f(glGetUniformLocation(yuvProgram, name), x, y);
    };
    uniform2f("sourceSize", p.outputWidth(), p.outputHeight());
    uniform2f("encodedSize", p.width, p.height);
    uniform2f("coreOrigin", p.layout.left, p.layout.top);
    uniform2f("coreSize", p.layout.coreWidth, p.layout.coreHeight);
    const bool full = p.colorRange == AVCOL_RANGE_JPEG;
    const bool bt709 = p.colorSpace == AVCOL_SPC_BT709;
    uniform1f("yOffset", full ? 0.f : 16.f / 255.f);
    uniform1f("yScale", full ? 1.f : 255.f / 219.f);
    uniform1f("chromaOffset", full ? .5f : 128.f / 255.f);
    uniform1f("rCr", bt709 ? (full ? 1.5748f : 1.792741f)
                            : (full ? 1.402f : 1.596027f));
    uniform1f("gCb", bt709 ? (full ? -.187324f : -.213249f)
                            : (full ? -.344136f : -.391762f));
    uniform1f("gCr", bt709 ? (full ? -.468124f : -.532909f)
                            : (full ? -.714136f : -.812968f));
    uniform1f("bCb", bt709 ? (full ? 1.8556f : 2.112402f)
                            : (full ? 1.772f : 2.017232f));
    glBegin(GL_QUADS);
    glTexCoord2f(0, 0); glVertex2f(-1, -1);
    glTexCoord2f(1, 0); glVertex2f(1, -1);
    glTexCoord2f(1, 1); glVertex2f(1, 1);
    glTexCoord2f(0, 1); glVertex2f(-1, 1);
    glEnd();
    glUseProgram(0);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    glActiveTexture(GL_TEXTURE0);
    if (glGetError() != GL_NO_ERROR)
      throw std::runtime_error("YUV shader conversion failed");
  }

public:
  struct CurvatureReadback {
    bool readable = false;
    float value = 0;
    int errorCode = -1;
  };
  struct CurvatureResult {
    bool supported = false;
    bool applied = false;
    float requested = 0;
    bool hasValue = false;
    float value = 0;
    int errorCode = -1;
  };
  Placement placement;
  std::string key;
  int lastSubmitError = 0;
  const char *exitReason = "running";
  double syncMs = 0, submitMs = 0;
  double gpuConvertMs = 0;
  // Fence handoff: time from issuing a picture's GL work to observing its
  // completion (GPU latency plus polling granularity), and blocking waits.
  double gpuLatencyMs = 0;
  uint64_t blockingWaits = 0;
  // "fence" overlaps GPU conversion with the next upload; "finish" is the
  // previous serial glFinish handoff, kept for comparison and rollback.
  std::string handoff = "fence";
  struct Completion {
    std::shared_ptr<Picture> picture;
    bool submitted = false;
    GLuint texture = 0;
    int width = 0, height = 0;
  };
  Overlay(bool test, const std::string &overlayKey, bool foveation = false)
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
    if (foveation) gazeBridge = std::make_unique<GazeBridge>(library);
    char name[] = "Frame Workspace";
    check(overlay->CreateOverlay(key.data(), name, &handle), "CreateOverlay");
    check(overlay->SetOverlayTextureColorSpace(handle,
                                               EColorSpace_ColorSpace_Gamma),
          "color space");
    check(overlay->SetOverlayInputMethod(handle, VROverlayInputMethod_None),
          "input method");
    // Pictures are stored top row first (the first uploaded or rendered row
    // is GL's t = 0), but SteamVR reads GL textures bottom-up. Flip V so the
    // desktop is upright in the headset; readback dumps are unaffected.
    VRTextureBounds_t bounds{0, 1, 1, 0};
    check(overlay->SetOverlayTextureBounds(handle, &bounds), "texture bounds");
  }
  const char *gaze(double &u, double &v) {
    if(!gazeBridge) return "unavailable";
    std::lock_guard<std::mutex> lock(gazeGeometryMutex);
    if(gazeTextureChanging) return "overlay-resizing";
    if(!gazeCalibrated && nowMs()-lastGazeCalibration>=1000) {
      lastGazeCalibration=nowMs();gazeCalibrated=calibrateGaze();
    }
    if(!gazeCalibrated) return "overlay-calibration-unavailable";
    EyeRay rays[2];
    const char *status=gazeBridge->sample(rays);
    if(std::string(status)!="valid") return status;
    float us[2]{},vs[2]{};
    for(int i=0;i<2;++i) if(!gazeIntersection(rays[i],us[i],vs[i])) return "outside-overlay";
    u=(us[0]+us[1])*.5;v=(vs[0]+vs[1])*.5;
    if(gazeFlipX) u=1-u;
    if(gazeFlipY) v=1-v;
    return "valid";
  }
  ~Overlay() {
    // The main-scope GazeProducer has already joined before this object dies.
    if (handle) {
      overlay->HideOverlay(handle);
      overlay->ClearOverlayTexture(handle);
      overlay->DestroyOverlay(handle);
    }
    if (shutdown)
      shutdown();
    if (yuvFramebuffer)
      glDeleteFramebuffers(1, &yuvFramebuffer);
    if (yuvProgram)
      glDeleteProgram(yuvProgram);
    for (auto &s : slots) {
      if (s.fence)
        glDeleteSync(s.fence);
      if (s.yuv[0] || s.yuv[1] || s.yuv[2])
        glDeleteTextures(3, s.yuv);
      if (s.texture)
        glDeleteTextures(1, &s.texture);
    }
    if (!retired.empty())
      glDeleteTextures(GLsizei(retired.size()), retired.data());
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
    std::lock_guard<std::mutex> lock(gazeGeometryMutex);
    gazeCalibrated = false; lastGazeCalibration = 0;
    gazePlacement = placement;
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
  CurvatureReadback curvatureReadback() const {
    CurvatureReadback result;
    if (noVR || !overlay || !overlay->GetOverlayCurvature)
      return result;
    float value = 0;
    auto error = overlay->GetOverlayCurvature(handle, &value);
    result.errorCode = int(error);
    if (error == EVROverlayError_VROverlayError_None && std::isfinite(value) &&
        value >= 0.f && value <= 1.f) {
      result.readable = true;
      result.value = value;
    } else if (error == EVROverlayError_VROverlayError_None) {
      result.errorCode = -2; // OpenVR returned an invalid value.
    }
    return result;
  }
  CurvatureResult setCurvature(float requested) {
    std::lock_guard<std::mutex> lock(gazeGeometryMutex);
    gazeCalibrated = false; lastGazeCalibration = 0;
    CurvatureResult result;
    result.requested = requested;
    if (!std::isfinite(requested) || requested < 0.f || requested > 1.f) {
      result.errorCode = -3; // Invalid request; do not call OpenVR.
      return result;
    }
    if (noVR || !overlay || !overlay->SetOverlayCurvature ||
        !overlay->GetOverlayCurvature)
      return result;
    auto setError = overlay->SetOverlayCurvature(handle, requested);
    if (setError != EVROverlayError_VROverlayError_None) {
      result.errorCode = int(setError);
      auto current = curvatureReadback();
      result.hasValue = current.readable;
      result.value = current.value;
      return result;
    }
    auto current = curvatureReadback();
    result.supported = current.readable;
    result.hasValue = current.readable;
    result.value = current.value;
    result.errorCode = current.readable ? 0 : current.errorCode;
    result.applied = current.readable && std::fabs(current.value - requested) <= 0.0001f;
    if (current.readable && !result.applied)
      result.errorCode = -4; // The command ran, but readback did not confirm it.
    return result;
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
  bool fenced() const { return handoff == "fence"; }
  bool busy() const { return !inFlight.empty(); }
  // Issues one picture's upload and conversion. The finish handoff completes
  // and submits it here. The fence handoff returns once the work is flushed;
  // it completes the oldest in-flight picture first only if no slot is free.
  void stage(std::shared_ptr<Picture> picture, std::vector<Completion> &done) {
    const Picture &p = *picture;
    if (fenced() && int(inFlight.size()) >= maxSlots - 1)
      complete(-1, done);
    int index = 0;
    if (fenced())
      while (index < maxSlots &&
             (index == displayed ||
              std::find(inFlight.begin(), inFlight.end(), index) != inFlight.end()))
        ++index;
    if (index >= maxSlots)
      throw std::runtime_error("no free texture slot");
    Slot &s = slots[index];
    if (!s.texture || p.outputWidth() != s.width || p.outputHeight() != s.height) {
      // New names on resize avoid stale SteamVR texture metadata. The old one
      // may still be displayed, so it is deleted after the next submission.
      if (s.texture)
        retired.push_back(s.texture);
      s.texture = 0;
      glGenTextures(1, &s.texture);
      glBindTexture(GL_TEXTURE_2D, s.texture);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
      glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, p.outputWidth(), p.outputHeight(), 0, GL_RGBA,
                   GL_UNSIGNED_BYTE, nullptr);
      s.width = p.outputWidth();
      s.height = p.outputHeight();
    } else
      glBindTexture(GL_TEXTURE_2D, s.texture);
    if (p.format == Picture::Format::RGBA && !p.layout.warped) {
      gpuConvertMs = 0;
      glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
      glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, p.width, p.height, GL_RGBA,
                      GL_UNSIGNED_BYTE, p.rgba.data());
    } else {
      double convertStart = nowMs();
      convertYuv(s, p);
      gpuConvertMs = nowMs() - convertStart;
    }
    s.picture = std::move(picture);
    if (fenced()) {
      if (s.fence)
        glDeleteSync(s.fence);
      s.fence = glFenceSync(GL_SYNC_GPU_COMMANDS_COMPLETE, 0);
      if (s.fence) {
        glFlush();
        if (glGetError() != GL_NO_ERROR)
          throw std::runtime_error("GL upload failed");
        s.staged = nowMs();
        inFlight.push_back(index);
        return;
      }
      // Without ARB_sync this is the serial handoff; inFlight is empty here.
      fprintf(stderr, "[native] GL fences unavailable; using finish handoff\n");
      handoff = "finish";
    }
    // Complete producer work before SteamVR imports the texture. Frame Zink
    // has shown incomplete images with flush alone. This wait occurs only
    // for new desktop pictures, never at the compositor head-tracking rate.
    double syncStart = nowMs();
    glFlush();
    glFinish();
    syncMs = nowMs() - syncStart;
    if (glGetError() != GL_NO_ERROR)
      throw std::runtime_error("GL upload failed");
    done.push_back(submit(index));
  }
  // Submits in-flight pictures whose GL work has completed, oldest first, so
  // SteamVR never imports a texture the GPU is still writing (the same
  // completion guarantee as glFinish). waitMs bounds the wait for the oldest:
  // 0 polls, negative blocks.
  void complete(double waitMs, std::vector<Completion> &done) {
    while (!inFlight.empty()) {
      const int index = inFlight.front();
      Slot &s = slots[index];
      const GLuint64 timeout =
          waitMs < 0 ? GLuint64(2000000000) : GLuint64(waitMs * 1e6);
      double waitStart = nowMs();
      GLenum result = glClientWaitSync(s.fence, 0, timeout);
      double waited = nowMs() - waitStart;
      if (result == GL_WAIT_FAILED)
        throw std::runtime_error("GL fence wait failed");
      if (result == GL_TIMEOUT_EXPIRED) {
        if (waitMs < 0)
          throw std::runtime_error("GL work did not complete within 2 s");
        break;
      }
      if (waitMs < 0)
        ++blockingWaits;
      syncMs = waited;
      glDeleteSync(s.fence);
      s.fence = nullptr;
      gpuLatencyMs = nowMs() - s.staged;
      inFlight.pop_front();
      done.push_back(submit(index));
      waitMs = 0;
    }
  }
  Completion submit(int index) {
    Slot &s = slots[index];
    Completion c{std::move(s.picture), true, s.texture, s.width, s.height};
    width = s.width;
    height = s.height;
    double submitStart = nowMs();
    if (!noVR) {
      // A resize changes the runtime's intersection aspect ratio. Invalidate
      // only around submission, so gaze keeps using the displayed geometry
      // while GL conversion/upload works on the replacement texture.
      bool sizeChanged = false;
      {
        std::lock_guard<std::mutex> lock(gazeGeometryMutex);
        sizeChanged = width != gazeWidth || height != gazeHeight || gazeTextureChanging;
        if (sizeChanged) {
          HmdVector2_t mouseScale{{float(width), float(height)}};
          check(overlay->SetOverlayMouseScale(handle, &mouseScale),
                "mouse scale");
          gazeTextureChanging = true;
          gazeCalibrated = false; lastGazeCalibration = 0;
        }
      }
      Texture_t t{reinterpret_cast<void *>(uintptr_t(s.texture)),
                  ETextureType_TextureType_OpenGL,
                  EColorSpace_ColorSpace_Gamma};
      lastSubmitError = int(overlay->SetOverlayTexture(handle, &t));
      c.submitted = lastSubmitError == 0;
      if (sizeChanged) {
        std::lock_guard<std::mutex> lock(gazeGeometryMutex);
        if (c.submitted) { gazeWidth = width; gazeHeight = height; }
        // A failed submission may have changed runtime geometry. Keep gaze
        // invalid until a successful retry establishes the displayed size.
        gazeTextureChanging = !c.submitted;
      }
      if (c.submitted)
        check(overlay->ShowOverlay(handle), "ShowOverlay");
    }
    submitMs = nowMs() - submitStart;
    if (c.submitted) {
      texture = s.texture;
      displayed = index;
      if (!retired.empty()) {
        glDeleteTextures(GLsizei(retired.size()), retired.data());
        retired.clear();
      }
    }
    return c;
  }
  // Reads back the texture a completion submitted; a later completion in the
  // same batch may already have replaced the displayed one.
  void dump(const std::string &path, const Completion &c) {
    const int width = c.width, height = c.height;
    std::vector<uint8_t> rgba(size_t(width) * height * 4);
    glBindTexture(GL_TEXTURE_2D, c.texture);
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
