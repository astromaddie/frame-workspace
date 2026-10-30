#pragma once
#include "launcher_render.hpp"
#include "session.hpp"
#include "host.hpp"
namespace fw {
// A compact shared dock, one selected window's affordances, and an edge beacon.
// These are required overlays: capture is not ready if creation/submission fails.
class WorkspaceMouseView {
  struct Layer {VROverlayHandle_t handle=0;GLuint texture=0;std::string signature;};
  Layer dock,frame,beacon;
  std::shared_ptr<OverlaySession> session;
  LauncherPainter painter;
  void draw(Layer &layer,const char *name,const WorkspacePicture &picture,const std::string &hot,
            const Placement &placement,float height,uint32_t order) {
    std::string signature=std::to_string(picture.width)+":"+std::to_string(picture.height)+hot;
    for(const auto &w:picture.widgets)signature+=w.label+":"+std::to_string(w.selected);
    if(signature!=layer.signature) {
      auto bytes=painter.workspacePixels(picture,hot);
      if(!layer.texture)glGenTextures(1,&layer.texture);
      glBindTexture(GL_TEXTURE_2D,layer.texture);
      glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MIN_FILTER,GL_LINEAR);
      glTexParameteri(GL_TEXTURE_2D,GL_TEXTURE_MAG_FILTER,GL_LINEAR);
      glTexImage2D(GL_TEXTURE_2D,0,GL_RGBA8,picture.width,picture.height,0,GL_RGBA,GL_UNSIGNED_BYTE,bytes.data());
      layer.signature=signature;
    }
    if(session->noVR)return;
    if(!layer.handle) {
      auto key="com.frame-workspace.mouse."+std::to_string(getpid())+"."+name;
      if(session->createOverlay(key,"Frame Workspace",&layer.handle))throw std::runtime_error("workspace controls unavailable");
      VRTextureBounds_t bounds{0,1,1,0};
      session->overlay->SetOverlayTextureBounds(layer.handle,&bounds);
      session->overlay->SetOverlayFlag(layer.handle,VROverlayFlags_IsPremultiplied,true);
    }
    (void)height;
    auto pose=placement.matrix;
    if(session->overlay->SetOverlaySortOrder(layer.handle,order) ||
       session->overlay->SetOverlayWidthInMeters(layer.handle,placement.width) ||
       session->overlay->SetOverlayTransformAbsolute(layer.handle,ETrackingUniverseOrigin_TrackingUniverseStanding,&pose))
      throw std::runtime_error("workspace control placement failed");
    Texture_t texture{reinterpret_cast<void *>(uintptr_t(layer.texture)),ETextureType_TextureType_OpenGL,EColorSpace_ColorSpace_Gamma};
    glFlush();
    if(session->overlay->SetOverlayTexture(layer.handle,&texture)||session->overlay->ShowOverlay(layer.handle))
      throw std::runtime_error("workspace control submission failed");
  }
  void hide(Layer &l){if(l.handle&&!session->noVR)session->overlay->HideOverlay(l.handle);}
public:
  WorkspacePicture dockPicture,framePicture;
  Placement dockPose,framePose;
  float dockHeight=0,frameHeight=0;
  std::string frameKey;
  explicit WorkspaceMouseView(std::shared_ptr<OverlaySession> s):session(std::move(s)){}
  ~WorkspaceMouseView(){for(auto *l:{&dock,&frame,&beacon}){if(l->handle)session->destroyOverlay(l->handle);if(l->texture)glDeleteTextures(1,&l->texture);}}
  void layout(const HmdMatrix34_t &head,bool arrange,bool menu,bool placing,bool active,
              const std::string &key,const Placement *window,float windowHeight,const std::string &title,bool reduced,
              bool choosing=false,const std::vector<std::string> &choices={},size_t page=0,const std::string &notice="") {
    dockPicture=workspaceDock(arrange,menu,placing,active,choosing,choices,page,notice);dockPose.matrix=head;dockPose.width=.98f;
    for(int r=0;r<3;++r)dockPose.matrix.m[r][3]+=-head.m[r][2]*1.25f+head.m[r][1]*(-.40f+(dockPicture.height-184)*.98f/840/2);
    dockHeight=dockPose.width*dockPicture.height/dockPicture.width;
    frameKey=key;
    if(window) {
      framePicture=workspaceFrame(window->width/windowHeight,title,arrange,reduced);
      framePose=*window;framePose.curvature=framePose.bendA=framePose.bendB=0;framePose.bendVertical=false;
      framePose.width=window->width*1.1f;frameHeight=window->width*framePicture.height/1000;
      // Content opening begins at y=90, with 42 below: center it on the window.
      for(int r=0;r<3;++r)framePose.matrix.m[r][3]+=window->matrix.m[r][1]*window->width*.024f+window->matrix.m[r][2]*.012f;
    }
  }
  std::pair<std::string,std::string> hit(const EyeRay &ray)const {
    auto h=surfacePick(ray,{"dock",dockPose,dockHeight});
    if(h){auto action=dockPicture.hit(h.u,h.v);if(!action.empty())return {action,""};}
    if(!frameKey.empty()) {
      h=surfacePick(ray,{frameKey,framePose,frameHeight});
      if(h){auto action=framePicture.hit(h.u,h.v);if(!action.empty())return {action,frameKey};}
    }
    return {};
  }
  void render(const std::string &hot,bool offscreen,const HmdMatrix34_t &head,const std::array<float,3> &point) {
    draw(dock,"dock",dockPicture,hot,dockPose,dockHeight,210);
    if(!frameKey.empty())draw(frame,"frame",framePicture,hot,framePose,frameHeight,205);else hide(frame);
    if(offscreen) {
      float x=0,y=0,z=0;
      for(int r=0;r<3;++r){auto d=point[r]-head.m[r][3];x+=head.m[r][0]*d;y+=head.m[r][1]*d;z-=head.m[r][2]*d;}
      if(std::hypot(x,y)<.001f)x=1;
      auto pose=dockPose;pose.width=.40f;pose.matrix=head;
      float norm=std::max(std::abs(x)/.72f,std::abs(y)/.43f);x/=norm;y/=norm;
      for(int r=0;r<3;++r)pose.matrix.m[r][3]+=-head.m[r][2]*1.1f+head.m[r][0]*x+head.m[r][1]*y;
      std::string direction=std::abs(x)>std::abs(y)?(x>0?"→":"←"):(y>0?"↑":"↓");
      if(z<=0)direction+=" Behind";
      const auto p=workspaceBeacon(direction);
      draw(beacon,"beacon",p,"",pose,.075f,215);
    }else hide(beacon);
  }
  void hide(){hide(dock);hide(frame);hide(beacon);}
};
} // namespace fw
