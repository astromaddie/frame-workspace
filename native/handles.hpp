#pragma once
// VR-first window controls (FW-14) in Palladium silver-lavender. Each window
// has a machined bezel floating 1.5 cm in front of it, and its controls on a
// layer 3 cm in front: a move pill and a separate close disc below, always
// there, and resize corners and curve grips that slide out (thin) when the
// laser leaves the window towards them, widen under the laser, and glow from
// inside while dragged. The pill carries the window rigidly with the
// controller, corners resize it about its centre, and the side grips curve it
// as they are pulled towards or pushed away from the viewer.
//
// This file is the input-independent part: the measurements (shared with the
// shader in chrome.hpp), hit zones and drag maths. Coordinates are
// panel-local meters: x right, y up, origin at the window centre.
#include "placement.hpp"
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <vector>

namespace fw {

enum class Handle { None, Pill, Close, CornerTL, CornerTR, CornerBL, CornerBR, BarLeft, BarRight, BarTop, BarBottom };
constexpr int handleCount = 10; // Pill..BarBottom, indexed handle - 1
inline int handleIndex(Handle h) { return int(h) - 1; }

// Measurements around a window of `width` x `height` meters.
struct HandleLayout {
  float width = 2.4f, height = 2.13f;
  // F2 machined bezel.
  float bezel() const { return std::clamp(0.0105f * width, 0.02f, 0.035f); }
  // Corners and side grips: one Palladium rod, 6 mm slid out, 15 mm under the laser.
  static constexpr float rodRest = 0.006f, rodWide = 0.015f;
  // Window edge to the rods' centre line: just clear of the bezel when wide.
  float handleOffset() const { return bezel() + 0.006f + rodWide / 2; }
  float arm() const { return std::clamp(0.07f * width, 0.06f, 0.18f); }
  float bend() const { return 0.24f * arm(); }
  float gripLength() const { return std::clamp(0.1f * height, 0.08f, 0.25f); }   // the side grips
  float gripLengthAcross() const { return std::clamp(0.1f * width, 0.08f, 0.25f); }  // top and bottom
  // P4: a close disc left of the move pill, 3 cm under the bezel.
  float pillWidth() const { return std::clamp(0.12f * width, 0.11f, 0.33f); }
  float pillHeight() const { return std::clamp(0.18f * pillWidth(), 0.024f, 0.05f); }
  float discDiameter() const { return 1.36f * pillHeight(); }
  float rowGap() const { return 0.45f * pillHeight(); }
  float rowY() const { return -(height / 2 + bezel() + 0.03f + discDiameter() / 2); }
  float closeX() const { return -(discDiameter() + rowGap() + pillWidth()) / 2 + discDiameter() / 2; }
  float pillX() const { return (discDiameter() + rowGap() + pillWidth()) / 2 - pillWidth() / 2; }
  // The laser reveals a side's handles within this distance of the window.
  float reach() const { return handleOffset() + 0.10f; }
  // How close the laser must come to a handle's centre line to take it.
  static constexpr float tolerance = 0.03f;
  // The bezel layer, and the controls layer (symmetric about the centre).
  // The bezel layer, with room around it for the gaze focus halo.
  static constexpr float haloMargin = 0.02f;
  float frameWidth() const { return width + 2 * (bezel() + haloMargin); }
  float frameHeight() const { return height + 2 * (bezel() + haloMargin); }
  float chromeWidth() const { return width + 2 * (handleOffset() + rodWide / 2 + 0.012f); }
  float chromeHeight() const {
    return 2 * std::max(height / 2 + handleOffset() + rodWide / 2 + 0.012f, -rowY() + discDiameter() / 2 + 0.012f);
  }

  // Where a corner's bend sits: sx, sy are its side (+1 right/top).
  void cornerSigns(Handle h, float &sx, float &sy) const {
    sx = (h == Handle::CornerTR || h == Handle::CornerBR) ? 1.f : -1.f;
    sy = (h == Handle::CornerTL || h == Handle::CornerTR) ? 1.f : -1.f;
  }
  static float segment(float px, float py, float ax, float ay, float bx, float by) {
    const float vx = bx - ax, vy = by - ay;
    const float t = std::clamp(((px - ax) * vx + (py - ay) * vy) / std::max(vx * vx + vy * vy, 1e-12f), 0.f, 1.f);
    return std::hypot(px - ax - t * vx, py - ay - t * vy);
  }
  // Distance from (x, y) to a handle's centre line (pill and disc: to their
  // edge, so 0 means on the outline).
  float distance(Handle h, float x, float y) const {
    const float hx = width / 2, hy = height / 2, o = handleOffset();
    switch (h) {
    case Handle::Pill: {
      const float half = pillWidth() / 2 - pillHeight() / 2;
      return std::max(0.f, segment(x, y, pillX() - half, rowY(), pillX() + half, rowY()) - pillHeight() / 2);
    }
    case Handle::Close:
      return std::max(0.f, std::hypot(x - closeX(), y - rowY()) - discDiameter() / 2);
    case Handle::BarLeft: case Handle::BarRight: {
      const float gx = (h == Handle::BarLeft ? -1 : 1) * (hx + o);
      return segment(x, y, gx, -gripLength() / 2, gx, gripLength() / 2);
    }
    case Handle::BarTop: case Handle::BarBottom: {
      const float gy = (h == Handle::BarBottom ? -1 : 1) * (hy + o);
      return segment(x, y, -gripLengthAcross() / 2, gy, gripLengthAcross() / 2, gy);
    }
    case Handle::None:
      return 1e9f;
    default: {
      // One L: the bend (radius r) at the corner, arms running back along the edges.
      float sx, sy;
      cornerSigns(h, sx, sy);
      const float bx = sx * (hx + o), by = sy * (hy + o), r = bend(), a = arm();
      const float cx = bx - sx * r, cy = by - sy * r; // the bend's centre
      float d = std::min(segment(x, y, bx - sx * r, by, bx - sx * a, by), segment(x, y, bx, by - sy * r, bx, by - sy * a));
      if ((x - cx) * sx >= 0 && (y - cy) * sy >= 0) d = std::min(d, std::fabs(std::hypot(x - cx, y - cy) - r));
      return d;
    }
    }
  }
  // Which sides' handles the laser at (x, y) reveals, as a bit per handle
  // index (the pill and close disc are always shown, so not included).
  unsigned revealed(float x, float y) const {
    const float hx = width / 2, hy = height / 2, r = reach();
    if (std::fabs(x) > hx + r || std::fabs(y) > hy + r) return 0;
    const bool right = x > hx, left = x < -hx, top = y > hy, bottom = y < -hy;
    unsigned m = 0;
    auto set = [&](Handle h, bool on) { if (on) m |= 1u << handleIndex(h); };
    set(Handle::CornerTL, left || top);
    set(Handle::CornerTR, right || top);
    set(Handle::CornerBL, left || bottom);
    set(Handle::CornerBR, right || bottom);
    set(Handle::BarLeft, left);
    set(Handle::BarRight, right);
    set(Handle::BarTop, top);
    set(Handle::BarBottom, bottom);
    return m;
  }
  // The handle the laser at (x, y) would take: the nearest shown one within
  // `tolerance`, or None. Nothing inside the window.
  Handle zone(float x, float y) const {
    if (std::fabs(x) <= width / 2 && std::fabs(y) <= height / 2) return Handle::None;
    const unsigned shown = revealed(x, y) | (1u << handleIndex(Handle::Pill)) | (1u << handleIndex(Handle::Close));
    Handle best = Handle::None;
    float bestD = tolerance;
    for (int i = 0; i < handleCount; i++) {
      if (!(shown & (1u << i))) continue;
      const Handle h = Handle(i + 1);
      const float d = distance(h, x, y);
      if (d <= bestD) { bestD = d; best = h; }
    }
    return best;
  }
};

// A 3x4 rigid transform (rotation columns + translation) as plain floats.
inline HmdMatrix34_t multiply(const HmdMatrix34_t &a, const HmdMatrix34_t &b) {
  HmdMatrix34_t out{};
  for (int r = 0; r < 3; r++) {
    for (int c = 0; c < 4; c++) {
      float v = c == 3 ? a.m[r][3] : 0;
      for (int k = 0; k < 3; k++) v += a.m[r][k] * b.m[k][c];
      out.m[r][c] = v;
    }
  }
  return out;
}
inline HmdMatrix34_t inverseRigid(const HmdMatrix34_t &a) {
  HmdMatrix34_t out{};
  for (int r = 0; r < 3; r++)
    for (int c = 0; c < 3; c++) out.m[r][c] = a.m[c][r];
  for (int r = 0; r < 3; r++)
    out.m[r][3] = -(out.m[r][0] * a.m[0][3] + out.m[r][1] * a.m[1][3] + out.m[r][2] * a.m[2][3]);
  return out;
}

// One drag, from press to release. `start` captures the controller pose and
// the placement when the handle was pressed; update() gives the placement for
// the controller's current pose.
struct HandleDrag {
  Handle handle = Handle::None;
  bool bothSides = false;  // a grip taken with the grip button bends both halves
  HmdMatrix34_t controllerStart{};
  Placement placementStart;
  float startDistance = 0;  // corner: controller ray hit distance from centre
  float startAspect = 2560.f / 2880.f;  // the window's height / width, for top-to-bottom bends

  // Where the controller's pointing ray (its -z axis) meets the panel plane,
  // in panel-local x/y. False when it points away from or along the plane.
  static bool rayHit(const Placement &p, const HmdMatrix34_t &c, float &x, float &y) {
    const auto &m = p.matrix.m;
    const float o[3] = {c.m[0][3] - m[0][3], c.m[1][3] - m[1][3], c.m[2][3] - m[2][3]};
    const float d[3] = {-c.m[0][2], -c.m[1][2], -c.m[2][2]};
    const float n[3] = {m[0][2], m[1][2], m[2][2]};
    const float denom = d[0] * n[0] + d[1] * n[1] + d[2] * n[2];
    if (std::fabs(denom) < 1e-4f) return false;
    const float t = -(o[0] * n[0] + o[1] * n[1] + o[2] * n[2]) / denom;
    if (t <= 0) return false;
    const float h[3] = {o[0] + t * d[0], o[1] + t * d[1], o[2] + t * d[2]};
    x = h[0] * m[0][0] + h[1] * m[1][0] + h[2] * m[2][0];
    y = h[0] * m[0][1] + h[1] * m[1][1] + h[2] * m[2][1];
    return true;
  }

  void start(Handle h, const HmdMatrix34_t &controller, const Placement &placement, bool both = false) {
    handle = h;
    bothSides = both;
    controllerStart = controller;
    placementStart = placement;
    float x = 0, y = 0;
    startDistance = rayHit(placement, controller, x, y) ? std::hypot(x, y) : 0;
  }

  Placement update(const HmdMatrix34_t &controller) const {
    Placement p = placementStart;
    switch (handle) {
    case Handle::Pill:
      // Rigid follow: the window keeps its pose relative to the controller.
      p.matrix = multiply(controller, multiply(inverseRigid(controllerStart), placementStart.matrix));
      break;
    case Handle::CornerTL: case Handle::CornerTR: case Handle::CornerBL: case Handle::CornerBR: {
      float x = 0, y = 0;
      if (startDistance > 0.01f && rayHit(placementStart, controller, x, y))
        p.width = std::clamp(placementStart.width * std::hypot(x, y) / startDistance, 0.4f, 8.f);
      break;
    }
    case Handle::BarLeft: case Handle::BarRight: case Handle::BarTop: case Handle::BarBottom: {
      // Pulling a grip towards the viewer bends the window: about full OpenVR
      // curvature for a pull of a quarter of the window along the bend. The
      // trigger bends the grip's own half; the grip button bends both. Side
      // grips bend side to side, top and bottom grips top to bottom; changing
      // direction starts from flat.
      const bool vertical = handle == Handle::BarTop || handle == Handle::BarBottom;
      const auto &m = placementStart.matrix.m;
      const float span = vertical ? placementStart.width * startAspect : placementStart.width;
      float pull = 0;
      for (int r = 0; r < 3; r++) pull += (controller.m[r][3] - controllerStart.m[r][3]) * m[r][2];
      const bool sameDirection = placementStart.bendVertical == vertical;
      float a = sameDirection ? placementStart.bendA : 0, b = sameDirection ? placementStart.bendB : 0;
      // A: left or bottom half; B: right or top.
      const bool sideB = handle == Handle::BarRight || handle == Handle::BarTop;
      const float from = sideB ? b : a;
      const float to = std::clamp(from + pull / (0.25f * span), 0.f, 1.f);
      if (bothSides) a = b = to;
      else (sideB ? b : a) = to;
      p.bendVertical = vertical;
      p.bendA = a;
      p.bendB = b;
      syncCurvature(p);
      break;
    }
    case Handle::Close: // a click, handled by the caller; never a drag
    case Handle::None:
      break;
    }
    return p;
  }
};

} // namespace fw
