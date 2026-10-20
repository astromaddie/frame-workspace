#pragma once
// A panel's placement in the room (width, distance, curvature and a rigid
// 3x4 transform), its pose-file format, and the 3DOF move/rotate maths.
#include "vendor/openvr/openvr_capi.h"
#include "protocol.hpp"
#include <algorithm>
#include <array>
#include <cmath>
#include <string>

namespace fw {
struct Placement {
  float width = 2.4f, distance = 2.0f;
  // An even side-to-side bend as OpenVR curvature 0-1 (what the Mac page sets);
  // 0 when the halves differ or the bend runs top to bottom.
  float curvature = 0;
  // The bend itself (bend.hpp): its direction, and each half in the same units
  // as `curvature`: A the left (or bottom) half, B the right (or top).
  bool bendVertical = false;
  float bendA = 0, bendB = 0;
  HmdMatrix34_t matrix{{{1, 0, 0, 0}, {0, 1, 0, 1.3f}, {0, 0, 1, -2}}};
};
// Sets an even side-to-side bend (the Mac page's curvature).
inline void setEvenBend(Placement &p, float curvature) {
  p.curvature = p.bendA = p.bendB = curvature;
  p.bendVertical = false;
}
// Keeps `curvature` in step after the halves or direction change.
inline void syncCurvature(Placement &p) {
  p.curvature = !p.bendVertical && std::fabs(p.bendA - p.bendB) <= 1e-4f ? p.bendA : 0.f;
}
inline std::string placementJSON(const Placement &p) {
  std::string s = "{\"version\":1,\"width\":" + std::to_string(p.width) +
                  ",\"distance\":" + std::to_string(p.distance) +
                  ",\"curvature\":" + std::to_string(p.curvature) +
                  ",\"bend\":{\"vertical\":" + (p.bendVertical ? "true" : "false") +
                  ",\"a\":" + std::to_string(p.bendA) + ",\"b\":" + std::to_string(p.bendB) + "}" +
                  ",\"matrix\":[";
  for (int r = 0; r < 3; r++)
    for (int c = 0; c < 4; c++) {
      if (r || c)
        s += ",";
      s += std::to_string(p.matrix.m[r][c]);
    }
  return s + "]}";
}
  // Moves the panel in its own frame (meters): x right, y up, z toward the
  // viewer. False (unchanged) if that would leave it over 10 m from the room
  // origin.
inline bool movePlacement(Placement &placement, float dx, float dy, float dz) {
  auto &m = placement.matrix.m;
    float t[3];
    for (int r = 0; r < 3; r++)
      t[r] = m[r][3] + m[r][0] * dx + m[r][1] * dy + m[r][2] * dz;
    if (std::hypot(t[0], t[1], t[2]) > 10.f)
      return false;
    for (int r = 0; r < 3; r++)
      m[r][3] = t[r];
    placement.distance = std::clamp(placement.distance - dz, .4f, 10.f);
    return true;
  }
  // Turns the panel about its own centre (degrees): yaw about its up axis,
  // pitch about its right axis, roll about its normal. The rotation is
  // re-orthonormalised so repeated nudges cannot shear the panel.
inline void rotatePlacement(Placement &placement, float yaw, float pitch, float roll) {
    const double d = M_PI / 180;
    const double cy = std::cos(yaw * d), sy = std::sin(yaw * d);
    const double cp = std::cos(pitch * d), sp = std::sin(pitch * d);
    const double cr = std::cos(roll * d), sr = std::sin(roll * d);
    // Local rotation Ry * Rx * Rz, applied on the right (in the panel's frame).
    const double local[3][3] = {
        {cy * cr + sy * sp * sr, -cy * sr + sy * sp * cr, sy * cp},
        {cp * sr, cp * cr, -sp},
        {-sy * cr + cy * sp * sr, sy * sr + cy * sp * cr, cy * cp}};
    auto &m = placement.matrix.m;
    double out[3][3]{};
    for (int r = 0; r < 3; r++)
      for (int c = 0; c < 3; c++)
        for (int k = 0; k < 3; k++)
          out[r][c] += m[r][k] * local[k][c];
    // Gram-Schmidt on the columns: right, then up, then normal = right x up.
    auto column = [&](int c) { return std::array<double, 3>{out[0][c], out[1][c], out[2][c]}; };
    auto x = column(0), y = column(1);
    auto norm = [](std::array<double, 3> &v) {
      double l = std::sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
      for (auto &e : v) e /= l;
    };
    norm(x);
    double dot = x[0] * y[0] + x[1] * y[1] + x[2] * y[2];
    for (int i = 0; i < 3; i++) y[i] -= dot * x[i];
    norm(y);
    std::array<double, 3> z{x[1] * y[2] - x[2] * y[1], x[2] * y[0] - x[0] * y[2], x[0] * y[1] - x[1] * y[0]};
    for (int r = 0; r < 3; r++) {
      m[r][0] = float(x[r]);
      m[r][1] = float(y[r]);
      m[r][2] = float(z[r]);
    }
  }
inline bool parsePlacement(const std::string &text, Placement &p);
inline bool loadPlacement(const std::string &path, Placement &p) {
  if (path.empty())
    return false;
  try {
    return parsePlacement(readFile(path), p);
  } catch (...) {
    return false;
  }
}
// A placement in the pose-file format (version, width, distance, optional
// curvature, 12-value matrix with an orthonormal rotation).
inline bool parsePlacement(const std::string &text, Placement &p) {
  try {
    auto j = json(text);
    auto a = field(j, "matrix");
    if (num(j, "version") != 1 || !a || json_object_array_length(a) != 12)
      return false;
    Placement candidate;
    candidate.width = std::clamp(num(j, "width", 2.4), .4, 8.0);
    candidate.distance = std::clamp(num(j, "distance", 2), .4, 10.0);
    // Optional: pose files written before curvature persistence omit it.
    const double curvature = num(j, "curvature", 0);
    setEvenBend(candidate, std::isfinite(curvature) ? float(std::clamp(curvature, 0.0, 1.0)) : 0.f);
    // Optional: per-half bends and their direction (older files are even, side to side).
    if (auto bend = field(j, "bend")) {
      auto member = [&](const char *name) {
        json_object *v = nullptr;
        return json_object_object_get_ex(bend, name, &v) ? v : nullptr;
      };
      auto amount = [&](const char *name) {
        auto v = member(name);
        const double d = v && (json_object_is_type(v, json_type_double) || json_object_is_type(v, json_type_int))
                             ? json_object_get_double(v) : 0;
        return std::isfinite(d) ? float(std::clamp(d, 0.0, 1.0)) : 0.f;
      };
      auto vertical = member("vertical");
      candidate.bendVertical = vertical && json_object_is_type(vertical, json_type_boolean) &&
                               json_object_get_boolean(vertical);
      candidate.bendA = amount("a");
      candidate.bendB = amount("b");
      syncCurvature(candidate);
    }
    for (int r = 0; r < 3; r++)
      for (int c = 0; c < 4; c++) {
        double v =
            json_object_get_double(json_object_array_get_idx(a, r * 4 + c));
        if (!std::isfinite(v) || fabs(v) > 100)
          return false;
        candidate.matrix.m[r][c] = v;
      }
    for (int r = 0; r < 3; r++)
      for (int s = 0; s < 3; s++) {
        double dot = 0;
        for (int c = 0; c < 3; c++)
          dot += candidate.matrix.m[r][c] * candidate.matrix.m[s][c];
        if (fabs(dot - (r == s ? 1 : 0)) > .02)
          return false;
      }
    p = candidate;
    return true;
  } catch (...) {
    return false;
  }
}

} // namespace fw
