#pragma once
#include <cmath>
#include <initializer_list>

namespace fw {
struct EyeRay { float origin[3]{}, direction[3]{}; };
// No OpenVR types cross this boundary. The optional v2.15.6 ABI is compiled in
// gaze_bridge.cpp only; the overlay continues using the pinned v2.5.1 ABI.
class GazeBridge {
  void *system = nullptr;
public:
  explicit GazeBridge(void *alreadyInitializedLibrary);
  const char *sample(EyeRay (&rays)[2]);
};
// Invert a homogeneous projection, then transform its ray through eye-to-head
// and standing head pose. Exposed here for deterministic coordinate tests.
inline bool eyeRay(const float *projection, const float *eye, const float *head,
                   float ndcX, float ndcY, EyeRay &out) {
  if (!std::isfinite(ndcX) || !std::isfinite(ndcY) ||
      ndcX < -1 || ndcX > 1 || ndcY < -1 || ndcY > 1) return false;
  double a[4][8]{};
  for (int r=0;r<4;++r) for(int c=0;c<4;++c) {
    if (!std::isfinite(projection[r*4+c])) return false;
    a[r][c]=projection[r*4+c]; a[r][c+4]=r==c;
  }
  for(int c=0;c<4;++c) {
    int pivot=c;
    for(int r=c+1;r<4;++r) if(std::abs(a[r][c])>std::abs(a[pivot][c])) pivot=r;
    if(std::abs(a[pivot][c])<1e-10) return false;
    for(int k=0;k<8;++k) { double t=a[c][k];a[c][k]=a[pivot][k];a[pivot][k]=t; }
    double divisor=a[c][c]; for(double &v:a[c]) v/=divisor;
    for(int r=0;r<4;++r) if(r!=c) {
      double factor=a[r][c];for(int k=0;k<8;++k) a[r][k]-=factor*a[c][k];
    }
  }
  const double clip[4]{ndcX, ndcY, .5, 1}; double point[4]{};
  for(int r=0;r<4;++r) for(int c=0;c<4;++c) point[r]+=a[r][c+4]*clip[c];
  if(!std::isfinite(point[3]) || std::abs(point[3])<1e-10) return false;
  double dir[3]{point[0]/point[3],point[1]/point[3],point[2]/point[3]};
  if(dir[2]>=0) return false;
  double hd[3]{},ho[3]{};
  for(int r=0;r<3;++r) {
    for(int c=0;c<4;++c) if(!std::isfinite(eye[r*4+c]) || !std::isfinite(head[r*4+c])) return false;
    ho[r]=eye[r*4+3]; for(int c=0;c<3;++c) hd[r]+=eye[r*4+c]*dir[c];
  }
  double norm=0;
  for(int r=0;r<3;++r) {
    double origin=head[r*4+3],direction=0;
    for(int c=0;c<3;++c) { origin+=head[r*4+c]*ho[c];direction+=head[r*4+c]*hd[c]; }
    out.origin[r]=float(origin);out.direction[r]=float(direction);norm+=direction*direction;
  }
  if(!std::isfinite(norm) || norm<1e-12) return false;
  for(int r=0;r<3;++r) {
    out.direction[r]/=float(std::sqrt(norm));
    if(!std::isfinite(out.origin[r]) || !std::isfinite(out.direction[r])) return false;
  }
  return true;
}
// Calibrate returned runtime UV orientation from four geometry-only rays at
// local left/right and upper/lower positions. This is not gaze substitution.
inline bool uvOrientation(float leftU, float rightU, float upperV, float lowerV,
                          bool &flipX, bool &flipY) {
  for(float value:{leftU,rightU,upperV,lowerV})
    if(!std::isfinite(value) || value<0 || value>1) return false;
  if(std::abs(leftU-rightU)<.01f || std::abs(upperV-lowerV)<.01f) return false;
  flipX=leftU>rightU;flipY=upperV>lowerV;return true;
}
} // namespace fw
