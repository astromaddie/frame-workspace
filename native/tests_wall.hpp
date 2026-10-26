#pragma once
#include "wall.hpp"
#include <fstream>
#include <unistd.h>
namespace fw {
inline void selfTestWall() {
  auto check = [](bool ok, const char *why) { if (!ok) throw std::runtime_error(why); };
  const auto identity = spatialIdentity();
  // Slots 0-13 keep their order; 14-20 the top row in front; 21-32 the far
  // columns below it; 33-36 the top row's far columns.
  const int yaws[]{0,-25,25,0,-25,25,-50,50,-50,50,-75,75,-75,75, 0,-30,30,-60,60,-90,90,
                   -100,100,-100,100,-125,125,-125,125,-150,150,-150,150, -120,120,-150,150};
  const int rows[]{0,0,0,1,1,1,0,0,1,1,0,0,1,1, 2,2,2,2,2,2,2, 0,0,1,1,0,0,1,1,0,0,1,1, 2,2,2,2};
  static_assert(sizeof yaws / sizeof *yaws == wallRingCells && sizeof rows / sizeof *rows == wallRingCells);
  for (int i = 0; i < wallRingCells; ++i) {
    auto p = wallPlacement(i, 1.65f, identity);
    const auto &m = p.matrix.m;
    check(std::abs(std::hypot(m[0][3], m[1][3], m[2][3]) - wallDistance) < 1e-5, "wall radius");
    check(std::abs(std::atan2(m[0][3], -m[2][3]) * 180 / M_PI - yaws[i]) < 1e-3, "wall slot order");
    check(std::abs(std::atan2(m[1][3], std::hypot(m[0][3], m[2][3])) * 180 / M_PI - wallRowDegrees[rows[i]]) < 1e-3,
          "wall row order");
    for (int r = 0; r < 3; ++r)
      check(std::abs(m[r][2] + m[r][3] / wallDistance) < 1e-5, "wall faces anchor");
    Placement roundtrip;
    check(parsePlacement(placementJSON(p), roundtrip), "wall rigid pose");
  }
  for (float aspect : {.08f, .5f, 1.f, 1.65f, 3.5f}) {
    auto p = wallPlacement(0, aspect, identity);
    check(p.width <= wallWidth + 1e-6 && p.width / aspect <= wallHeight + 1e-6, "aspect fits cell");
    Placement restored;
    check(parsePlacement(placementJSON(p), restored) && std::abs(restored.width - p.width) < 1e-6, "portrait pose roundtrip");
  }
  WallAnchor anchor;
  auto head = identity; head.m[0][3] = 2; head.m[1][3] = 1.7f; head.m[2][3] = 3;
  Placement turned; turned.matrix = head; rotatePlacement(turned, 35, 20, 12);
  anchor.capture(turned.matrix);
  auto first = wallPlacement(8, 1, anchor.matrix);
  anchor.capture(identity);
  auto later = wallPlacement(8, 1, anchor.matrix);
  check(placementJSON(first) == placementJSON(later), "later panels keep initial anchor");
  check(anchor.matrix.m[1][0] == 0 && anchor.matrix.m[1][1] == 1 && anchor.matrix.m[1][2] == 0 &&
        anchor.matrix.m[1][3] == 1.7f && anchor.matrix.m[0][3] == 2, "anchor level and positioned");
  // Conservative visible-chrome bounds: wide rods, bezel halo, pill and disc.
  // Transparent raster padding is excluded. Project at the actual controls depth.
  struct Bounds { float left=100, right=-100, bottom=100, top=-100; };
  auto bounds = [&](const Placement &p, float width, float bottom, float top, float depth) {
    Bounds b;
    for (float x : {-width/2, width/2}) for (float y : {bottom, top}) {
      auto t = identity; t.m[0][3]=x; t.m[1][3]=y; t.m[2][3]=depth;
      auto m = multiply(p.matrix,t);
      float yaw=std::atan2(m.m[0][3],-m.m[2][3]);
      float pitch=std::atan2(m.m[1][3],std::hypot(m.m[0][3],m.m[2][3]));
      b.left=std::min(b.left,yaw); b.right=std::max(b.right,yaw);
      b.bottom=std::min(b.bottom,pitch); b.top=std::max(b.top,pitch);
    }
    return b;
  };
  std::vector<Bounds> cells;
  for (int i=0;i<wallRingCells;++i) {
    auto p=wallPlacement(i,wallWidth/wallHeight,identity);
    HandleLayout l; l.width=p.width; l.height=wallHeight;
    float edge=l.handleOffset()+HandleLayout::rodWide/2;
    cells.push_back(bounds(p,l.width+2*edge,l.rowY()-l.discDiameter()/2,l.height/2+edge,controlsDepth));
  }
  for (size_t i=0;i<cells.size();++i) for (size_t j=0;j<i;++j) {
    auto a=cells[i], b=cells[j];
    check(a.left>b.right || b.left>a.right || a.bottom>b.top || b.bottom>a.top, "neighbour chrome clearance");
  }
  // A later ring keeps the grid's angles, 1 m further out and scaled to match.
  {
    auto near = wallPlacement(5, 1.6f, identity), far = wallPlacement(5 + wallRingCells, 1.6f, identity);
    const auto &a = near.matrix.m, &b = far.matrix.m;
    check(std::abs(std::hypot(b[0][3], b[1][3], b[2][3]) - wallDistance - 1) < 1e-4, "second ring distance");
    check(std::abs(std::atan2(a[0][3], -a[2][3]) - std::atan2(b[0][3], -b[2][3])) < 1e-5, "second ring angle");
    check(std::abs(far.width / near.width - (wallDistance + 1) / wallDistance) < 1e-4, "second ring scale");
    check(wallCellDistance(1023) <= wallDistance + wallRings - 1, "rings stop growing");
  }
  // Panel size follows the Mac window: one points-to-metres scale, capped to
  // the cell, with a floor so small windows stay usable.
  check(std::abs(wallPanelWidth(1.6f, 1280, 800) - .64f) < 1e-5, "full-cell window");
  check(std::abs(wallPanelWidth(1.6f, 640, 400) - .32f) < 1e-5, "half-size window keeps text size");
  check(std::abs(wallPanelWidth(1.6f, 2560, 1600) - .64f) < 1e-5, "large window capped to its cell");
  check(std::abs(wallPanelWidth(.6f, 720, 1200) - .24f) < 1e-5, "portrait window fits the cell height");
  check(std::abs(wallPanelWidth(2, 120, 60) - wallMinimumSide) < 1e-5, "tiny window floor");
  check(std::abs(wallPanelWidth(1.6f, 0, 0) - .64f) < 1e-5, "unknown size fills the cell");
  check(std::abs(wallPlacement(0, 1.6f, identity, 640, 400).width - .32f) < 1e-5, "wall placement uses points");

  // Allocation: every window gets its own cell, none on top of another.
  auto footprint = [&](const Placement &p, float aspect) { return panelWallBounds(p, p.width / aspect, identity); };
  {
    std::vector<int> taken;
    std::vector<WallBounds> placed;
    for (int i = 0; i < 60; ++i) {
      const int cell = wallAllocate(0, {}, taken);
      check(std::find(taken.begin(), taken.end(), cell) == taken.end(), "no shared cell");
      taken.push_back(cell);
      const float aspect = i % 3 == 1 ? .6f : 1.6f;
      auto p = wallPlacement(cell, aspect, identity, i % 2 ? 900 : 1600, i % 2 ? 600 : 1000);
      if (cell < wallRingCells) {
        auto b = footprint(p, aspect);
        for (const auto &o : placed) check(!wallOverlap(b, o), "allocated panels never overlap");
        placed.push_back(b);
      }
    }
    check(taken[0] == 0 && taken[1] == 1 && taken[2] == 2 && taken[13] == 13 && taken[14] == 14, "wall order kept");
    check(int(std::set<int>(taken.begin(), taken.end()).size()) == 60, "sixty windows, sixty cells");
    check(taken[wallRingCells] == wallRingCells, "the next ring after a full one");
  }
  // A companion slot is kept while its cell is free.
  check(wallAllocate(8, {}, {0}) == 8 && wallAllocate(8, {}, {8}) == 0, "preferred slot honoured if free");
  // Hand-placed panels occupy what they cover: the centre one pushes newcomers
  // aside, a big one in front blocks several cells, one far off blocks none.
  {
    auto user = wallPlacement(0, 1.6f, identity); movePlacement(user, .05f, -.03f, 0);
    std::vector<WallBounds> occupied{footprint(user, 1.6f)};
    const int cell = wallAllocate(0, occupied, {});
    check(cell == 1, "user panel in the centre cell");
    check(!wallOverlap(footprint(wallPlacement(cell, 1.6f, identity), 1.6f), occupied[0]), "newcomer clear of user panel");
    Placement big; big.width = 2.4f; big.matrix = identity; big.matrix.m[2][3] = -2;
    occupied = {footprint(big, 1.6f)};
    std::vector<int> taken;
    for (int i = 0; i < 4; ++i) {
      const int c = wallAllocate(-1, occupied, taken);
      taken.push_back(c);
      check(!wallOverlap(wallCellBounds(c), occupied[0]), "no cell behind a big user panel");
    }
    check(taken[0] != 0, "big panel takes the centre");
    // The stale test poses: about 100 degrees to the left and 3 m up.
    Placement aside; aside.width = .8f; aside.matrix = identity;
    aside.matrix.m[0][3] = -1.75f * std::sin(100 * float(M_PI) / 180); aside.matrix.m[1][3] = 3;
    aside.matrix.m[2][3] = -1.75f * std::cos(100 * float(M_PI) / 180);
    occupied = {footprint(aside, 1.6f)};
    check(wallAllocate(0, occupied, {}) == 0, "far-off panel leaves the front free");
  }
  // Arrange: focused first, then most recently focused, then previous order.
  {
    std::vector<WallArrangeItem> items{{false, 0, 4}, {false, 7, 9}, {true, 3, 6}, {false, 0, 1}, {false, 9, 2}};
    auto order = wallArrangeOrder(items);
    check((order == std::vector<size_t>{2, 4, 1, 3, 0}), "arrange order");
    WallAnchor turned; turned.capture(identity);
    Placement facing; facing.matrix = identity; rotatePlacement(facing, 60, 0, 0);
    turned.recapture(facing.matrix);
    auto front = wallPlacement(0, 1.6f, turned.matrix);
    check(std::abs(std::atan2(front.matrix.m[0][3], -front.matrix.m[2][3]) * 180 / M_PI + 60) < 1e-3,  // a 60-degree turn to the left
          "arrange re-anchors the wall where the user faces");
  }

  // Remembered poses: only user-set ones for this window identity, and
  // provenance-less files are moved aside (never deleted).
  {
    namespace fs = std::filesystem;
    const auto folder = fs::temp_directory_path() / ("fw-wall-test-" + std::to_string(getpid()));
    fs::remove_all(folder); fs::create_directories(folder);
    auto write = [&](const std::string &name, const std::string &text) {
      std::ofstream(folder / name) << text;
      return (folder / name).string();
    };
    auto p = wallPlacement(3, 1.6f, identity);
    PoseIdentity terminal{"com.apple.Terminal", "Shell", "s1"};
    Placement out;
    const auto user = write("w42-pose.json", userPoseJSON(p, terminal, 1700000000));
    check(loadUserPose(user, terminal, out) == PoseCheck::Restored && std::abs(out.width - p.width) < 1e-5,
          "user pose restored");
    check(loadUserPose(user, {"com.apple.Terminal", "shell", ""}, out) == PoseCheck::Restored, "title case-folded");
    check(loadUserPose(user, {"com.google.Chrome", "Shell", ""}, out) == PoseCheck::OtherWindow, "other app ignored");
    check(loadUserPose(user, {"com.apple.Terminal", "Other", ""}, out) == PoseCheck::OtherWindow, "other title ignored");
    check(loadUserPose(user, {"", "", ""}, out) == PoseCheck::Restored, "unknown identity accepts");
    const auto legacy = write("w7-pose.json", placementJSON(p));
    check(loadUserPose(legacy, terminal, out) == PoseCheck::Unverified, "provenance-less pose ignored");
    {
      auto loose = userPoseJSON(p, terminal);
      loose.replace(loose.find("\"userSet\":true"), 14, "\"userSet\":1");  // only a real true counts
      write("w8-pose.json", loose);
    }
    write("w9-pose.json", "not json");
    write("workspace-pose.json", placementJSON(p));
    write("spatial-shelf-pose.json", placementJSON(p));
    check(loadUserPose((folder / "missing.json").string(), terminal, out) == PoseCheck::Missing, "missing pose");
    std::time_t day = 1793000000;  // 2026-10-26
    check(sweepUnverifiedPoses(folder.string(), day) == 3, "three unverified window poses moved");
    char stamp[16]; std::tm local{}; localtime_r(&day, &local); std::strftime(stamp, sizeof stamp, "%Y-%m-%d", &local);
    const auto aside = folder / (std::string("set-aside-") + stamp);
    check(fs::exists(aside / "w7-pose.json") && fs::exists(aside / "w8-pose.json") && fs::exists(aside / "w9-pose.json"),
          "moved into the dated folder");
    check(fs::exists(folder / "w42-pose.json") && fs::exists(folder / "workspace-pose.json") &&
          fs::exists(folder / "spatial-shelf-pose.json"), "user, Workspace and shelf poses stay");
    write("w7-pose.json", placementJSON(p));
    check(setAsidePose((folder / "w7-pose.json").string(), day) && fs::exists(aside / "w7-pose-1.json"),
          "a second set-aside never overwrites");
    fs::remove_all(folder);
  }
  SpatialState state; state.spatial=true; state.apps.push_back({});
  auto tiles=spatialLayout(state,spatialAnchor(identity,1.3f,0));
  SurfaceMessage status; status.rect={0,0,330,30};
  auto strip=statusStripPlacement(tiles,status,.001f);
  auto shelf=tiles.front().surface;
  for (auto surface : {shelf,strip}) {
    auto b=bounds(surface.placement,surface.placement.width,-surface.height/2,surface.height/2,0);
    for (const auto &cell:cells) check(cell.bottom>b.top, "wall above shelf and status");
  }
  for (auto raw : {"-1","1.5","true","1024","null"}) {
    bool rejected=false;
    try { wallSlot(json(std::string("{\"slot\":")+raw+"}")); } catch (...) { rejected=true; }
    check(rejected,"invalid wall slot rejected");
  }
}
} // namespace fw
