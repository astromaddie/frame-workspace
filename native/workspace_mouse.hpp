#pragma once
#include "placement.hpp"
#include "gaze_bridge.hpp"

namespace fw {
// Remembered gaze cursor placement is subordinate to newer physical input,
// including packets received by the control thread but not yet applied.
struct MouseGazeRestore {
  std::string key;uint64_t token=0,sequence=0;
  void requested(const std::string &panel,uint64_t request,uint64_t motion){key=panel;token=request;sequence=motion;}
  bool acknowledge(const std::string &panel,uint64_t request,uint64_t applied,uint64_t received) {
    if(!token||panel!=key||request!=token)return false;
    token=0;return applied==sequence&&received==sequence;
  }
};
// World-space point, never a head-relative angular offset. Head changes alone
// cannot move it. The host only calls motion for a new cumulative snapshot.
struct WorkspacePointer {
  std::array<float, 3> point{0, 1.3f, -2};
  double x = 0, y = 0, lastMotionMs = -1e9;
  float depth = 2;
  uint64_t sequence = 0;
  std::string session, host;
  void begin(std::string s, std::string h, const HmdMatrix34_t &head) {
    session = std::move(s); host = std::move(h); x = y = 0; sequence = 0;
    recenter(head);
  }
  void recenter(const HmdMatrix34_t &head) {
    for (int r = 0; r < 3; ++r) point[r] = head.m[r][3] - head.m[r][2] * depth;
  }
  bool motion(const std::string &s, const std::string &h, uint64_t q,
              double absoluteX, double absoluteY, const HmdMatrix34_t &head, double now) {
    if (s != session || h != host || !q || q <= sequence || !std::isfinite(absoluteX) || !std::isfinite(absoluteY)) return false;
    const double dx = absoluteX - x, dy = absoluteY - y;
    x = absoluteX; y = absoluteY; sequence = q;
    // 1000 physical pixels cover one metre at two metres depth.
    const float scale = depth * .0005f;
    for (int r = 0; r < 3; ++r) point[r] += float(dx * head.m[r][0] - dy * head.m[r][1]) * scale;
    if (dx != 0 || dy != 0) lastMotionMs = now;
    return true;
  }
  bool gazeMayDwell(double now, bool held, bool menu, bool placing) const {
    return !held && !menu && !placing && now - lastMotionMs >= 1000;
  }
};

// Every update starts from the captured pose and absolute cumulative mouse
// coordinates. Frame rate and dropped intermediate snapshots cannot add drift.
class MousePlacementGesture {
  std::array<float,3> origin{}, normal{}, grabbed{}, depthRay{}, headOrigin{}, viewUp{};
  float height = 1, aspect = 1, initialDepth = 2;
  int cornerX = 1, cornerY = 1;
  static float dot(const std::array<float,3> &a, const std::array<float,3> &b) {
    return a[0]*b[0]+a[1]*b[1]+a[2]*b[2];
  }
  bool intersect(const EyeRay &ray, std::array<float,3> &point) const {
    std::array<float,3> direction{ray.direction[0],ray.direction[1],ray.direction[2]}, offset{};
    for(int r=0;r<3;++r) offset[r]=origin[r]-ray.origin[r];
    const float denominator=dot(direction,normal);
    if(!std::isfinite(denominator)||std::fabs(denominator)<1e-5f)return false;
    const float t=dot(offset,normal)/denominator;
    if(!std::isfinite(t)||t<=0)return false;
    for(int r=0;r<3;++r)point[r]=ray.origin[r]+ray.direction[r]*t;
    return true;
  }
public:
  enum class Kind { Move, Depth, Resize };
  bool active = false;
  Placement start;
  Kind kind = Kind::Move;
  bool begin(Kind k, const Placement &p, const HmdMatrix34_t &head,
             const EyeRay &ray, float panelHeight, int sx=1, int sy=1) {
    if(panelHeight<=0)return false;
    kind=k;start=p;height=panelHeight;aspect=p.width/height;cornerX=sx<0?-1:1;cornerY=sy<0?-1:1;
    for(int r=0;r<3;++r) {
      origin[r]=p.matrix.m[r][3];headOrigin[r]=head.m[r][3];
      normal[r]=k==Kind::Resize?p.matrix.m[r][2]:head.m[r][2];
      viewUp[r]=head.m[r][1];depthRay[r]=origin[r]-headOrigin[r];
    }
    initialDepth=std::sqrt(dot(depthRay,depthRay));
    if(initialDepth<.01f)return false;
    for(auto &v:depthRay)v/=initialDepth;
    active=intersect(ray,grabbed);return active;
  }
  Placement update(const EyeRay &ray, double cumulativeWheel=0) const {
    Placement result=start;std::array<float,3> point{},delta{};
    if(!active||!std::isfinite(cumulativeWheel)||!intersect(ray,point))return result;
    for(int r=0;r<3;++r)delta[r]=point[r]-grabbed[r];
    if(kind==Kind::Move) {
      for(int r=0;r<3;++r)result.matrix.m[r][3]+=delta[r];
    } else if(kind==Kind::Depth) {
      result.distance=std::clamp(initialDepth-dot(delta,viewUp)+float(cumulativeWheel)*.001f,.4f,10.f);
      for(int r=0;r<3;++r)result.matrix.m[r][3]+=depthRay[r]*(result.distance-initialDepth);
    } else {
      float dx=0,dy=0;
      for(int r=0;r<3;++r){dx+=delta[r]*start.matrix.m[r][0];dy+=delta[r]*start.matrix.m[r][1];}
      const float requested=(cornerX*dx+cornerY*dy/aspect)/(1+1/(aspect*aspect));
      result.width=std::clamp(start.width+requested,.4f,8.f);
      const float dw=result.width-start.width,dh=dw/aspect;
      // Keep the opposite corner fixed in the original panel plane.
      for(int r=0;r<3;++r)result.matrix.m[r][3]+=.5f*(cornerX*dw*start.matrix.m[r][0]+cornerY*dh*start.matrix.m[r][1]);
    }
    return result;
  }
  Placement cancel() {active=false;return start;}
  Placement finish(const EyeRay &ray,double cumulativeWheel=0) {
    auto result=update(ray,cumulativeWheel);active=false;return result;
  }
};
} // namespace fw
