#pragma once
// How a window bends (FW-14): side to side or top to bottom, each half on its
// own. SteamVR only curves an overlay side to side about its middle, so:
// - an even side-to-side bend is one overlay with OpenVR curvature;
// - halves that differ are two overlays, each an arc (or flat) starting at the
//   window's centre line, tangent to each other there;
// - a top-to-bottom bend turns each overlay a quarter turn on its face (its
//   texture is drawn transposed) so SteamVR's curve runs vertically.
// This file is the geometry, shared by the window, its chrome layers, the
// laser's hit test and the tip's placement. Panel-local meters: x right, y up,
// z toward the viewer; layers sit `z` in front, on concentric surfaces.
#include "handles.hpp"
#include <algorithm>
#include <cmath>

namespace fw {

struct BendShape {
  bool vertical = false;      // bends about a horizontal axis (top/bottom grips)
  float span = 2.4f;          // the window along the bend (width, or height when vertical)
  float amount[2] = {0, 0};   // the negative half (left / bottom) and positive half (right / top),
                              // as OpenVR curvature for the whole span: radius = span / (2 pi amount)
  bool split() const { return std::fabs(amount[0] - amount[1]) > 1e-4f; }
  int segments() const { return split() ? 2 : 1; }
  float radius(int half) const { return amount[half] > 1e-4f ? span / (2 * float(M_PI) * amount[half]) : 0; }
};

inline BendShape bendShape(const Placement &p, float height) {
  BendShape b;
  b.vertical = p.bendVertical;
  b.span = p.bendVertical ? height : p.width;
  b.amount[0] = p.bendA;
  b.amount[1] = p.bendB;
  return b;
}

// A point on the window's surface (or a layer `z` in front of it), with the
// surface's orientation there, as a panel-local transform. `s` runs along the
// bend (x, or y when vertical), `t` across it.
inline HmdMatrix34_t bendPoint(const BendShape &b, float s, float t, float z) {
  const int half = s < 0 ? 0 : 1;
  const float R = b.radius(half);
  float along = s, depth = z, a = 0;
  if (R > 0) {
    const float r = std::max(R - z, 0.05f), sign = s < 0 ? -1.f : 1.f;
    a = sign * std::fabs(s) / R;
    along = r * std::sin(a);
    depth = R - r * std::cos(a);
  }
  const float c = std::cos(a), sn = std::sin(a);
  if (!b.vertical)
    return {{{c, 0, -sn, along}, {0, 1, 0, t}, {sn, 0, c, depth}}};
  return {{{1, 0, 0, t}, {0, c, -sn, along}, {0, sn, c, depth}}};
}
inline HmdMatrix34_t bendPointXY(const BendShape &b, float x, float y, float z) {
  return b.vertical ? bendPoint(b, y, x, z) : bendPoint(b, x, y, z);
}

// One SteamVR overlay's share of a window or layer: its transform (panel
// local), width, OpenVR curvature and texture bounds (top row first, so v runs
// 1 -> 0). `across` is the size across the bend (height, or width when vertical).
struct BendSegment {
  HmdMatrix34_t local{};
  float width = 0, curvature = 0;
  VRTextureBounds_t bounds{0, 1, 1, 0};
};
inline BendSegment bendSegment(const BendShape &b, int index, float layerSpan, float z) {
  // layerSpan: this layer's size along the bend in window coordinates (the
  // bezel and controls reach past the window's edges).
  const bool two = b.split();
  // Halves overlap by 2 mm, each continuing a little along its own curve, so
  // no hairline shows between them at the centre line.
  const float seam = 0.002f;
  const float lo = two ? (index == 0 ? -layerSpan / 2 : -seam) : -layerSpan / 2;
  const float hi = two ? (index == 0 ? seam : layerSpan / 2) : layerSpan / 2;
  const int half = two ? index : 0;
  const float R = b.radius(half);
  BendSegment out;
  const float mid = (lo + hi) / 2;
  out.local = bendPoint(b, two ? mid : 0.f, 0, z);
  if (!two) {
    // One overlay, bent evenly about its own middle.
    if (R > 0) {
      const float r = std::max(R - z, 0.05f);
      out.width = layerSpan * r / R;
      out.curvature = std::min(out.width / (2 * float(M_PI) * r), 1.f);
    } else {
      out.width = layerSpan;
    }
  } else if (R > 0) {
    // A half: an arc from the centre line, centred on its own middle.
    const float r = std::max(R - z, 0.05f);
    out.width = (hi - lo) * r / R;
    out.curvature = std::min(out.width / (2 * float(M_PI) * r), 1.f);
  } else {
    out.width = hi - lo;
  }
  const float u0 = (lo + layerSpan / 2) / layerSpan, u1 = (hi + layerSpan / 2) / layerSpan;
  out.bounds = {u0, 1, u1, 0};
  if (b.vertical) {
    // A quarter turn on its face: the overlay's x runs up the window, so its
    // (transposed) texture's u runs from the bottom edge to the top.
    const HmdMatrix34_t turn{{{0, -1, 0, 0}, {1, 0, 0, 0}, {0, 0, 1, 0}}};
    out.local = multiply(out.local, turn);
  }
  return out;
}

// Where a ray (panel local origin o, direction d) meets the surface `z` in
// front of the window, in window coordinates. Past the window's edges the
// outer halves continue, so handles beyond them are still hit.
inline bool bendHit(const BendShape &b, const float o[3], const float d[3], float z, float &x, float &y, float *distance = nullptr) {
  // Work in (along, across, depth).
  const float oa = b.vertical ? o[1] : o[0], oc = b.vertical ? o[0] : o[1], od = o[2];
  const float da = b.vertical ? d[1] : d[0], dc = b.vertical ? d[0] : d[1], dd = d[2];
  float bestT = -1, bestS = 0, bestC = 0;
  for (int half = 0; half < 2; half++) {
    const float R = b.radius(half), sign = half == 0 ? -1.f : 1.f;
    if (R <= 0) {
      if (std::fabs(dd) < 1e-6f) continue;
      const float t = (z - od) / dd;
      if (t <= 0) continue;
      const float s = oa + t * da;
      if ((s < 0) != (half == 0) && std::fabs(s) > 1e-6f) continue;
      if (bestT < 0 || t < bestT) { bestT = t; bestS = s; bestC = oc + t * dc; }
      continue;
    }
    // The half's cylinder: axis across, centre at depth R on the centre line.
    const float r = std::max(R - z, 0.05f), ox = oa, oz = od - R;
    const float qa = da * da + dd * dd, qb = 2 * (ox * da + oz * dd), qc = ox * ox + oz * oz - r * r;
    const float disc = qb * qb - 4 * qa * qc;
    if (qa < 1e-10f || disc < 0) continue;
    for (float t : {(-qb - std::sqrt(disc)) / (2 * qa), (-qb + std::sqrt(disc)) / (2 * qa)}) {
      if (t <= 0) continue;
      const float pa = oa + t * da, pd = od + t * dd;
      const float angle = std::atan2(pa, R - pd);
      if ((angle < 0) != (half == 0) && std::fabs(angle) > 1e-6f) continue;
      // Only the window's own arc, and 20 cm past its edge for the handles.
      if (std::fabs(angle) > std::min((b.span / 2 + 0.2f) / R, float(M_PI) * 0.98f)) continue;
      if (bestT < 0 || t < bestT) { bestT = t; bestS = sign * std::fabs(angle) * R; bestC = oc + t * dc; }
    }
  }
  if (bestT < 0) return false;
  if (distance) *distance = bestT;
  x = b.vertical ? bestC : bestS;
  y = b.vertical ? bestS : bestC;
  return true;
}

} // namespace fw
