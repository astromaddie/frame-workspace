#pragma once
#include "depth.hpp"
#include "spatial.hpp"
namespace fw {
inline void selfTestDepth() {
 auto check=[](bool ok,const char *why){if(!ok)throw std::runtime_error(why);};
 HandleLayout l;l.width=2;l.height=1.2f;
 for(bool vertical:{false,true})for(auto amounts:{std::array<float,2>{0,0},{.08f,.08f},{.3f,.3f},{.1f,.3f}}) {
  Placement p;p.width=l.width;p.bendVertical=vertical;p.bendA=amounts[0];p.bendB=amounts[1];
  auto b=bendShape(p,l.height);auto walls=frameWalls(b,l);
  check(walls.size()==4,"four real wall overlays for every bend");
  check(walls[0].facets>=1&&walls[0].facets<=4,"wall facets bounded by curvature");
  if(!amounts[0]&&!amounts[1])check(walls[0].facets==1,"flat wall has one facet");
  if(b.split())check(walls[0].facets%2==0,"split wall retains centre seam");
  check(windowOverlayCount(b)==(b.split()?10:7),"overlay count includes live halves and four walls");
  for(const auto &w:walls)for(auto v:w.polygon) {
    auto pt=w.local;for(int r=0;r<3;++r)pt.m[r][3]+=pt.m[r][0]*v[0]+pt.m[r][1]*v[1];
    float along=vertical?pt.m[1][3]:pt.m[0][3],across=vertical?pt.m[0][3]:pt.m[1][3];
    float R=b.radius(along<0?0:1),s=R>0?std::atan2(along,R-pt.m[2][3])*R:along;
    float z=R>0?R-std::hypot(along,R-pt.m[2][3]):pt.m[2][3];
    check(std::fabs(z)<1e-5||std::fabs(z-frameDepth)<1e-5,"wall vertices exactly join rear and front planes");
    check(std::fabs(s)>b.span/2||std::fabs(across)>(vertical?l.width:l.height)/2,"wall vertices stay outside picture");
    float radius=l.bezel()*.7f;
    float qx=std::fabs(s)-b.span/2-l.bezel()+radius;
    float qy=std::fabs(across)-(vertical?l.width:l.height)/2-l.bezel()+radius;
    check(std::hypot(std::max(qx,0.f),std::max(qy,0.f))+std::min(std::max(qx,qy),0.f)-radius<=1e-6f,
          "rear shell vertices stay under rounded front silhouette");
  }
  // Exact support-plane joins remain closed through the entire thickness.
  for(int alongSign:{-1,1})for(int acrossSign:{-1,1})for(float z:{.004f,.0075f,.011f}) {
    auto joint=bendPoint(b,alongSign*(b.span/2+l.bezel()*.3f),
        acrossSign*((vertical?l.width:l.height)/2+l.bezel()*.3f),z);
    for(int edge:{acrossSign<0?0:1,alongSign<0?2:3}) {
      auto point=multiply(inverseRigid(walls[edge].local),joint);
      check(std::fabs(point.m[2][3])<1e-5f && walls[edge].contains(point.m[0][3],point.m[1][3]),
          "recessed shell planes join without corner opening");
    }
  }
  // Actual wall plane rays hit the same polygons submitted to the renderer.
  for(const auto &w:walls) {
    auto mid=w.polygon[0];mid[0]=(mid[0]+w.polygon.back()[0])*.5f;mid[1]=(mid[1]+w.polygon.back()[1])*.5f;
    // Sample an interior point from the first edge quad.
    mid={0,0};for(auto v:w.polygon){mid[0]+=v[0]/w.polygon.size();mid[1]+=v[1]/w.polygon.size();}
    if(!w.contains(mid[0],mid[1]))continue; // Arc strip's bounding-box centre can be empty.
    float o[3],d[3];for(int r=0;r<3;++r){o[r]=w.local.m[r][3]+w.local.m[r][0]*mid[0]+w.local.m[r][1]*mid[1]+w.local.m[r][2]*.05f;d[r]=-w.local.m[r][2];}
    float x,y,t;check(frameWallHit(b,l,o,d,x,y,t),"side ray hits real wall");
  }
  for(float x:{-.8f,0.f,.8f})for(float y:{-.4f,0.f,.4f}) {
    auto pt=bendPointXY(b,x,y,pictureDepth);float o[3],d[3];for(int r=0;r<3;++r){o[r]=pt.m[r][3]+pt.m[r][2];d[r]=-pt.m[r][2];}
    float xx,yy,t;check(!frameWallHit(b,l,o,d,xx,yy,t),"walls never steal picture rays");
  }
 }
 SpatialState ui;ui.spatial=true;ui.open=true;
 for(int i=0;i<4;++i)ui.apps.push_back({"app"+std::to_string(i),"App","",true,false});
 for(int i=0;i<9;++i)ui.windows.push_back({"window"+std::to_string(i),"Title","App"});
 auto retained=spatialLayout(ui,spatialIdentity(),"app0");
 check(retained.size()==15,"real shelf plus seven switcher tiles and three menu rows");
 int nineWindowOverlays=0;
 for(int i=0;i<9;++i){auto p=wallPlacement(i,1.6f,spatialIdentity());nineWindowOverlays+=windowOverlayCount(bendShape(p,p.width/1.6f));}
 check(nineWindowOverlays+int(retained.size())+1+4+1+1+2==87,"actual nine-window wall plus UI high-water stays under100");
 check(workspaceOverlayCount(9,false,4,false,9,true)==87,"budget counts retained hidden UI overlays");
 check(workspaceOverlayCount(9,true,4,true,9,true)==115,"maximum split plus retained UI fits128");
 check(workspaceOverlayCount(9)==77 && workspaceOverlayCount(9)<100,"nine-window wall stays under 100 including fixed UI");
 check(workspaceOverlayCount(9,true,4,true)==105&&workspaceOverlayCount(9,true,4,true)<128,"nine split windows and split strip fit system ceiling");
 int half=0,creates=0,destroys=0;
 auto create=[&](){++creates;return creates;};auto destroy=[&](int){++destroys;};
 for(bool needed:{false,true,true,false,false,true,false})reconcileHalf(half,needed,create,destroy);
 check(!half&&creates==2&&destroys==2,"production half lifecycle creates lazily and destroys on unbend");
 CursorOwnership cursor;int a=0,b=0;
 check(cursor.claim(&a,true)&&!cursor.claim(&b,false)&&cursor.owner==&a,"only focused owner claims shared cursor");
 check(!cursor.release(&b)&&cursor.owner==&a&&cursor.release(&a)&&!cursor.owner,"unfocused window cannot hide another cursor");
 auto m=SurfaceMessage::parse(R"({"t":"surface","v":1,"seq":1,"visible":true,"rect":[100,200,600,36],"anchorRect":[100,200,600,36],"selectedRect":[180,200,40,36]})");
 check(m.hasSelectedRect&&m.selectedRect.x==180,"selected global rect parses");
 PanelSurface window{"window",{},1};window.placement.width=2;window.placement.bendA=.1f;window.placement.bendB=.2f;
 auto strip=menuStripPlacement(window,m,.002f,1);
 check(std::fabs(strip.pressedRect.x-(.006f+.16f))<1e-6&&std::fabs(strip.pressedRect.w-.08f)<1e-6,"pressed rect maps with picture inset and scale");
 float px=strip.pressedRect.x+strip.pressedRect.w/2-strip.placement.width/2;
 float py=strip.height/2-strip.pressedRect.y-strip.pressedRect.h/2;
 auto point=surfacePoint(strip,px,py);EyeRay ray;for(int r=0;r<3;++r){ray.origin[r]=point.m[r][3]+point.m[r][2];ray.direction[r]=-point.m[r][2];}
 auto hit=surfacePick(ray,strip);check(hit&&std::fabs(hit.u-100.f/600)<1e-4,"pressed title follows curved strip picture UV");
 for(const char *r:{"[1,2,0,4]","[1,2,3]","[1,2,3,\"x\"]"}){bool bad=false;try{SurfaceMessage::parse(std::string("{\"t\":\"surface\",\"v\":1,\"seq\":1,\"visible\":false,\"selectedRect\":")+r+"}");}catch(...){bad=true;}check(bad,"malformed selected rect rejected");}
 check(!SurfaceMessage::parse(R"({"t":"surface","v":1,"seq":2,"visible":false})").hasSelectedRect,"absent selection clears pressed material");
}
}
