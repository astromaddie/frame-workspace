#pragma once
// An offline compositor for the overlays the native host submits. The camera
// and room are diagnostic stand-ins; panel geometry/materials are production.
#include "overlay.hpp"
#include "spatial_render.hpp"
#include "wall.hpp"
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
    float extentU = 1, extentV = 1; // the part of the texture that is drawn on
    float tint = 1;                 // SetOverlayColor: a non-active window is dimmed
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
           float width = 0, float height = 0, float extentU = 1, float extentV = 1) {
    layers.push_back({s, bendShape(s.placement, s.height), t,
                      width ? width : s.placement.width,
                      height ? height : s.height, depth, order, extentU, extentV});
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
    const auto &b = l.surface.region.attached ? l.surface.region.shape : l.bend;
    glBindTexture(GL_TEXTURE_2D, l.texture);
    glColor4f(l.tint, l.tint, l.tint, 1);
    const int count = l.surface.region.attached ? l.surface.region.segments(l.width,l.height) : b.segments();
    for (int half = 0; half < count; ++half) {
      const auto seg = l.surface.region.attached ? l.surface.region.segment(half,l.width,l.height) :
          bendSegment(b, half, b.vertical ? l.height : l.width, l.depth);
      const float across = b.vertical ? l.width : l.height;
      const float radius = seg.curvature > 1e-4f ? seg.width / (2 * float(M_PI) * seg.curvature) : 0;
      glPushMatrix(); matrix(multiply(l.surface.placement.matrix, seg.local));
      const int steps = radius ? 128 : 1;
      glBegin(GL_QUAD_STRIP);
      for (int i = 0; i <= steps; ++i) {
        float u = float(i) / steps, x = (u - .5f) * seg.width;
        float px = radius ? radius * std::sin(x / radius) : x;
        float pz = radius ? radius * (1 - std::cos(x / radius)) : 0;
        float tu = (seg.bounds.uMin + u * (seg.bounds.uMax - seg.bounds.uMin)) * l.extentU;
        for (int top : {1, 0}) {
          float tv = (top ? 1 - seg.bounds.vMin : 1 - seg.bounds.vMax) * l.extentV;
          const bool sourceTurned = b.vertical && !l.surface.region.attached;
          glTexCoord2f(sourceTurned ? tv : tu, sourceTurned ? 1 - tu : tv);
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
    FrameWallRenderer walls;
    PanelSurface focused;
    int focusedPixels = 0;
    HmdMatrix34_t tip{};
    for (size_t i = 0; i < json_object_array_length(panels); ++i) {
      auto p = object(json_object_array_get_idx(panels, i));
      PanelSurface s;
      s.key = str(p, "id");
      if (str(p, "role") != "window") throw std::runtime_error("panel role must be window");
      auto pose = field(p, "placement");
      Image pic = load(str(p, "texture"));
      const int slot = wallSlot(p);
      if (pose) {
        if (!parsePlacement(json_object_to_json_string(pose), s.placement))
          throw std::runtime_error("invalid scene placement");
      } else if (slot >= 0)
        s.placement = wallPlacement(slot, float(pic.width) / pic.height, spatialIdentity());
      else throw std::runtime_error("scene needs placement or slot");
      s.height = slot >= 0 && !pose ? s.placement.width * pic.height / pic.width :
          float(num(p, "height", s.placement.width * pic.height / pic.width));
      if (!std::isfinite(s.height) || s.height <= 0 || s.height > 8)
        throw std::runtime_error("invalid scene panel height");
      bool selected = flag(p, "focused");
      if (selected && focusedPixels) throw std::runtime_error("only one focused scene panel allowed");
      int order = selected ? 50 : 0;
      add(s, pic.texture, order + 1, pictureDepth);
      if (flag(p, "dimmed")) layers.back().tint = spatialInactiveTint;
      HandleLayout layout; layout.width = s.placement.width; layout.height = s.height;
      ChromeState frame, controls;
      frame.focused = selected;
      frame.focusGlow = float(num(p, "focusGlow", 0));
      frame.focusSnap = float(num(p, "focusSnap", 1));
      frame.focusProgress = float(num(p, "focusProgress", 0));
      if (flag(p, "pillLaser")) {
        controls.tip = true; controls.tipX = layout.pillX(); controls.tipY = layout.rowY();
        const unsigned shown = layout.revealed(controls.tipX, controls.tipY);
        for (int h = 0; h < handleCount; ++h) if (shown & (1u << h)) controls.reveal[h] = 1;
        tip = multiply(s.placement.matrix, bendPointXY(bendShape(s.placement, s.height),
                        controls.tipX, controls.tipY, controlsDepth + .002f));
      }
      if (selected) {
        focused = s; focusedPixels = pic.width;
        if (!flag(p, "pillLaser")) {
        frame.tip = true; frame.tipX = layout.width * .30f; frame.tipY = -layout.height * .23f;
        tip = multiply(s.placement.matrix, bendPointXY(bendShape(s.placement, s.height),
                        frame.tipX, frame.tipY, pictureDepth + .002f));
        }
      }
      for(const auto &wall:frameWalls(bendShape(s.placement,s.height),layout)) {
        GLuint t=texture();walls.render(wall,layout,frame,t);
        PanelSurface side;side.role=PanelRole::Menu;side.placement.matrix=multiply(s.placement.matrix,wall.local);
        side.placement.width=wall.width;side.height=wall.height;
        add(side,t,order+1);
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
    SurfaceMessage statusMessage;
    if (auto statusItem = field(scene, "status")) {
      auto metadata = field(object(statusItem), "surface");
      if (metadata) statusMessage = SurfaceMessage::parse(json_object_to_json_string(metadata));
    }
    // Without a "shelf" object the shelf is the old full-size one. With it, the
    // shelf is pinned and drawn like the host draws it: "scale" (1 full size,
    // 0.6 at rest), "magnify" (0..1), "icon" (the app index under the
    // laser; sets the pointer and lights it), "handle" (show the move pill).
    const auto anchor = spatialAnchor(spatialIdentity(), 1.3f, 0);
    ShelfView view;
    int shelfHot = -1;
    HmdMatrix34_t shelfTip{};
    bool shelfTipped = false;
    auto shelfConfig = field(scene, "shelf");
    if (shelfConfig) {
      auto cfg = object(shelfConfig);
      view.pinned = true;
      view.pose = anchor;
      view.scale = float(num(cfg, "scale", 1));
      view.magnify = float(num(cfg, "magnify", 0));
      if (!std::isfinite(view.scale) || view.scale < .3f || view.scale > 1.5f ||
          !std::isfinite(view.magnify) || view.magnify < 0 || view.magnify > 1)
        throw std::runtime_error("invalid scene shelf");
      const int icon = int(num(cfg, "icon", -1));
      if (icon >= int(state.apps.size())) throw std::runtime_error("invalid scene shelf icon");
      if (icon >= 0) {
        const float step = std::min(.105f, 1.35f / std::max(size_t(1), state.apps.size())) * view.scale;
        view.pointer = (float(icon) - (state.apps.size() - 1) * .5f) * step;
      }
      auto first = spatialLayout(state, anchor, "", &statusMessage, view);
      if (flag(cfg, "handle") && !first.empty()) {
        view.handle = true;
        const auto &l = first.front().layout;
        view.chrome.reveal[handleIndex(Handle::Pill)] = 1;
        view.chrome.tip = true;
        view.chrome.tipX = l.pillX();
        view.chrome.tipY = l.rowY();
      }
      if (icon >= 0) shelfHot = icon + 1; // the shelf comes first, then the icons in order
    }
    // An optional "launcher" object opens Launcher A in front of the HMD pose
    // as the host does: "catalog" (launcher.json's format, inline), "query",
    // "results", "selected", "keyboard", "folder", "page", "hover"
    // ({"kind": "cell"|"recent"|"dot"|"back", "index": N}) and "carry"
    // ({"id": ID, "x": X, "y": Y} in panel units, 600 x 372). Icons are
    // <bundle>.png beside the JSON.
    LauncherCatalog launcherCatalog;
    LauncherView launcherView;
    LauncherTarget launcherHover;
    float hoverU = .5f, hoverV = .5f;
    if (auto rawLauncher = field(scene, "launcher")) {
      auto cfg = object(rawLauncher);
      auto catalog = field(cfg, "catalog");
      if (!catalog || !LauncherCatalog::parse(json_object_to_json_string(catalog), launcherCatalog))
        throw std::runtime_error("invalid scene launcher catalog");
      auto &l = state.launcher;
      l.available = l.open = true;
      l.keyboard = flag(cfg, "keyboard");
      l.query = str(cfg, "query");
      l.selected = int(num(cfg, "selected", 0));
      if (auto results = field(cfg, "results"))
        for (size_t i = 0; i < json_object_array_length(results); ++i)
          l.results.push_back(json_object_get_string(json_object_array_get_idx(results, i)));
      launcherView.update(l, true, spatialIdentity());
      launcherView.folder = str(cfg, "folder");
      launcherView.page = int(num(cfg, "page", 0));
      const auto listing = launcherListing(launcherCatalog, l, launcherView.folder);
      launcherView.clampPage(listing.pages());
      if (auto hover = field(cfg, "hover")) {
        auto h = object(hover);
        const auto kind = str(h, "kind");
        const int index = int(num(h, "index", 0));
        float x = 0, y = 0;
        if (kind == "cell") {
          launcherHover = {LauncherTarget::Cell, launcherView.page * launcherPerPage + index};
          launcherSlot(index, x, y);
          y += launcherIcon * .55f;
        } else if (kind == "recent") {
          launcherHover = {LauncherTarget::Recent, index};
          x = launcherRecentX(index, int(launcherCatalog.recent.size()));
          y = launcherRecentTop + launcherRecentIcon * .55f;
        } else if (kind == "dot") {
          launcherHover = {LauncherTarget::Dot, index};
          x = launcherDotX(index, listing.pages());
          y = launcherDotsY;
        } else if (kind == "back") {
          launcherHover = {LauncherTarget::Back, 0};
          x = launcherBackX + launcherBackW / 2;
          y = launcherBackY + launcherBackH / 2;
        } else throw std::runtime_error("invalid scene launcher hover");
        hoverU = x / launcherUnits;
        hoverV = y / launcherUnitsHigh;
      }
      if (auto carry = field(cfg, "carry")) {
        auto c = object(carry);
        auto &k = launcherView.carry;
        k.hand = 1;
        k.id = str(c, "id");
        k.container = listing.container;
        k.x = float(num(c, "x", 0));
        k.y = float(num(c, "y", 0));
        k.onPanel = true;
        k.gap = launcherGapAt(k.x, k.y, launcherView.page, int(listing.cells.size()));
        hoverU = k.x / launcherUnits;
        hoverV = k.y / launcherUnitsHigh;
      }
    }
    auto tiles = spatialLayout(state, anchor, "", &statusMessage, view);
    int launcherTile = -1;
    if (launcherView.open) {
      auto picture = std::make_shared<LauncherPicture>(
          launcherPicture(launcherCatalog, state.launcher, launcherView, launcherHover, false, root.string()));
      const int carried = launcherView.carry.active() ? launcherCatalog.app(launcherView.carry.id) : -1;
      launcherTiles(tiles, launcherView, picture,
                    carried >= 0 ? (root / (launcherCatalog.apps[carried].id + ".png")).string() : "");
      for (size_t i = 0; i < tiles.size(); ++i)
        if (tiles[i].kind == SpatialTile::Launcher) launcherTile = int(i);
      if (launcherHover.kind != LauncherTarget::None || launcherView.carry.active()) shelfHot = launcherTile;
    }
    SpatialRenderer shelf(session);
    shelf.draw(state, tiles, shelfHot, shelfHot == launcherTile ? hoverU : .5f, shelfHot == launcherTile ? hoverV : .5f,
               false, root.string(), [](const std::string &) { return SpatialPicture{}; });
    for (size_t i = 0; i < tiles.size(); ++i) {
      if (!tiles[i].shown) continue;
      float eu = 1, ev = 1;
      shelf.tileExtent(i, eu, ev);
      add(tiles[i].surface, shelf.tileTexture(i), int(SpatialRenderer::sortOrder(tiles[i].kind)), 0, 0, 0, eu, ev);
    }
    if (shelfHot >= 0 && shelfHot == launcherTile) {
      const auto &surface = tiles[launcherTile].surface;
      shelfTip = surfacePoint(surface, (hoverU - .5f) * surface.placement.width, (.5f - hoverV) * surface.height, .004f);
      shelfTipped = true;
    } else if (shelfHot >= 0) {
      shelfTip = multiply(tiles[shelfHot].surface.placement.matrix, bendPointXY(
          bendShape(tiles[shelfHot].surface.placement, tiles[shelfHot].surface.height), 0, 0, .004f));
      shelfTipped = true;
    } else if (view.handle) {
      const auto &move = *std::find_if(tiles.begin(), tiles.end(), [](const auto &t) { return t.kind == SpatialTile::Move; });
      shelfTip = multiply(move.surface.placement.matrix, bendPointXY(
          bendShape(move.surface.placement, move.surface.height), move.layout.pillX() * view.scale, move.layout.rowY() * view.scale, .004f));
      shelfTipped = true;
    }
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
      const float mpp = windowMetresPerPoint(focused.placement.width, focusedPixels,
                                             float(num(item, "logicalWidth", 0)));
      if (std::string(name) == "menubar") {
        if (!focusedPixels) throw std::runtime_error("menubar needs a focused window");
        menubar = s = menuStripPlacement(focused, m, mpp, float(num(item, "reveal", m.hot ? 1.f : 0.f)));
      } else if (std::string(name) == "status") {
        status = s = statusStripPlacement(tiles, m, .001f, view.scale);
      } else {
        auto anchor = m.anchor == "status" ? status : m.anchor == "menubar" ? menubar : focused;
        s = surfacePlacement(anchor, m, m.anchor == "status" ? .001f : mpp);
      }
      if (s.height <= 0 || s.placement.width <= 0) throw std::runtime_error("missing auxiliary anchor");
      auto pic = load(str(item, "texture")); GLuint t = texture();
      const bool transposed = s.region.attached && s.region.shape.vertical;
      material.render(pic.texture, t, transposed ? pic.height : pic.width,
          transposed ? pic.width : pic.height, s, transposed, false);
      add(s, t, std::string(name) == "dropdown" ? 104 : std::string(name) == "menubar" ? 51 : 102);
    }
    if (!focusedPixels && shelfTipped) tip = shelfTip;
    if (focusedPixels || shelfTipped) {
      GLuint cursor = createCursorTexture(); textures.push_back(cursor);
      PanelSurface s; s.placement.matrix = tip; s.placement.width = .014f; s.height = .014f;
      add(s, cursor, focusedPixels ? 54 : 101);
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
    if (focusedPixels || shelfTipped) {
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
