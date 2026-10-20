#pragma once
// VR-first window handles (FW-14), visionOS-style: hidden until the laser
// leaves the window towards them. A pill below the window moves it (it follows
// the controller rigidly, position and angle together), corners outside the
// window resize it about its centre, and bars beside the left and right edges
// curve it as they are pulled towards or pushed away from the viewer. Handles
// look frosted white (style A) when shown and Steam blue (style B) while held.
//
// This file is the input-independent part: zones, drag maths and drawing.
// Coordinates are panel-local meters: x right, y up, origin at the window centre.
#include "placement.hpp"
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <vector>

namespace fw {

enum class Handle { None, Pill, Close, CornerTL, CornerTR, CornerBL, CornerBR, BarLeft, BarRight };

// Handle layout around a window of `width` x `height` meters. The chrome
// surface extends `margin` beyond each edge, plus room for the pill below.
struct HandleLayout {
  float width = 2.4f, height = 2.13f;
  float margin() const { return std::clamp(0.06f * width, 0.08f, 0.25f); }
  float pillWidth() const { return std::clamp(0.18f * width, 0.16f, 0.5f); }
  float pillHeight() const { return 0.25f * margin(); }
  float pillGap() const { return 0.45f * margin(); }  // window edge to pill centre
  // A round close button left of the pill, as on visionOS.
  float closeRadius() const { return 0.6f * pillHeight() + 0.01f; }
  float closeX() const { return -(pillWidth() / 2 + 0.35f * margin() + closeRadius()); }
  float chromeWidth() const { return width + 2 * margin(); }
  float chromeHeight() const { return height + 2 * margin(); }

  // Which handle the laser is pointing towards at (x, y), or None inside the
  // window or too far out. Corners win where both edges are crossed.
  Handle zone(float x, float y) const {
    const float hx = width / 2, hy = height / 2, m = margin();
    const bool outX = std::fabs(x) > hx, outY = std::fabs(y) > hy;
    if (!outX && !outY) return Handle::None;
    if (std::fabs(x) > hx + m || std::fabs(y) > hy + m) return Handle::None;
    if (outX && outY)
      return y > 0 ? (x < 0 ? Handle::CornerTL : Handle::CornerTR)
                   : (x < 0 ? Handle::CornerBL : Handle::CornerBR);
    if (outX) return x < 0 ? Handle::BarLeft : Handle::BarRight;
    if (y < 0 && std::fabs(x - closeX()) <= 1.8f * closeRadius()) return Handle::Close;
    if (y < 0 && std::fabs(x) <= pillWidth()) return Handle::Pill;
    return Handle::None;
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
  HmdMatrix34_t controllerStart{};
  Placement placementStart;
  float startDistance = 0;  // corner: controller ray hit distance from centre

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

  void start(Handle h, const HmdMatrix34_t &controller, const Placement &placement) {
    handle = h;
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
    case Handle::BarLeft: case Handle::BarRight: {
      // Pulling the bar towards the viewer bends the window more: about full
      // OpenVR curvature for a pull of a quarter of the window width.
      const auto &m = placementStart.matrix.m;
      float pull = 0;
      for (int r = 0; r < 3; r++) pull += (controller.m[r][3] - controllerStart.m[r][3]) * m[r][2];
      p.curvature = std::clamp(placementStart.curvature + pull / (0.25f * placementStart.width), 0.f, 1.f);
      break;
    }
    case Handle::Close: // a click, handled by the caller; never a drag
    case Handle::None:
      break;
    }
    return p;
  }
};

// Draws the visible handle (if any) into an RGBA image covering the chrome
// surface: transparent everywhere else. `held` switches style A to style B.
inline void drawHandle(const HandleLayout &l, Handle h, bool held, int pixelsWide, std::vector<uint8_t> &rgba,
                       int &pixelsHigh) {
  const float metersPerPixel = l.chromeWidth() / float(pixelsWide);
  pixelsHigh = std::max(1, int(std::lround(l.chromeHeight() / metersPerPixel)));
  rgba.assign(size_t(pixelsWide) * pixelsHigh * 4, 0);
  if (h == Handle::None) return;
  // Style A: frosted white; style B: Steam blue (#1a9fff) with a soft glow.
  const float color[3] = {held ? 26.f : 235.f, held ? 159.f : 238.f, held ? 255.f : 242.f};
  const float alpha = held ? 0.95f : 0.82f;
  const float thick = std::max(0.012f, 0.2f * l.margin());
  const float glow = held ? 0.8f * thick / metersPerPixel : 0.f; // glow falloff, pixels
  const float hx = l.width / 2, hy = l.height / 2, m = l.margin();
  // Signed distance (meters) from (x, y) to the handle's shape.
  auto distance = [&](float x, float y) {
    auto capsule = [](float px, float py, float ax, float ay, float bx, float by, float r) {
      const float vx = bx - ax, vy = by - ay;
      const float t = std::clamp(((px - ax) * vx + (py - ay) * vy) / std::max(vx * vx + vy * vy, 1e-9f), 0.f, 1.f);
      return std::hypot(px - ax - t * vx, py - ay - t * vy) - r;
    };
    switch (h) {
    case Handle::Pill:
    case Handle::Close: {
      const float y0 = -hy - l.pillGap(), half = l.pillWidth() / 2;
      const float pill = capsule(x, y, -half, y0, half, y0, l.pillHeight() / 2);
      // The close button: a ring with a cross, drawn as one distance field.
      const float cx = l.closeX(), r = l.closeRadius();
      const float ring = std::fabs(std::hypot(x - cx, y - y0) - r) - 0.12f * r;
      const float a = 0.42f * r;
      const float cross = std::min(capsule(x, y, cx - a, y0 - a, cx + a, y0 + a, 0.09f * r),
                                   capsule(x, y, cx - a, y0 + a, cx + a, y0 - a, 0.09f * r));
      return std::min(pill, std::min(ring, cross));
    }
    case Handle::BarLeft: case Handle::BarRight: {
      const float x0 = (h == Handle::BarLeft ? -1 : 1) * (hx + 0.45f * m);
      return capsule(x, y, x0, -0.12f * l.height, x0, 0.12f * l.height, thick / 2);
    }
    default: {
      // A quarter arc hugging the corner, just outside it.
      const float sx = (h == Handle::CornerTL || h == Handle::CornerBL) ? -1.f : 1.f;
      const float sy = (h == Handle::CornerTL || h == Handle::CornerTR) ? 1.f : -1.f;
      // Radius and offset keep the arc clear of the window's corner.
      const float r = std::max(0.9f * m, 0.07f * l.width), reach = 0.45f * m;
      const float cx = sx * (hx + reach - r), cy = sy * (hy + reach - r);
      const float dx = (x - cx) * sx, dy = (y - cy) * sy;
      // Only the part outside the window, never over its content.
      if (dx < 0 || dy < 0 || (std::fabs(x) <= hx && std::fabs(y) <= hy)) return 1e9f;
      return std::fabs(std::hypot(dx, dy) - r) - thick / 2;
    }
    }
  };
  for (int py = 0; py < pixelsHigh; py++)
    for (int px = 0; px < pixelsWide; px++) {
      const float x = (px + 0.5f) * metersPerPixel - l.chromeWidth() / 2;
      const float y = l.chromeHeight() / 2 - (py + 0.5f) * metersPerPixel;
      const float d = distance(x, y) / metersPerPixel;  // in pixels
      float a = std::clamp(0.5f - d, 0.f, 1.f) * alpha;  // anti-aliased edge
      if (glow > 0 && d > 0) a = std::max(a, 0.45f * std::exp(-d / glow));
      if (a <= 0) continue;
      uint8_t *o = &rgba[(size_t(py) * pixelsWide + px) * 4];
      o[0] = uint8_t(color[0]); o[1] = uint8_t(color[1]); o[2] = uint8_t(color[2]);
      o[3] = uint8_t(std::lround(a * 255));
    }
}

} // namespace fw
