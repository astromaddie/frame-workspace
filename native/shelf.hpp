#pragma once
// Spatial Mode shelf behaviour (FW-15 D1), apart from drawing: where the shelf
// is pinned and how that is saved, how Recenter brings it back, the Dock-style
// magnification that grows it when the controller laser or the eyes land on it,
// and the move pill that carries it with a controller the way windows move.
// Everything here is plain geometry and timing, so self-tests can run it.
#include "chrome.hpp"
#include "gaze_bridge.hpp"
#include "placement.hpp"
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <sys/stat.h>

namespace fw {

// Tunables.
inline constexpr float shelfRestScale = .6f;      // size at rest, as a fraction of full size
inline constexpr float shelfEaseSeconds = .2f;    // grow and shrink take this long (eased)
inline constexpr double shelfGraceMs = 500;       // the ray may leave this long before it shrinks
inline constexpr float shelfRestMargin = .015f;   // metres around the shelf that count as on it at rest
inline constexpr float shelfActiveMargin = .06f;  // the same while enlarged (hysteresis)
inline constexpr float shelfGazeMargin = .02f;    // extra for the eyes, which are less precise
inline constexpr float shelfIconBoost = .5f;      // the icon under the pointer reaches 1.5x full size
inline constexpr float shelfIconReach = 2.2f;     // magnification falls off over this many icons
inline constexpr float shelfMagnifySeconds = .15f;
inline constexpr float shelfRowDrop = .48f;       // icon row centre below the pose frame's origin
inline constexpr float shelfIconHeight = .115f;   // full-size icon tile height
inline constexpr float shelfPixelsPerMetre = 1200;

inline HmdMatrix34_t shelfIdentity() {
  return {{{1, 0, 0, 0}, {0, 1, 0, 0}, {0, 0, 1, 0}}};
}
inline float shelfEase(float t) { // smootherstep: no snap at either end
  t = std::clamp(t, 0.f, 1.f);
  return t * t * t * (t * (t * 6 - 15) + 10);
}

// What to draw: a uniform size, the Dock-style icon magnification and, when
// pinned, where the shelf is. Default values draw the old full-size shelf.
struct ShelfView {
  float scale = 1;     // 1 full size, shelfRestScale at rest
  float magnify = 0;   // 0..1 icon magnification amount
  float pointer = 0;   // pointer x (m) along the row at the current size, 0 at its centre
  bool pinned = false; // true: shelf tiles use `pose`; false: they use the layout anchor
  HmdMatrix34_t pose = shelfIdentity();
  // The move pill (the same one windows have), when it should be drawn.
  ChromeState chrome;
  bool handle = false;
};

// Where the point `u` on the resting row (metres from the row centre) lands
// once icons near `pointer` are enlarged: the row stretches by
// `1 + boost * f(distance)` where the pointer is, so the spot under the pointer
// itself stays put. `f` is a cosine bell over `reach` metres.
inline float shelfBellIntegral(float d, float reach) {
  if (d <= -reach) return -reach / 2;
  if (d >= reach) return reach / 2;
  return d / 2 + reach / (2 * float(M_PI)) * std::sin(float(M_PI) * d / reach);
}
inline float shelfMagnifiedX(float u, float pointer, float boost, float reach) {
  return u + boost * shelfBellIntegral(u - pointer, reach);
}

// Smooth state for the enlarge/shrink animation. Timing is in milliseconds as
// elsewhere in the host. `inSmall` / `inLarge` say whether the pointer is on
// the shelf with the at-rest and the enlarged margins; the larger region
// applies while enlarged so a ray near the edge does not make it flicker.
struct ShelfHover {
  float progress = 0;  // 0 at rest .. 1 enlarged (linear in time; eased when read)
  float magnify = 0;   // icon magnification amount
  float pointer = 0;   // smoothed pointer x
  bool active = false; // enlarged, or shrinking is still in its grace period
  bool pointerSeen = false;
  double lastHit = -1e9;
  float scale() const { return shelfRestScale + (1 - shelfRestScale) * shelfEase(progress); }
  bool update(double now, float dt, bool inSmall, bool inLarge, bool hasX, float x) {
    dt = std::clamp(dt, 0.f, .1f);
    const bool hit = active ? inLarge : inSmall;
    if (hit) {
      lastHit = now;
      active = true;
    } else if (active && now - lastHit >= shelfGraceMs) {
      active = false;
    }
    const float step = dt / shelfEaseSeconds;
    progress = std::clamp(progress + (active ? step : -step), 0.f, 1.f);
    const bool magnifying = hit && hasX;
    magnify = std::clamp(magnify + (magnifying ? 1.f : -1.f) * dt / shelfMagnifySeconds, 0.f, 1.f);
    if (magnifying) {
      // Follow quickly but without hand jitter; start exactly where the ray lands.
      pointer = pointerSeen && magnify > .02f ? pointer + (x - pointer) * (1 - std::exp(-dt / .04f)) : x;
      pointerSeen = true;
    }
    return hit;
  }
};

// Where a ray meets the plane of a pose frame (x right, y up), metres.
inline bool shelfPlaneHit(const EyeRay &ray, const HmdMatrix34_t &pose, float &x, float &y) {
  float o[3], d[3];
  for (int r = 0; r < 3; ++r) {
    o[r] = 0;
    d[r] = 0;
    for (int c = 0; c < 3; ++c) {
      if (!std::isfinite(ray.origin[c]) || !std::isfinite(ray.direction[c])) return false;
      o[r] += pose.m[c][r] * (ray.origin[c] - pose.m[c][3]);
      d[r] += pose.m[c][r] * ray.direction[c];
    }
  }
  if (o[2] <= 0 || d[2] >= -1e-4f) return false; // from the front, towards the plane
  const float t = -o[2] / d[2];
  if (t > 12) return false;
  x = o[0] + t * d[0];
  y = o[1] + t * d[1];
  return true;
}

// Edges of the shelf in its pose frame.
struct ShelfRect {
  float minX = 0, maxX = 0, minY = 0, maxY = 0;
  bool valid = false;
  void add(float cx, float cy, float w, float h) {
    if (!valid) {
      minX = cx - w / 2; maxX = cx + w / 2; minY = cy - h / 2; maxY = cy + h / 2;
      valid = true;
      return;
    }
    minX = std::min(minX, cx - w / 2); maxX = std::max(maxX, cx + w / 2);
    minY = std::min(minY, cy - h / 2); maxY = std::max(maxY, cy + h / 2);
  }
  bool contains(float x, float y, float margin) const {
    return valid && x >= minX - margin && x <= maxX + margin && y >= minY - margin && y <= maxY + margin;
  }
};

// Where the shelf is pinned. A pose that was placed and moved by the user is
// saved like a window pose (same JSON, same validation as native/placement.hpp
// plus a handedness and distance check); an unsaved default is placed in front
// of the user once and stays where it is.
inline float shelfDeterminant(const HmdMatrix34_t &a) {
  const auto &m = a.m;
  return m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1]) - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0]) +
         m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]);
}
inline bool shelfPoseUsable(const HmdMatrix34_t &a) {
  for (int r = 0; r < 3; ++r)
    for (int c = 0; c < 4; ++c)
      if (!std::isfinite(a.m[r][c])) return false;
  // Rows orthonormal to the same tolerance as a window pose file.
  for (int r = 0; r < 3; ++r)
    for (int q = 0; q < 3; ++q) {
      float dot = 0;
      for (int c = 0; c < 3; ++c) dot += a.m[r][c] * a.m[q][c];
      if (std::fabs(dot - (r == q ? 1.f : 0.f)) > .02f) return false;
    }
  return shelfDeterminant(a) > 0 && std::hypot(a.m[0][3], a.m[1][3], a.m[2][3]) <= 10.f;
}
inline bool loadShelfPose(const std::string &path, HmdMatrix34_t &pose) {
  Placement p;
  if (!loadPlacement(path, p) || !shelfPoseUsable(p.matrix)) return false;
  pose = p.matrix;
  return true;
}
inline bool saveShelfPose(const std::string &path, const HmdMatrix34_t &pose) {
  if (path.empty() || !shelfPoseUsable(pose)) return false;
  try {
    Placement p;
    p.width = 1;
    p.distance = 1.3f;
    p.matrix = pose;
    std::filesystem::create_directories(std::filesystem::path(path).parent_path());
    const auto tmp = path + ".tmp";
    {
      std::ofstream f(tmp);
      if (!f || !(f << placementJSON(p) << "\n")) return false;
    }
    chmod(tmp.c_str(), 0600);
    return rename(tmp.c_str(), path.c_str()) == 0;
  } catch (...) {
    return false;
  }
}

// The frame the shelf hangs in, in front of a head pose: level, 1.3 m ahead
// of the head's horizontal facing, at the head's height (the icon row then
// sits shelfRowDrop below it).
inline HmdMatrix34_t shelfFrontOf(const HmdMatrix34_t &head, float distance = 1.3f) {
  float x = head.m[0][2], z = head.m[2][2], n = std::hypot(x, z);
  if (n < .01f) { x = 0; z = 1; } else { x /= n; z /= n; }
  return {{{z, 0, x, head.m[0][3] - x * distance},
           {0, 1, 0, head.m[1][3]},
           {-x, 0, z, head.m[2][3] - z * distance}}};
}

struct ShelfPin {
  HmdMatrix34_t pose = shelfIdentity();
  bool placed = false;
  bool recenterPending = false;
  // Companion recenter requests (a counter in the state file) and runtime
  // zero-pose resets: both bring the shelf back in front of the user.
  bool seqSeen = false;
  int64_t seq = 0;
  unsigned resets = 0;
  bool resetsSeen = false;
  void request() { recenterPending = true; }
  void observeSequence(int64_t next) {
    if (seqSeen && next > seq) request();
    seq = next;
    seqSeen = true;
  }
  void observeResets(unsigned count) {
    if (resetsSeen && count != resets) request();
    resets = count;
    resetsSeen = true;
  }
  enum Change { Unchanged, Defaulted, Recentered };
  // Call every frame with the head pose (when valid). Defaulted: first
  // placement, in front of the user and not saved. Recentered: saved by the caller.
  Change update(bool headValid, const HmdMatrix34_t &head) {
    if (!headValid) return Unchanged;
    if (recenterPending) {
      pose = shelfFrontOf(head);
      placed = true;
      recenterPending = false;
      return Recentered;
    }
    if (!placed) {
      pose = shelfFrontOf(head);
      placed = true;
      return Defaulted;
    }
    return Unchanged;
  }
};

// Where the move pill is, in the shelf's pose frame: the virtual window the
// pill hangs from (the full-size shelf) is `layout`, drawn `scale` times
// as large with its centre at (ox, cy).
struct ShelfHandleFrame {
  float ox = 0, cy = 0, scale = 1;
  HandleLayout layout;
  bool valid = false;
  void toVirtual(float x, float y, float &xv, float &yv) const {
    xv = (x - ox) / scale;
    yv = (y - cy) / scale;
  }
};

// The shelf's own move interaction: the pill reveals under the laser, a press
// on it takes the shelf rigidly with the controller (HandleDrag::Pill, as for
// windows) until release, and the caller saves the pose then.
struct ShelfMover {
  struct Hand {
    bool valid = false, grab = false, squeeze = false;
    HmdMatrix34_t pose{};
  };
  HandleReveal showing;
  HandleDrag drag;
  int dragHand = -1;
  bool bySqueeze = false, hot = false, tip = false;
  int pointing = -1;
  float tipX = 0, tipY = 0;
  bool grabWas[2]{}, squeezeWas[2]{};
  bool visible() const { return showing.amount > .001f || dragHand >= 0; }
  bool dragging() const { return dragHand >= 0; }
  // A hand on the revealed pill, or carrying the shelf, belongs to the shelf.
  bool owns(int hand) const { return dragHand == hand || (hot && pointing == hand); }
  ChromeState chrome() const {
    ChromeState s;
    s.reveal[handleIndex(Handle::Pill)] = showing.amount;
    s.held[handleIndex(Handle::Pill)] = dragHand >= 0 ? 1.f : 0.f;
    s.tip = tip;
    s.tipX = tipX;
    s.tipY = tipY;
    return s;
  }
  // `hovered`: a controller is on the shelf, so the pill slides out. Returns true
  // when a move just ended (save the pose).
  bool update(const ShelfHandleFrame &g, HmdMatrix34_t &pose, const Hand (&hands)[2], bool allowSqueeze,
              bool hovered, double now, float dt) {
    bool ended = false, near = hovered;
    float hx[2]{}, hy[2]{};
    bool onPlane[2]{};
    Placement frame;
    frame.matrix = pose;
    for (int i = 0; i < 2; ++i)
      if (hands[i].valid && g.valid && HandleDrag::rayHit(frame, hands[i].pose, hx[i], hy[i])) {
        onPlane[i] = true;
        float xv, yv;
        g.toVirtual(hx[i], hy[i], xv, yv);
        if (g.layout.pillZone(xv, yv)) near = true;
      }
    if (dragHand >= 0) {
      const auto &h = hands[dragHand];
      if (h.valid && (bySqueeze ? h.squeeze : h.grab)) {
        auto moved = drag.update(h.pose).matrix;
        if (shelfPoseUsable(moved)) pose = moved;
      } else {
        dragHand = -1;
        ended = true;
      }
    }
    const int previous = pointing;
    const bool wasHot = hot;
    hot = false;
    pointing = -1;
    tip = false;
    if (dragHand >= 0) {
      pointing = dragHand;
      hot = true;
    } else {
      for (int i : {1, 0}) {
        if (!onPlane[i]) continue;
        float xv, yv;
        g.toVirtual(hx[i], hy[i], xv, yv);
        const float d = g.layout.distance(Handle::Pill, xv, yv);
        // Keep the pill a little beyond its edge once taken, as windows do.
        const float limit = HandleLayout::tolerance * (wasHot && previous == i ? 1.5f : 1.f);
        if (showing.amount > .001f && d <= limit) {
          hot = true;
          pointing = i;
          tipX = xv;
          tipY = yv;
          tip = true;
          break;
        }
        if (d < .06f && showing.amount > .001f) {
          pointing = i;
          tipX = xv;
          tipY = yv;
          tip = true;
          break;
        }
      }
      for (int i = 0; i < 2 && dragHand < 0 && hot; ++i) {
        const bool pressed = pointing == i &&
            ((hands[i].grab && !grabWas[i]) || (allowSqueeze && hands[i].squeeze && !squeezeWas[i]));
        if (pressed) {
          bySqueeze = !(hands[i].grab && !grabWas[i]);
          drag.start(Handle::Pill, hands[i].pose, frame, bySqueeze);
          dragHand = i;
        }
      }
    }
    showing.update(near, hot, dragHand >= 0, now, dt);
    for (int i = 0; i < 2; ++i) {
      grabWas[i] = hands[i].grab;
      squeezeWas[i] = hands[i].squeeze;
    }
    return ended;
  }
};

} // namespace fw
