#pragma once
// Draws the launcher (FW-15, Launcher A) the way the approved mockup does
// (evidence/launcher-mockup.html, option A): a Palladium panel with a crisp
// bevel, a recessed search field, section labels, a groove, icon tiles and
// page dots; #1a9fff is the only accent. Also the shelf's Applications button
// and the backdrop that dims the windows behind. Cairo, loaded at run time
// like the other spatial labels, so there is no new link dependency.
#include "launcher.hpp"
#include <SDL_opengl.h>
#include <dlfcn.h>

namespace fw {

class LauncherPainter {
  using P = void *;
  void *lib = nullptr;
  struct Extents {
    double xb, yb, width, height, xa, ya;
  };
  template <class T> void load(T &fn, const char *name) {
    fn = reinterpret_cast<T>(dlsym(lib, name));
    if (!fn) throw std::runtime_error(std::string("Cairo function unavailable: ") + name);
  }
  P (*imageSurface)(int, int, int) = nullptr;
  P (*fromPNG)(const char *) = nullptr;
  P (*create)(P) = nullptr;
  void (*destroy)(P) = nullptr, (*surfaceDestroy)(P) = nullptr, (*flush)(P) = nullptr;
  unsigned char *(*data)(P) = nullptr;
  int (*imageWidth)(P) = nullptr, (*imageHeight)(P) = nullptr, (*status)(P) = nullptr;
  void (*rgba)(P, double, double, double, double) = nullptr;
  void (*paint)(P) = nullptr, (*paintAlpha)(P, double) = nullptr;
  void (*moveTo)(P, double, double) = nullptr, (*lineTo)(P, double, double) = nullptr;
  void (*arc)(P, double, double, double, double, double) = nullptr;
  void (*newPath)(P) = nullptr, (*newSubPath)(P) = nullptr, (*closePath)(P) = nullptr;
  void (*rectangle)(P, double, double, double, double) = nullptr;
  void (*fill)(P) = nullptr, (*fillPreserve)(P) = nullptr, (*stroke)(P) = nullptr, (*clip)(P) = nullptr;
  void (*lineWidth)(P, double) = nullptr, (*lineCap)(P, int) = nullptr, (*op)(P, int) = nullptr;
  void (*fontFace)(P, const char *, int, int) = nullptr, (*fontSize)(P, double) = nullptr;
  void (*showText)(P, const char *) = nullptr, (*textExtents)(P, const char *, Extents *) = nullptr;
  void (*sourceSurface)(P, P, double, double) = nullptr, (*source)(P, P) = nullptr;
  void (*scale)(P, double, double) = nullptr, (*translate)(P, double, double) = nullptr;
  void (*save)(P) = nullptr, (*restore)(P) = nullptr;
  P (*linear)(double, double, double, double) = nullptr, (*radial)(double, double, double, double, double, double) = nullptr;
  void (*stop)(P, double, double, double, double, double) = nullptr, (*patternDestroy)(P) = nullptr;

  struct Icon {
    P surface = nullptr;
    std::string stamp;
    double checked = -1e9;
  };
  std::map<std::string, Icon> icons;

  static void hex(uint32_t c, double &r, double &g, double &b) {
    r = ((c >> 16) & 255) / 255.;
    g = ((c >> 8) & 255) / 255.;
    b = (c & 255) / 255.;
  }
  void color(P c, uint32_t value, double alpha = 1) {
    double r, g, b;
    hex(value, r, g, b);
    rgba(c, r, g, b, alpha);
  }
  void addStop(P pattern, double at, uint32_t value, double alpha = 1) {
    double r, g, b;
    hex(value, r, g, b);
    stop(pattern, at, r, g, b, alpha);
  }
  void rounded(P c, double x, double y, double w, double h, double r) {
    r = std::min({r, w / 2, h / 2});
    newSubPath(c);
    arc(c, x + w - r, y + r, r, -M_PI / 2, 0);
    arc(c, x + w - r, y + h - r, r, 0, M_PI / 2);
    arc(c, x + r, y + h - r, r, M_PI / 2, M_PI);
    arc(c, x + r, y + r, r, M_PI, 3 * M_PI / 2);
    closePath(c);
  }
  // A CSS-style linear gradient across a box at `degrees` (0 = up, 90 = right).
  P cssLinear(double x, double y, double w, double h, double degrees) {
    const double a = degrees * M_PI / 180, dx = std::sin(a), dy = -std::cos(a);
    const double half = (std::fabs(w * dx) + std::fabs(h * dy)) / 2, cx = x + w / 2, cy = y + h / 2;
    return linear(cx - dx * half, cy - dy * half, cx + dx * half, cy + dy * half);
  }
  void fillWith(P c, P pattern, bool preserve = false) {
    source(c, pattern);
    preserve ? fillPreserve(c) : fill(c);
    patternDestroy(pattern);
  }
  // The Palladium face: a 115-degree gradient, a sheen, a light-to-shade bevel
  // just inside a crisp border. `pressed` is the mockup's .pal.pressed.
  void palladium(P c, double x, double y, double w, double h, double r, bool pressed = false, bool hot = false) {
    auto face = cssLinear(x, y, w, h, 115);
    if (pressed) {
      addStop(face, 0, 0xd9d3e3); addStop(face, .48, 0xcdc6d9); addStop(face, 1, 0xbdb5cb);
    } else {
      addStop(face, 0, 0xfbfafd); addStop(face, .48, 0xe3dfea); addStop(face, 1, 0xcbc4d6);
    }
    rounded(c, x, y, w, h, r);
    fillWith(c, face);
    if (pressed || hot) {
      auto glow = radial(x + w / 2, y + h * .55, 0, x + w / 2, y + h * .55, std::max(w, h) * .62);
      addStop(glow, 0, pressed ? 0x6fbcff : 0xc4e4ff, pressed ? .28 : .9);
      addStop(glow, .3, 0x6fbcff, pressed ? .19 : .6);
      addStop(glow, .7, 0x3a9dff, pressed ? .08 : .2);
      addStop(glow, 1, 0x3a9dff, 0);
      rounded(c, x, y, w, h, r);
      fillWith(c, glow);
    }
    auto sheen = cssLinear(x, y, w, h, 160);
    addStop(sheen, 0, 0xffffff, .22);
    addStop(sheen, .38, 0xffffff, 0);
    rounded(c, x, y, w, h, r);
    fillWith(c, sheen);
    // Bevel: light on the top and left, shade on the bottom and right.
    auto bevel = cssLinear(x, y, w, h, 135);
    if (pressed) {
      addStop(bevel, 0, 0x8f84a3); addStop(bevel, .5, 0xbdb5cb, .4); addStop(bevel, 1, 0xffffff);
    } else {
      addStop(bevel, 0, 0xffffff); addStop(bevel, .5, 0xffffff, .35); addStop(bevel, 1, 0xa296b3);
    }
    lineWidth(c, 1.1);
    rounded(c, x + 1.3, y + 1.3, w - 2.6, h - 2.6, std::max(0., r - 1.3));
    source(c, bevel);
    stroke(c);
    patternDestroy(bevel);
    if (pressed) {
      // The pressed piece's blue inner rim.
      color(c, 0x2f9dff, .55);
      lineWidth(c, 1.4);
      rounded(c, x + 1, y + 1, w - 2, h - 2, std::max(0., r - 1));
      stroke(c);
    }
    color(c, pressed ? 0x5b8fc4 : 0x8d82a0);
    lineWidth(c, .9);
    rounded(c, x + .45, y + .45, w - .9, h - .9, r);
    stroke(c);
  }
  // The recessed field (.well).
  void well(P c, double x, double y, double w, double h, double r) {
    auto face = linear(x, y, x, y + h);
    addStop(face, 0, 0xebe8f0);
    addStop(face, 1, 0xf8f7fa);
    rounded(c, x, y, w, h, r);
    fillWith(c, face);
    auto inner = cssLinear(x, y, w, h, 135);
    addStop(inner, 0, 0xb7aec5);
    addStop(inner, .5, 0xd9d3e3, .5);
    addStop(inner, 1, 0xffffff);
    lineWidth(c, 1.2);
    rounded(c, x + 1.2, y + 1.2, w - 2.4, h - 2.4, std::max(0., r - 1.2));
    source(c, inner);
    stroke(c);
    patternDestroy(inner);
    color(c, 0x9d92ae);
    lineWidth(c, .9);
    rounded(c, x + .45, y + .45, w - .9, h - .9, r);
    stroke(c);
  }
  void text(P c, const std::string &value, double x, double y, double size, uint32_t ink, bool bold = false,
            int align = -1, double maxWidth = 0) {
    fontFace(c, "sans-serif", 0, bold ? 1 : 0);
    fontSize(c, size);
    std::string shown = value;
    Extents e{};
    textExtents(c, shown.c_str(), &e);
    if (maxWidth > 0 && e.xa > maxWidth) {
      // Ellipsize on a UTF-8 boundary.
      while (!shown.empty()) {
        size_t n = shown.size() - 1;
        while (n > 0 && (static_cast<unsigned char>(shown[n]) & 0xC0) == 0x80) --n;
        shown.erase(n);
        textExtents(c, (shown + "…").c_str(), &e);
        if (e.xa <= maxWidth) break;
      }
      shown += "…";
      textExtents(c, shown.c_str(), &e);
    }
    color(c, ink);
    moveTo(c, align < 0 ? x : align == 0 ? x - e.xa / 2 : x - e.xa, y);
    showText(c, shown.c_str());
  }
  double advance(P c, const std::string &value, double size, bool bold = false) {
    fontFace(c, "sans-serif", 0, bold ? 1 : 0);
    fontSize(c, size);
    Extents e{};
    textExtents(c, value.c_str(), &e);
    return e.xa;
  }
  void groove(P c, double x0, double x1, double y) {
    for (auto [dy, ink] : {std::pair{.5, 0xa296b3u}, {1.5, 0xcbc4d6u}, {2.5, 0xffffffu}}) {
      color(c, ink);
      rectangle(c, x0, y + dy - .5, x1 - x0, 1);
      fill(c);
    }
  }
  // An app icon from the companion's 128 px PNG; a lettered tile until it arrives.
  void drawIcon(P c, const std::string &path, const std::string &name, double x, double y, double size, double alpha,
                double now) {
    if (P image = iconSurface(path, now)) {
      save(c);
      translate(c, x, y);
      scale(c, size / 128, size / 128);
      sourceSurface(c, image, 0, 0);
      alpha < 1 ? paintAlpha(c, alpha) : paint(c);
      restore(c);
      return;
    }
    rounded(c, x + size * .06, y + size * .06, size * .88, size * .88, size * .2);
    color(c, 0xd9d3e3, alpha);
    fill(c);
    std::string letter = name.empty() ? "?" : name.substr(0, 1);
    if ((static_cast<unsigned char>(letter[0]) & 0x80) && name.size() > 1) {
      size_t n = 1;
      while (n < name.size() && (static_cast<unsigned char>(name[n]) & 0xC0) == 0x80) ++n;
      letter = name.substr(0, n);
    }
    text(c, letter, x + size / 2, y + size * .66, size * .45, 0x6e6182, true, 0);
  }
  void folderTile(P c, const LauncherPicture::Tile &t, double x, double y, double alpha, double now) {
    const double s = t.size, r = s * .23;
    rounded(c, x, y, s, s, r);
    color(c, 0xcbc4d6, .62 * alpha);
    fillPreserve(c);
    color(c, 0x8d82a0, .7 * alpha);
    lineWidth(c, .7);
    stroke(c);
    rounded(c, x + 1, y + 1, s - 2, s - 2, r - 1);
    color(c, 0xffffff, .55 * alpha);
    lineWidth(c, .6);
    stroke(c);
    const double pad = s * .1, gap = s * .05, mini = (s - 2 * pad - 2 * gap) / 3;
    for (size_t i = 0; i < t.mini.size() && i < 9; ++i)
      drawIcon(c, t.mini[i], "", x + pad + (i % 3) * (mini + gap), y + pad + (i / 3) * (mini + gap), mini, alpha, now);
  }
  void tile(P c, const LauncherPicture::Tile &t, double now) {
    const double s = t.size, x = t.cx - s / 2, y = t.top;
    if (t.hot || t.selected || t.pressed) {
      // The mockup's lit icon: a blue glow and a white ring (blue when chosen
      // from the keyboard, deeper while pressed).
      auto glow = radial(t.cx, y + s / 2, s * .2, t.cx, y + s / 2, s * .95);
      addStop(glow, 0, 0x1a9fff, t.pressed ? .55 : .4);
      addStop(glow, 1, 0x1a9fff, 0);
      rectangle(c, t.cx - s, y - s / 2, 2 * s, 2 * s);
      fillWith(c, glow);
      rounded(c, x - 2.2, y - 2.2, s + 4.4, s + 4.4, s * .23 + 2);
      color(c, t.pressed ? 0x5b8fc4 : t.selected && !t.hot ? 0x1a9fff : 0xffffff);
      lineWidth(c, 1.8);
      stroke(c);
    }
    const double alpha = t.ghost ? .28 : 1;
    if (t.folder) folderTile(c, t, x, y, alpha, now);
    else drawIcon(c, t.icon, t.name, x, y, s, alpha, now);
    text(c, t.name, t.cx, y + s + 13, s >= launcherIcon ? 10 : 9.5, t.ghost ? 0xb7aec5 : 0x3a2f47, false, 0, 62);
  }
  GLuint upload(P surface, int w, int h, GLuint texture) {
    flush(surface);
    if (!texture) glGenTextures(1, &texture);
    glBindTexture(GL_TEXTURE_2D, texture);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_BGRA, GL_UNSIGNED_BYTE, data(surface));
    return texture;
  }

public:
  LauncherPainter() {
    for (auto n : {"libcairo.so.2", "libcairo.2.dylib", "/opt/homebrew/lib/libcairo.2.dylib"})
      if ((lib = dlopen(n, RTLD_NOW | RTLD_LOCAL))) break;
    if (!lib) throw std::runtime_error("Cairo runtime unavailable for the launcher");
    load(imageSurface, "cairo_image_surface_create");
    load(fromPNG, "cairo_image_surface_create_from_png");
    load(create, "cairo_create");
    load(destroy, "cairo_destroy");
    load(surfaceDestroy, "cairo_surface_destroy");
    load(flush, "cairo_surface_flush");
    load(data, "cairo_image_surface_get_data");
    load(imageWidth, "cairo_image_surface_get_width");
    load(imageHeight, "cairo_image_surface_get_height");
    load(status, "cairo_surface_status");
    load(rgba, "cairo_set_source_rgba");
    load(paint, "cairo_paint");
    load(paintAlpha, "cairo_paint_with_alpha");
    load(moveTo, "cairo_move_to");
    load(lineTo, "cairo_line_to");
    load(arc, "cairo_arc");
    load(newPath, "cairo_new_path");
    load(newSubPath, "cairo_new_sub_path");
    load(closePath, "cairo_close_path");
    load(rectangle, "cairo_rectangle");
    load(fill, "cairo_fill");
    load(fillPreserve, "cairo_fill_preserve");
    load(stroke, "cairo_stroke");
    load(clip, "cairo_clip");
    load(lineWidth, "cairo_set_line_width");
    load(lineCap, "cairo_set_line_cap");
    load(op, "cairo_set_operator");
    load(fontFace, "cairo_select_font_face");
    load(fontSize, "cairo_set_font_size");
    load(showText, "cairo_show_text");
    load(textExtents, "cairo_text_extents");
    load(sourceSurface, "cairo_set_source_surface");
    load(source, "cairo_set_source");
    load(scale, "cairo_scale");
    load(translate, "cairo_translate");
    load(save, "cairo_save");
    load(restore, "cairo_restore");
    load(linear, "cairo_pattern_create_linear");
    load(radial, "cairo_pattern_create_radial");
    load(stop, "cairo_pattern_add_color_stop_rgba");
    load(patternDestroy, "cairo_pattern_destroy");
  }
  ~LauncherPainter() {
    for (auto &[path, icon] : icons)
      if (icon.surface) surfaceDestroy(icon.surface);
    if (lib) dlclose(lib);
  }
  LauncherPainter(const LauncherPainter &) = delete;
  LauncherPainter &operator=(const LauncherPainter &) = delete;

  // A decoded icon, reloaded when its file changes; missing or changed files
  // are rechecked once a second.
  P iconSurface(const std::string &path, double now) {
    if (path.empty()) return nullptr;
    auto &icon = icons[path];
    if (now - icon.checked >= 1000) {
      icon.checked = now;
      std::error_code error;
      auto stamp = std::filesystem::last_write_time(path, error);
      const std::string version = error ? "" : std::to_string(static_cast<long long>(stamp.time_since_epoch().count()));
      if (version != icon.stamp) {
        if (icon.surface) surfaceDestroy(icon.surface);
        icon.surface = nullptr;
        icon.stamp = version;
        if (!version.empty()) {
          P image = fromPNG(path.c_str());
          if (!status(image) && imageWidth(image) == 128 && imageHeight(image) == 128) icon.surface = image;
          else surfaceDestroy(image);
        }
      }
    }
    if (icons.size() > 1200) { // bounded: drop the cache, reload what is visible
      for (auto &[p, i] : icons)
        if (i.surface) surfaceDestroy(i.surface);
      icons.clear();
    }
    auto it = icons.find(path);
    return it == icons.end() ? nullptr : it->second.surface;
  }
  // A cheap signature of the visible icons' files: a changed one redraws.
  std::string iconStamp(const LauncherPicture &p, double now) {
    std::string out;
    for (const auto *row : {&p.recent, &p.grid})
      for (const auto &t : *row)
        for (const auto &path : t.folder ? t.mini : std::vector<std::string>{t.icon}) {
          iconSurface(path, now);
          auto it = icons.find(path);
          out += it != icons.end() && it->second.surface ? it->second.stamp.substr(0, 6) : "-";
        }
    return out;
  }

  GLuint panel(const LauncherPicture &p, int w, int h, GLuint texture, double now) {
    P surface = imageSurface(0, w, h);
    P c = create(surface);
    const double k = w / launcherUnits;
    scale(c, k, double(h) / launcherUnitsHigh);
    lineCap(c, 1);
    palladium(c, .5, .5, launcherUnits - 1, launcherUnitsHigh - 1, 10);

    // Search Applications, typed on the Mac keyboard.
    const double fx = launcherUnits / 2 - 145, fy = 16, fw = 290, fh = 32;
    well(c, fx, fy, fw, fh, 7);
    color(c, 0x3a2f47);
    lineWidth(c, 1.4);
    newPath(c);
    arc(c, fx + 17.3, fy + 15.3, 4.6, 0, 2 * M_PI);
    stroke(c);
    lineWidth(c, 1.7);
    moveTo(c, fx + 20.6, fy + 18.6);
    lineTo(c, fx + 24.2, fy + 22.2);
    stroke(c);
    const double tx = fx + 34, baseline = fy + 21;
    double caretX = tx;
    save(c);
    rectangle(c, fx + 30, fy, fw - 40, fh);
    clip(c);
    if (p.query.empty()) {
      text(c, "Search Applications", tx, baseline, 13, 0x958aa6);
      caretX = tx + advance(c, "Search Applications", 13) + 1;
    } else {
      // Long queries show their end, where the caret is.
      const double width = advance(c, p.query, 13);
      const double start = std::min(tx, fx + fw - 14 - width);
      text(c, p.query, start, baseline, 13, 0x3a2f47);
      caretX = start + width + 1;
    }
    restore(c);
    if (p.keyboard) {
      color(c, 0x1a9fff);
      rectangle(c, caretX, fy + 8.5, 1.6, 15);
      fill(c);
    }

    if (!p.recent.empty()) {
      text(c, "Recent Apps", 30, 70, 10.5, 0x6e6182, true);
      for (const auto &t : p.recent) tile(c, t, now);
    }
    groove(c, 22, launcherUnits - 22, 136);

    if (p.inFolder && !p.searching) {
      // A folder opens in place: a recessed area with its own title and Back.
      well(c, 18, 165, launcherUnits - 36, 176, 10);
      palladium(c, launcherBackX, launcherBackY, launcherBackW, launcherBackH, 6, p.backPressed, p.backHot);
      color(c, 0x5d5170);
      lineWidth(c, 1.5);
      moveTo(c, launcherBackX + 14, launcherBackY + 6);
      lineTo(c, launcherBackX + 10, launcherBackY + 10);
      lineTo(c, launcherBackX + 14, launcherBackY + 14);
      stroke(c);
      text(c, "Back", launcherBackX + 19, launcherBackY + 14, 10.5, 0x3a2f47, true);
      text(c, p.section, launcherBackX + launcherBackW + 12, 156, 11.5, 0x3a2f47, true, -1, 380);
    } else {
      text(c, p.section, 30, 158, 10.5, 0x6e6182, true, -1, 400);
    }
    for (const auto &t : p.grid) tile(c, t, now);
    if (p.empty) text(c, "No applications match.", launcherUnits / 2, 252, 12, 0x958aa6, false, 0);

    if (p.markerX >= 0) {
      auto glow = radial(p.markerX, p.markerTop + 18, 0, p.markerX, p.markerTop + 18, 16);
      addStop(glow, 0, 0x1a9fff, .45);
      addStop(glow, 1, 0x1a9fff, 0);
      rectangle(c, p.markerX - 16, p.markerTop - 4, 32, 46);
      fillWith(c, glow);
      rounded(c, p.markerX - 1.6, p.markerTop - 2, 3.2, 40, 1.6);
      color(c, 0x1a9fff);
      fill(c);
    }
    if (p.pages > 1)
      for (int i = 0; i < p.pages; ++i) {
        const double x = launcherDotX(i, p.pages);
        if (i == p.page) {
          auto glow = radial(x, launcherDotsY, 0, x, launcherDotsY, 6);
          addStop(glow, 0, 0x1a9fff, .6);
          addStop(glow, 1, 0x1a9fff, 0);
          rectangle(c, x - 6, launcherDotsY - 6, 12, 12);
          fillWith(c, glow);
        }
        newPath(c);
        arc(c, x, launcherDotsY, 3.1, 0, 2 * M_PI);
        color(c, i == p.page ? 0x1a9fff : 0xa296b3);
        fill(c);
        if (i != p.page) {
          newPath(c);
          arc(c, x - .4, launcherDotsY - .4, 2.4, M_PI, 1.5 * M_PI);
          color(c, 0x8d82a0);
          lineWidth(c, .8);
          stroke(c);
        }
        if (i == p.hotDot) {
          newPath(c);
          arc(c, x, launcherDotsY, 5.4, 0, 2 * M_PI);
          color(c, 0x1a9fff, .9);
          lineWidth(c, 1.2);
          stroke(c);
        }
      }
    destroy(c);
    texture = upload(surface, w, h, texture);
    surfaceDestroy(surface);
    return texture;
  }

  // The shelf's Applications button: a Palladium key with the grid glyph,
  // pressed while the launcher is open. Sized like a Dock tile (128 x 140).
  GLuint button(bool lit, int w, int h, GLuint texture) {
    P surface = imageSurface(0, w, h);
    P c = create(surface);
    const double s = std::min(w * .76, h * .72), x = (w - s) / 2, y = 5;
    palladium(c, x, y, s, s, s * .23, lit, false);
    const double g = s * .6, gx = x + (s - g) / 2, gy = y + (s - g) / 2, unit = g / 24;
    color(c, lit ? 0x2f6fb8 : 0x5d5170);
    for (int r = 0; r < 3; ++r)
      for (int q = 0; q < 3; ++q) {
        rounded(c, gx + (4 + q * 6) * unit, gy + (4 + r * 6) * unit, 4 * unit, 4 * unit, unit);
        fill(c);
      }
    destroy(c);
    texture = upload(surface, w, h, texture);
    surfaceDestroy(surface);
    return texture;
  }

  // Dark, premultiplied, feathered at every edge: dims what is behind it.
  GLuint backdrop(int w, int h, double alpha, GLuint texture) {
    P surface = imageSurface(0, w, h);
    P c = create(surface);
    auto across = linear(0, 0, w, 0);
    addStop(across, 0, 0, 0);
    addStop(across, .1, 0, alpha);
    addStop(across, .9, 0, alpha);
    addStop(across, 1, 0, 0);
    rectangle(c, 0, 0, w, h);
    fillWith(c, across);
    auto down = linear(0, 0, 0, h);
    addStop(down, 0, 0, 0);
    addStop(down, .22, 0, 1);
    addStop(down, .78, 0, 1);
    addStop(down, 1, 0, 0);
    op(c, 8 /* CAIRO_OPERATOR_DEST_IN */);
    rectangle(c, 0, 0, w, h);
    fillWith(c, down);
    destroy(c);
    texture = upload(surface, w, h, texture);
    surfaceDestroy(surface);
    return texture;
  }
};

} // namespace fw
