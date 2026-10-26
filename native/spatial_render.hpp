#pragma once
#include "launcher_render.hpp"
#include "spatial.hpp"
#include "session.hpp"
#include <array>
// Cairo is already a runtime dependency of frame-workspace-dash. Resolve its
// small text/PNG surface API here so the native viewer retains its link
// closure.
namespace fw {
class SpatialRaster {
  void *lib = nullptr;
  template <class T> T sym(const char *n) {
    auto p = dlsym(lib, n);
    if (!p)
      throw std::runtime_error("Cairo surface API unavailable");
    return reinterpret_cast<T>(p);
  }

public:
  using P = void *;
  struct Extents {
    double xb, yb, width, height, xa, ya;
  };
  void (*extents)(P, const char *, Extents *);
  P (*surface)(int, int, int);
  P (*png)(const char *);
  P (*create)(P);
  void (*destroy)(P), (*surfaceDestroy)(P), (*flush)(P);
  unsigned char *(*data)(P);
  int (*width)(P), (*height)(P), (*status)(P);
  void (*rgba)(P, double, double, double, double), (*paint)(P),
      (*move)(P, double, double), (*fontSize)(P, double),
      (*text)(P, const char *), (*source)(P, P, double, double),
      (*scale)(P, double, double), (*save)(P), (*restore)(P),
      (*rectangle)(P, double, double, double, double), (*fill)(P);
  SpatialRaster() {
    for (auto n : {"libcairo.so.2", "libcairo.2.dylib",
                   "/opt/homebrew/lib/libcairo.2.dylib"})
      if ((lib = dlopen(n, RTLD_NOW | RTLD_LOCAL)))
        break;
    if (!lib)
      throw std::runtime_error("Cairo runtime unavailable for spatial labels");
#define C(member, name) member = sym<decltype(member)>("cairo_" name)
    C(surface, "image_surface_create");
    C(png, "image_surface_create_from_png");
    C(create, "create");
    C(destroy, "destroy");
    C(surfaceDestroy, "surface_destroy");
    C(flush, "surface_flush");
    C(data, "image_surface_get_data");
    C(width, "image_surface_get_width");
    C(height, "image_surface_get_height");
    C(status, "surface_status");
    C(rgba, "set_source_rgba");
    C(paint, "paint");
    C(move, "move_to");
    C(fontSize, "set_font_size");
    C(text, "show_text");
    C(extents, "text_extents");
    C(source, "set_source_surface");
    C(scale, "scale");
    C(save, "save");
    C(restore, "restore");
    C(rectangle, "rectangle");
    C(fill, "fill");
#undef C
  }
  ~SpatialRaster() {
    if (lib)
      dlclose(lib);
  }
  // `divider` >= 0 cuts the shelf's 3 px V-groove (the launcher's groove, turned
  // upright) at that column, from row `from` to row `to`.
  GLuint label(const std::string &label, const std::string &icon, int w, int h,
               bool running, int divider = -1, int from = 0, int to = 0) {
    auto s = surface(0, w, h);
    auto c = create(s);
    if (divider >= 0)
      for (auto [dx, r, g, b] : {std::array<double, 4>{0, .635, .588, .702}, {1, .796, .769, .839}, {2, 1, 1, 1}}) {
        rgba(c, r, g, b, 1);
        rectangle(c, divider - 1 + dx, from, 1, to - from);
        fill(c);
      }
    if (!icon.empty()) {
      auto im = png(icon.c_str());
      if (!status(im) && width(im) == 128 && height(im) == 128) {
        save(c);
        double size = std::min(w * .76, h * .72);
        move(c, 0, 0); // source transformed in pixel coordinates
        scale(c, size / 128, size / 128);
        source(c, im, (w - size) * .5 * 128 / size, 5 * 128 / size);
        paint(c);
        restore(c);
      }
      surfaceDestroy(im);
    }
    rgba(c, .22, .18, .28, 1);
    double fs = h == 64 ? 29 : 28;
    fontSize(c, fs);
    Extents ex{};
    extents(c, label.c_str(), &ex);
    if (ex.width > w - 16) {
      fs *= double(w - 16) / ex.width;
      fontSize(c, fs);
      extents(c, label.c_str(), &ex);
    }
    move(c, (w - ex.width) * .5 - ex.xb, h - 9);
    text(c, label.c_str());
    if (running) {
      fontSize(c, 18);
      move(c, w / 2 - 3, h - 24);
      text(c, "·");
    }
    flush(s);
    GLuint t;
    glGenTextures(1, &t);
    glBindTexture(GL_TEXTURE_2D, t);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_BGRA, GL_UNSIGNED_BYTE,
                 data(s));
    destroy(c);
    surfaceDestroy(s);
    return t;
  }
};
struct SpatialPicture {
  GLuint texture = 0;
  bool transposed = false;
};
class SpatialRenderer {
  GLuint program = 0, fb = 0;
  std::unique_ptr<SpatialRaster> raster;
  struct Layer {
    GLuint texture = 0, label = 0;
    VROverlayHandle_t overlay = 0;
    std::string signature, drawSignature;
    std::string iconPath, iconVersion;
    double iconChecked = -1000;
    int width = 0, height = 0;
    int kind = -1;         // the tile kind its overlay's sort order was set for
    float curvature = 0;   // OpenVR curvature last set
    float boundsU = -1, boundsV = -1; // shelf: the part of its texture the overlay shows
  };
  std::unique_ptr<LauncherPainter> painter; // the launcher, its button and backdrop
  unsigned shelfAllocations = 0;

public:
  static uint32_t sortOrder(SpatialTile::Kind kind) { return spatialSortOrder(kind); }
  // Where a tile is drawn: its layout, lifted by a launching icon's hop (a little
  // in front, so it clears its neighbours).
  static HmdMatrix34_t drawnMatrix(const SpatialTile &t) {
    if (t.lift == 0) return t.surface.placement.matrix;
    auto up = spatialIdentity();
    up.m[1][3] = t.lift;
    up.m[2][3] = .004f;
    return multiply(t.surface.placement.matrix, up);
  }

private:
  std::vector<Layer> layers;
  Layer handleLayer; // the move pill's own overlay, whichever tile index it has
  std::vector<GLuint> drawn;
  std::vector<std::pair<float, float>> extent;
  std::shared_ptr<OverlaySession> session;
  ChromeRenderer chrome; // the shelf's move pill is the windows' own pill
  static GLuint shader(GLenum type, const char *src) {
    auto s = glCreateShader(type);
    glShaderSource(s, 1, &src, nullptr);
    glCompileShader(s);
    GLint ok = 0;
    glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
    if (!ok)
      throw std::runtime_error("spatial shader compilation failed");
    return s;
  }
  static void quad(float x0 = -1, float y0 = -1, float x1 = 1, float y1 = 1,
                   bool transpose = false) {
    glBegin(GL_QUADS);
    for (auto v : {std::array<float, 4>{0, 0, x0, y0},
                   {1, 0, x1, y0},
                   {1, 1, x1, y1},
                   {0, 1, x0, y1}}) {
      glTexCoord2f(transpose ? 1 - v[1] : v[0], transpose ? v[0] : v[1]);
      glVertex2f(v[2], v[3]);
    }
    glEnd();
  }

public:
  explicit SpatialRenderer(std::shared_ptr<OverlaySession> s)
      : session(std::move(s)) {}
  ~SpatialRenderer() {
    layers.push_back(handleLayer);
    for (auto &l : layers) {
      if (l.overlay)
        session->destroyOverlay(l.overlay);
      glDeleteTextures(1, &l.texture);
      glDeleteTextures(1, &l.label);
    }
    chrome.release();
    if (program)
      glDeleteProgram(program);
    if (fb)
      glDeleteFramebuffers(1, &fb);
  }
  void draw(const SpatialState &state, const std::vector<SpatialTile> &tiles,
            int hot, float u, float v, bool held, const std::string &icons,
            const std::function<SpatialPicture(const std::string &)> &picture) {
    if (tiles.empty()) {
      for (auto &l : layers)
        if (l.overlay)
          session->overlay->HideOverlay(l.overlay);
      return;
    }
    if (!program) {
      raster = std::make_unique<SpatialRaster>();
      auto vs = shader(
          GL_VERTEX_SHADER,
          "#version 120\nvoid "
          "main(){gl_Position=gl_Vertex;gl_TexCoord[0]=gl_MultiTexCoord0;}");
      auto fs = shader(GL_FRAGMENT_SHADER, R"GLSL(#version 120
uniform vec2 size;uniform vec3 tip;uniform float held,selected,clear,lighting,reach;
void main(){vec2 q=gl_TexCoord[0].xy,p=q*size;float r=7.;vec2 d=abs(p-size*.5)-(size*.5-r);float edge=length(max(d,0.))+min(max(d.x,d.y),0.)-r;float a=1.-smoothstep(-1.,0.,edge);vec3 c=mix(vec3(.985,.98,.995),vec3(.79,.76,.84),q.y*.75+q.x*.25);float rim=smoothstep(-4.,-1.,edge);c+=rim*(.12*(1.-q.y)-.12*q.y);vec2 t=(q-tip.xy)*size/reach;float light=tip.z*exp(-dot(t,t)/1000.);if(lighting>.5){float core=tip.z*exp(-dot(t,t)/3.);float alpha=min(1.,light*.22+core)*a;gl_FragColor=vec4(mix(vec3(.5,.78,1.),vec3(1.),core)*alpha,alpha);return;}c+=light*vec3(.12,.20,.24);c=mix(c,vec3(.45,.72,.95),held*.45);c=mix(c,vec3(.35,.68,1.),selected*rim*.8);c+=vec3(1.)*tip.z*exp(-dot(t,t)/3.);if(clear>.5){float core=tip.z*exp(-dot(t,t)/3.);a=max(held*.3+light*.18,core);c=mix(vec3(.48,.74,1.),vec3(1.),core); }gl_FragColor=vec4(c*a,a);})GLSL");
      program = glCreateProgram();
      glAttachShader(program, vs);
      glAttachShader(program, fs);
      glLinkProgram(program);
      glDeleteShader(vs);
      glDeleteShader(fs);
      GLint ok;
      glGetProgramiv(program, GL_LINK_STATUS, &ok);
      if (!ok)
        throw std::runtime_error("spatial shader link failed");
      glGenFramebuffers(1, &fb);
    }
    while (layers.size() < tiles.size())
      layers.emplace_back();
    GLint vp[4];
    glGetIntegerv(GL_VIEWPORT, vp);
    drawn.assign(tiles.size(), 0);
    extent.assign(tiles.size(), {1.f, 1.f});
    for (size_t i = 0; i < layers.size(); ++i) {
      if (i >= tiles.size()) {
        if (layers[i].overlay)
          session->overlay->HideOverlay(layers[i].overlay);
        continue;
      }
      auto &t = tiles[i];
      const bool move = t.kind == SpatialTile::Move;
      auto &l = move ? handleLayer : layers[i];
      if (move && layers[i].overlay)
        session->overlay->HideOverlay(layers[i].overlay);
      if (move && !t.shown) {
        if (l.overlay)
          session->overlay->HideOverlay(l.overlay);
        continue;
      }
      // The shelf is drawn at the size it has (whole 8 px steps, so a still
      // shelf does not redraw) into the corner of a texture that keeps one size
      // while it animates; see shelfRaster. cw x ch is the texture, w x h what is
      // drawn on it. Every other tile fills its texture.
      const bool shelf = t.kind == SpatialTile::Shelf;
      const ShelfRaster sr = shelf ? shelfRaster(t) : ShelfRaster{};
      const bool iconTile = t.kind == SpatialTile::Dock || t.kind == SpatialTile::Apps || t.kind == SpatialTile::Carry;
      int w = shelf                        ? sr.w
              : iconTile                        ? 128
              : t.kind == SpatialTile::Launcher ? int(launcherUnits * launcherPixelsPerUnit)
              : t.kind == SpatialTile::Dim      ? 256
                                                : 384,
          h = shelf                             ? sr.h
              : iconTile                        ? 140
              : t.kind == SpatialTile::Launcher ? int(std::lround(launcherUnitsHigh * launcherPixelsPerUnit))
              : t.kind == SpatialTile::Dim
                  ? std::max(16, int(std::lround(256 * t.surface.height / std::max(.01f, t.surface.placement.width))))
              : t.kind == SpatialTile::Menu ? 64
                                            : 280;
      const int cw = shelf ? sr.canvasW : w, ch = shelf ? sr.canvasH : h;
      if (shelf)
        extent[i] = {sr.u, sr.v};
      bool redraw = false;
      if (move) {
        ChromeRenderer::textureSize(t.layout.chromeWidth(), t.layout.chromeHeight(), false, w, h);
        const auto &c = t.chrome;
        const int pill = handleIndex(Handle::Pill);
        auto signature = std::to_string(w) + "x" + std::to_string(h) + ":" +
                         std::to_string(int(c.reveal[pill] * 64)) + "," +
                         std::to_string(int(c.held[pill] * 64)) + "," + std::to_string(c.tip) + "," +
                         std::to_string(int(c.tipX * 1000)) + "," + std::to_string(int(c.tipY * 1000));
        if (signature != l.drawSignature || !l.texture) {
          if (!l.texture)
            glGenTextures(1, &l.texture);
          chrome.render(1, t.layout, c, t.layout.chromeWidth(), t.layout.chromeHeight(), false, l.texture, w, h);
          l.drawSignature = signature;
          l.width = w;
          l.height = h;
          redraw = true;
        }
      } else {
        std::string icon =
            t.kind == SpatialTile::Dock && !state.apps[t.index].icon.empty()
                ? (std::filesystem::path(icons) / state.apps[t.index].icon).string()
            : t.kind == SpatialTile::Carry ? t.icon
                                           : "";
        bool running = t.kind == SpatialTile::Dock && state.apps[t.index].running;
        const bool painted = t.kind == SpatialTile::Apps || t.kind == SpatialTile::Launcher || t.kind == SpatialTile::Dim;
        if (painted && !painter) painter = std::make_unique<LauncherPainter>();
        // The shelf's groove sits at a column of the picture drawn at its current size.
        const int divider = shelf && t.dividerX >= 0
            ? std::clamp(int(std::lround(t.dividerX / std::max(.001f, t.surface.placement.width) * w)), 2, std::max(2, w - 3))
            : -1;
        auto signature = t.surface.key + t.label + icon + std::to_string(running) + std::to_string(t.kind) +
                         std::to_string(t.lit) + std::to_string(w) + "x" + std::to_string(h) +
                         "d" + std::to_string(divider);
        if (t.kind == SpatialTile::Launcher && t.picture)
          signature += t.picture->signature() + painter->iconStamp(*t.picture, nowMs());
        // Icons and JSON travel separately. Recheck without rerasterizing static
        // labels, so an icon arriving after its state entry can become visible.
        if (icon != l.iconPath || nowMs() - l.iconChecked >= 1000) {
          l.iconPath = icon;
          l.iconChecked = nowMs();
          std::error_code error;
          auto stamp = std::filesystem::last_write_time(icon, error);
          l.iconVersion = error ? "missing" : std::to_string(
              std::chrono::duration_cast<std::chrono::nanoseconds>(stamp.time_since_epoch()).count());
        }
        signature += l.iconVersion;
        bool labelChanged = signature != l.signature;
        if (labelChanged) {
          if (painted) {
            if (t.kind == SpatialTile::Apps) l.label = painter->button(t.lit, w, h, l.label);
            else if (t.kind == SpatialTile::Dim) l.label = painter->backdrop(w, h, .45, l.label);
            else if (t.picture) l.label = painter->panel(*t.picture, w, h, l.label, nowMs());
          } else {
            if (l.label)
              glDeleteTextures(1, &l.label);
            l.label = raster->label(t.kind == SpatialTile::Dock || t.kind == SpatialTile::Carry ? "" : t.label,
                                    icon, w, h, running, divider, int(h * .22f), int(h * .78f));
          }
          l.signature = signature;
        }
        auto drawSignature = signature + std::to_string(t.kind) + std::to_string(w) + "x" + std::to_string(h) +
                             std::to_string(int(i) == hot) +
                             std::to_string(held && int(i) == hot) +
                             std::to_string(t.kind == SpatialTile::Window &&
                                            t.index == state.index);
        if (int(i) == hot)
          drawSignature +=
              std::to_string(int(u * 128)) + "," + std::to_string(int(v * 128));
        redraw = labelChanged || drawSignature != l.drawSignature ||
                      t.kind == SpatialTile::Window;
        if (redraw) {
          l.drawSignature = drawSignature;
          if (!l.texture)
            glGenTextures(1, &l.texture);
          glBindTexture(GL_TEXTURE_2D, l.texture);
          glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
          glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
          if (l.width != cw || l.height != ch) {
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, cw, ch, 0, GL_RGBA,
                         GL_UNSIGNED_BYTE, nullptr);
            l.width = cw;
            l.height = ch;
            if (shelf)
              ++shelfAllocations;
          }
          glBindFramebuffer(GL_FRAMEBUFFER, fb);
          glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                                 GL_TEXTURE_2D, l.texture, 0);
          if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE)
            throw std::runtime_error("spatial framebuffer incomplete");
          glViewport(0, 0, w, h);
          if (shelf) {
            // What an earlier, larger drawing left outside this one must not show.
            glClearColor(0, 0, 0, 0);
            glClear(GL_COLOR_BUFFER_BIT);
          }
          glDisable(GL_DEPTH_TEST);
          glDisable(GL_BLEND);
          glUseProgram(program);
          glUniform1f(glGetUniformLocation(program, "lighting"), 0);
          // The launcher's laser spot is drawn larger: its texture is dense.
          glUniform1f(glGetUniformLocation(program, "reach"), t.kind == SpatialTile::Launcher ? 3.f : 1.f);
          glUniform2f(glGetUniformLocation(program, "size"), w, h);
          glUniform3f(glGetUniformLocation(program, "tip"), u, v, int(i) == hot);
          glUniform1f(glGetUniformLocation(program, "held"),
                      held && int(i) == hot);
          glUniform1f(glGetUniformLocation(program, "clear"), iconTile || painted);
          glUniform1f(glGetUniformLocation(program, "selected"),
                      t.kind == SpatialTile::Window && t.index == state.index);
          quad();
          glUseProgram(0);
          glEnable(GL_TEXTURE_2D);
          glColor4f(1, 1, 1, 1);
          if (t.kind == SpatialTile::Window) {
            auto pic = picture(state.windows[t.index].key);
            if (pic.texture) {
              glBindTexture(GL_TEXTURE_2D, pic.texture);
              quad(-.95f, -.94f, .95f, .72f, pic.transposed);
            }
          }
          glEnable(GL_BLEND);
          glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA);
          glBindTexture(GL_TEXTURE_2D, l.label);
          quad();
          glDisable(GL_TEXTURE_2D);
          if (int(i) == hot) {
            glUseProgram(program);
            glUniform1f(glGetUniformLocation(program, "lighting"), 1);
            quad();
            glUseProgram(0);
          }
          glDisable(GL_BLEND);
          glBindFramebuffer(GL_FRAMEBUFFER, 0);
        }
      }
      drawn[i] = l.texture;
      if (!session->noVR) {
        if (!l.overlay) {
          std::string key = "com.frame-workspace.spatial." +
                            std::to_string(getpid()) + "." + (move ? "move" : std::to_string(i));
          if (session->createOverlay(
                  key, "Frame Workspace", &l.overlay))
            throw std::runtime_error("spatial overlay unavailable");
          VRTextureBounds_t bounds{0, 1, 1, 0};
          session->overlay->SetOverlayTextureBounds(l.overlay, &bounds);
          l.boundsU = l.boundsV = 1;
          session->overlay->SetOverlayFlag(l.overlay, VROverlayFlags_IsPremultiplied, true);
        }
        if (l.kind != int(t.kind)) {
          // Layers are reused as the tile list changes.
          session->overlay->SetOverlaySortOrder(l.overlay, sortOrder(t.kind));
          l.kind = int(t.kind);
        }
        const float curvature = t.surface.placement.bendA;
        if (curvature != l.curvature && session->overlay->SetOverlayCurvature) {
          session->overlay->SetOverlayCurvature(l.overlay, curvature);
          l.curvature = curvature;
        }
        auto m = drawnMatrix(t);
        session->overlay->SetOverlayTransformAbsolute(
            l.overlay, ETrackingUniverseOrigin_TrackingUniverseStanding, &m);
        session->overlay->SetOverlayWidthInMeters(l.overlay,
                                                  t.surface.placement.width * t.visualScale);
        if (shelf && (sr.u != l.boundsU || sr.v != l.boundsV)) {
          // Rows run top to bottom from v = 0, as the overlay's default flip
          // above has it: the drawn corner is [0, u] x [0, v].
          VRTextureBounds_t bounds{0, sr.v, sr.u, 0};
          session->overlay->SetOverlayTextureBounds(l.overlay, &bounds);
          l.boundsU = sr.u;
          l.boundsV = sr.v;
        }
        Texture_t tex{reinterpret_cast<void *>(uintptr_t(l.texture)),
                      ETextureType_TextureType_OpenGL,
                      EColorSpace_ColorSpace_Gamma};
        if (redraw) {
          glFlush();
          session->overlay->SetOverlayTexture(l.overlay, &tex);
        }
        session->overlay->ShowOverlay(l.overlay);
      }
    }
    glViewport(vp[0], vp[1], vp[2], vp[3]);
  }
  // Borrowed texture after draw(); owned until the renderer is destroyed. For
  // the shelf only the top-left `tileExtent` of it is drawn on.
  GLuint tileTexture(size_t index) const { return drawn.at(index); }
  // The drawn part of tile `index`'s texture, as fractions (u across, v down).
  void tileExtent(size_t index, float &u, float &v) const {
    u = extent.at(index).first;
    v = extent.at(index).second;
  }
  // How many times the shelf's texture storage has been allocated. It changes
  // only when the shelf's content (its app count or status band) does, never
  // while the shelf grows, shrinks or magnifies.
  unsigned shelfAllocationCount() const { return shelfAllocations; }
  bool dump(const std::string &path, const std::vector<SpatialTile> &tiles) {
    const int w = 1280, h = 720;
    GLuint output;
    glGenTextures(1, &output);
    glBindTexture(GL_TEXTURE_2D, output);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE,
                 nullptr);
    glBindFramebuffer(GL_FRAMEBUFFER, fb);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D,
                           output, 0);
    glViewport(0, 0, w, h);
    glClearColor(.09, .08, .11, 1);
    glClear(GL_COLOR_BUFFER_BIT);
    glMatrixMode(GL_PROJECTION);
    glLoadIdentity();
    glFrustum(-.10, .10, -.05625, .05625, .1, 10);
    glMatrixMode(GL_MODELVIEW);
    glLoadIdentity();
    glEnable(GL_BLEND);
    glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA);
    glEnable(GL_TEXTURE_2D);
    glColor4f(1, 1, 1, 1);
    for (size_t i = 0; i < tiles.size(); ++i) {
      auto &t = tiles[i];
      if (!t.shown || i >= drawn.size())
        continue;
      const auto m = drawnMatrix(t);
      float matrix[16] = {m.m[0][0], m.m[1][0], m.m[2][0], 0,
                          m.m[0][1], m.m[1][1], m.m[2][1], 0,
                          m.m[0][2], m.m[1][2], m.m[2][2], 0,
                          m.m[0][3], m.m[1][3], m.m[2][3], 1};
      glPushMatrix();
      glMultMatrixf(matrix);
      glBindTexture(GL_TEXTURE_2D, drawn[i]);
      float x = t.surface.placement.width * t.visualScale / 2, y = t.surface.height * t.visualScale / 2;
      const float eu = extent[i].first, ev = extent[i].second;
      glBegin(GL_QUADS);
      glTexCoord2f(0, 0);
      glVertex2f(-x, y);
      glTexCoord2f(eu, 0);
      glVertex2f(x, y);
      glTexCoord2f(eu, ev);
      glVertex2f(x, -y);
      glTexCoord2f(0, ev);
      glVertex2f(-x, -y);
      glEnd();
      glPopMatrix();
    }
    std::vector<unsigned char> p(w * h * 3);
    glReadPixels(0, 0, w, h, GL_RGB, GL_UNSIGNED_BYTE, p.data());
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    glDeleteTextures(1, &output);
    std::ofstream f(path, std::ios::binary);
    f << "P6\n" << w << " " << h << "\n255\n";
    for (int y = h - 1; y >= 0; --y)
      f.write(reinterpret_cast<char *>(p.data() + y * w * 3), w * 3);
    return bool(f);
  }
};
} // namespace fw
