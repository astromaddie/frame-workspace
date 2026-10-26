#pragma once
// Where Spatial Mode windows go (FW-15 L2): the curved wall in front of the
// user, cells handed out against what is actually occupied, panels sized for
// their Mac window, and the poses a user set by hand. Production chrome
// occupies the angular gutters between content cells.
#include "spatial.hpp"
#include <ctime>
#include <filesystem>
#include <regex>
namespace fw {
constexpr float wallWidth = .64f, wallHeight = .40f, wallDistance = 1.75f;
// Columns 25 degrees apart; rows at +11.5 (eye level), -6.5 (lower, clear of
// the shelf) and +29.5 (the top row, used once the first two are full). Up
// there columns converge, so the top row's are 30 degrees apart.
constexpr float wallColumnDegrees = 25, wallTopColumnDegrees = 30, wallRowDegrees[3]{11.5f, -6.5f, 29.5f};
// One cell is the column and row spacing; a panel's chrome stays inside it.
constexpr float wallCellPitch = 18;
// 37 cells per ring: 7 columns x 2 rows (slots 0-13, as before), the top row
// in front (14-20: 0, +-30, +-60, +-90 degrees), columns +-100, +-125 and
// +-150 in the two lower rows (21-32), then the top row's +-120 and +-150
// (33-36). Later rings repeat the grid 1 m further out, panels scaled to keep
// the same angular size.
constexpr int wallRingCells = 37, wallRings = 5;
// Mac points to metres: a 1280-point-wide window fills a cell, smaller windows
// keep the same text size, and nothing is smaller than this on its longer side.
constexpr float wallMetresPerPoint = wallWidth / 1280, wallMinimumSide = .22f;
struct WallCell {
  int column = 0, row = 0, ring = 0;
  float yaw() const { return column * (row == 2 ? wallTopColumnDegrees : wallColumnDegrees); }
  float width() const { return row == 2 ? wallTopColumnDegrees : wallColumnDegrees; }
};
inline WallCell wallCell(int slot) {
  if (slot < 0 || slot > 1023) throw std::runtime_error("invalid wall cell");
  WallCell c;
  c.ring = std::min(slot / wallRingCells, wallRings - 1);
  const int i = slot % wallRingCells;
  if (i < 6) {
    const int columns[]{0, -1, 1}; c.column = columns[i % 3]; c.row = i / 3;
  } else if (i < 14) {
    const int offset = i - 6;
    c.column = (2 + offset / 4) * (offset % 2 ? 1 : -1);
    c.row = (offset % 4) / 2;
  } else if (i < 21) {
    const int columns[]{0, -1, 1, -2, 2, -3, 3}; c.column = columns[i - 14]; c.row = 2;
  } else if (i < 33) {
    const int offset = i - 21;
    c.column = (4 + offset / 4) * (offset % 2 ? 1 : -1);
    c.row = (offset % 4) / 2;
  } else {
    const int offset = i - 33;
    c.column = (4 + offset / 2) * (offset % 2 ? 1 : -1); c.row = 2;
  }
  return c;
}
inline float wallCellDistance(int slot) { return wallDistance + float(wallCell(slot).ring); }
inline int wallSlot(const Json &j) {
  json_object *value = nullptr;
  if (!json_object_object_get_ex(j.get(), "slot", &value)) return -1;
  if (!json_object_is_type(value, json_type_int) || num(j, "slot") < 0 || num(j, "slot") > 1023)
    throw std::runtime_error("invalid wall slot");
  return int(num(j, "slot"));
}
// Panel width in a cell: the picture's aspect fitted to the cell, and with the
// Mac window's size in points at the shared scale (so text reads the same on
// every panel), never below wallMinimumSide on its longer side.
inline float wallPanelWidth(float aspect, float pointsWidth, float pointsHeight, float scale = 1) {
  float width = std::min(wallWidth, wallHeight * aspect) * scale;
  if (std::isfinite(pointsWidth) && std::isfinite(pointsHeight) && pointsWidth > 0 && pointsHeight > 0) {
    float natural = pointsWidth * wallMetresPerPoint * scale;
    const float longer = std::max(natural, natural / aspect);
    if (longer < wallMinimumSide * scale) natural *= wallMinimumSide * scale / longer;
    width = std::min(width, natural);
  }
  return width;
}
inline Placement wallPlacement(int slot, float aspect, const HmdMatrix34_t &anchor,
                               float pointsWidth = 0, float pointsHeight = 0) {
  if (slot < 0 || slot > 1023 || !std::isfinite(aspect) || aspect <= 0)
    throw std::runtime_error("invalid wall cell");
  const auto cell = wallCell(slot);
  const float distance = wallCellDistance(slot), scale = distance / wallDistance;
  const float yaw = cell.yaw() * float(M_PI) / 180;
  const float pitch = wallRowDegrees[cell.row] * float(M_PI) / 180;
  const float cy = std::cos(yaw), sy = std::sin(yaw), cp = std::cos(pitch), sp = std::sin(pitch);
  Placement p; p.width = wallPanelWidth(aspect, pointsWidth, pointsHeight, scale); p.distance = distance;
  HmdMatrix34_t local{{{cy, -sy * sp, -sy * cp, distance * sy * cp},
                       {0, cp, -sp, distance * sp},
                       {sy, cy * sp, cy * cp, -distance * cy * cp}}};
  p.matrix = multiply(anchor, local);
  return p;
}
struct WallAnchor {
  HmdMatrix34_t matrix = spatialIdentity();
  bool ready = false;
  void capture(const HmdMatrix34_t &head) {
    if (!ready) { matrix = spatialAnchor(head, 0, 0); ready = true; }
  }
  // Arrange Windows: the wall moves to wherever the user faces now.
  void recapture(const HmdMatrix34_t &head) { matrix = spatialAnchor(head, 0, 0); ready = true; }
};

// Angular extent (degrees, yaw right and pitch up) as seen from the wall
// anchor. Yaw is unwrapped around `yaw`, the centre.
struct WallBounds { float yaw = 0, left = 0, right = 0, bottom = 0, top = 0; };
inline WallBounds wallCellBounds(int slot) {
  const auto c = wallCell(slot);
  const float yaw = c.yaw(), pitch = wallRowDegrees[c.row];
  return {yaw, yaw - c.width() / 2, yaw + c.width() / 2, pitch - wallCellPitch / 2, pitch + wallCellPitch / 2};
}
// A placed panel's footprint with its chrome (`margin` metres all round),
// sampled over its bent surface.
inline WallBounds panelWallBounds(const Placement &p, float height, const HmdMatrix34_t &anchor, float margin = .045f) {
  const auto inverse = inverseRigid(anchor);
  const auto shape = bendShape(p, height);
  auto angles = [&](float x, float y, float &yaw, float &pitch) {
    const auto m = multiply(inverse, multiply(p.matrix, bendPointXY(shape, x, y, 0)));
    yaw = std::atan2(m.m[0][3], -m.m[2][3]) * 180 / float(M_PI);
    pitch = std::atan2(m.m[1][3], std::hypot(m.m[0][3], m.m[2][3])) * 180 / float(M_PI);
  };
  WallBounds b;
  float pitch;
  angles(0, 0, b.yaw, pitch);
  b.left = b.right = b.yaw; b.bottom = b.top = pitch;
  for (int i = -1; i <= 1; ++i)
    for (int k = -1; k <= 1; ++k) {
      float yaw;
      angles(i * (p.width / 2 + margin), k * (height / 2 + margin), yaw, pitch);
      while (yaw - b.yaw > 180) yaw -= 360;
      while (yaw - b.yaw < -180) yaw += 360;
      b.left = std::min(b.left, yaw); b.right = std::max(b.right, yaw);
      b.bottom = std::min(b.bottom, pitch); b.top = std::max(b.top, pitch);
    }
  return b;
}
// Whether two footprints cover the same part of the view by more than
// `slack` degrees each way (touching neighbours do not count).
inline bool wallOverlap(const WallBounds &a, const WallBounds &b, float slack = 1) {
  float shift = b.yaw - a.yaw;
  while (shift > 180) shift -= 360;
  while (shift < -180) shift += 360;
  const float left = a.yaw + shift + (b.left - b.yaw), right = a.yaw + shift + (b.right - b.yaw);
  return std::min(a.right, right) - std::max(a.left, left) > slack &&
         std::min(a.top, b.top) - std::max(a.bottom, b.bottom) > slack;
}
// The first free cell: `preferred` (the companion's slot) if it is free, else
// the lowest in wall order. A cell is taken by a wall panel in it, and
// occupied by a hand-placed panel covering it. Behind a full ring the next
// ring's cells count as free. With every cell in use, the lowest not taken.
inline int wallAllocate(int preferred, const std::vector<WallBounds> &occupied, const std::vector<int> &taken) {
  auto free = [&](int cell) {
    if (std::find(taken.begin(), taken.end(), cell) != taken.end()) return false;
    const auto bounds = wallCellBounds(cell);
    for (const auto &o : occupied) if (wallOverlap(bounds, o)) return false;
    return true;
  };
  if (preferred >= 0 && preferred <= 1023 && free(preferred)) return preferred;
  for (int cell = 0; cell < wallRingCells * wallRings; ++cell) if (free(cell)) return cell;
  for (int cell = 0; cell <= 1023; ++cell)
    if (std::find(taken.begin(), taken.end(), cell) == taken.end()) return cell;
  return 0;
}
// Arrange Windows order: the focused window in the centre cell, then the most
// recently focused, then the rest as they were (wall cell or companion slot).
struct WallArrangeItem { bool focused = false; uint64_t recency = 0; int order = 0; };
inline std::vector<size_t> wallArrangeOrder(const std::vector<WallArrangeItem> &items) {
  std::vector<size_t> order(items.size());
  for (size_t i = 0; i < order.size(); ++i) order[i] = i;
  std::stable_sort(order.begin(), order.end(), [&](size_t a, size_t b) {
    const auto &x = items[a], &y = items[b];
    if (x.focused != y.focused) return x.focused;
    if (x.recency != y.recency) return x.recency > y.recency;
    return x.order < y.order;
  });
  return order;
}

// Window poses are remembered only once the user has placed the panel
// (moved, resized, curved, recentered or arranged it). The file records that,
// and the window it belongs to: Mac window IDs are reused, so a pose for
// another app's (or another title's) window under the same ID is ignored.
struct PoseIdentity { std::string bundleID, title, session; };
inline std::string userPoseJSON(const Placement &p, const PoseIdentity &id, std::time_t now = std::time(nullptr)) {
  auto s = placementJSON(p);
  s.pop_back();
  return s + ",\"userSet\":true,\"bundleID\":" + jsonString(id.bundleID) + ",\"title\":" + jsonString(id.title) +
         ",\"session\":" + jsonString(id.session) + ",\"savedAt\":" + std::to_string(int64_t(now)) + "}";
}
enum class PoseCheck { Missing, Invalid, Unverified, OtherWindow, Restored };
inline PoseCheck loadUserPose(const std::string &path, const PoseIdentity &id, Placement &p) {
  if (path.empty()) return PoseCheck::Missing;
  std::error_code error;
  if (!std::filesystem::exists(path, error)) return PoseCheck::Missing;
  std::string raw;
  try { raw = readFile(path); } catch (...) { return PoseCheck::Unverified; }
  Placement candidate;
  try {
    auto j = json(raw);
    if (!j || !json_object_is_type(j.get(), json_type_object)) return PoseCheck::Unverified;
    auto user = field(j, "userSet");
    if (!user || !json_object_is_type(user, json_type_boolean) || !json_object_get_boolean(user))
      return PoseCheck::Unverified;
    if (!parsePlacement(raw, candidate)) return PoseCheck::Invalid;
    auto same = [&](const char *name, const std::string &mine) {
      auto v = field(j, name);
      if (!v || !json_object_is_type(v, json_type_string)) return true;
      const std::string theirs = json_object_get_string(v);
      if (theirs.empty() || mine.empty()) return true;
      auto fold = [](std::string s) { for (auto &c : s) c = char(std::tolower((unsigned char)c)); return s; };
      return fold(theirs) == fold(mine);
    };
    if (!same("bundleID", id.bundleID) || !same("title", id.title)) return PoseCheck::OtherWindow;
  } catch (...) {
    return PoseCheck::Unverified;
  }
  p = candidate;
  return PoseCheck::Restored;
}
// Moves a pose file without provenance into a dated folder beside it
// (set-aside-YYYY-MM-DD), never deleting it. False if it could not be moved.
inline bool setAsidePose(const std::string &path, std::time_t now = std::time(nullptr)) {
  namespace fs = std::filesystem;
  std::error_code error;
  const fs::path source(path);
  if (fs::is_symlink(source, error) || !fs::is_regular_file(source, error)) return false;
  char day[16]{};
  std::tm local{};
  localtime_r(&now, &local);
  std::strftime(day, sizeof day, "%Y-%m-%d", &local);
  const fs::path folder = source.parent_path() / (std::string("set-aside-") + day);
  fs::create_directories(folder, error);
  if (error) return false;
  fs::permissions(folder, fs::perms::owner_all, fs::perm_options::replace, error);
  fs::path target = folder / source.filename();
  for (int n = 1; fs::exists(target, error) && n < 1000; ++n)
    target = folder / (source.stem().string() + "-" + std::to_string(n) + source.extension().string());
  fs::rename(source, target, error);
  return !error;
}
// First run: every window pose (w<ID>-pose.json) in `folder` without
// provenance is moved aside. Returns how many were moved.
inline int sweepUnverifiedPoses(const std::string &folder, std::time_t now = std::time(nullptr)) {
  namespace fs = std::filesystem;
  static const std::regex name("w[0-9]{1,15}-pose\\.json");
  std::error_code error;
  int moved = 0;
  std::vector<fs::path> candidates;
  for (fs::directory_iterator it(folder, error), end; !error && it != end; it.increment(error))
    if (std::regex_match(it->path().filename().string(), name)) candidates.push_back(it->path());
  for (const auto &path : candidates) {
    Placement ignored;
    if (loadUserPose(path.string(), {}, ignored) == PoseCheck::Unverified && setAsidePose(path.string(), now)) ++moved;
  }
  return moved;
}
} // namespace fw
