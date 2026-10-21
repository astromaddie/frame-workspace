#pragma once
#include "bend.hpp"
#include <array>

namespace fw {
inline constexpr float frameDepth = .03f, pictureDepth = frameDepth, controlsDepth = frameDepth + .002f;
// Four planar overlays enclose the frame. Arc facets on either long edge are
// coplanar, so their silhouette shares one texture instead of one overlay each.
struct FrameWall {
  HmdMatrix34_t local{};
  float width = 0, height = 0;
  std::vector<std::array<float,2>> polygon;
  static constexpr int maxFacets = 32, maxPoints = 2 * (maxFacets + 1);
  static constexpr float chordError = .001f;
  int facets = 1;
  float bevelRadius = frameDepth / 2;
  float depthDirection = 1;
  // Subpixel coverage overlap hides the junction of planar overlay textures.
  static constexpr float joinOverlap = .0003f;
  bool contains(float x, float y) const {
    bool in = false;
    for (size_t i=0,j=polygon.size()-1;i<polygon.size();j=i++) {
      const auto &a=polygon[i], &b=polygon[j];
      if ((a[1]>y)!=(b[1]>y) && x<(b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]) in=!in;
    }
    // Include the shared boundary itself: adjoining support planes meet
    // exactly here, with no alpha cutout and no protruding overlap tab.
    if(!in)for(size_t i=0;i<polygon.size();++i) {
      const auto &a=polygon[i],&b=polygon[(i+1)%polygon.size()];
      float dx=b[0]-a[0],dy=b[1]-a[1],length=dx*dx+dy*dy;
      float t=length>0?std::clamp(((x-a[0])*dx+(y-a[1])*dy)/length,0.f,1.f):0;
      if(std::hypot(x-a[0]-t*dx,y-a[1]-t*dy)<1e-6f)return true;
    }
    return in;
  }
};
inline std::array<FrameWall,4> frameWalls(const BendShape &shape, const HandleLayout &l) {
  // A closed rectangular rear shell sits beneath the rounded front lip.
  // The four planes meet exactly; material normals round their edge lighting.
  const float lip=l.bezel()*.3f;
  const float along=(shape.vertical?l.height:l.width)/2+lip;
  const float across=(shape.vertical?l.width:l.height)/2+lip;
  // Bound chord sagitta to 1 mm on either radius, keeping all samples in the
  // same plane/overlay. A fixed angular step produced visible 17 mm tabs.
  int facets=1;
  for(int h=0;h<2;++h)if(shape.radius(h)>0) {
    const double R=shape.radius(h);
    double step=2*R*std::acos(std::clamp(1-FrameWall::chordError/R,-1.,1.));
    facets=std::max(facets,int(std::ceil(2*along/step)));
  }
  if(shape.split())facets=2*((facets+1)/2); // Preserve the exact bend seam.
  facets=std::min(facets,FrameWall::maxFacets);
  std::array<FrameWall,4> out;
  for(int edge=0;edge<2;++edge) {
    auto &w=out[edge]; w.facets=facets;
    const float sign=edge?1.f:-1.f;
    // Local x follows the bend; local y is depth. Normal points out of the frame.
    if(!shape.vertical) w.local={{{1,0,0,0},{0,0,sign,sign*across},{0,-sign,0,0}}};
    else w.local={{{0,0,sign,sign*across},{1,0,0,0},{0,sign,0,0}}};
    w.depthDirection = shape.vertical ? sign : -sign;
    auto inv=inverseRigid(w.local);
    for(int face=0;face<2;++face) for(int i=0;i<=facets;++i) {
      float s=-along+2*along*(face?facets-i:i)/facets;
      auto p=bendPoint(shape,s,sign*across,face?0.f:frameDepth);
      auto q=multiply(inv,p);
      w.polygon.push_back({q.m[0][3],q.m[1][3]});
    }
  }
  for(int edge=0;edge<2;++edge) {
    auto &w=out[edge+2]; const float sign=edge?1.f:-1.f;
    auto tangent=bendPoint(shape,sign*along,0,0);
    HmdMatrix34_t turn=!shape.vertical?HmdMatrix34_t{{{0,0,sign,0},{1,0,0,0},{0,sign,0,0}}}:
        HmdMatrix34_t{{{1,0,0,0},{0,0,sign,0},{0,-sign,0,0}}};
    w.local=multiply(tangent,turn);
    w.depthDirection = shape.vertical ? -sign : sign;
    auto inv=inverseRigid(w.local);
    for(auto yz: {std::array<float,2>{-across,0},{across,0},{across,frameDepth},{-across,frameDepth}}) {
      auto q=multiply(inv,bendPoint(shape,sign*along,yz[0],yz[1]));
      w.polygon.push_back({q.m[0][3],q.m[1][3]});
    }
  }
  for(auto &w:out) {
    float lo[2]={1e6f,1e6f},hi[2]={-1e6f,-1e6f};
    for(auto p:w.polygon) for(int k=0;k<2;++k){lo[k]=std::min(lo[k],p[k]);hi[k]=std::max(hi[k],p[k]);}
    w.width=hi[0]-lo[0]+2*FrameWall::joinOverlap;w.height=hi[1]-lo[1]+2*FrameWall::joinOverlap;
    for(int r=0;r<3;++r) w.local.m[r][3]+=w.local.m[r][0]*(lo[0]+hi[0])/2+w.local.m[r][1]*(lo[1]+hi[1])/2;
    for(auto &p:w.polygon) for(int k=0;k<2;++k)p[k]-=(lo[k]+hi[k])/2;
  }
  return out;
}
inline bool frameWallHit(const BendShape &shape,const HandleLayout &l,const float o[3],const float d[3],
                         float &x,float &y,float &distance,HmdMatrix34_t *point=nullptr) {
  // Picture ownership wins, including oblique rays through the front opening.
  float px,py,pt;
  if(bendHit(shape,o,d,pictureDepth,px,py,&pt) && std::fabs(px)<l.width/2 && std::fabs(py)<l.height/2) return false;
  bool hit=false;
  for(const auto &w:frameWalls(shape,l)) {
    auto inv=inverseRigid(w.local);float a[3]{},v[3]{};
    for(int r=0;r<3;++r){a[r]=inv.m[r][3];for(int c=0;c<3;++c){a[r]+=inv.m[r][c]*o[c];v[r]+=inv.m[r][c]*d[c];}}
    if(std::fabs(v[2])<1e-6f)continue;
    float t=-a[2]/v[2];if(t<=0 || (hit && t>=distance))continue;
    float u=a[0]+t*v[0],vv=a[1]+t*v[1];if(!w.contains(u,vv))continue;
    auto p=w.local;for(int r=0;r<3;++r)p.m[r][3]+=p.m[r][0]*u+p.m[r][1]*vv;
    // Recover the closest frame edge in window coordinates for handle reveal.
    float along=shape.vertical?p.m[1][3]:p.m[0][3],z=p.m[2][3];
    int half=along<0?0:1;float R=shape.radius(half);
    float s=R>0?std::atan2(along,R-z)*R:along;
    x=shape.vertical?p.m[0][3]:s;y=shape.vertical?s:p.m[1][3];
    distance=t;hit=true;if(point)*point=p;
  }
  return hit;
}
template<class Handle,class Create,class Destroy>
inline void reconcileHalf(Handle &handle,bool needed,Create create,Destroy destroy) {
  if(!needed){if(handle){destroy(handle);handle=0;}return;}
  if(!handle)handle=create();
}
struct CursorOwnership {
  const void *owner=nullptr;
  bool claim(const void *next,bool allowed){if(!allowed)return false;owner=next;return true;}
  bool release(const void *old){if(owner!=old)return false;owner=nullptr;return true;}
};
inline int windowOverlayCount(const BendShape &b) { return 3*b.segments()+4; }
// Shelf apps/backing, status, four menu slots, strip, cursor, dashboard and thumbnail.
inline int workspaceOverlayCount(int windows,bool split=false,int apps=4,bool splitMenu=false,
                                 int retainedSwitcher=0,bool retainedAppMenu=false) {
  // SpatialRenderer retains its largest tile vector. spatialLayout displays at
  // most seven switcher tiles; three app-menu rows may share that high-water.
  return windows*(split?10:7)+apps+1+1+4+(splitMenu?2:1)+1+2+
      std::min(7,retainedSwitcher)+(retainedAppMenu?3:0);
}
} // namespace fw
