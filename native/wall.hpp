#pragma once
// L2 content cells; production chrome occupies the remaining angular gutters.
#include "spatial.hpp"
namespace fw {
constexpr float wallWidth = .64f, wallHeight = .40f, wallDistance = 1.75f;
inline int wallSlot(const Json &j) {
  json_object *value = nullptr;
  if (!json_object_object_get_ex(j.get(), "slot", &value)) return -1;
  if (!json_object_is_type(value, json_type_int) || num(j, "slot") < 0 || num(j, "slot") > 1023)
    throw std::runtime_error("invalid wall slot");
  return int(num(j, "slot"));
}
inline Placement wallPlacement(int slot, float aspect, const HmdMatrix34_t &anchor) {
  if (slot < 0 || slot > 1023 || !std::isfinite(aspect) || aspect <= 0)
    throw std::runtime_error("invalid wall cell");
  int column, row;
  if (slot < 6) {
    const int columns[]{0, -1, 1}; column = columns[slot % 3]; row = slot / 3;
  } else {
    const int offset = slot - 6;
    column = (2 + offset / 4) * (offset % 2 ? 1 : -1);
    row = (offset % 4) / 2;
  }
  const float yaw = column * 25.f * float(M_PI) / 180;
  const float pitch = (row ? -6.5f : 11.5f) * float(M_PI) / 180;
  const float cy = std::cos(yaw), sy = std::sin(yaw), cp = std::cos(pitch), sp = std::sin(pitch);
  Placement p; p.width = std::min(wallWidth, wallHeight * aspect); p.distance = wallDistance;
  HmdMatrix34_t local{{{cy, -sy * sp, -sy * cp, wallDistance * sy * cp},
                       {0, cp, -sp, wallDistance * sp},
                       {sy, cy * sp, cy * cp, -wallDistance * cy * cp}}};
  p.matrix = multiply(anchor, local);
  return p;
}
struct WallAnchor {
  HmdMatrix34_t matrix = spatialIdentity();
  bool ready = false;
  void capture(const HmdMatrix34_t &head) {
    if (!ready) { matrix = spatialAnchor(head, 0, 0); ready = true; }
  }
};
} // namespace fw
