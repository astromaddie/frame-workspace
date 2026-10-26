#pragma once
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-variable"
#include "vendor/openvr/openvr_capi.h"
#pragma GCC diagnostic pop
#include "video.hpp"
#include "gaze_bridge.hpp"
#include "session.hpp"
#include "placement.hpp"
#include "handles.hpp"
#include "chrome.hpp"
#include "depth_render.hpp"
#include "bend.hpp"
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
// A small Palladium rim is part of an auxiliary stream's texture, not window
// chrome: these panels never allocate handles, bezels or placement input.
class SurfaceRenderer {
  GLuint program = 0, framebuffer = 0;
public:
  ~SurfaceRenderer() { release(); }
  void release() {
    if (program) glDeleteProgram(program);
    if (framebuffer) glDeleteFramebuffers(1, &framebuffer);
    program = framebuffer = 0;
  }
  void render(GLuint source, GLuint target, int width, int height, const PanelSurface &surface = {}, bool transposed = false, bool sourceTransposed = true) {
    if (!program) {
      auto shader = [](GLenum type, const char *src) {
        GLuint sh = glCreateShader(type);
        glShaderSource(sh, 1, &src, nullptr); glCompileShader(sh);
        GLint ok = 0; glGetShaderiv(sh, GL_COMPILE_STATUS, &ok);
        if (!ok) { glDeleteShader(sh); throw std::runtime_error("surface shader compilation failed"); }
        return sh;
      };
      auto vs = shader(GL_VERTEX_SHADER, "#version 120\nvoid main(){gl_Position=gl_Vertex;gl_TexCoord[0]=gl_MultiTexCoord0;}");
      std::string fragment = R"GLSL(#version 120
uniform sampler2D picture; uniform vec2 size; uniform vec4 pressedRect; uniform vec3 uTip; uniform float uPx;
)GLSL" + ChromeRenderer::materialSource() + R"GLSL(

uniform int role, transposed, sourceTransposed; uniform vec2 metres, pictureSize; uniform float fullHeight, inset; uniform vec2 recessOffset;
vec4 samplePicture(vec2 uv) { return texture2D(picture,sourceTransposed!=0?vec2(1.-uv.y,uv.x):uv); }
vec3 panelMetal(vec2 p, vec3 normal) {
  float t=clamp(dot(p/(vec2(metres.x,fullHeight)*.5),vec2(.9063,-.4226))*.5+.5,0.,1.);
  vec3 top=vec3(.984,.980,.992), face=vec3(.890,.875,.918), bottom=vec3(.796,.769,.839);
  vec3 base=t<.48?mix(top,face,t/.48):mix(face,bottom,(t-.48)/.52);
  return base*(.68+.42*dot(normal,vec3(-.4511,.5514,.7018)))+
      vec3(pow(max(dot(normal,vec3(-.2497,.3052,.9190)),0.),40.)*.32);
}
void main(){
  vec2 coord=transposed!=0?vec2(gl_TexCoord[0].y,1.-gl_TexCoord[0].x):gl_TexCoord[0].xy;
  if(role==2){ // Status belongs to the shelf; no separate floating rim.
    vec4 image=samplePicture(coord);
    gl_FragColor=image; return;
  }
  if(role==1 && fullHeight>0.) {
    vec2 p=coord*metres, halfSize=vec2(metres.x,fullHeight)*.5;
    float radius=min(.008,halfSize.y*.45);
    vec2 q=abs(p-halfSize)-halfSize+radius;
    float edge=length(max(q,0.))+min(max(q.x,q.y),0.)-radius;
    vec2 signP=sign(p-halfSize), grad=q.x>q.y?vec2(signP.x,0.):vec2(0.,signP.y);
    if(q.x>0.&&q.y>0.) grad=normalize(q)*signP;
    float px=max(metres.x/size.x,metres.y/size.y), inside=-edge;
    vec3 n=normalize(vec3(grad.x,-grad.y,1.)*vec3(inside<.003?.75:0.,inside<.003?.75:0.,1.));
    vec3 panelColor=panelMetal(vec2(p.x-metres.x*.5,fullHeight*.5-p.y),n);
    if(inside<px*1.2) panelColor=mix(panelColor,vec3(.553,.510,.627),.6);
    vec2 uv=(p-vec2(inset))/pictureSize;
    bool content=uv.x>=0.&&uv.x<=1.&&uv.y>=0.&&uv.y<=1.&&p.x<metres.x-inset;
    vec4 image=samplePicture(clamp(uv,0.,1.));
    vec3 color=content?image.rgb+panelColor*(1.-image.a):panelColor;
    if(pressedRect.z>0. && pressedRect.w>0.) {
      vec2 halfRect=pressedRect.zw*.5, g;
      float d=sdBox(p-pressedRect.xy-halfRect,halfRect,min(.004,halfRect.y*.3),g);
      float inside=max(-d,0.), R=halfRect.y, band=.38*R;
      // Deep concave walls: upper-left shadow and a lit lower-right lip.
      vec3 normal=normalize(vec3(inside<band?-vec2(g.x,-g.y)*1.8*(1.-smoothstep(0.,band,inside)):vec2(0.),1.));
      vec3 c=lit(metal(dot(vec2(p.x-pressedRect.x-halfRect.x,halfRect.y-p.y+pressedRect.y),SHEEN)/(2.*halfRect.x)+.5,true),normal);
      if(inside<px*1.2)c=mix(c,vec3(.36,.56,.77),.55);
      c=innerGlow(c,inside,.75*R);
      vec2 fromTopLeft=p-pressedRect.xy;
      float lipShadow=max(exp(-fromTopLeft.x/(.22*R)),exp(-fromTopLeft.y/(.22*R)));
      c*=.94-.28*lipShadow;
      // Sample the captured legend at the bottom of the recess, down/right
      // by 0.8 capture points. Replace the original glyph, never double it.
      vec4 legend=samplePicture(clamp(uv-recessOffset/pictureSize,0.,1.));
      // Infer the captured selection fill from its quiet top/bottom corners.
      // This keeps antialiased ink from transparent fixtures, light menus and
      // white-on-accent/dark menus; all use readable dark ink in the recess.
      vec2 a=(pressedRect.xy+pressedRect.zw*vec2(.08,.10)-vec2(inset))/pictureSize;
      vec2 b=(pressedRect.xy+pressedRect.zw*vec2(.92,.90)-vec2(inset))/pictureSize;
      vec4 bg=(samplePicture(vec2(a.x,a.y))+samplePicture(vec2(b.x,a.y))+
               samplePicture(vec2(a.x,b.y))+samplePicture(vec2(b.x,b.y)))*.25;
      vec3 foreground=legend.rgb/max(legend.a,.001), background=bg.rgb/max(bg.a,.001);
      float contrast=length(foreground-background);
      vec3 inkSource=dot(background,vec3(.2126,.7152,.0722))<.55?vec3(1.):vec3(0.);
      float ink=legend.a*(bg.a<.5?1.:clamp((contrast-.03)/max(length(inkSource-background)-.03,.1),0.,1.));
      c=mix(c,vec3(.085,.105,.14)*(1.-.28*lipShadow),ink);
      color=mix(color,c,clamp(.5-d/px,0.,1.));
    }
    float alpha=clamp(.5-edge/px,0.,1.);
    gl_FragColor=vec4(color*alpha,alpha); return;
  }

  vec2 q=coord,p=q*size,r=vec2(min(6.,size.y*.18));
  vec2 d=abs(p-size*.5)-(size*.5-r);
  float edge=length(max(d,0.))+min(max(d.x,d.y),0.)-r.x;
  float a=1.-smoothstep(-1.,0.,edge),rim=smoothstep(-3.,-1.,edge);
  vec3 metal=mix(vec3(.985,.98,.995),vec3(.79,.76,.84),q.y*.75+q.x*.25);
  metal+=rim*(.12*(1.-q.y)-.12*q.y);
  vec4 image=samplePicture(q);
  vec3 content=image.rgb+metal*(1.-image.a);
  gl_FragColor=vec4(mix(content,metal,rim)*a,a);
})GLSL";
      auto fs = shader(GL_FRAGMENT_SHADER, fragment.c_str());
      program = glCreateProgram(); glAttachShader(program, vs); glAttachShader(program, fs);
      glLinkProgram(program); glDeleteShader(vs); glDeleteShader(fs);
      GLint ok = 0; glGetProgramiv(program, GL_LINK_STATUS, &ok);
      if (!ok) throw std::runtime_error("surface shader link failed");
      glGenFramebuffers(1, &framebuffer);
    }
    GLint viewport[4]; glGetIntegerv(GL_VIEWPORT, viewport);
    glBindTexture(GL_TEXTURE_2D, target);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    GLint w = 0, h = 0;
    glGetTexLevelParameteriv(GL_TEXTURE_2D, 0, GL_TEXTURE_WIDTH, &w);
    glGetTexLevelParameteriv(GL_TEXTURE_2D, 0, GL_TEXTURE_HEIGHT, &h);
    if (w != width || h != height)
      glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, width, height, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
    glBindFramebuffer(GL_FRAMEBUFFER, framebuffer);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, target, 0);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE)
      throw std::runtime_error("surface framebuffer incomplete");
    glViewport(0, 0, width, height); glDisable(GL_BLEND); glDisable(GL_DEPTH_TEST);
    glDisable(GL_SCISSOR_TEST); glUseProgram(program);
    glActiveTexture(GL_TEXTURE0); glBindTexture(GL_TEXTURE_2D, source);
    glUniform1i(glGetUniformLocation(program, "picture"), 0);
    glUniform2f(glGetUniformLocation(program, "size"), transposed ? height : width, transposed ? width : height);
    glUniform1i(glGetUniformLocation(program, "transposed"), transposed);
    glUniform1i(glGetUniformLocation(program, "sourceTransposed"), transposed && sourceTransposed);
    glUniform1i(glGetUniformLocation(program, "role"), surface.role == PanelRole::Menubar ? 1 : surface.role == PanelRole::Status ? 2 : 0);
    glUniform2f(glGetUniformLocation(program, "metres"), surface.placement.width, surface.height);
    glUniform2f(glGetUniformLocation(program, "pictureSize"), surface.pictureWidth, surface.pictureHeight);
    glUniform1f(glGetUniformLocation(program, "fullHeight"), surface.fullHeight);
    glUniform1f(glGetUniformLocation(program, "inset"), surface.inset);
    glUniform2f(glGetUniformLocation(program, "recessOffset"),surface.recessOffset,surface.recessOffset);
    const auto &r=surface.pressedRect;
    glUniform4f(glGetUniformLocation(program,"pressedRect"),r.x,r.y,r.w,r.h);
    glBegin(GL_QUADS);
    glTexCoord2f(0,0); glVertex2f(-1,-1); glTexCoord2f(1,0); glVertex2f(1,-1);
    glTexCoord2f(1,1); glVertex2f(1,1); glTexCoord2f(0,1); glVertex2f(-1,1);
    glEnd(); glUseProgram(0); glBindFramebuffer(GL_FRAMEBUFFER, 0);
    glViewport(viewport[0], viewport[1], viewport[2], viewport[3]);
    if (glGetError() != GL_NO_ERROR) throw std::runtime_error("surface rendering failed");
  }
};
class Overlay {
  std::shared_ptr<OverlaySession> session;
  VR_IVROverlay_FnTable *overlay = nullptr;
  VR_IVRSystem_FnTable *system = nullptr;
  VROverlayHandle_t handle = 0;
  // The window's second half, shown only while its halves bend differently.
  VROverlayHandle_t handle2 = 0;
  bool showingHalf = false;
  // The last picture shown, kept to redraw it when the bend changes direction.
  std::shared_ptr<Picture> shownPicture;
  bool shownTransposed = false;
  // One output texture plus its own source planes per slot. With the fence
  // handoff, the GPU converts one picture while the CPU uploads the next; a
  // slot is reused only after SteamVR has copied a newer submitted texture
  // (vrclient blits each SetOverlayTexture source inside this GL context).
  struct Slot {
    GLuint texture = 0, surfaceSource = 0;
    GLuint yuv[3] = {};
    int yuvWidth = 0, yuvHeight = 0;
    Picture::Format yuvFormat = Picture::Format::RGBA;
    int width = 0, height = 0;   // the texture (transposed: the picture's height x width)
    bool transposed = false;     // drawn a quarter turn, for a top-to-bottom bend
    GLsync fence = nullptr;
    std::shared_ptr<Picture> picture;
    double staged = 0;
  };
  static constexpr int maxSlots = 4;
  Slot slots[maxSlots];
  std::deque<int> inFlight; // GL work issued, fence pending; oldest first
  std::deque<int> ready;    // GL work complete, waiting for a paced slot
  uint64_t lastPresentVsync = 0, lastVsyncCounter = 0;
  bool havePresentVsync = false;
  double lastVsyncChange = 0, vsyncPeriodMs = 0, lastPeriodRead = 0;
  int displayed = -1;
  std::vector<GLuint> retired; // previous-size textures, deleted after a submit
  GLuint texture = 0;          // the last submitted texture
  GLuint yuvFramebuffer = 0, yuvProgram = 0;
  int width = 0, height = 0;   // dimensions of the last submitted texture
  // VR-first placement (FW-14): the viewer reads the controllers itself through
  // SteamVR Input (SteamVR's own laser only serves its dashboard). Each window
  // has a Palladium bezel meeting its picture at 3 cm, four rearward walls,
  // controls just ahead and a host-shared white-hot laser tip.
  VR_IVRInput_FnTable *input = nullptr;
  VRActionSetHandle_t actionSet = 0;
  VRActionHandle_t aimAction = 0, grabAction = 0, squeezeAction = 0, hapticAction = 0;
  VRInputValueHandle_t hands[2] = {};
  VROverlayHandle_t frameHandle = 0, chromeHandle = 0;
  VROverlayHandle_t frameHandle2 = 0, chromeHandle2 = 0;  // their second halves, for uneven bends
  GLuint frameTexture = 0, chromeTexture = 0;
  VROverlayHandle_t wallHandles[4]{};
  GLuint wallTextures[4]{};
  bool wallGroupRegistered = false, wallTextureReady = false;
  FrameWallRenderer wallRenderer;
  ChromeRenderer chromeRenderer;
  ChromeState chromeState, chromeDrawn, frameDrawn, wallDrawn;
  bool chromeShown = false, chromeFailed = false, closing = false;
  double lastChromeUpdate = 0;
  HandleDrag drag;
  int dragHand = -1;
  bool dragBySqueeze = false;           // the drag was started with the grip button
  bool grabWas[2] = {}, squeezeWas[2] = {};
  Handle hotWas[2] = {Handle::None, Handle::None};
  HandleReveal handleReveal[handleCount];
public:
  bool closeRequested = false; // the Close button was selected in the headset
private:
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
  bool createWalls() {
    if (noVR) return false;
    // A failed prior restore can leave a handle whose destruction also
    // failed. Release that tracked residue before retrying, rather than
    // overwriting the only reference to it.
    for (auto &handle : wallHandles) if (handle) {
      overlay->HideOverlay(handle);
      if (!session->destroyOverlay(handle)) return false;
      handle = 0;
    }
    auto discardCreated = [&](int last) {
      for (int j = 0; j <= last; ++j) if (wallHandles[j]) {
        overlay->HideOverlay(wallHandles[j]);
        if (session->destroyOverlay(wallHandles[j])) wallHandles[j] = 0;
      }
    };
    for (int i = 0; i < 4; ++i) {
      std::string wallKey = key + ".wall" + std::to_string(i);
      if (session->createOptionalWallOverlay(this, wallKey, name, &wallHandles[i])) {
        discardCreated(i - 1);
        return false;
      }
      const auto h = wallHandles[i];
      VRTextureBounds_t bounds{0, 1, 1, 0};
      if (overlay->SetOverlayTextureBounds(h, &bounds) ||
          overlay->SetOverlayInputMethod(h, VROverlayInputMethod_None) ||
          overlay->SetOverlayFlag(h, VROverlayFlags_IsPremultiplied, true) ||
          overlay->SetOverlaySortOrder(h, spatialForeground ? 51 : 1)) {
        discardCreated(i);
        return false;
      }
    }
    wallTextureReady = false;
    session->recordWallTransition(key, true);
    return true;
  }
  bool destroyWalls() {
    bool released = true;
    for (auto &h : wallHandles) if (h) {
      overlay->HideOverlay(h);
      if (session->destroyOverlay(h)) h = 0;
      else released = false;
    }
    if (released) {
      wallTextureReady = false;
      session->recordWallTransition(key, false);
    }
    return released;
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
    glViewport(0, 0, s.width, s.height);
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
    // Even unused samplers must name complete textures on strict GL drivers.
    // RGBA transpose has only plane 0; NV12 has planes 0 and 1.
    uniform1i("uTexture", p.format == Picture::Format::RGBA ? 0 : 1);
    uniform1i("vTexture", p.format == Picture::Format::YUV420P ? 2 :
                             p.format == Picture::Format::RGBA ? 0 : 1);
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
    if (s.transposed) {
      // A quarter turn (top row first in both): output (u, v) shows the
      // picture at (v, 1 - u), so u runs up the picture from its bottom edge.
      glTexCoord2f(0, 1); glVertex2f(-1, -1);
      glTexCoord2f(0, 0); glVertex2f(1, -1);
      glTexCoord2f(1, 0); glVertex2f(1, 1);
      glTexCoord2f(1, 1); glVertex2f(-1, 1);
    } else {
      glTexCoord2f(0, 0); glVertex2f(-1, -1);
      glTexCoord2f(1, 0); glVertex2f(1, -1);
      glTexCoord2f(1, 1); glVertex2f(1, 1);
      glTexCoord2f(0, 1); glVertex2f(-1, 1);
    }
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
  PanelRole role = PanelRole::Window;
  bool surfaceVisible = false;
  float surfaceHeight = 0;
  PanelSurface surfaceGeometry;
  SurfaceRenderer surfaceRenderer;
  bool auxiliary() const { return role != PanelRole::Window; }
  void showSurface(bool visible) {
    if (surfaceVisible == visible) return;
    surfaceVisible = visible;
    if (noVR || !handle) return;
    for (auto h : {handle, handle2}) if (h) {
      if (visible && texture && (h == handle || showingHalf)) overlay->ShowOverlay(h);
      else overlay->HideOverlay(h);
    }
  }
  void placeSurface(const PanelSurface &surface) {
    if (sameSurfaceGeometry(surfaceGeometry, surface)) return;
    placement = surface.placement; surfaceHeight = surface.height;
    surfaceGeometry = surface;
    apply();
    // A held still picture must follow every animation frame too.
    if (role == PanelRole::Menubar && displayed >= 0) {
      auto &slot = slots[displayed];
      surfaceRenderer.render(slot.surfaceSource, slot.texture, slot.width, slot.height, surfaceGeometry, slot.transposed);
      if (!noVR) {
        glFinish();
        Texture_t t{reinterpret_cast<void *>(uintptr_t(slot.texture)), ETextureType_TextureType_OpenGL, EColorSpace_ColorSpace_Auto};
        for (auto h : {handle, handle2}) if (h)
          check(overlay->SetOverlayTexture(h, &t), "menu reveal texture");
      }
    }
  }
  bool spatialForeground = false;
  // Preserve each panel's content/bezel/controls ordering while placing the
  // selected panel ahead of other panels, below the spatial UI's 99/100 band.
  void setSpatialForeground(bool selected) {
    if (selected == spatialForeground) return;
    spatialForeground = selected;
    if (noVR) return;
    const uint32_t base = selected ? 50 : 0;
    for (auto h : {handle, handle2}) if (h) overlay->SetOverlaySortOrder(h, base + 1);
    for (auto h : {frameHandle, frameHandle2}) if (h) overlay->SetOverlaySortOrder(h, base + 2);
    for (auto h : {chromeHandle, chromeHandle2}) if (h) overlay->SetOverlaySortOrder(h, base + 3);
    for(auto h:wallHandles)if(h)overlay->SetOverlaySortOrder(h,base+1);
    if(!selected)session->hideCursor(this);
  }
  FocusFeedback focusFeedback;
  GLuint spatialTexture() const { return texture; }
  bool spatialTransposed() const { return shownTransposed; }
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
    bool dropped = false; // superseded before its paced slot; not an error
  };
  // "paced" submits finished pictures in order on refresh boundaries, one per
  // contentRefreshes() headset refreshes, so 60 fps content shows for an even
  // two 120 Hz refreshes each. "immediate" submits as soon as GL work ends.
  std::string present = "paced";
  uint64_t pacedDrops = 0, pacedFallbacks = 0;
  uint64_t presentGaps[5] = {}; // refreshes between submissions: 1, 2, 3, 4+ (0 unused)
  std::string name = "Frame Workspace"; // shown by SteamVR
  Overlay(bool test, const std::string &overlayKey, bool foveation = false,
          const std::string &overlayName = "", const std::string &actionManifest = "",
          std::shared_ptr<OverlaySession> shared = {}, PanelRole panelRole = PanelRole::Window)
      : session(shared ? std::move(shared) : std::make_shared<OverlaySession>(test)),
        noVR(test), key(overlayKey) {
    role = panelRole;
    if (!overlayName.empty()) name = overlayName;
    overlay = session->overlay; system = session->system;
    if (noVR) return;
    if (foveation) gazeBridge = std::make_unique<GazeBridge>(session->library);
    try {
      check(session->createOverlay(key, name, &handle), "CreateOverlay");
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
      if (auxiliary()) {
        for (auto h : {handle, handle2}) if (h) {
          overlay->SetOverlaySortOrder(h, role == PanelRole::Menu ? 104 : role == PanelRole::Menubar ? 51 : 102);
          overlay->SetOverlayFlag(h, VROverlayFlags_IsPremultiplied, true);
        }
      }
      if (!auxiliary() && !actionManifest.empty())
        initPlacementInput(actionManifest);
    } catch (...) { release(); throw; }
  }
  // Loads the viewer's action set (aim pose + trigger "grab" per hand) and
  // creates the chrome and cursor overlays. Any failure leaves placement
  // input off; the window itself is unaffected.
  void initPlacementInput(const std::string &manifest) {
    if (!session->initInput(manifest)) {
      fprintf(stderr, "[native] placement input unavailable\n");
      return;
    }
    auto table = session->input;
    actionSet = session->actionSet; aimAction = session->aim; grabAction = session->grab;
    squeezeAction = session->squeeze; hapticAction = session->haptic;
    for (int i = 0; i < 2; ++i) hands[i] = session->hands[i];
    std::string frameKey=key+".frame", chromeKey=key+".handles";
    check(session->createOverlay(frameKey,name,&frameHandle),"frame overlay");
    check(session->createOverlay(chromeKey,name,&chromeHandle),"control overlay");
    VRTextureBounds_t bounds{0,1,1,0};
    for(auto h:{frameHandle,chromeHandle}) {
      overlay->SetOverlayTextureBounds(h,&bounds);overlay->SetOverlayInputMethod(h,VROverlayInputMethod_None);
      overlay->SetOverlayFlag(h,VROverlayFlags_IsPremultiplied,true);
      overlay->SetOverlaySortOrder(h,h==frameHandle?2:3);
    }
    overlay->SetOverlaySortOrder(handle,1);
    glGenTextures(1,&frameTexture);glGenTextures(1,&chromeTexture);glGenTextures(4,wallTextures);
    wallGroupRegistered = true;
    session->registerOptionalWalls(this, key, [this] { return destroyWalls(); },
                                   [this] { return createWalls(); });
    input = table;
    fprintf(stderr, "[native] placement input ready\n");
  }
  // The window's height in meters from its texture aspect (9:8 until the
  // first picture).
  float panelHeight() const {
    if (auxiliary() && surfaceHeight > 0) return surfaceHeight;
    return placement.width * (width > 0 && height > 0 ? float(height) / float(width) : 2560.f / 2880.f);
  }
  // A transform `z` in front of the window at window coordinates (x, y), on
  // its bent surface and facing out of it.
  HmdMatrix34_t panelPoint(float x, float y, float z) const {
    return multiply(placement.matrix, bendPointXY(shape(), x, y, z));
  }
  // Where a controller's pointing ray meets the layer `z` in front of the
  // window, in window coordinates (arc positions where it bends).
  bool layerHit(const HmdMatrix34_t &c, float z, float &x, float &y, float *distance = nullptr) const {
    const HmdMatrix34_t local = multiply(inverseRigid(placement.matrix), c);
    const float o[3] = {local.m[0][3], local.m[1][3], local.m[2][3]};
    const float d[3] = {-local.m[0][2], -local.m[1][2], -local.m[2][2]};
    return bendHit(shape(), o, d, z, x, y, distance);
  }
  bool wallHit(const HmdMatrix34_t &pose,float &x,float &y,float &distance,HmdMatrix34_t *point=nullptr) const {
    if (!frameWallGroupActive(wallHandles)) return false;
    auto local=multiply(inverseRigid(placement.matrix),pose);
    float o[3]={local.m[0][3],local.m[1][3],local.m[2][3]},d[3]={-local.m[0][2],-local.m[1][2],-local.m[2][2]};
    HandleLayout l;l.width=placement.width;l.height=panelHeight();
    return frameWallHit(shape(),l,o,d,x,y,distance,point);
  }
  // Used by the host to route each hand to the nearest window, while an
  // ongoing drag keeps ownership even after the ray leaves its panel.
  float interactionDistance(const HmdMatrix34_t &pose) const {
    if (!interactive() || auxiliary()) return -1;
    float x = 0, y = 0, distance = 0;
    HandleLayout l; l.width = placement.width; l.height = panelHeight();
    if(wallHit(pose,x,y,distance))return distance;
    if (!layerHit(pose, controlsDepth, x, y, &distance) ||
        std::fabs(x) > l.chromeWidth() / 2 || std::fabs(y) > l.chromeHeight() / 2) return -1;
    unsigned visible = 0;
    for (int i = 0; i < handleCount; ++i) if (chromeState.reveal[i] > .001f) visible |= 1u << i;
    if ((std::fabs(x) <= l.width / 2 + l.bezel() && std::fabs(y) <= l.height / 2 + l.bezel()) ||
        l.zone(x, y, visible) != Handle::None) return distance;
    return -1; // Transparent padding and hidden controls pass the ray through.
  }
  float revealDistance(const HmdMatrix34_t &pose) const {
    if (!interactive() || auxiliary()) return -1;
    float x = 0, y = 0, distance = 0;
    HandleLayout l; l.width = placement.width; l.height = panelHeight();
    if(wallHit(pose,x,y,distance))return distance;
    if (!layerHit(pose, controlsDepth, x, y, &distance)) return -1;
    if ((std::fabs(x) <= l.width/2+l.bezel() && std::fabs(y) <= l.height/2+l.bezel()) || l.revealed(x,y))
      return distance;
    return -1;
  }
  // A press here belongs to placement (a shown handle under the laser, or
  // this hand's drag), never to the window's content.
  bool handleAt(const HmdMatrix34_t &pose, int hand) const {
    if (!interactive() || auxiliary()) return false;
    if (dragHand >= 0 && dragHand == hand) return true;
    HandleLayout l; l.width = placement.width; l.height = panelHeight();
    float x = 0, y = 0;
    if (!layerHit(pose, controlsDepth, x, y)) return false;
    unsigned visible = 0;
    for (int i = 0; i < handleCount; ++i) if (chromeState.reveal[i] > .001f) visible |= 1u << i;
    return l.zone(x, y, visible) != Handle::None;
  }
  // What the layers were last placed and drawn for.
  struct LayerGeometry {
    float width = -1, height = -1, a = -1, b = -1;
    bool vertical = false;
    bool operator==(const LayerGeometry &o) const {
      return width == o.width && height == o.height && a == o.a && b == o.b && vertical == o.vertical;
    }
  };
  LayerGeometry placedGeometry, drawnLayerGeometry;
  LayerGeometry currentGeometry(const HandleLayout &l) const {
    return {placement.width, l.height, placement.bendA, placement.bendB, placement.bendVertical};
  }
  // Keeps the bezel and controls on the window: concentric with it, in one
  // overlay each, or two where its halves bend differently.
  void syncHalf(VROverlayHandle_t &h,bool needed,const char *suffix,uint32_t order,GLuint tex) {
    reconcileHalf(h,needed,[&]() {
      VROverlayHandle_t created=0;std::string k=key+suffix;
      check(session->createOverlay(k,name,&created),"bend half overlay");
      overlay->SetOverlayInputMethod(created,VROverlayInputMethod_None);
      overlay->SetOverlayFlag(created,VROverlayFlags_IsPremultiplied,true);
      overlay->SetOverlayTextureColorSpace(created,EColorSpace_ColorSpace_Gamma);
      overlay->SetOverlaySortOrder(created,order);
      if(tex){GLint w=0;glBindTexture(GL_TEXTURE_2D,tex);glGetTexLevelParameteriv(GL_TEXTURE_2D,0,GL_TEXTURE_WIDTH,&w);
        if(w){Texture_t t{reinterpret_cast<void *>(uintptr_t(tex)),ETextureType_TextureType_OpenGL,EColorSpace_ColorSpace_Gamma};
          auto error=overlay->SetOverlayTexture(created,&t);if(error){session->destroyOverlay(created);check(error,"bend half texture");}}}
      return created;
    },[&](VROverlayHandle_t old){session->destroyOverlay(old);});
  }
  void placeLayers(const HandleLayout &l) {
    syncHalf(frameHandle2,shape().split(),".frame.half",spatialForeground?52:2,frameTexture);
    syncHalf(chromeHandle2,shape().split(),".handles.half",spatialForeground?53:3,chromeTexture);
    const auto walls=frameWalls(shape(),l);
    for(int i=0;i<4;++i) {
      if (!wallHandles[i]) continue;
      auto m=multiply(placement.matrix,walls[i].local);
      overlay->SetOverlayTransformAbsolute(wallHandles[i],ETrackingUniverseOrigin_TrackingUniverseStanding,&m);
      overlay->SetOverlayWidthInMeters(wallHandles[i],walls[i].width);
      GLint w=0,h=0;glBindTexture(GL_TEXTURE_2D,wallTextures[i]);glGetTexLevelParameteriv(GL_TEXTURE_2D,0,GL_TEXTURE_WIDTH,&w);glGetTexLevelParameteriv(GL_TEXTURE_2D,0,GL_TEXTURE_HEIGHT,&h);
      if(w&&h)overlay->SetOverlayTexelAspect(wallHandles[i],walls[i].width*h/(walls[i].height*w));
      if(chromeShown)overlay->ShowOverlay(wallHandles[i]);
    }
    struct { VROverlayHandle_t h[2]; float z, width, height; } layers[2] = {
        {{frameHandle, frameHandle2}, frameDepth, l.frameWidth(), l.frameHeight()},
        {{chromeHandle, chromeHandle2}, controlsDepth, l.chromeWidth(), l.chromeHeight()}};
    const LayerGeometry now = currentGeometry(l);
    const bool resized = !(now == placedGeometry);
    const BendShape b = shape();
    for (auto &layer : layers) {
      const float span = b.vertical ? layer.height : layer.width;
      for (int i = 0; i < b.segments(); i++) {
        if (!layer.h[i]) continue;
        const BendSegment seg = bendSegment(b, i, span, layer.z);
        HmdMatrix34_t m = multiply(placement.matrix, seg.local);
        overlay->SetOverlayTransformAbsolute(layer.h[i], ETrackingUniverseOrigin_TrackingUniverseStanding, &m);
        if (resized) {
          VRTextureBounds_t bounds = seg.bounds;
          overlay->SetOverlayWidthInMeters(layer.h[i], seg.width);
          overlay->SetOverlayTexelAspect(layer.h[i],seg.width/(span*(seg.bounds.uMax-seg.bounds.uMin)));
          overlay->SetOverlayTextureBounds(layer.h[i], &bounds);
          if (overlay->SetOverlayCurvature) overlay->SetOverlayCurvature(layer.h[i], seg.curvature);
        }
      }
      if (layer.h[1] && chromeShown && resized) {
        if (b.segments() == 2) overlay->ShowOverlay(layer.h[1]);
        else overlay->HideOverlay(layer.h[1]);
      }
    }
    placedGeometry = now;
  }
  static bool sameChrome(const ChromeState &a, const ChromeState &b) {
    for (int i = 0; i < handleCount; i++)
      if (std::fabs(a.reveal[i] - b.reveal[i]) > 1e-3f || std::fabs(a.wide[i] - b.wide[i]) > 1e-3f ||
          a.held[i] != b.held[i])
        return false;
    return std::fabs(a.focusGlow-b.focusGlow)<1e-3f && std::fabs(a.focusSnap-b.focusSnap)<1e-3f && a.focused==b.focused && a.tip == b.tip && (!a.tip || (std::fabs(a.tipX - b.tipX) < 5e-4f && std::fabs(a.tipY - b.tipY) < 5e-4f));
  }
  // Draws whichever layers changed and hands them to SteamVR.
  void drawChrome(const HandleLayout &l, const ChromeState &frameState) {
    const LayerGeometry now = currentGeometry(l);
    const bool geometry = !(now == drawnLayerGeometry);
    const bool transposed = placement.bendVertical;
    struct { int layer; VROverlayHandle_t h[2]; GLuint texture; const ChromeState &state; ChromeState &drawn;
             float width, height; } layers[2] = {
        {0, {frameHandle, frameHandle2}, frameTexture, frameState, frameDrawn, l.frameWidth(), l.frameHeight()},
        {1, {chromeHandle, chromeHandle2}, chromeTexture, chromeState, chromeDrawn, l.chromeWidth(), l.chromeHeight()}};
    for (auto &layer : layers) {
      if (!geometry && sameChrome(layer.state, layer.drawn)) continue;
      int w = 0, h = 0;
      ChromeRenderer::textureSize(layer.width, layer.height, transposed, w, h);
      try {
        chromeRenderer.render(layer.layer, l, layer.state, layer.width, layer.height, transposed, layer.texture, w, h);
      } catch (const std::exception &e) {
        fprintf(stderr, "[native] window chrome unavailable: %s\n", e.what());
        chromeFailed = true;
        return;
      }
      glFlush(); // SteamVR copies it on submission, in this context
      Texture_t t{reinterpret_cast<void *>(uintptr_t(layer.texture)), ETextureType_TextureType_OpenGL,
                  EColorSpace_ColorSpace_Gamma};
      HmdVector2_t mouseScale{{float(w), float(h)}};
      for (auto h2 : layer.h)
        if (h2) {
          overlay->SetOverlayMouseScale(h2, &mouseScale);
          overlay->SetOverlayTexture(h2, &t);
        }
      layer.drawn = layer.state;
    }
    const bool wallsAllocated = frameWallGroupActive(wallHandles);
    if(geometry || (wallsAllocated && !wallTextureReady) || !sameChrome(frameState,wallDrawn)) {
      const auto walls=frameWalls(shape(),l);
      for(int i=0;i<4;++i) {
        if (!wallHandles[i]) continue;
        wallRenderer.render(walls[i],l,frameState,wallTextures[i]);glFlush();
        Texture_t t{reinterpret_cast<void *>(uintptr_t(wallTextures[i])),ETextureType_TextureType_OpenGL,EColorSpace_ColorSpace_Gamma};
        overlay->SetOverlayTexture(wallHandles[i],&t);
      }
      wallDrawn=frameState;
      wallTextureReady = wallsAllocated;
      placeLayers(l);
    }
    drawnLayerGeometry = now;
  }
  // A short buzz on one hand's controller: a light tick when the laser comes
  // onto a handle, a firmer one when it is taken, a soft one on letting go.
  void pulse(int hand, float seconds, float frequency, float amplitude) {
    if (input && hapticAction)
      input->TriggerHapticVibrationAction(hapticAction, 0, seconds, frequency, amplitude, hands[hand]);
  }
  void hideChrome() {
    for (auto h : {frameHandle, chromeHandle, frameHandle2, chromeHandle2, handle2, wallHandles[0], wallHandles[1], wallHandles[2], wallHandles[3]})
      if (h) overlay->HideOverlay(h);
    chromeShown = false;
    session->hideCursor(this);
  }
  // One step of VR placement: reads both hands, shows the window's chrome,
  // slides out the handles the laser is heading for, lights the metal around
  // its tip, and drives a drag while the trigger is held. Returns true when a
  // drag ended, so the caller saves the pose.
  int draggingHand() const { return dragHand; }
  bool interactive() const { return texture && !closing && (!auxiliary() || surfaceVisible); }
  bool updatePlacementInput(unsigned allowedHands = 3, bool consumeSqueeze = false, unsigned revealHands = 3) {
    if (auxiliary() || !input || noVR || chromeFailed || closing) return false;
    const double now = nowMs();
    const float dt = lastChromeUpdate > 0 ? float(std::clamp(now - lastChromeUpdate, 0.0, 100.0) / 1000.0) : 0.f;
    lastChromeUpdate = now;
    HandleLayout l;
    l.width = placement.width;
    l.height = panelHeight();
    struct HandRay { bool valid = false, grab = false, squeeze = false, hit = false, onControls = false, onFrame = false;
                     HmdMatrix34_t pose{}; float x = 0, y = 0, fx = 0, fy = 0; };
    HandRay rays[2];
    unsigned revealZones = 0;
    for (int i = 0; i < 2; i++) {
      const auto &h = session->hand[i];
      if (h.valid && (revealHands & (1u << i))) {
        float x = 0, y = 0;
        float distance;
        if (wallHit(h.pose,x,y,distance) || layerHit(h.pose, controlsDepth, x, y)) revealZones |= l.revealed(x, y);
      }
      rays[i].valid = h.valid && ((allowedHands & (1u << i)) || dragHand == i);
      rays[i].grab = h.grab;
      rays[i].squeeze = h.squeeze && !consumeSqueeze;
      if (rays[i].valid) {
        rays[i].pose = h.pose;
        rays[i].onControls = layerHit(rays[i].pose, controlsDepth, rays[i].x, rays[i].y);
        rays[i].onFrame = layerHit(rays[i].pose, frameDepth, rays[i].fx, rays[i].fy);
        float wallDistance;
        if(wallHit(rays[i].pose,rays[i].fx,rays[i].fy,wallDistance)) {
          rays[i].onFrame=true;rays[i].x=rays[i].fx;rays[i].y=rays[i].fy;rays[i].onControls=true;
        }
        rays[i].hit = rays[i].onControls;
      }
    }
    // Nothing until the window has a picture; nothing once it is closing.
    if (!texture) return false;
    if (!chromeShown) {
      for (auto h : {frameHandle, chromeHandle}) overlay->ShowOverlay(h);
      chromeShown = true;
      placedGeometry = LayerGeometry{}; // shows the halves too, if bent unevenly
    }
    placeLayers(l);
    bool ended = false;
    int pointing = -1; // the hand whose laser is on or near this window
    Handle hot = Handle::None;
    if (dragHand >= 0) {
      HandRay &r = rays[dragHand];
      if (r.valid && (dragBySqueeze ? r.squeeze : r.grab)) {
        placement = drag.update(r.pose);
        apply();
        l.width = placement.width;
        l.height = panelHeight();
        placeLayers(l);
        r.onControls = layerHit(r.pose, controlsDepth, r.x, r.y);
        r.onFrame = layerHit(r.pose, frameDepth, r.fx, r.fy);
        pointing = dragHand;
        hot = drag.handle;
      } else {
        pulse(dragHand, 0.012f, 160, 0.2f);
        dragHand = -1;
        ended = true;
      }
    }
    unsigned visible = 0;
    for (int i = 0; i < handleCount; ++i) if (chromeState.reveal[i] > .001f) visible |= 1u << i;
    if (dragHand < 0) {
      for (int i : {1, 0}) {
        if (!rays[i].onControls) continue;
        if (std::fabs(rays[i].x) > l.chromeWidth() / 2 || std::fabs(rays[i].y) > l.chromeHeight() / 2) continue;
        pointing = i;
        hot = l.zone(rays[i].x, rays[i].y, visible);
        // Keep the handle under the laser a little beyond its edge, so it
        // doesn't flicker between neighbours or off at the boundary.
        const Handle was = hotWas[i];
        if (was != Handle::None && hot != was && (chromeState.reveal[handleIndex(was)] > .001f) &&
            l.distance(was, rays[i].x, rays[i].y) < HandleLayout::tolerance * 1.5f)
          hot = was;
        break;
      }
      for (int i = 0; i < 2; i++) {
        const Handle now = i == pointing ? hot : Handle::None;
        if (now != Handle::None && now != hotWas[i]) pulse(i, 0.01f, 180, 0.14f);
        hotWas[i] = now;
      }
      const bool pressed = pointing >= 0 && ((rays[pointing].grab && !grabWas[pointing]) ||
                                             (rays[pointing].squeeze && !squeezeWas[pointing]));
      if (pressed && hot == Handle::Close) {
        // Close: hide at once for immediate feedback; the companion then
        // stops this panel's stream (or the Workspace) and the viewer exits.
        pulse(pointing, 0.03f, 100, 0.45f);
        closeRequested = closing = true;
        overlay->HideOverlay(handle);
        hideChrome();
        for (int i = 0; i < 2; i++) { grabWas[i] = rays[i].grab; squeezeWas[i] = rays[i].squeeze; }
        return false;
      }
      if (pressed && hot != Handle::None) {
        dragBySqueeze = !(rays[pointing].grab && !grabWas[pointing]);
        drag.start(hot, rays[pointing].pose, placement, dragBySqueeze);
        drag.startAspect = panelHeight() / placement.width;
        dragHand = pointing;
        pulse(pointing, 0.022f, 120, 0.35f);
      }
    }
    // Targets: a side's handles slide out while the laser is past that edge
    // (or one of them is held); a handle widens under the laser.
    const unsigned shown = revealZones;
    auto approach = [&](float &v, float target, float seconds) {
      const float step = seconds > 0 ? dt / seconds : 1.f;
      v = v < target ? std::min(target, v + step) : std::max(target, v - step);
    };
    for (int i = 0; i < handleCount; i++) {
      const Handle h = Handle(i + 1);
      const bool pill = h == Handle::Pill || h == Handle::Close;
      const bool isHot = h == hot && hot != Handle::None;
      const bool held = dragHand >= 0 && h == drag.handle;
      const bool movingPill = pill && dragHand >= 0 && drag.handle == Handle::Pill;
      handleReveal[i].update(shown & (1u << i), isHot, held || movingPill, now, dt);
      chromeState.reveal[i] = handleReveal[i].amount;
      approach(chromeState.wide[i], !pill && (isHot || held) ? 1.f : 0.f, 0.09f);
      chromeState.held[i] = held ? 1.f : 0.f;
    }
    // The laser's light: on the controls near any of them, and on the bezel.
    ChromeState frameState;
    focusFeedback.apply(frameState);
    chromeState.tip = pointing >= 0 && rays[pointing].onControls;
    if (chromeState.tip) {
      chromeState.tipX = rays[pointing].x;
      chromeState.tipY = rays[pointing].y;
      // Only redraw for the tip while it is near something it lights.
      bool nearPiece = false;
      for (int i = 0; i < handleCount; i++)
        if (chromeState.reveal[i] > 0 && l.distance(Handle(i + 1), rays[pointing].x, rays[pointing].y) < 0.06f)
          nearPiece = true;
      chromeState.tip = nearPiece;
    }
    if (pointing >= 0 && rays[pointing].onFrame) {
      const float ex = std::fabs(rays[pointing].fx) - l.width / 2, ey = std::fabs(rays[pointing].fy) - l.height / 2;
      const float ring = std::max(ex, ey);  // < 0 inside the window, ~bezel at its outer edge
      if (ring > -0.06f && ring < l.bezel() + 0.06f) {
        frameState.tip = true;
        frameState.tipX = rays[pointing].fx;
        frameState.tipY = rays[pointing].fy;
      }
    }
    drawChrome(l, frameState);
    // The tip's core, on whatever it lands on: a control, the bezel, or the window.
    if (pointing >= 0 && dragHand < 0) {
      float x = rays[pointing].x, y = rays[pointing].y, z = controlsDepth + 0.002f;
      if (hot == Handle::None) {
        const float ex = std::fabs(rays[pointing].fx) - l.width / 2, ey = std::fabs(rays[pointing].fy) - l.height / 2;
        if (rays[pointing].onFrame && std::max(ex, ey) >= 0 && std::max(ex, ey) <= l.bezel()) {
          x = rays[pointing].fx; y = rays[pointing].fy; z = frameDepth + 0.002f;
        } else if (layerHit(rays[pointing].pose, pictureDepth, x, y)) {
          z = pictureDepth + 0.002f;
        }
      }
      HmdMatrix34_t m = panelPoint(x, y, z),wallPoint;float wallDistance;
      if(hot==Handle::None && wallHit(rays[pointing].pose,x,y,wallDistance,&wallPoint))m=multiply(placement.matrix,wallPoint);
      session->showCursor(this,m,spatialForeground?54:4,spatialForeground);
    } else session->hideCursor(this);
    for (int i = 0; i < 2; i++) { grabWas[i] = rays[i].grab; squeezeWas[i] = rays[i].squeeze; }
    return ended;
  }
  const char *gaze(double &u, double &v) {
    if(!gazeBridge) return "unavailable";
    std::lock_guard<std::mutex> lock(gazeGeometryMutex);
    // Gaze maps through the window's single overlay; halves bent differently
    // or a turned (top-to-bottom) window are not mapped yet.
    if(gazePlacement.bendVertical || std::fabs(gazePlacement.bendA-gazePlacement.bendB)>1e-4f) return "bent-window";
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
  ~Overlay() { release(); }
  void release() {
    // The main-scope GazeProducer has already joined before this object dies.
    session->hideCursor(this);
    wallRenderer.release();
    chromeRenderer.release();
    surfaceRenderer.release();
    if (wallGroupRegistered) {
      session->unregisterOptionalWalls(this);
      wallGroupRegistered = false;
    }
    for (auto h : {frameHandle, chromeHandle, frameHandle2, chromeHandle2, handle2, wallHandles[0], wallHandles[1], wallHandles[2], wallHandles[3]})
      if (h) session->destroyOverlay(h);
    for (auto t : {frameTexture, chromeTexture, wallTextures[0], wallTextures[1], wallTextures[2], wallTextures[3]})
      if (t) glDeleteTextures(1, &t);
    if (handle) {
      overlay->HideOverlay(handle);
      overlay->ClearOverlayTexture(handle);
      session->destroyOverlay(handle);
    }
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
      if (s.surfaceSource) glDeleteTextures(1, &s.surfaceSource);
    }
    if (!retired.empty())
      glDeleteTextures(GLsizei(retired.size()), retired.data());
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
    w = std::clamp(w, std::min(.4f, placement.width), 8.f);
    d = std::clamp(d, .4f, 10.f);
    float delta = d - placement.distance;
    for (int r = 0; r < 3; r++)
      placement.matrix.m[r][3] -= placement.matrix.m[r][2] * delta;
    placement.width = w;
    placement.distance = d;
    apply();
  }
  bool move(float dx, float dy, float dz) {
    if (!movePlacement(placement, dx, dy, dz))
      return false;
    apply();
    return true;
  }
  void rotate(float yaw, float pitch, float roll) {
    rotatePlacement(placement, yaw, pitch, roll);
    apply();
  }
  void apply() {
    std::lock_guard<std::mutex> lock(gazeGeometryMutex);
    gazeCalibrated = false; lastGazeCalibration = 0;
    gazePlacement = placement;
    if (noVR)
      return;
    const bool split = surfaceGeometry.region.attached ? surfaceGeometry.region.segments(placement.width,surfaceHeight)==2 : shape().split();
    syncHalf(handle2,split,".half",auxiliary()?(role==PanelRole::Menubar?51:104):(spatialForeground?51:1),texture);
    if(!split)showingHalf=false;
    if (surfaceGeometry.region.attached) {
      const auto &region = surfaceGeometry.region;
      const int count = region.segments(placement.width,surfaceHeight);
      for (int i=0;i<count;++i) {
        auto h = i == 0 ? handle : handle2;
        if (!h) continue;
        auto seg = region.segment(i,placement.width,surfaceHeight);
        auto m = multiply(placement.matrix,seg.local);
        check(overlay->SetOverlayWidthInMeters(h,seg.width), "surface width");
        check(overlay->SetOverlayTransformAbsolute(h,ETrackingUniverseOrigin_TrackingUniverseStanding,&m), "surface transform");
        overlay->SetOverlayTextureBounds(h,&seg.bounds);
        if (overlay->SetOverlayCurvature) overlay->SetOverlayCurvature(h,seg.curvature);
        // Bounds crop the source; aspect supplies the layer's physical scale,
        // including the concentric radius correction along the bend.
        const float fraction = seg.bounds.uMax-seg.bounds.uMin;
        const float across = region.shape.vertical ? placement.width : surfaceHeight;
        const float texWidth = region.shape.vertical ? height : width;
        const float texHeight = region.shape.vertical ? width : height;
        if (texWidth > 0 && texHeight > 0)
          overlay->SetOverlayTexelAspect(h,seg.width*texHeight/(across*texWidth*fraction));
      }
      showingHalf = count == 2;
      if (handle2) {
        if (showingHalf && texture && surfaceVisible) overlay->ShowOverlay(handle2);
        else overlay->HideOverlay(handle2);
      }
      return;
    }
    if (auxiliary() && width > 0 && height > 0 && surfaceHeight > 0)
      overlay->SetOverlayTexelAspect(handle, placement.width * height / (surfaceHeight * width));
    const BendShape b = shape();
    for (int i = 0; i < b.segments(); i++) {
      const BendSegment seg = bendSegment(b, i, b.span, auxiliary()?0:pictureDepth);
      const VROverlayHandle_t h = i == 0 ? handle : handle2;
      HmdMatrix34_t m = multiply(placement.matrix, seg.local);
      VRTextureBounds_t bounds = seg.bounds;
      check(overlay->SetOverlayWidthInMeters(h, seg.width), "width");
      if(!auxiliary())overlay->SetOverlayTexelAspect(h,seg.width/(b.span*(seg.bounds.uMax-seg.bounds.uMin)));
      check(overlay->SetOverlayTransformAbsolute(h, ETrackingUniverseOrigin_TrackingUniverseStanding, &m),
            "world transform");
      overlay->SetOverlayTextureBounds(h, &bounds);
      if (overlay->SetOverlayCurvature) overlay->SetOverlayCurvature(h, seg.curvature);
    }
    const bool half = b.segments() == 2;
    if (handle2 && half != showingHalf) {
      if (!half) overlay->HideOverlay(handle2);
      else if (texture && (!auxiliary() || surfaceVisible)) overlay->ShowOverlay(handle2);
      showingHalf = half;
    }
  }
  // The window's bend now (bend.hpp).
  BendShape shape() const { return bendShape(placement, panelHeight()); }
  bool wantsTransposed() const { return placement.bendVertical; }
  // A picture to redraw when the bend changed direction since it was shown.
  std::shared_ptr<Picture> restagePicture() const {
    return shownPicture && shownTransposed != wantsTransposed() && ready.empty() && inFlight.empty() ? shownPicture
                                                                                                      : nullptr;
  }
  void focus() {
    if (!noVR && texture && (!auxiliary() || surfaceVisible))
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
  // Bends each half on its own (control file {"bend":{"vertical":..,"a":..,"b":..}}).
  void setBend(bool vertical, float a, float b) {
    placement.bendVertical = vertical;
    placement.bendA = std::clamp(a, 0.f, 1.f);
    placement.bendB = std::clamp(b, 0.f, 1.f);
    syncCurvature(placement);
    apply();
  }
  CurvatureResult setCurvature(float requested) {
    // The Mac page's curvature is an even side-to-side bend: back to one overlay.
    if (std::isfinite(requested) && requested >= 0.f && requested <= 1.f && !noVR &&
        (placement.bendVertical || std::fabs(placement.bendA - placement.bendB) > 1e-4f)) {
      setEvenBend(placement, requested);
      apply();
    }
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
    if (result.applied)
      setEvenBend(placement, result.value);
    return result;
  }
  bool events(bool sharedEvents = true) {
    if (sharedEvents && !session->events()) { exitReason = session->exitReason; return false; }
    if (noVR) return true;
    VREvent_t e{};
    while (overlay->PollNextOverlayEvent(handle, &e, sizeof(e)))
      if (e.eventType == EVREventType_VREvent_Quit) {
        exitReason = "openvr-overlay-quit";
        return false;
      }
    for (auto h : {frameHandle, chromeHandle, frameHandle2, chromeHandle2, handle2, wallHandles[0], wallHandles[1], wallHandles[2], wallHandles[3]})
      if (h)
        while (overlay->PollNextOverlayEvent(h, &e, sizeof(e))) {
        }
    return true;
  }
  bool fenced() const { return handoff == "fence"; }
  bool busy() const { return !inFlight.empty(); }
  // Issues one picture's upload and conversion. The finish handoff completes
  // and submits it here. The fence handoff returns once the work is flushed;
  // it completes the oldest in-flight picture first only if no slot is free.
  int freeSlot() const {
    for (int index = 0; index < maxSlots; ++index)
      if (index != displayed &&
          std::find(inFlight.begin(), inFlight.end(), index) == inFlight.end() &&
          std::find(ready.begin(), ready.end(), index) == ready.end())
        return index;
    return -1;
  }
  void dropOldestReady(std::vector<Completion> &done) {
    Slot &old = slots[ready.front()];
    ready.pop_front();
    Completion c{std::move(old.picture), false, old.texture, old.width, old.height, true};
    ++pacedDrops;
    done.push_back(std::move(c));
  }
  void stage(std::shared_ptr<Picture> picture, std::vector<Completion> &done) {
    const Picture &p = *picture;
    int index = 0;
    if (fenced()) {
      // Every other slot is busy: a newer picture supersedes the oldest one
      // still waiting for its slot, else wait for the oldest GL work.
      if (freeSlot() < 0 && ready.empty())
        complete(-1, done);
      if (freeSlot() < 0 && !ready.empty())
        dropOldestReady(done);
      index = freeSlot();
    }
    if (index < 0)
      throw std::runtime_error("no free texture slot");
    Slot &s = slots[index];
    const bool transposed = wantsTransposed();
    const int texW = transposed ? p.outputHeight() : p.outputWidth();
    const int texH = transposed ? p.outputWidth() : p.outputHeight();
    if (!s.texture || texW != s.width || texH != s.height || transposed != s.transposed) {
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
      glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, texW, texH, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
      s.width = texW;
      s.height = texH;
      s.transposed = transposed;
    } else
      glBindTexture(GL_TEXTURE_2D, s.texture);
    if (p.format == Picture::Format::RGBA && !p.layout.warped && !transposed) {
      gpuConvertMs = 0;
      glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
      glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, p.width, p.height, GL_RGBA,
                      GL_UNSIGNED_BYTE, p.rgba.data());
    } else {
      double convertStart = nowMs();
      convertYuv(s, p);
      gpuConvertMs = nowMs() - convertStart;
    }
    if (auxiliary()) {
      if (!s.surfaceSource) glGenTextures(1, &s.surfaceSource);
      surfaceRenderer.render(s.texture, s.surfaceSource, texW, texH, surfaceGeometry, s.transposed);
      std::swap(s.texture, s.surfaceSource);
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
      ready.push_back(index);
      waitMs = 0;
    }
    presentReady(done);
  }
  bool paced() const { return fenced() && present == "paced" && !noVR; }
  bool waiting() const { return !ready.empty(); }
  // Headset refreshes per content frame: 2 at 120 Hz, 1 at 90 Hz (60 fps).
  int contentRefreshes() const {
    return vsyncPeriodMs > 0 ? std::max(1, int(std::floor(1000.0 / vsyncPeriodMs / 60.0 + 0.25))) : 1;
  }
  // The compositor's refresh counter, and milliseconds since its last
  // refresh. False when unavailable or stalled (headset asleep), so pacing
  // never holds a picture back indefinitely.
  bool vsync(uint64_t &counter, double &sinceMs) {
    if (noVR || !system || !system->GetTimeSinceLastVsync)
      return false;
    double now = nowMs();
    if (now - lastPeriodRead > 1000) {
      lastPeriodRead = now;
      ETrackedPropertyError error = ETrackedPropertyError_TrackedProp_Success;
      float hz = system->GetFloatTrackedDeviceProperty(0, ETrackedDeviceProperty_Prop_DisplayFrequency_Float, &error);
      vsyncPeriodMs = error == ETrackedPropertyError_TrackedProp_Success && hz > 20 && hz < 500 ? 1000.0 / hz : 0;
    }
    float since = 0;
    if (!system->GetTimeSinceLastVsync(&since, &counter) || vsyncPeriodMs <= 0)
      return false;
    if (counter != lastVsyncCounter) {
      lastVsyncCounter = counter;
      lastVsyncChange = now;
    }
    sinceMs = since * 1000.0;
    return now - lastVsyncChange < 4 * vsyncPeriodMs;
  }
  // Milliseconds until the next refresh plus a small margin, when a paced
  // picture is waiting; otherwise a default.
  double untilNextSlotMs(double otherwise) {
    uint64_t counter = 0;
    double since = 0;
    if (!paced() || ready.empty() || !vsync(counter, since))
      return otherwise;
    return std::clamp(vsyncPeriodMs - since + 0.5, 0.5, otherwise);
  }
  void presentReady(std::vector<Completion> &done) {
    if (ready.empty())
      return;
    uint64_t counter = 0;
    double since = 0;
    if (!paced() || !vsync(counter, since)) {
      if (paced())
        ++pacedFallbacks;
      while (!ready.empty()) {
        int index = ready.front();
        ready.pop_front();
        done.push_back(submit(index));
      }
      return;
    }
    // In order, one picture per content period. When pictures are queued
    // behind (a delivery burst), the next one may follow after a single
    // refresh so the queue drains without skipping motion; only a backlog
    // beyond three pictures is dropped.
    const uint64_t gap = havePresentVsync ? counter - lastPresentVsync : 0;
    const uint64_t needed = ready.size() >= 2 ? 1 : uint64_t(contentRefreshes());
    if (havePresentVsync && gap < needed)
      return;
    while (ready.size() > 3)
      dropOldestReady(done);
    int index = ready.front();
    ready.pop_front();
    if (havePresentVsync)
      ++presentGaps[std::min<uint64_t>(gap, 4)];
    lastPresentVsync = counter;
    havePresentVsync = true;
    done.push_back(submit(index));
  }
  Completion submit(int index) {
    Slot &s = slots[index];
    std::shared_ptr<Picture> picture = s.picture;
    Completion c{std::move(s.picture), true, s.texture, s.width, s.height};
    // The window's own size (the texture is turned for a top-to-bottom bend).
    width = s.transposed ? s.height : s.width;
    height = s.transposed ? s.width : s.height;
    double submitStart = nowMs();
    if (role == PanelRole::Menubar && s.surfaceSource) {
      surfaceRenderer.render(s.surfaceSource, s.texture, s.width, s.height, surfaceGeometry, s.transposed);
      glFinish();
    }
    if (auxiliary() && !noVR && surfaceHeight > 0) apply();
    if (!noVR) {
      // A resize changes the runtime's intersection aspect ratio. Invalidate
      // only around submission, so gaze keeps using the displayed geometry
      // while GL conversion/upload works on the replacement texture.
      bool sizeChanged = false;
      {
        std::lock_guard<std::mutex> lock(gazeGeometryMutex);
        sizeChanged = width != gazeWidth || height != gazeHeight || gazeTextureChanging;
        if (sizeChanged || s.transposed != shownTransposed) {
          // SteamVR sizes its intersection tests by this: the texture as
          // submitted (turned for a top-to-bottom bend).
          HmdVector2_t mouseScale{{float(s.width), float(s.height)}};
          check(overlay->SetOverlayMouseScale(handle, &mouseScale),
                "mouse scale");
          if (handle2) overlay->SetOverlayMouseScale(handle2, &mouseScale);
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
      if (c.submitted) {
        if (!auxiliary() || surfaceVisible)
          check(overlay->ShowOverlay(handle), "ShowOverlay");
        if (handle2 && showingHalf && (!auxiliary() || surfaceVisible)) {
          overlay->SetOverlayTexture(handle2, &t);
          overlay->ShowOverlay(handle2);
        }
        if (s.transposed != shownTransposed) {
          // The texture turned with the bend: put the overlays on the new shape.
          shownTransposed = s.transposed;
          apply();
        }
      }
    }
    submitMs = nowMs() - submitStart;
    if (c.submitted) {
      texture = s.texture;
      displayed = index;
      shownTransposed = s.transposed;
      shownPicture = picture;
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
