#pragma once
#include "chrome.hpp"
#include "host.hpp"
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
  std::string chosen;
  std::vector<SpatialApp> apps;
  std::vector<SpatialWindow> windows;
  static SpatialState parse(const std::string &raw) {
    auto j = json(raw);
    if (!j || !json_object_is_type(j.get(), json_type_object))
      throw std::runtime_error("spatial state object required");
    SpatialState s;
    s.spatial = str(j, "mode") == "spatial";
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
    if (s.key == active) {
      auto hit = surfacePick(ray, s, .02f);
      if (hit) return hit;
    }
  return pickPanel(ray, surfaces, active);
}
// Keep a visual fade independently of the uninterrupted confirmation timer.
struct HostFocus {
  std::map<std::string, FocusFeedback> feedback;
  std::string active, pending, source;
  float elapsed = 0;
  bool update(const PanelPick &hit, float dt, const std::string &input) {
    if (!hit || hit.key != pending || input != source) {
      pending = hit.key;
      source = input;
      elapsed = 0;
    }
    dt = std::clamp(dt, 0.f, .05f);
    if (hit)
      elapsed += dt;
    bool confirmed = false;
    for (auto &[key, f] : feedback) {
      bool looking = hit && hit.key == key;
      // Visual glow may survive a glance, but it cannot shorten the next dwell.
      float saved = f.glow;
      bool snap = f.update(dt, looking);
      if (snap && elapsed < FocusFeedback::dwellSeconds) {
        f.focused = false;
        f.glow = std::min(.99f, saved + dt / FocusFeedback::dwellSeconds);
      }
      if (looking && key != active && elapsed >= FocusFeedback::dwellSeconds) {
        confirmed = true;
        f.focused = false;
        f.glow = 1;
      }
    }
    return confirmed;
  }
  void activate(const std::string &key) {
    for (auto &[k, f] : feedback)
      if (k != key)
        f.lose();
    auto &f = feedback[key];
    f.focused = true;
    f.glow = 0;
    f.snap = 0;
    active = key;
  }
  void remove(const std::string &key) {
    feedback.erase(key);
    if (active == key)
      active.clear();
    if (pending == key) {
      pending.clear();
      elapsed = 0;
    }
  }
};
struct SpatialTile {
  enum Kind { Dock, Window, Menu, Shelf } kind = Dock;
  int index = 0;
  PanelSurface surface;
  std::string label;
};
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
inline std::vector<SpatialTile> spatialLayout(const SpatialState &s,
                                              const HmdMatrix34_t &anchor,
                                              const std::string &menuID = "") {
  const int menu = spatialAppIndex(s, menuID);
  std::vector<SpatialTile> tiles;
  if (!s.spatial)
    return tiles;
  // Scale the whole ordered shelf to fit 1.35 m; no app silently disappears.
  float step = std::min(.105f, 1.35f / std::max(size_t(1), s.apps.size()));
  if (!s.apps.empty()) {
    Placement p;
    p.width = step * s.apps.size() + .022f;
    auto t = spatialIdentity();
    t.m[1][3] = -.48f;
    t.m[2][3] = -.002f;
    p.matrix = multiply(anchor, t);
    tiles.push_back({SpatialTile::Shelf, 0, {"shelf", p, .115f}, ""});
  }
  for (size_t i = 0; i < s.apps.size(); ++i) {
    Placement p;
    p.width = step;
    p.matrix = anchor;
    auto t = spatialIdentity();
    t.m[0][3] = (float(i) - (s.apps.size() - 1) * .5f) * step;
    t.m[1][3] = -.48f;
    p.matrix = multiply(anchor, t);
    tiles.push_back({SpatialTile::Dock,
                     int(i),
                     {"dock:" + s.apps[i].id, p, .115f},
                     s.apps[i].name});
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
                       {"window:" + s.windows[i].key, p, .215f},
                       s.windows[i].app});
    }
  if (menu >= 0 && menu < int(s.apps.size()))
    for (int i = 0; i < 3; ++i) {
      Placement p;
      p.width = .25f;
      auto t = spatialIdentity();
      t.m[0][3] = (menu - (s.apps.size() - 1) * .5f) * step;
      t.m[1][3] = -.30f - i * .045f;
      t.m[2][3] = .025f;
      p.matrix = multiply(anchor, t);
      tiles.push_back({SpatialTile::Menu,
                       i,
                       {"menu:" + menuID + ":" + std::to_string(i), p, .045f},
                       i == 0   ? "Show All Windows"
                       : i == 1 ? (s.apps[menu].hidden ? "Unhide" : "Hide")
                                : "Quit"});
    }
  return tiles;
}
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
