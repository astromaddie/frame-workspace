#pragma once
#include "chrome.hpp"
#include "host.hpp"
#include "launcher.hpp"
#include "shelf.hpp"
#include <functional>
namespace fw {
inline HmdMatrix34_t spatialIdentity() {
  return {{{1, 0, 0, 0}, {0, 1, 0, 0}, {0, 0, 1, 0}}};
}
struct SpatialWindow {
  std::string key, title, app;
};
struct SpatialApp {
  std::string id, name, icon;
  bool running = false, hidden = false;
};
struct SpatialState {
  bool spatial = false, open = false;
  int index = 0;
  int64_t seq = 0;
  // Counts Recenter requests made from the dashboard in Spatial Mode.
  int64_t recenterSeq = 0;
  std::string chosen;
  std::vector<SpatialApp> apps;
  std::vector<SpatialWindow> windows;
  // Launcher A: present when the companion has a catalogue.
  LauncherState launcher;
  static SpatialState parse(const std::string &raw) {
    auto j = json(raw);
    if (!j || !json_object_is_type(j.get(), json_type_object))
      throw std::runtime_error("spatial state object required");
    SpatialState s;
    s.spatial = str(j, "mode") == "spatial";
    {
      auto r = field(j, "recenterSeq");
      if (r && json_object_is_type(r, json_type_int)) s.recenterSeq = std::max<int64_t>(0, json_object_get_int64(r));
    }
    auto a = field(j, "apps");
    if (a && json_object_is_type(a, json_type_array))
      for (size_t i = 0; i < json_object_array_length(a) && i < 256; ++i) {
        auto v = json_object_array_get_idx(a, i);
        Json item(json_object_get(v), json_object_put);
        SpatialApp app{str(item, "bundleID"), str(item, "name"),
                       str(item, "icon"), flag(item, "running"),
                       flag(item, "hidden")};
        if (app.id.empty())
          continue;
        if (app.icon.find('/') != std::string::npos ||
            app.icon.find("..") != std::string::npos)
          app.icon.clear();
        s.apps.push_back(app);
        auto w = field(item, "windows");
        if (w && json_object_is_type(w, json_type_array))
          for (size_t k = 0;
               k < json_object_array_length(w) && s.windows.size() < 512; ++k) {
            Json win(json_object_get(json_object_array_get_idx(w, k)),
                     json_object_put);
            auto key = str(win, "key");
            if (validOverlayKey(key))
              s.windows.push_back({key, str(win, "title"), app.name});
          }
      }
    auto sw = field(j, "switcher");
    if (sw && json_object_is_type(sw, json_type_object)) {
      Json v(json_object_get(sw), json_object_put);
      s.open = flag(v, "open");
      s.index = int(num(v, "index"));
      s.seq = json_object_get_int64(field(v, "seq"));
      s.chosen = str(v, "chosen");
    }
    if (s.index < 0 || s.index >= int(s.windows.size()))
      s.index = 0;
    s.launcher = LauncherState::parse(field(j, "launcher"));
    return s;
  }
};
// A local gesture stays authoritative until the companion acknowledges it or
// begins a newer keyboard switcher sequence. Unrelated app-state updates must
// not reopen a strip which the user just dismissed.
struct SpatialVisibility {
  int desired = -1;
  int64_t seq = 0;
  void request(bool open, int64_t sequence) {
    desired = open ? 1 : 0;
    seq = sequence;
  }
  void reconcile(SpatialState &state) {
    if (desired < 0)
      return;
    if (state.seq != seq || state.open == bool(desired)) {
      desired = -1;
      return;
    }
    state.open = bool(desired);
  }
};
struct SpatialNavigation {
  bool previous[2]{};
  int local = -1;
  int64_t seq = 0;
  void reconcile(SpatialState &state) {
    if (local < 0)
      return;
    if (state.seq != seq || !state.open || state.windows.empty()) {
      local = -1;
      return;
    }
    state.index = local = std::clamp(local, 0, int(state.windows.size()) - 1);
  }
  void update(SpatialState &state, const bool (&down)[2]) {
    if (state.spatial && state.open && !state.windows.empty()) {
      int delta =
          (down[1] && !previous[1] ? 1 : 0) - (down[0] && !previous[0] ? 1 : 0);
      if (delta) {
        int count = int(state.windows.size());
        state.index = (state.index + delta + count) % count;
        local = state.index;
        seq = state.seq;
      }
    }
    for (int i = 0; i < 2; ++i)
      previous[i] = down[i];
  }
};
inline int spatialAppIndex(const SpatialState &state, const std::string &id) {
  for (size_t i = 0; i < state.apps.size(); ++i)
    if (state.apps[i].id == id)
      return int(i);
  return -1;
}
// Match the selected panel's foreground render band, including when another
// panel is physically nearer. Outside it, ordinary nearest-surface picking wins.
inline PanelPick spatialPanelPick(const EyeRay &ray,
                                  const std::vector<PanelSurface> &surfaces,
                                  const std::string &active) {
  for (const auto &s : surfaces)
    if (focusSurface(s) && s.key == active) {
      auto hit = surfacePick(ray, s, .02f);
      if (hit) return hit;
    }
  std::vector<PanelSurface> windows;
  for (const auto &s : surfaces) if (focusSurface(s)) windows.push_back(s);
  return pickPanel(ray, windows, active);
}
// The gaze highlight the helper is told about (`gaze-highlight`, v1): sent on
// the looked-at window's stream when it lights and goes out, and refreshed
// while it lasts; the helper forgets one not refreshed within inputExpiryMs.
struct GazeHighlight { static constexpr double refreshMs = 500; };
// Typing on the Mac's own keyboard, as the helper reports it: a long stare
// never moves focus away from the window being typed into.
struct TypingWatch {
  static constexpr double guardMs = 1000;  // after the last physical key
  double last = -1e12;
  void typed(double now) { last = now; }
  bool recent(double now) const { return now - last < guardMs; }
};
// What holds a long stare back from committing focus. While any of these is
// true the stare does not count; once they all clear it starts from zero.
struct StareGuards {
  bool eyes = true;          // only the eyes stare; a resting laser (the gaze fallback) never commits
  bool typing = false;       // a physical key in the last TypingWatch::guardMs
  bool physicalHeld = false; // a trackpad or mouse button is down
  bool controllerHeld = false, pointerOwned = false;
  bool menuOpen = false, launcherOpen = false, switcherOpen = false;
  bool placing = false;      // a window or the shelf is being moved
  bool allow() const {
    return eyes && !typing && !physicalHeld && !controllerHeld && !pointerOwned && !menuOpen && !launcherOpen &&
        !switcherOpen && !placing;
  }
};
// Eyes highlight, an action commits (FW-15). The looked-at window is
// highlighted after FocusFeedback::dwellSeconds of uninterrupted look; it
// becomes active only through the host's activate() (controller, trackpad
// click, switcher), or when update() reports a long stare: `commitSeconds` of
// uninterrupted, unguarded look. Desktop Mode passes dwellSeconds there and
// keeps the original quick gaze focus.
struct HostFocus {
  std::map<std::string, FocusFeedback> feedback;
  std::string active, pending, source, highlighted;
  float elapsed = 0, stare = 0;
  bool committed = false;  // this stare's commit went out
  bool update(const PanelPick &hit, float dt, const std::string &input,
              float commitSeconds = FocusFeedback::stareCommitSeconds, bool commitAllowed = true) {
    if (!hit || hit.key != pending || input != source) {
      pending = hit.key;
      source = input;
      elapsed = stare = 0;
      committed = false;
    }
    dt = std::clamp(dt, 0.f, .05f);
    const bool other = hit && hit.key != active;
    if (hit) elapsed += dt;
    if (other && commitAllowed) stare += dt;
    else { stare = 0; if (!commitAllowed) committed = false; }
    highlighted = other && elapsed >= FocusFeedback::dwellSeconds ? hit.key : std::string();
    bool commit = false;
    if (other && commitAllowed && !committed && stare >= commitSeconds) commit = committed = true;
    for (auto &[key, f] : feedback) {
      const bool looking = other && hit.key == key;
      f.update(dt, looking, looking ? stare * FocusFeedback::stareCommitSeconds / std::max(commitSeconds, 1e-3f) : 0,
               looking && committed);
    }
    return commit;
  }
  void activate(const std::string &key) {
    for (auto &[k, f] : feedback)
      if (k != key)
        f.lose();
    auto &f = feedback[key];
    f.focused = true;
    f.glow = 0;
    f.snap = 0;
    f.progress = 0;
    active = key;
    if (highlighted == key) highlighted.clear();
  }
  void remove(const std::string &key) {
    feedback.erase(key);
    if (active == key)
      active.clear();
    if (highlighted == key) highlighted.clear();
    if (pending == key) {
      pending.clear();
      elapsed = stare = 0;
      committed = false;
    }
  }
};
struct SpatialTile {
  // Apps: the Applications button at the shelf's left end. Launcher: the
  // launcher panel; Dim: the backdrop behind it; Carry: an icon being moved.
  enum Kind { Dock, Window, Menu, Shelf, Move, Apps, Launcher, Dim, Carry } kind = Dock;
  int index = 0;
  PanelSurface surface;
  std::string label;
  SpatialTile() = default;
  SpatialTile(Kind k, int i, PanelSurface s, std::string l)
      : kind(k), index(i), surface(std::move(s)), label(std::move(l)) {}
  // Shelf: layout is the full-size shelf. Move: the pill's virtual window and
  // what it shows; it is not pickable, because its texture is mostly
  // transparent, so the shelf mover tests the pill itself.
  bool pickable = true, shown = true;
  HandleLayout layout;
  ChromeState chrome;
  // Shelf: the largest the backing can be drawn (full size, icons magnified to
  // the limit), in metres. The renderer keeps its texture this size, so the
  // animation never changes the texture's dimensions.
  float canvasWidth = 0, canvasHeight = 0;
  bool lit = false;                                 // Apps: the launcher is open
  std::shared_ptr<const LauncherPicture> picture;   // Launcher
  std::string icon;                                 // Carry: the icon's file
};
// How the shelf backing is rasterised. The texture handed to the overlay is
// the fixed canvas (the shelf's largest size, which changes only when its
// content does: the number of apps, the status band). The backing drawn at its
// current size, in whole 8 px steps so a still shelf does not redraw, fills the
// canvas's top-left corner and the overlay shows just that part through its
// texture bounds (0..u across, 0..v down in texture rows). Texels per metre
// stay shelfPixelsPerMetre and SteamVR sees one texture size throughout.
struct ShelfRaster {
  int canvasW = 0, canvasH = 0; // texture size
  int w = 0, h = 0;             // the drawn part
  float u = 1, v = 1;           // the drawn part as fractions of the canvas
};
inline ShelfRaster shelfRaster(const SpatialTile &t) {
  auto eighths = [](float metres, bool up) {
    const float px = metres * shelfPixelsPerMetre / 8;
    return std::max(16, int(up ? std::ceil(px) : std::lround(px)) * 8);
  };
  ShelfRaster r;
  r.canvasW = std::min(2048, eighths(std::max(t.canvasWidth, t.surface.placement.width), true));
  r.canvasH = std::min(2048, eighths(std::max(t.canvasHeight, t.surface.height), true));
  r.w = std::min(r.canvasW, eighths(t.surface.placement.width, false));
  r.h = std::min(r.canvasH, eighths(t.surface.height, false));
  r.u = float(r.w) / r.canvasW;
  r.v = float(r.h) / r.canvasH;
  return r;
}
// Overlay sort bands: windows 1-4 (51-54 selected), the launcher's backdrop
// above them, the shelf, the shelf's icons and cards, the launcher panel and
// the icon carried across it.
inline uint32_t spatialSortOrder(SpatialTile::Kind kind) {
  switch (kind) {
  case SpatialTile::Dim: return 90;
  case SpatialTile::Shelf: return 99;
  case SpatialTile::Launcher: return 101;
  case SpatialTile::Carry: return 102;
  default: return 100;
  }
}
inline HmdMatrix34_t spatialAnchor(const HmdMatrix34_t &head, float distance,
                                   float down) {
  float x = head.m[0][2], z = head.m[2][2], n = std::hypot(x, z);
  if (n < .01f) {
    x = 0;
    z = 1;
  } else {
    x /= n;
    z /= n;
  }
  return {{{z, 0, x, head.m[0][3] - x * distance},
           {0, 1, 0, head.m[1][3] - down},
           {-x, 0, z, head.m[2][3] - z * distance}}};
}
inline constexpr float shelfStatusBand = .052f, shelfInset = .006f;
// The dock shelf, then its icons, the app menu, the switcher cards and last the
// move pill. The shelf and its icons hang from `view.pose` once pinned (else
// from `anchor`, as before) and are drawn view.scale times full size about the
// middle of the icon row. Icons near view.pointer are enlarged while the row
// stretches around them, so each tile here is where the overlay is drawn.
inline std::vector<SpatialTile> spatialLayout(const SpatialState &s,
                                              const HmdMatrix34_t &anchor,
                                              const std::string &menuID = "",
                                              const SurfaceMessage *status = nullptr,
                                              const ShelfView &view = {}) {
  const int menu = spatialAppIndex(s, menuID);
  std::vector<SpatialTile> tiles;
  if (!s.spatial)
    return tiles;
  const HmdMatrix34_t &base = view.pinned ? view.pose : anchor;
  const float k = std::clamp(view.scale, .05f, 1.5f);
  // The Applications button takes the row's first slot when the launcher exists.
  const size_t lead = s.launcher.available ? 1 : 0, slots = s.apps.size() + lead;
  // Scale the whole ordered shelf to fit 1.35 m; no app silently disappears.
  const float step0 = std::min(.105f, 1.35f / std::max(size_t(1), slots));
  const float step = step0 * k, n = float(slots);
  const float reach = shelfIconReach * step, boost = shelfIconBoost * std::clamp(view.magnify, 0.f, 1.f);
  auto stretch = [&](float u) { return shelfMagnifiedX(u, view.pointer, boost, reach); };
  auto place = [&](float x, float y, float z) {
    auto t = spatialIdentity();
    t.m[0][3] = x;
    t.m[1][3] = y;
    t.m[2][3] = z;
    return multiply(base, t);
  };
  // Icon i spans [stretch(a), stretch(b)] along the row; bottoms stay level.
  const float rowBottom = -shelfRowDrop - shelfIconHeight * k / 2;
  float menuX = 0, menuTop = 0, shelfX = 0, shelfY = 0;
  if (slots) {
    const float fullWidth = step0 * n + .022f;
    Placement p;
    const float scale = status && status->rect.w > 0 ? std::min(.001f, (fullWidth - 2*shelfInset) / status->rect.w) : .001f;
    const float band = status ? std::max(shelfStatusBand, status->rect.h * scale + 2*shelfInset) : shelfStatusBand;
    const float left = stretch(-n * step / 2), right = stretch(n * step / 2);
    p.width = right - left + .022f * k;
    shelfX = (left + right) / 2;
    shelfY = -shelfRowDrop - band * k / 2;
    p.matrix = place(shelfX, shelfY, -.002f);
    SpatialTile shelf{SpatialTile::Shelf, 0, {"shelf", p, (shelfIconHeight + band) * k, PanelRole::Menu}, ""};
    shelf.layout.width = fullWidth;
    shelf.layout.height = shelfIconHeight + band;
    // Magnifying stretches the row by at most boost * reach (the bell's area).
    const float most = std::max(1.f, k);
    shelf.canvasWidth = most * (fullWidth + shelfIconBoost * shelfIconReach * step0);
    shelf.canvasHeight = most * (shelfIconHeight + band);
    tiles.push_back(shelf);
  }
  for (size_t i = 0; i < s.apps.size(); ++i) {
    Placement p;
    const float a = (float(i + lead) - n / 2) * step, xa = stretch(a), xb = stretch(a + step);
    p.width = xb - xa;
    const float height = shelfIconHeight * k * p.width / step;
    p.matrix = place((xa + xb) / 2, rowBottom + height / 2, 0);
    if (int(i) == menu) {
      menuX = (xa + xb) / 2;
      menuTop = rowBottom + height;
    }
    tiles.push_back({SpatialTile::Dock,
                     int(i),
                     {"dock:" + s.apps[i].id, p, height, PanelRole::Menu},
                     s.apps[i].name});
  }
  if (lead) {
    // After the app icons in the list (their tile indices stay put), drawn at
    // the left end and magnified like them.
    Placement p;
    const float a = -n / 2 * step, xa = stretch(a), xb = stretch(a + step);
    p.width = xb - xa;
    const float height = shelfIconHeight * k * p.width / step;
    p.matrix = place((xa + xb) / 2, rowBottom + height / 2, 0);
    SpatialTile apps{SpatialTile::Apps, 0, {"apps", p, height, PanelRole::Menu}, "Applications"};
    apps.lit = s.launcher.open;
    tiles.push_back(apps);
  }
  if (s.open)
    for (size_t i = 0; i < s.windows.size(); ++i) {
      // A moving viewport preserves order and a usable thumbnail size for large
      // lists.
      int first =
          std::clamp(s.index - 3, 0, std::max(0, int(s.windows.size()) - 7));
      if (int(i) < first || int(i) >= first + 7)
        continue;
      int count = std::min(7, int(s.windows.size()));
      float angle = (int(i) - first - (count - 1) * .5f) * .25f;
      Placement p;
      p.width = .29f;
      auto t = spatialIdentity();
      float c = std::cos(angle), sn = std::sin(angle);
      t.m[0][0] = c;
      t.m[0][2] = -sn;
      t.m[2][0] = sn;
      t.m[2][2] = c;
      t.m[0][3] = 1.3f * sn;
      t.m[2][3] = 1.3f * (1 - c);
      t.m[1][3] = .02f;
      p.matrix = multiply(anchor, t);
      tiles.push_back({SpatialTile::Window,
                       int(i),
                       {"window:" + s.windows[i].key, p, .215f, PanelRole::Menu},
                       s.windows[i].app});
    }
  if (menu >= 0 && menu < int(s.apps.size()))
    for (int i = 0; i < 3; ++i) {
      Placement p;
      p.width = .25f * k;
      // Resting on the enlarged icon, first item at the top.
      p.matrix = place(menuX, menuTop + (.01f + .0225f + (2 - i) * .045f) * k, .025f);
      tiles.push_back({SpatialTile::Menu,
                       i,
                       {"menu:" + menuID + ":" + std::to_string(i), p, .045f * k, PanelRole::Menu},
                       i == 0   ? "Show All Windows"
                       : i == 1 ? (s.apps[menu].hidden ? "Unhide" : "Hide")
                                : "Quit"});
    }
  if (slots && view.pinned) {
    // The window move pill, hung from a virtual window the size of the full
    // shelf and shrunk with it; centred under the shelf as drawn.
    SpatialTile move{SpatialTile::Move, 0, {"shelf:move", {}, 0, PanelRole::Menu}, ""};
    move.layout = tiles.front().layout;
    move.chrome = view.chrome;
    move.pickable = false;
    move.shown = view.handle;
    const auto &l = move.layout;
    move.surface.placement.width = l.chromeWidth() * k;
    move.surface.height = l.chromeHeight() * k;
    move.surface.placement.matrix = place(shelfX - l.pillX() * k, shelfY, .004f);
    tiles.push_back(move);
  }
  return tiles;
}
// The launcher's tiles, appended after the shelf's: the dimming backdrop (not
// pickable), the curved panel, and the icon being carried, which follows the
// laser a little in front of the panel.
inline PanelSurface launcherSurface(const LauncherView &v) {
  PanelSurface s{"launcher", {}, launcherHeight, PanelRole::Menu};
  s.placement.width = launcherWidth;
  s.placement.matrix = v.pose;
  s.placement.bendA = s.placement.bendB = launcherCurvature();
  return s;
}
inline void launcherTiles(std::vector<SpatialTile> &tiles, const LauncherView &v,
                          std::shared_ptr<const LauncherPicture> picture, const std::string &carryIcon = "") {
  if (!v.open) return;
  PanelSurface dim{"launcher:dim", {}, 2.6f, PanelRole::Menu};
  const float wrap = .72f; // of a full circle round the head
  dim.placement.width = 2 * float(M_PI) * launcherDimDistance * wrap;
  dim.placement.matrix = v.dimPose;
  dim.placement.bendA = dim.placement.bendB = wrap;
  SpatialTile backdrop{SpatialTile::Dim, 0, dim, ""};
  backdrop.pickable = false;
  tiles.push_back(backdrop);
  SpatialTile panel{SpatialTile::Launcher, 0, launcherSurface(v), ""};
  panel.picture = std::move(picture);
  tiles.push_back(panel);
  if (v.carry.active() && v.carry.onPanel) {
    const auto surface = launcherSurface(v);
    const float x = (v.carry.x / launcherUnits - .5f) * launcherWidth;
    const float y = (.5f - v.carry.y / launcherUnitsHigh) * launcherHeight;
    const float size = launcherIcon / launcherUnits * launcherWidth * 1.2f;
    PanelSurface icon{"launcher:carry", {}, size * 140 / 128, PanelRole::Menu};
    icon.placement.width = size;
    icon.placement.matrix = surfacePoint(surface, x, y, .03f);
    SpatialTile carried{SpatialTile::Carry, 0, icon, ""};
    carried.pickable = false;
    carried.icon = carryIcon;
    tiles.push_back(carried);
  }
}
// Metres per Mac point on the focused window, for its menu strip and menus.
// The helper's input geometry gives the window's logical width (and how much
// of the picture is that window), which holds when capture is capped; a
// helper without it leaves the old estimate of an uncapped 2x picture.
inline float windowMetresPerPoint(float panelWidth, int decodedWidth, float logicalWidth = 0,
                                  float contentFraction = 1) {
  if (std::isfinite(logicalWidth) && logicalWidth > 0 && std::isfinite(contentFraction) && contentFraction > 0 &&
      contentFraction <= 1 && std::isfinite(panelWidth) && panelWidth > 0)
    return panelWidth * contentFraction / logicalWidth;
  return decodedWidth > 0 ? panelWidth * 2 / decodedWidth : .001f;
}
// Auxiliary poses and their window-layer regions are host-owned; UV stays top-left.
inline PanelSurface menuStripPlacement(const PanelSurface &window, const SurfaceMessage &m,
                                        float metresPerPoint, float wide) {
  PanelSurface s{"menubar", {}, 0, PanelRole::Menubar};
  wide = std::clamp(wide, 0.f, 1.f);
  HandleLayout frame; frame.width = window.placement.width; frame.height = window.height;
  s.inset = .006f; s.covered = .006f;
  s.recessOffset = .8f * metresPerPoint;
  s.pictureWidth = m.rect.w * metresPerPoint;
  s.pictureHeight = m.rect.h * metresPerPoint;
  if (m.hasSelectedRect) {
    const float x=std::max(m.rect.x,m.selectedRect.x), y=std::max(m.rect.y,m.selectedRect.y);
    const float right=std::min(m.rect.x+m.rect.w,m.selectedRect.x+m.selectedRect.w);
    const float bottom=std::min(m.rect.y+m.rect.h,m.selectedRect.y+m.selectedRect.h);
    if(right>x && bottom>y) s.pressedRect={s.inset+(x-m.rect.x)*metresPerPoint,
        s.inset+(y-m.rect.y)*metresPerPoint,(right-x)*metresPerPoint,(bottom-y)*metresPerPoint};
  }
  s.fullHeight = s.pictureHeight + 2 * s.inset + s.covered;
  const float fullWidth = (m.menusWidth > 0 ? m.menusWidth : m.rect.w) * metresPerPoint + 2 * s.inset;
  s.placement.width = window.placement.width * .35f * (1 - wide) + fullWidth * wide;
  // Only the top 4 mm lip shows at rest; the picture keeps its point scale
  // as the panel slides, rather than squeezing its titles into the lip.
  s.height = .004f + s.covered + (s.fullHeight - .004f - s.covered) * wide;
  s.region = {bendShape(window.placement, window.height),
      -window.placement.width/2 + s.placement.width/2,
      window.height/2 + frame.bezel() + s.height/2 - s.covered, .008f, true};
  s.placement.matrix = multiply(window.placement.matrix, s.region.centre());
  s.placement.bendVertical = s.region.shape.vertical;
  return s;
}
inline PanelSurface statusStripPlacement(const std::vector<SpatialTile> &tiles,
                                          const SurfaceMessage &m, float metresPerPoint,
                                          float shelfScale = 1) {
  PanelSurface s{"status", {}, 0, PanelRole::Status};
  for (const auto &tile : tiles) if (tile.kind == SpatialTile::Shelf) {
    // The picture and its padding shrink with the shelf, and ride its right end.
    const float inset = shelfInset * shelfScale;
    const float scale = std::min(metresPerPoint * shelfScale,
        (tile.surface.placement.width - 2 * inset) / std::max(1.f, m.rect.w));
    const float band = tile.surface.height - .115f * shelfScale;
    s.placement.width = m.rect.w * scale;
    s.height = m.rect.h * scale;
    auto t = spatialIdentity();
    t.m[0][3] = (tile.surface.placement.width - s.placement.width) / 2 - inset;
    t.m[1][3] = -tile.surface.height / 2 + band / 2;
    t.m[2][3] = .003f;
    s.menuRise = tile.surface.height / 2 - t.m[1][3] - s.height / 2 + .006f * shelfScale;
    s.placement.matrix = multiply(tile.surface.placement.matrix, t);
    return s;
  }
  return s;
}
inline PanelSurface surfacePlacement(const PanelSurface &anchor, const SurfaceMessage &m,
                                      float metresPerPoint) {
  if (m.anchor == "status" && m.anchorRect.w > 0)
    metresPerPoint = anchor.placement.width / m.anchorRect.w;
  PanelSurface s{"menu", {}, m.rect.h * metresPerPoint, PanelRole::Menu};
  s.placement.width = m.rect.w * metresPerPoint;
  auto t = spatialIdentity();
  t.m[0][3] = -anchor.placement.width / 2 + anchor.inset +
      (m.rect.x - m.anchorRect.x) * metresPerPoint + s.placement.width / 2;
  const float dy = (m.rect.y - (m.anchorRect.y + m.anchorRect.h)) * metresPerPoint;
  t.m[1][3] = m.anchor == "status" ? anchor.height / 2 + anchor.menuRise + std::max(0.f, dy) + s.height / 2 :
      (anchor.pictureHeight > 0 ? anchor.height / 2 - anchor.inset - anchor.pictureHeight :
                                  -anchor.height / 2) - dy - s.height / 2;
  t.m[2][3] = .01f;
  if (anchor.region.attached) {
    // The flat menu hangs from its own point on the strip, with the strip's
    // tangent there even when the window bends vertically.
    const float edge = anchor.height/2 - anchor.inset - anchor.pictureHeight;
    auto down = spatialIdentity(); down.m[1][3] = -dy-s.height/2;
    s.placement.matrix = multiply(surfacePoint(anchor,t.m[0][3],edge,.01f),down);
  } else s.placement.matrix = surfacePoint(anchor,t.m[0][3],t.m[1][3],t.m[2][3]);
  return s;
}
struct MenuStripReveal {
  float wide = 0;
  double lastOpen = -1000;
  void update(bool hot, bool menuVisible, double now, float dt) {
    if (hot || menuVisible) lastOpen = now;
    const float target = hot || menuVisible || now - lastOpen < 1000 ? 1.f : 0.f;
    const float step = std::clamp(dt, 0.f, .1f) / handleRevealSeconds;
    wide += std::clamp(target - wide, -step, step);
  }
};
// Trigger edges belong to a hand, not to whichever tile it enters mid-hold.
struct SpatialPress {
  bool previous[2]{};
  int owner = -1;
  std::string key;
  double since = 0;
  bool held = false;
  struct Result {
    std::string click, hold;
    bool outside = false;
  };
  Result update(const std::string (&hits)[2], const bool (&down)[2],
                double now) {
    Result r;
    for (int i = 0; i < 2; ++i)
      if (down[i] && !previous[i] && hits[i].empty())
        r.outside = true;
    if (owner >= 0) {
      if (!down[owner]) {
        if (!held && hits[owner] == key)
          r.click = key;
        owner = -1;
        key.clear();
      } else if (hits[owner] != key) {
        owner = -1;
        key.clear();
      } else if (!held && now - since >= 500) {
        held = true;
        r.hold = key;
      }
    }
    if (owner < 0)
      for (int hand : {1, 0})
        if (down[hand] && !previous[hand] && !hits[hand].empty()) {
          owner = hand;
          key = hits[hand];
          since = now;
          held = false;
          break;
        }
    for (int i = 0; i < 2; ++i)
      previous[i] = down[i];
    return r;
  }
};
inline int spatialPick(const EyeRay &ray, const std::vector<SpatialTile> &tiles,
                       PanelPick *result = nullptr) {
  int best = -1;
  PanelPick hit;
  for (size_t i = 0; i < tiles.size(); ++i) {
    if (!tiles[i].pickable)
      continue;
    auto h = surfacePick(ray, tiles[i].surface);
    if (h && (!hit || h.distance < hit.distance)) {
      hit = h;
      best = int(i);
    }
  }
  if (result)
    *result = hit;
  return best;
}
} // namespace fw
