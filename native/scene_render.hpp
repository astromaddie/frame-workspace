#pragma once
// An offline compositor for the overlays the native host submits. The camera
// and room are diagnostic stand-ins; panel geometry/materials are production.
#include "overlay.hpp"
#include "spatial_render.hpp"
#include <sstream>

namespace fw {
struct SceneCamera {
  float eye[3]{0, 0, 0}, yaw = 0, pitch = 0, fov = 100;
  int width = 1920, height = 1200;
};
inline std::vector<float> sceneNumbers(const std::string &text, size_t count) {
  std::istringstream in(text);
  std::vector<float> values;
  for (std::string part; std::getline(in, part, ',');) {
    size_t end = 0;
    float v = std::stof(part, &end);
    if (end != part.size() || !std::isfinite(v))
      throw std::runtime_error("invalid scene camera value");
    values.push_back(v);
  }
  if (values.size() != count) throw std::runtime_error("invalid scene camera vector");
  return values;
}
class SceneRenderer {
  SpatialRaster raster;
  void *cairo = nullptr;
  std::vector<GLuint> textures;
  GLuint framebuffer = 0;
  struct Image { GLuint texture; int width, height; };
  struct Layer {
    PanelSurface surface;
    BendShape bend;
    GLuint texture;
    float width, height, depth;
    int order;
  };
  std::vector<Layer> layers;
  std::filesystem::path root;
  GLuint texture() {
    GLuint t; glGenTextures(1, &t); textures.push_back(t); return t;
  }
  Image load(const std::string &path) {
    if (path.empty()) throw std::runtime_error("scene texture path required");
    auto s = raster.png((root / path).c_str());
    const int w = raster.width(s), h = raster.height(s);
    if (raster.status(s) || w < 1 || h < 1 || w > 8192 || h > 8192) {
      raster.surfaceDestroy(s);
      throw std::runtime_error("invalid scene PNG: " + path);
    }
    raster.flush(s);
    GLuint t = texture();
    glBindTexture(GL_TEXTURE_2D, t);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    // Cairo's ARGB32 is premultiplied BGRA on the supported little-endian hosts.
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_BGRA,
                 GL_UNSIGNED_BYTE, raster.data(s));
    raster.surfaceDestroy(s);
    return {t, w, h};
  }
  static Json object(json_object *o) {
    if (!o || !json_object_is_type(o, json_type_object))
      throw std::runtime_error("scene object required");
    return Json(json_object_get(o), json_object_put);
  }
  void add(const PanelSurface &s, GLuint t, int order, float depth = 0,
           float width = 0, float height = 0) {
    layers.push_back({s, bendShape(s.placement, s.height), t,
                      width ? width : s.placement.width,
                      height ? height : s.height, depth, order});
  }
  static void matrix(const HmdMatrix34_t &m) {
    float v[16] = {m.m[0][0],m.m[1][0],m.m[2][0],0,
                   m.m[0][1],m.m[1][1],m.m[2][1],0,
                   m.m[0][2],m.m[1][2],m.m[2][2],0,
                   m.m[0][3],m.m[1][3],m.m[2][3],1};
    glMultMatrixf(v);
  }
  // Reconstruct each actual SteamVR overlay, including its split seam and UV
  // bounds. Vertical source sampling is the inverse of the host's transpose.
  static void draw(const Layer &l) {
    const auto &b = l.bend;
    glBindTexture(GL_TEXTURE_2D, l.texture);
    for (int half = 0; half < b.segments(); ++half) {
      const auto seg = bendSegment(b, half, b.vertical ? l.height : l.width, l.depth);
      const float across = b.vertical ? l.width : l.height;
      const float radius = seg.curvature > 1e-4f ? seg.width / (2 * float(M_PI) * seg.curvature) : 0;
      glPushMatrix(); matrix(multiply(l.surface.placement.matrix, seg.local));
      const int steps = radius ? 128 : 1;
      glBegin(GL_QUAD_STRIP);
      for (int i = 0; i <= steps; ++i) {
        float u = float(i) / steps, x = (u - .5f) * seg.width;
        float px = radius ? radius * std::sin(x / radius) : x;
        float pz = radius ? radius * (1 - std::cos(x / radius)) : 0;
        float tu = seg.bounds.uMin + u * (seg.bounds.uMax - seg.bounds.uMin);
        for (int top : {1, 0}) {
          float tv = top ? 1 - seg.bounds.vMin : 1 - seg.bounds.vMax;
          glTexCoord2f(b.vertical ? tv : tu, b.vertical ? 1 - tu : tv);
          glVertex3f(px, (top ? .5f : -.5f) * across, pz);
        }
      }
      glEnd(); glPopMatrix();
    }
  }
public:
  SceneRenderer() {
    for (auto n : {"libcairo.so.2", "libcairo.2.dylib", "/opt/homebrew/lib/libcairo.2.dylib"})
      if ((cairo = dlopen(n, RTLD_NOW | RTLD_LOCAL))) break;
    if (!cairo) throw std::runtime_error("Cairo PNG writer unavailable");
  }
  ~SceneRenderer() {
    if (!textures.empty()) glDeleteTextures(GLsizei(textures.size()), textures.data());
    if (framebuffer) glDeleteFramebuffers(1, &framebuffer);
    if (cairo) dlclose(cairo);
  }
  void render(const std::string &scenePath, const std::string &out, const SceneCamera &camera,
              const std::shared_ptr<OverlaySession> &session) {
    if (camera.width < 64 || camera.height < 64 || camera.width > 4096 || camera.height > 4096 ||
        !std::isfinite(camera.fov) || camera.fov < 20 || camera.fov > 160)
      throw std::runtime_error("scene size must be 64..4096 and FOV 20..160");
    root = std::filesystem::absolute(scenePath).parent_path();
    auto scene = json(readFile(scenePath, 1024 * 1024));
    auto panels = field(scene, "panels");
    if (!panels || !json_object_is_type(panels, json_type_array) ||
        json_object_array_length(panels) < 1 || json_object_array_length(panels) > 32)
      throw std::runtime_error("scene needs 1..32 panels");
    ChromeRenderer chrome;
    PanelSurface focused;
    int focusedPixels = 0;
    HmdMatrix34_t tip{};
    for (size_t i = 0; i < json_object_array_length(panels); ++i) {
      auto p = object(json_object_array_get_idx(panels, i));
      PanelSurface s;
      s.key = str(p, "id");
      if (str(p, "role") != "window") throw std::runtime_error("panel role must be window");
      auto pose = field(p, "placement");
      if (!pose || !parsePlacement(json_object_to_json_string(pose), s.placement))
        throw std::runtime_error("invalid scene placement");
      Image pic = load(str(p, "texture"));
      s.height = float(num(p, "height", s.placement.width * pic.height / pic.width));
      if (!std::isfinite(s.height) || s.height <= 0 || s.height > 8)
        throw std::runtime_error("invalid scene panel height");
      bool selected = flag(p, "focused");
      if (selected && focusedPixels) throw std::runtime_error("only one focused scene panel allowed");
      int order = selected ? 50 : 0;
      add(s, pic.texture, order + 1);
      HandleLayout layout; layout.width = s.placement.width; layout.height = s.height;
      ChromeState frame, controls;
      frame.focused = selected;
      frame.focusGlow = float(num(p, "focusGlow", 0));
      frame.focusSnap = float(num(p, "focusSnap", 1));
      controls.reveal[handleIndex(Handle::Pill)] = controls.reveal[handleIndex(Handle::Close)] = 1;
      if (selected) {
        focused = s; focusedPixels = pic.width;
        frame.tip = true; frame.tipX = layout.width * .30f; frame.tipY = -layout.height * .23f;
        tip = multiply(s.placement.matrix, bendPointXY(bendShape(s.placement, s.height),
                        frame.tipX, frame.tipY, .002f));
      }
      for (int layer = 0; layer < 2; ++layer) {
        float w = layer ? layout.chromeWidth() : layout.frameWidth();
        float h = layer ? layout.chromeHeight() : layout.frameHeight();
        int tw, th; ChromeRenderer::textureSize(w, h, false, tw, th);
        GLuint t = texture();
        chrome.render(layer, layout, layer ? controls : frame, w, h, false, t, tw, th);
        add(s, t, order + 2 + layer, layer ? controlsDepth : frameDepth, w, h);
      }
    }
    chrome.release();
    SpatialState state; state.spatial = true;
    if (auto apps = field(scene, "apps")) {
      if (!json_object_is_type(apps, json_type_array) || json_object_array_length(apps) > 128)
        throw std::runtime_error("invalid scene apps");
      for (size_t i = 0; i < json_object_array_length(apps); ++i) {
        auto app = object(json_object_array_get_idx(apps, i));
        state.apps.push_back({str(app, "id"), str(app, "name"), str(app, "icon"), true, false});
      }
    }
    // HMD pose is the scene's room origin. Moving the diagnostic eye does not
    // recenter existing panels or the head-anchored shelf.
    auto tiles = spatialLayout(state, spatialAnchor(spatialIdentity(), 1.3f, 0));
    SpatialRenderer shelf(session);
    shelf.draw(state, tiles, -1, 0, 0, false, root.string(),
               [](const std::string &) { return SpatialPicture{}; });
    for (size_t i = 0; i < tiles.size(); ++i)
      add(tiles[i].surface, shelf.tileTexture(i), tiles[i].kind == SpatialTile::Shelf ? 99 : 100);
    SurfaceRenderer material;
    PanelSurface menubar, status;
    for (const char *name : {"menubar", "status", "dropdown"}) {
      auto raw = field(scene, name);
      if (!raw) continue;
      auto item = object(raw);
      auto msg = field(item, "surface");
      if (!msg) throw std::runtime_error("scene auxiliary surface required");
      auto m = SurfaceMessage::parse(json_object_to_json_string(msg));
      if (!m.visible) continue;
      PanelSurface s;
      const float mpp = focusedPixels ? focused.placement.width * 2 / focusedPixels : .001f;
      if (std::string(name) == "menubar") {
        if (!focusedPixels) throw std::runtime_error("menubar needs a focused window");
        menubar = s = menuStripPlacement(focused, m, mpp, m.hot ? 1.f : 0.f);
      } else if (std::string(name) == "status") {
        status = s = statusStripPlacement(tiles, m, .001f);
      } else {
        auto anchor = m.anchor == "status" ? status : m.anchor == "menubar" ? menubar : focused;
        s = surfacePlacement(anchor, m, m.anchor == "status" ? .001f : mpp);
      }
      if (s.height <= 0 || s.placement.width <= 0) throw std::runtime_error("missing auxiliary anchor");
      auto pic = load(str(item, "texture")); GLuint t = texture();
      material.render(pic.texture, t, pic.width, pic.height);
      add(s, t, std::string(name) == "dropdown" ? 104 : 102);
    }
    if (focusedPixels) {
      GLuint cursor = createCursorTexture(); textures.push_back(cursor);
      PanelSurface s; s.placement.matrix = tip; s.placement.width = .014f; s.height = .014f;
      add(s, cursor, 54);
    }
    // Four source samples per output pixel (2x each dimension), including text.
    const int w = camera.width * 2, h = camera.height * 2;
    GLint limit = 0; glGetIntegerv(GL_MAX_TEXTURE_SIZE, &limit);
    if (w > limit || h > limit) throw std::runtime_error("scene exceeds GL texture limit");
    GLuint output = texture(); glBindTexture(GL_TEXTURE_2D, output);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
    glGenFramebuffers(1, &framebuffer); glBindFramebuffer(GL_FRAMEBUFFER, framebuffer);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, output, 0);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE)
      throw std::runtime_error("scene framebuffer incomplete");
    glViewport(0, 0, w, h); glDisable(GL_DEPTH_TEST); glDisable(GL_BLEND); glDisable(GL_TEXTURE_2D);
    glMatrixMode(GL_PROJECTION); glLoadIdentity();
    glMatrixMode(GL_MODELVIEW); glLoadIdentity();
    glBegin(GL_QUADS);
    glColor3f(.035f,.042f,.060f); glVertex2f(-1,-1); glVertex2f(1,-1);
    glColor3f(.11f,.13f,.17f); glVertex2f(1,1); glVertex2f(-1,1); glEnd();
    glMatrixMode(GL_PROJECTION); glLoadIdentity();
    const double right = .1 * std::tan(camera.fov * M_PI / 360);
    glFrustum(-right, right, -right * h / w, right * h / w, .1, 50);
    glMatrixMode(GL_MODELVIEW); glLoadIdentity();
    glRotatef(-camera.pitch, 1, 0, 0); glRotatef(camera.yaw, 0, 1, 0);
    glTranslatef(-camera.eye[0], -camera.eye[1], -camera.eye[2]);
    glColor3f(.13f,.15f,.18f); glLineWidth(1);
    glBegin(GL_LINES);
    for (int i = -20; i <= 20; ++i) {
      glVertex3f(i*.5f,-1.6f,-.1f); glVertex3f(i*.5f,-1.6f,-20);
      glVertex3f(-10,-1.6f,-.1f-i*.5f-10); glVertex3f(10,-1.6f,-.1f-i*.5f-10);
    }
    glEnd();
    // Sort bands match Overlay::setSpatialForeground and the spatial UI.
    std::stable_sort(layers.begin(), layers.end(), [&](const Layer &a, const Layer &b) {
      if (a.order != b.order) return a.order < b.order;
      auto distance = [&](const Layer &l) {
        float d = 0; for (int k = 0; k < 3; ++k) d += std::pow(l.surface.placement.matrix.m[k][3]-camera.eye[k], 2);
        return d;
      };
      return distance(a) > distance(b);
    });
    glEnable(GL_BLEND); glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA);
    glEnable(GL_TEXTURE_2D); glColor4f(1,1,1,1);
    for (const auto &l : layers) draw(l);
    if (focusedPixels) {
      // A diagnostic right-controller ray; the endpoint is the production tip.
      glDisable(GL_TEXTURE_2D); glLineWidth(2);
      glBegin(GL_LINES); glColor4f(.025f,.06f,.10f,.12f); glVertex3f(.28f,-.30f,-.32f);
      glColor4f(.20f,.48f,.72f,.8f); glVertex3f(tip.m[0][3],tip.m[1][3],tip.m[2][3]); glEnd();
    }
    glFinish();
    std::vector<unsigned char> pixels(size_t(w)*h*4);
    glReadPixels(0, 0, w, h, GL_BGRA, GL_UNSIGNED_BYTE, pixels.data());
    if (glGetError() != GL_NO_ERROR) throw std::runtime_error("scene GL rendering failed");
    auto image = raster.surface(0, camera.width, camera.height);
    auto dst = raster.data(image);
    for (int y = 0; y < camera.height; ++y)
      for (int x = 0; x < camera.width; ++x)
        for (int c = 0; c < 4; ++c) {
          unsigned sum = 0;
          for (int dy = 0; dy < 2; ++dy) for (int dx = 0; dx < 2; ++dx)
            sum += pixels[(size_t(h-1-y*2-dy)*w+x*2+dx)*4+c];
          dst[(size_t(y)*camera.width+x)*4+c] = (sum+2)/4;
        }
    auto dirty = reinterpret_cast<void (*)(void *)>(dlsym(cairo, "cairo_surface_mark_dirty"));
    auto write = reinterpret_cast<int (*)(void *, const char *)>(dlsym(cairo, "cairo_surface_write_to_png"));
    bool ok = dirty && write;
    if (ok) { dirty(image); ok = write(image, out.c_str()) == 0; }
    raster.surfaceDestroy(image); glBindFramebuffer(GL_FRAMEBUFFER, 0);
    if (!ok) throw std::runtime_error("scene PNG output failed");
  }
};
inline int renderSceneCommand(int argc, char **argv, int &i) {
  auto value = [&]() { if (++i >= argc) throw std::runtime_error("missing scene option value"); return std::string(argv[i]); };
  const auto scene = value(), out = value();
  SceneCamera camera;
  while (++i < argc) {
    std::string arg = argv[i];
    if (arg == "--eye") { auto v = sceneNumbers(value(), 3); std::copy(v.begin(), v.end(), camera.eye); }
    else if (arg == "--look") { auto v = sceneNumbers(value(), 2); camera.yaw = v[0]; camera.pitch = v[1]; }
    else if (arg == "--fov") camera.fov = sceneNumbers(value(), 1)[0];
    else if (arg == "--size") {
      auto size = value(); const auto x = size.find('x');
      if (x == std::string::npos) throw std::runtime_error("scene size must be WxH");
      auto dims = sceneNumbers(size.substr(0,x) + "," + size.substr(x+1), 2);
      if (dims[0] != std::floor(dims[0]) || dims[1] != std::floor(dims[1]) ||
          dims[0] < 64 || dims[1] < 64 || dims[0] > 4096 || dims[1] > 4096)
        throw std::runtime_error("scene size must be integer 64..4096");
      camera.width = int(dims[0]); camera.height = int(dims[1]);
    } else throw std::runtime_error("unknown scene option: " + arg);
  }
  auto session = std::make_shared<OverlaySession>(true);
  SceneRenderer renderer; renderer.render(scene, out, camera, session);
  return 0;
}
} // namespace fw
