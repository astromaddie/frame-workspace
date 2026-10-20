#pragma once
#include "bend.hpp"
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
struct PanelSpec {
  std::string generation, urlFile, key, name, controlFile, stateFile, poseFile, initialPose, requestDir;
  std::string decoder = "software", conversion = "cpu";
  bool foveation = false, gaze = false;
  static PanelSpec parse(const std::string &raw) {
    auto j = json(raw);
    if (!j || !json_object_is_type(j.get(), json_type_object) ||
        !json_object_is_type(field(j, "version"), json_type_int) || num(j, "version") != 1)
      throw std::runtime_error("invalid host panel spec version");
    PanelSpec s;
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
        s.controlFile.empty() || s.poseFile.empty() || s.requestDir.empty() ||
        s.name.size() > 64 || s.name.find_first_of("\r\n\t") != std::string::npos ||
        (s.decoder != "software" && s.decoder != "v4l2") ||
        (s.conversion != "cpu" && s.conversion != "gpu") || (s.foveation && s.gaze))
      throw std::runtime_error("invalid host panel spec");
    for (const auto &path : {s.urlFile, s.stateFile, s.controlFile, s.poseFile, s.requestDir})
      if (path.front() != '/' || path.size() > 4096 || path.find('\0') != std::string::npos)
        throw std::runtime_error("invalid host panel path");
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
struct PanelSurface { std::string key; Placement placement; float height = 0; };
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
  const auto inverse = inverseRigid(s.placement.matrix);
  float o[3]{}, d[3]{};
  for (int r = 0; r < 3; ++r) {
    o[r] = inverse.m[r][3];
    for (int c = 0; c < 3; ++c) {
      o[r] += inverse.m[r][c] * ray.origin[c];
      d[r] += inverse.m[r][c] * ray.direction[c] / std::sqrt(norm);
    }
  }
  float x = 0, y = 0, distance = 0;
  const auto shape = bendShape(s.placement, s.height);
  if (!bendHit(shape, o, d, 0, x, y, &distance) ||
      std::fabs(x) > s.placement.width / 2 + margin || std::fabs(y) > s.height / 2 + margin) return hit;
  const auto point = bendPointXY(shape, x, y, 0);
  float facing = 0;
  for (int i = 0; i < 3; ++i) facing += point.m[i][2] * d[i];
  if (facing >= -1e-5f) return hit;
  hit.key = s.key; hit.distance = distance;
  hit.u = std::clamp(x / s.placement.width + .5f, 0.f, 1.f);
  hit.v = std::clamp(.5f - y / s.height, 0.f, 1.f);
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
