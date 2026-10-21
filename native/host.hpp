#pragma once
#include "bend.hpp"
#include "depth.hpp"
#include "gaze_bridge.hpp"
#include <filesystem>
#include <set>

namespace fw {
inline bool validOverlayKey(const std::string &key) {
  return !key.empty() && key.size() <= 128 &&
      key.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-") == std::string::npos;
}
inline std::string jsonString(const std::string &s) {
  Json value(json_object_new_string_len(s.data(), int(s.size())), json_object_put);
  return json_object_to_json_string_ext(value.get(), JSON_C_TO_STRING_PLAIN);
}
enum class PanelRole { Window, Menubar, Status, Menu };
inline PanelRole panelRole(const std::string &role) {
  if (role == "window") return PanelRole::Window;
  if (role == "menubar") return PanelRole::Menubar;
  if (role == "status") return PanelRole::Status;
  if (role == "menu") return PanelRole::Menu;
  throw std::runtime_error("invalid panel role");
}
inline const char *roleName(PanelRole role) {
  return role == PanelRole::Window ? "window" : role == PanelRole::Menubar ? "menubar" :
         role == PanelRole::Status ? "status" : "menu";
}
struct SurfaceRect {
  float x = 0, y = 0, w = 0, h = 0;
};
struct SurfaceMessage {
  uint32_t seq = 0;
  bool visible = false, hot = false;
  SurfaceRect rect, anchorRect, selectedRect;
  bool hasSelectedRect = false;
  std::string anchor = "none";
  float menusWidth = 0;
  double received = 0;
  static SurfaceMessage parse(const std::string &raw) {
    auto j = json(raw);
    if (!j || str(j, "t") != "surface" ||
        !json_object_is_type(field(j, "v"), json_type_int) || num(j, "v") != 1 ||
        !json_object_is_type(field(j, "seq"), json_type_int) ||
        num(j, "seq", -1) < 0 || num(j, "seq") > UINT32_MAX ||
        !json_object_is_type(field(j, "visible"), json_type_boolean))
      throw std::runtime_error("invalid surface message");
    SurfaceMessage m;
    m.seq = uint32_t(num(j, "seq")); m.visible = flag(j, "visible");
    if (field(j, "hot") && !json_object_is_type(field(j, "hot"), json_type_boolean))
      throw std::runtime_error("invalid surface hot flag");
    m.hot = flag(j, "hot");
    if (field(j, "anchor")) m.anchor = str(j, "anchor");
    if (m.anchor != "menubar" && m.anchor != "status" && m.anchor != "none")
      throw std::runtime_error("invalid surface anchor");
    auto rect = [&](const char *name, SurfaceRect &out) {
      auto a = field(j, name);
      if (!a && !m.visible) return;
      if (!a || !json_object_is_type(a, json_type_array) || json_object_array_length(a) != 4)
        throw std::runtime_error("invalid surface rect");
      float values[4];
      for (int i = 0; i < 4; ++i) {
        auto v = json_object_array_get_idx(a, i);
        if ((!json_object_is_type(v, json_type_int) && !json_object_is_type(v, json_type_double)) ||
            !std::isfinite(json_object_get_double(v)) || std::abs(json_object_get_double(v)) > 1000000)
          throw std::runtime_error("invalid surface coordinate");
        values[i] = float(json_object_get_double(v));
      }
      if (values[2] <= 0 || values[3] <= 0)
        throw std::runtime_error("invalid surface size");
      out = {values[0], values[1], values[2], values[3]};
    };
    rect("rect", m.rect); rect("anchorRect", m.anchorRect);
    if (field(j, "selectedRect")) { rect("selectedRect", m.selectedRect); m.hasSelectedRect = true; }
    if (field(j, "menusWidth")) {
      auto v = field(j, "menusWidth");
      if ((!json_object_is_type(v, json_type_int) && !json_object_is_type(v, json_type_double)) ||
          !std::isfinite(num(j, "menusWidth")) || num(j, "menusWidth") <= 0 || num(j, "menusWidth") > 1000000)
        throw std::runtime_error("invalid menus width");
      m.menusWidth = float(num(j, "menusWidth"));
    }
    return m;
  }
};
inline bool sequenceNewer(uint32_t picture, uint32_t fence) {
  const uint32_t delta = picture - fence;
  return delta != 0 && delta < 0x80000000u;
}
inline bool surfacePictureReady(const SurfaceMessage &m, uint32_t seq, double received) {
  return m.visible && sequenceNewer(seq, m.seq) && received > m.received;
}
struct PanelSpec {
  std::string generation, urlFile, key, name, controlFile, stateFile, poseFile, initialPose, requestDir;
  std::string decoder = "software", conversion = "cpu";
  PanelRole role = PanelRole::Window;
  int slot = -1;
  bool foveation = false, gaze = false;
  static PanelSpec parse(const std::string &raw) {
    auto j = json(raw);
    if (!j || !json_object_is_type(j.get(), json_type_object) ||
        !json_object_is_type(field(j, "version"), json_type_int) || num(j, "version") != 1)
      throw std::runtime_error("invalid host panel spec version");
    PanelSpec s;
    json_object *role = nullptr;
    if (json_object_object_get_ex(j.get(), "role", &role)) {
      if (!json_object_is_type(role, json_type_string))
        throw std::runtime_error("invalid panel role");
      s.role = panelRole(json_object_get_string(role));
    }
    json_object *slot = nullptr;
    if (json_object_object_get_ex(j.get(), "slot", &slot)) {
      if (s.role != PanelRole::Window || !json_object_is_type(slot, json_type_int) ||
          num(j, "slot") < 0 || num(j, "slot") > 1023)
        throw std::runtime_error("invalid wall slot");
      s.slot = int(num(j, "slot"));
    }
    s.generation = str(j, "generation"); s.urlFile = str(j, "urlFile");
    s.key = str(j, "overlayKey"); s.name = str(j, "overlayName");
    s.controlFile = str(j, "controlFile"); s.stateFile = str(j, "stateFile");
    s.poseFile = str(j, "poseFile"); s.initialPose = str(j, "initialPose");
    s.requestDir = str(j, "requestDir");
    if (field(j, "decoder")) s.decoder = str(j, "decoder");
    if (field(j, "conversion")) s.conversion = str(j, "conversion");
    for (auto name : {"foveation", "gaze"})
      if (field(j, name) && !json_object_is_type(field(j, name), json_type_boolean))
        throw std::runtime_error("invalid host stream option");
    s.foveation = flag(j, "foveation"); s.gaze = flag(j, "gaze");
    if (s.generation.empty() || s.generation.size() > 64 ||
        s.generation.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_") != std::string::npos ||
        !validOverlayKey(s.key) || s.urlFile.empty() || s.stateFile.empty() ||
        s.controlFile.empty() || (s.role == PanelRole::Window && s.poseFile.empty()) || s.requestDir.empty() ||
        s.name.size() > 64 || s.name.find_first_of("\r\n\t") != std::string::npos ||
        (s.decoder != "software" && s.decoder != "v4l2") ||
        (s.conversion != "cpu" && s.conversion != "gpu") || (s.foveation && s.gaze))
      throw std::runtime_error("invalid host panel spec");
    for (const auto &path : {s.urlFile, s.stateFile, s.controlFile, s.poseFile, s.requestDir})
      if ((!path.empty() && path.front() != '/') || path.size() > 4096 || path.find('\0') != std::string::npos)
        throw std::runtime_error("invalid host panel path");
    if (s.role != PanelRole::Window && !s.initialPose.empty())
      throw std::runtime_error("auxiliary panels have no initial pose");
    if (!s.initialPose.empty()) {
      Placement p;
      if (!parsePlacement(s.initialPose, p)) throw std::runtime_error("invalid initial host pose");
    }
    return s;
  }
};
// Lifecycle identity is a spec filename plus its generation. Keep terminal
// generations recorded until their spec changes: one failed stream must not
// continuously reopen a scarce decoder or replay a single-use ticket.
struct PanelBook {
  std::map<std::string, std::string> generations;
  struct Changes { std::vector<std::string> remove, add; };
  Changes plan(const std::map<std::string, PanelSpec> &desired) const {
    Changes out;
    for (const auto &[path, generation] : generations) {
      auto it = desired.find(path);
      if (it == desired.end() || it->second.generation != generation) out.remove.push_back(path);
    }
    for (const auto &[path, spec] : desired) {
      auto it = generations.find(path);
      if (it == generations.end() || it->second != spec.generation) out.add.push_back(path);
    }
    return out;
  }
  void added(const std::string &path, const PanelSpec &s) { generations[path] = s.generation; }
  void removed(const std::string &path) { generations.erase(path); }
};
struct PanelSurface {
  std::string key; Placement placement; float height = 0; PanelRole role = PanelRole::Window;
  // Menu panel's unoccluded crop, picture inset/scale and anchor clearance.
  float fullHeight = 0, inset = 0, pictureWidth = 0, pictureHeight = 0;
  float covered = 0, menuRise = 0;
  BendRegion region{};
  SurfaceRect pressedRect{};
};
inline HmdMatrix34_t surfacePoint(const PanelSurface &s, float x, float y, float z = 0) {
  return multiply(s.placement.matrix, s.region.attached ? s.region.point(x,y,z) :
      bendPointXY(bendShape(s.placement,s.height),x,y,z));
}
inline void surfaceUV(const PanelSurface &s, float &u, float &v) {
  if (s.pictureWidth > 0 && s.pictureHeight > 0) {
    u = std::clamp((u * s.placement.width - s.inset) / s.pictureWidth, 0.f, 1.f);
    v = std::clamp((v * s.height - s.inset) / s.pictureHeight, 0.f, 1.f);
  }
}
inline bool sameSurfaceGeometry(const PanelSurface &a, const PanelSurface &b) {
  return a.placement.width == b.placement.width && a.height == b.height &&
      a.region == b.region &&
      a.pressedRect.x == b.pressedRect.x && a.pressedRect.y == b.pressedRect.y &&
      a.pressedRect.w == b.pressedRect.w && a.pressedRect.h == b.pressedRect.h && a.placement.bendA == b.placement.bendA &&
      a.placement.bendB == b.placement.bendB && a.placement.bendVertical == b.placement.bendVertical &&
      a.fullHeight == b.fullHeight && a.inset == b.inset && a.pictureWidth == b.pictureWidth &&
      a.pictureHeight == b.pictureHeight && a.covered == b.covered && a.menuRise == b.menuRise &&
      memcmp(&a.placement.matrix, &b.placement.matrix, sizeof(a.placement.matrix)) == 0;
}
// Reveal routing is separate from consuming a hit: a hidden pill may wake up
// without blocking the window behind, but cannot wake through foreground UI.
struct HandRoute {
  struct Pick {
    std::string key; float distance = 0; bool foreground = false;
    void consider(const std::string &next, float d, bool front) {
      if (d > 0 && (key.empty() || (front && !foreground) || (front == foreground && d < distance)))
        { key = next; distance = d; foreground = front; }
    }
  } hit, reveal;
  bool blocked = false, dragging = false;
  explicit HandRoute(bool uiBlocked = false) : blocked(uiBlocked) {}
  void consider(const std::string &key, float hitDistance, float revealDistance, bool foreground, bool drag = false) {
    if (blocked || dragging) return;
    if (drag) { hit.key = reveal.key = key; dragging = true; return; }
    hit.consider(key, hitDistance, foreground);
    reveal.consider(key, revealDistance, foreground);
  }
};
inline bool focusSurface(const PanelSurface &s) { return s.role == PanelRole::Window; }
struct PanelPick {
  std::string key;
  float u = 0, v = 0, distance = 0;
  explicit operator bool() const { return !key.empty(); }
};
// Uses the same arc lengths and asymmetric/vertical surfaces as the window
// and its chrome. UV is always the original window: right and down from top left.
inline PanelPick surfacePick(const EyeRay &ray, const PanelSurface &s, float margin = 0) {
  PanelPick hit;
  if (!std::isfinite(s.height) || s.height <= 0 || !std::isfinite(s.placement.width) || s.placement.width <= 0)
    return hit;
  float norm = 0;
  for (int i = 0; i < 3; ++i) {
    if (!std::isfinite(ray.origin[i]) || !std::isfinite(ray.direction[i])) return hit;
    norm += ray.direction[i] * ray.direction[i];
  }
  if (!std::isfinite(norm) || norm < 1e-10f) return hit;
  const auto base = s.region.attached ? multiply(s.placement.matrix, inverseRigid(s.region.centre())) : s.placement.matrix;
  const auto inverse = inverseRigid(base);
  float o[3]{}, d[3]{};
  for (int r = 0; r < 3; ++r) {
    o[r] = inverse.m[r][3];
    for (int c = 0; c < 3; ++c) {
      o[r] += inverse.m[r][c] * ray.origin[c];
      d[r] += inverse.m[r][c] * ray.direction[c] / std::sqrt(norm);
    }
  }
  float x = 0, y = 0, distance = 0;
  const auto shape = s.region.attached ? s.region.shape : bendShape(s.placement, s.height);
  const float depth = s.region.attached ? s.region.depth : s.role == PanelRole::Window ? pictureDepth : 0;
  const float edgeMargin = s.region.attached ? std::max(.2f,
      (shape.vertical ? std::fabs(s.region.y)+s.height/2 : std::fabs(s.region.x)+s.placement.width/2)
          -shape.span/2+std::max(0.f,margin)) : .2f;
  bool onPicture = bendHit(shape, o, d, depth, x, y, &distance, edgeMargin);
  if (s.role == PanelRole::Window && (!onPicture || std::fabs(x)>s.placement.width/2+margin || std::fabs(y)>s.height/2+margin)) {
    HandleLayout layout;layout.width=s.placement.width;layout.height=s.height;
    if(frameWallHit(shape,layout,o,d,x,y,distance)) {
      hit.key=s.key;hit.distance=distance;hit.u=std::clamp(x/layout.width+.5f,0.f,1.f);hit.v=std::clamp(.5f-y/layout.height,0.f,1.f);return hit;
    }
  }
  if (!onPicture) return hit;
  const auto point = bendPointXY(shape, x, y, depth);
  if (s.region.attached) { x -= s.region.x; y -= s.region.y; }
  if (std::fabs(x) > s.placement.width / 2 + margin || std::fabs(y) > s.height / 2 + margin || y < -s.height / 2 + s.covered) return hit;
  float facing = 0;
  for (int i = 0; i < 3; ++i) facing += point.m[i][2] * d[i];
  if (facing >= -1e-5f) return hit;
  hit.key = s.key; hit.distance = distance;
  hit.u = std::clamp(x / s.placement.width + .5f, 0.f, 1.f);
  hit.v = std::clamp(.5f - y / s.height, 0.f, 1.f);
  surfaceUV(s, hit.u, hit.v);
  return hit;
}
inline PanelPick pickPanel(const EyeRay &ray, const std::vector<PanelSurface> &surfaces,
                           const std::string &active = "") {
  PanelPick best;
  for (const auto &surface : surfaces) {
    // Keep the active surface 2 cm past its edge. A new target must be at
    // least 1 cm inside its own edge. Depth still wins for overlapping panels.
    auto hit = surfacePick(ray, surface, active.empty() ? 0.f : surface.key == active ? .02f : -.01f);
    if (hit && (!best || hit.distance < best.distance - .001f ||
                (std::fabs(hit.distance - best.distance) <= .001f && hit.key == active))) best = hit;
  }
  return best;
}
inline bool binocularRay(const EyeRay (&rays)[2], EyeRay &ray) {
  float norm = 0;
  for (int i = 0; i < 3; ++i) {
    ray.origin[i] = (rays[0].origin[i] + rays[1].origin[i]) * .5f;
    ray.direction[i] = (rays[0].direction[i] + rays[1].direction[i]) * .5f;
    if (!std::isfinite(ray.origin[i]) || !std::isfinite(ray.direction[i])) return false;
    norm += ray.direction[i] * ray.direction[i];
  }
  if (!std::isfinite(norm) || norm < 1e-10f) return false;
  for (auto &v : ray.direction) v /= std::sqrt(norm);
  return true;
}
} // namespace fw
