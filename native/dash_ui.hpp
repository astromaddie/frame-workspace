#pragma once
// The "Frame Workspace" SteamVR dashboard panel (FW-08): one bar entry for all
// virtual screens. Start or stop the Workspace, recenter it, and close any
// floating window. Drawn with cairo/Pango; the program owning the dashboard
// overlay supplies the state and hit-tests the returned buttons.
#include <cairo.h>
#include <pango/pangocairo.h>
#include <algorithm>
#include <cmath>
#include <string>
#include <vector>

namespace fw {

struct DashWindow {
  std::string title, key; // key: the panel's OpenVR key, for Close
};
struct DashState {
  bool macConnected = false;
  bool workspaceRunning = false, workspaceBusy = false;
  std::string busyText = "Starting…";  // shown while busy
  bool locked = false;  // a press is being carried out: every button waits
  std::string workspaceLabel = "9:8 · 2880 × 2560";
  std::vector<DashWindow> windows;
};
struct DashButton {
  double x = 0, y = 0, w = 0, h = 0;
  std::string action, key; // workspace_start, workspace_stop, recenter, close
  bool enabled = true;
  bool contains(double px, double py) const { return px >= x && px <= x + w && py >= y && py <= y + h; }
};

// Variant "list": one column; "tiles": windows as a grid of cards.
class DashRenderer {
public:
  static constexpr int width = 1280;
  int height = 800; // fitted to the content by measure()

  // The panel height that fits this state with no empty space below.
  int measure(const DashState &s, const std::string &variant = "list") const {
    double bottom = s.windows.empty() ? 336 + 26
                  : variant == "tiles" ? 334 + double((std::min<size_t>(s.windows.size(), 9) + 2) / 3) * 150 - 18
                                       : 332 + double(std::min<size_t>(s.windows.size(), 6)) * 74 - 12 +
                                             (s.windows.size() > 6 ? 34 : 0);
    if (!s.macConnected) bottom += 50;
    return int(bottom + 36);
  }

  std::vector<DashButton> draw(cairo_t *cr, const DashState &s, const std::string &hover,
                               const std::string &variant = "list") {
    buttons.clear();
    c = cr;
    background();
    text("FRAME WORKSPACE", 48, 40, 15, 700, muted, 2.6);
    chip(s.macConnected ? "Mac Connected" : "Mac Not Connected", s.macConnected);
    // Workspace card
    card(32, 92, width - 64, 176);
    text("Workspace", 64, 120, 26, 600, bright);
    text(s.workspaceBusy ? s.busyText : s.workspaceRunning ? "Running · " + s.workspaceLabel : "Stopped",
         64, 160, 17, 400, s.workspaceRunning ? green : muted);
    const bool can = s.macConnected && !s.workspaceBusy && !s.locked;
    if (s.workspaceRunning) {
      button(64, 198, 240, 50, "Stop Workspace", "workspace_stop", "", hover, false, can);
      button(320, 198, 180, 50, "Recenter", "recenter", "", hover, false, can);
    } else {
      button(64, 198, 260, 50, "Start Workspace", "workspace_start", "", hover, true, can);
    }
    // Windows
    text("WINDOWS", 48, 304, 14, 700, muted, 2.4);
    if (s.windows.empty()) {
      text("No floating windows are open. Show a window from the Mac to add one here.", 48, 336, 17, 400, dim);
    } else if (variant == "tiles") {
      const int cols = 3, gap = 18, tw = (width - 64 - gap * (cols - 1)) / cols, th = 132;
      for (size_t i = 0; i < s.windows.size() && i < 9; i++) {
        const double x = 32 + double(i % cols) * (tw + gap), y = 334 + double(i / cols) * (th + gap);
        card(x, y, tw, th);
        text(ellipsize(s.windows[i].title, 30), x + 22, y + 22, 18, 500, bright);
        button(x + 22, y + th - 66, 130, 46, "Close", "close", s.windows[i].key, hover, false, s.macConnected && !s.locked);
      }
    } else {
      for (size_t i = 0; i < s.windows.size() && i < 6; i++) {
        const double y = 332 + double(i) * 74;
        card(32, y, width - 64, 62);
        text(ellipsize(s.windows[i].title, 70), 64, y + 20, 19, 500, bright);
        button(width - 64 - 150, y + 9, 150, 44, "Close", "close", s.windows[i].key, hover, false, s.macConnected && !s.locked);
      }
      if (s.windows.size() > 6)
        text("+" + std::to_string(s.windows.size() - 6) + " more", 64, 332 + 6 * 74 + 6, 16, 400, dim);
    }
    if (!s.macConnected)
      text("Open Frame Control on the Mac to start the Workspace or show windows.", 48, height - 36 - 30, 16, 400, warn);
    return buttons;
  }

private:
  cairo_t *c = nullptr;
  std::vector<DashButton> buttons;
  struct Rgb { double r, g, b; };
  // Steam client palette, as in the Mac control page.
  const Rgb bg{0.106, 0.157, 0.220}, deep{0.055, 0.078, 0.106}, bright{1, 1, 1}, text_{0.863, 0.871, 0.875},
      muted{0.561, 0.596, 0.627}, dim{0.369, 0.408, 0.451}, blue{0.102, 0.624, 1}, green{0.349, 0.749, 0.251},
      warn{0.851, 0.635, 0.227};

  void rounded(double x, double y, double w, double h, double r) {
    cairo_new_sub_path(c);
    cairo_arc(c, x + w - r, y + r, r, -M_PI / 2, 0);
    cairo_arc(c, x + w - r, y + h - r, r, 0, M_PI / 2);
    cairo_arc(c, x + r, y + h - r, r, M_PI / 2, M_PI);
    cairo_arc(c, x + r, y + r, r, M_PI, 3 * M_PI / 2);
    cairo_close_path(c);
  }
  void background() {
    cairo_pattern_t *g = cairo_pattern_create_linear(0, 0, 0, height);
    cairo_pattern_add_color_stop_rgb(g, 0, bg.r, bg.g, bg.b);
    cairo_pattern_add_color_stop_rgb(g, 1, deep.r, deep.g, deep.b);
    rounded(0, 0, width, height, 28);
    cairo_set_source(c, g);
    cairo_fill(c);
    cairo_pattern_destroy(g);
  }
  void card(double x, double y, double w, double h) {
    rounded(x, y, w, h, 14);
    cairo_set_source_rgba(c, 1, 1, 1, 0.045);
    cairo_fill_preserve(c);
    cairo_set_source_rgba(c, 1, 1, 1, 0.07);
    cairo_set_line_width(c, 1.5);
    cairo_stroke(c);
  }
  void text(const std::string &t, double x, double y, double size, int weight, const Rgb &rgb, double tracking = 0) {
    PangoLayout *layout = pango_cairo_create_layout(c);
    PangoFontDescription *font = pango_font_description_new();
    pango_font_description_set_family(font, "Noto Sans, Helvetica Neue, sans-serif");
    pango_font_description_set_absolute_size(font, size * PANGO_SCALE);
    pango_font_description_set_weight(font, PangoWeight(weight));
    pango_layout_set_font_description(layout, font);
    if (tracking > 0) {
      PangoAttrList *attrs = pango_attr_list_new();
      pango_attr_list_insert(attrs, pango_attr_letter_spacing_new(int(tracking * PANGO_SCALE)));
      pango_layout_set_attributes(layout, attrs);
      pango_attr_list_unref(attrs);
    }
    pango_layout_set_text(layout, t.c_str(), -1);
    cairo_set_source_rgb(c, rgb.r, rgb.g, rgb.b);
    cairo_move_to(c, x, y);
    pango_cairo_show_layout(c, layout);
    pango_font_description_free(font);
    g_object_unref(layout);
  }
  void chip(const std::string &t, bool good) {
    const double w = 230, h = 40, x = width - 48 - w, y = 30;
    rounded(x, y, w, h, h / 2);
    cairo_set_source_rgba(c, good ? 0.349 : 0.851, good ? 0.749 : 0.635, good ? 0.251 : 0.227, 0.16);
    cairo_fill(c);
    cairo_arc(c, x + 24, y + h / 2, 6, 0, 2 * M_PI);
    const Rgb &dot = good ? green : warn;
    cairo_set_source_rgb(c, dot.r, dot.g, dot.b);
    cairo_fill(c);
    text(t, x + 42, y + 9, 16, 500, text_);
  }
  void button(double x, double y, double w, double h, const std::string &label, const std::string &action,
              const std::string &key, const std::string &hover, bool primary, bool enabled) {
    const bool hot = enabled && hover == action + ":" + key;
    rounded(x, y, w, h, 8);
    if (primary && enabled) {
      cairo_pattern_t *g = cairo_pattern_create_linear(x, 0, x + w, 0);
      cairo_pattern_add_color_stop_rgb(g, 0, hot ? 0.373 : 0.278, hot ? 0.792 : 0.749, 1);
      cairo_pattern_add_color_stop_rgb(g, 1, hot ? 0.141 : 0.102, hot ? 0.345 : 0.267, hot ? 0.839 : 0.761);
      cairo_set_source(c, g);
      cairo_fill(c);
      cairo_pattern_destroy(g);
    } else {
      cairo_set_source_rgba(c, 0.404, 0.439, 0.482, !enabled ? 0.1 : hot ? 0.5 : 0.24);
      cairo_fill(c);
    }
    if (hot) { // focus ring, matching the page's blue focus outline
      rounded(x - 3, y - 3, w + 6, h + 6, 10);
      cairo_set_source_rgba(c, blue.r, blue.g, blue.b, 0.9);
      cairo_set_line_width(c, 2.5);
      cairo_stroke(c);
    }
    PangoLayout *layout = pango_cairo_create_layout(c);
    PangoFontDescription *font = pango_font_description_from_string("Noto Sans, Helvetica Neue, sans-serif");
    pango_font_description_set_absolute_size(font, 19 * PANGO_SCALE);
    pango_font_description_set_weight(font, primary ? PANGO_WEIGHT_SEMIBOLD : PANGO_WEIGHT_MEDIUM);
    pango_layout_set_font_description(layout, font);
    pango_layout_set_text(layout, label.c_str(), -1);
    int tw = 0, th = 0;
    pango_layout_get_pixel_size(layout, &tw, &th);
    const Rgb &fg = !enabled ? dim : primary ? bright : text_;
    cairo_set_source_rgb(c, fg.r, fg.g, fg.b);
    cairo_move_to(c, x + (w - tw) / 2, y + (h - th) / 2);
    pango_cairo_show_layout(c, layout);
    pango_font_description_free(font);
    g_object_unref(layout);
    buttons.push_back({x, y, w, h, action, key, enabled});
  }
  static std::string ellipsize(const std::string &t, size_t n) {
    if (t.size() <= n) return t;
    size_t cut = n - 1;
    while (cut > 0 && (static_cast<unsigned char>(t[cut]) & 0xC0) == 0x80) --cut; // keep UTF-8 whole
    return t.substr(0, cut) + "…";
  }
};

} // namespace fw
