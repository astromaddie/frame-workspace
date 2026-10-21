#pragma once
#include "wall.hpp"
namespace fw {
inline void selfTestWall() {
  auto check = [](bool ok, const char *why) { if (!ok) throw std::runtime_error(why); };
  const auto identity = spatialIdentity();
  const int columns[]{0,-1,1,0,-1,1,-2,2,-2,2,-3,3,-3,3};
  for (int i = 0; i < 14; ++i) {
    auto p = wallPlacement(i, 1.65f, identity);
    const auto &m = p.matrix.m;
    check(std::abs(std::hypot(m[0][3], m[1][3], m[2][3]) - wallDistance) < 1e-5, "wall radius");
    check(std::abs(std::atan2(m[0][3], -m[2][3]) * 180 / M_PI - columns[i] * 25) < 1e-4, "wall slot order");
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
  for (int i=0;i<14;++i) {
    auto p=wallPlacement(i,wallWidth/wallHeight,identity);
    HandleLayout l; l.width=p.width; l.height=wallHeight;
    float edge=l.handleOffset()+HandleLayout::rodWide/2;
    cells.push_back(bounds(p,l.width+2*edge,l.rowY()-l.discDiameter()/2,l.height/2+edge,controlsDepth));
  }
  for (size_t i=0;i<cells.size();++i) for (size_t j=0;j<i;++j) {
    auto a=cells[i], b=cells[j];
    check(a.left>b.right || b.left>a.right || a.bottom>b.top || b.bottom>a.top, "neighbour chrome clearance");
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
