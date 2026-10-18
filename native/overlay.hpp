#pragma once
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-variable"
#include "vendor/openvr/openvr_capi.h"
#pragma GCC diagnostic pop
#include "video.hpp"
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
  GLuint texture = 0;
  GLuint yuvTextures[3] = {}, yuvFramebuffer = 0, yuvProgram = 0;
  int yuvWidth = 0, yuvHeight = 0;
  Picture::Format yuvFormat = Picture::Format::RGBA;
  int width = 0, height = 0;
  bool noVR = false;
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
        "uniform float yOffset; uniform float yScale; "
        "uniform float chromaOffset; uniform float rCr; "
        "uniform float gCb; uniform float gCr; uniform float bCb;\n"
        "void main(){ vec2 q=gl_TexCoord[0].st; "
        "float yy=(texture2D(yTexture,q).r-yOffset)*yScale; "
        "float cb; float cr; "
        "if(nv12!=0){ vec4 chroma=texture2D(uTexture,q); "
        "cb=chroma.r-chromaOffset; cr=chroma.a-chromaOffset; } "
        "else { cb=texture2D(uTexture,q).r-chromaOffset; "
        "cr=texture2D(vTexture,q).r-chromaOffset; } "
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
  void ensureYuvTextures(const Picture &p) {
    ensureYuvProgram();
    if (yuvWidth == p.width && yuvHeight == p.height &&
        yuvFormat == p.format)
      return;
    if (!yuvTextures[0])
      glGenTextures(3, yuvTextures);
    if (!yuvTextures[0] || !yuvTextures[1] ||
        (p.format == Picture::Format::YUV420P && !yuvTextures[2]))
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
    allocate(yuvTextures[0], p.width, p.height, GL_LUMINANCE);
    allocate(yuvTextures[1], cw, ch,
             p.format == Picture::Format::NV12 ? GL_LUMINANCE_ALPHA
                                                : GL_LUMINANCE);
    if (p.format == Picture::Format::YUV420P)
      allocate(yuvTextures[2], cw, ch, GL_LUMINANCE);
    glBindTexture(GL_TEXTURE_2D, yuvTextures[1]);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    if (p.format == Picture::Format::YUV420P) {
      glBindTexture(GL_TEXTURE_2D, yuvTextures[2]);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    }
    yuvWidth = p.width;
    yuvHeight = p.height;
    yuvFormat = p.format;
  }
  void convertYuv(const Picture &p) {
    ensureYuvTextures(p);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, yuvTextures[0]);
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, p.width, p.height,
                    GL_LUMINANCE, GL_UNSIGNED_BYTE, p.y.data());
    glActiveTexture(GL_TEXTURE1);
    glBindTexture(GL_TEXTURE_2D, yuvTextures[1]);
    if (p.format == Picture::Format::NV12) {
      glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, (p.width + 1) / 2,
                      (p.height + 1) / 2, GL_LUMINANCE_ALPHA,
                      GL_UNSIGNED_BYTE, p.uv.data());
    } else {
      glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, (p.width + 1) / 2,
                      (p.height + 1) / 2, GL_LUMINANCE, GL_UNSIGNED_BYTE,
                      p.u.data());
      glActiveTexture(GL_TEXTURE2);
      glBindTexture(GL_TEXTURE_2D, yuvTextures[2]);
      glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, (p.width + 1) / 2,
                      (p.height + 1) / 2, GL_LUMINANCE, GL_UNSIGNED_BYTE,
                      p.v.data());
    }
    glActiveTexture(GL_TEXTURE0);
    glBindFramebuffer(GL_FRAMEBUFFER, yuvFramebuffer);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D,
                           texture, 0);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE)
      throw std::runtime_error("YUV output framebuffer is incomplete");
    glViewport(0, 0, p.width, p.height);
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
    uniform1i("nv12", p.format == Picture::Format::NV12 ? 1 : 0);
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
  Placement placement;
  std::string key;
  int lastSubmitError = 0;
  const char *exitReason = "running";
  double syncMs = 0, submitMs = 0;
  double gpuConvertMs = 0;
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
    if (yuvFramebuffer)
      glDeleteFramebuffers(1, &yuvFramebuffer);
    if (yuvProgram)
      glDeleteProgram(yuvProgram);
    if (yuvTextures[0] || yuvTextures[1] || yuvTextures[2])
      glDeleteTextures(3, yuvTextures);
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
    if (p.format == Picture::Format::RGBA) {
      gpuConvertMs = 0;
      glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
      glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, p.width, p.height, GL_RGBA,
                      GL_UNSIGNED_BYTE, p.rgba.data());
    } else {
      double convertStart = nowMs();
      convertYuv(p);
      gpuConvertMs = nowMs() - convertStart;
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
