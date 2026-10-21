#pragma once
#include "depth.hpp"
#include "spatial.hpp"
#include "overlay_budget.hpp"
namespace fw {
inline void selfTestOverlayBudget() {
 struct Harness {
  struct Group {
   std::string key;
   std::array<OverlayBudget::Handle,4> walls{};
   bool failDrop=false;
   int failedRestores=0, restoreCalls=0;
  };
  OverlayBudget budget;
  OverlayBudget::Handle next=1;
  std::map<OverlayBudget::Handle,std::string> live;
  std::vector<OverlayBudget::Handle> required;
  std::vector<std::string> restored;
  explicit Harness(int total,int reserve=0):budget(total,reserve){}
  EVROverlayError create(const std::string &key,const std::string &,OverlayBudget::Handle *handle) {
   *handle=next++;live.emplace(*handle,key);return EVROverlayError_VROverlayError_None;
  }
  EVROverlayError destroy(OverlayBudget::Handle handle) {
   return live.erase(handle)?EVROverlayError_VROverlayError_None:EVROverlayError_VROverlayError_InvalidHandle;
  }
  void allocateRequired(const std::string &prefix,int count) {
   for(int i=0;i<count;++i) {
    OverlayBudget::Handle handle=0;
    auto error=budget.createRequired(prefix+"."+std::to_string(i),"required",&handle,
        [this](const auto &key,const auto &name,auto *out){return create(key,name,out);});
    if(error==EVROverlayError_VROverlayError_None)required.push_back(handle);
   }
  }
  void addGroup(Group &group,float angle=3.14159265f,uint64_t recency=0,bool focused=false) {
   budget.registerWalls(&group,group.key,
    [this,&group] {
     if(group.failDrop)return false;
     bool ok=true;
     for(auto &handle:group.walls)if(handle) {
      if(budget.destroy(handle,[this](auto h){return destroy(h);}))handle=0;else ok=false;
     }
     return ok;
    },
    [this,&group] {
     ++group.restoreCalls;
     if(group.failedRestores>0){--group.failedRestores;return false;}
     for(size_t i=0;i<group.walls.size();++i) {
      auto key=group.key+".wall"+std::to_string(i);
      auto error=budget.createOptional(&group,key,"wall",&group.walls[i],
          [this](const auto &k,const auto &name,auto *out){return create(k,name,out);});
      if(error!=EVROverlayError_VROverlayError_None) {
       for(auto &handle:group.walls)if(handle) {
        if(budget.destroy(handle,[this](auto h){return destroy(h);}))handle=0;
       }
       return false;
      }
     }
     restored.push_back(group.key);return true;
    });
   budget.updateWalls(&group,angle,recency,focused);
  }
  bool allocated(const Group &group) const { return budget.wallsAllocated(&group); }
  void freeRequired(size_t count) {
   count=std::min(count,required.size());
   for(size_t i=0;i<count;++i) {
    if(budget.destroy(required.back(),[this](auto h){return destroy(h);}))required.pop_back();
   }
  }
 };
 auto check=[](bool ok,const char *why){if(!ok)throw std::runtime_error(why);};
 VROverlayHandle_t wallHandles[4]{};
 check(!frameWallGroupActive(wallHandles),"dropped walls disable wall picking");
 for(int i=0;i<4;++i)wallHandles[i]=i+1;
 check(frameWallGroupActive(wallHandles),"restored whole wall group enables wall picking");

 // Failed allocation and destruction must leave the production ledger
 // accurate, including cleanup during constructor failure paths.
 OverlayBudget ledger(16,0);
 OverlayBudget::Handle failedHandle=0, liveHandle=0;
 auto createFailure=ledger.createRequired("failed","required",&failedHandle,
     [](const auto &,const auto &,auto *){return EVROverlayError_VROverlayError_RequestFailed;});
 check(createFailure==EVROverlayError_VROverlayError_RequestFailed && ledger.count()==0,
       "failed OpenVR creation does not consume a budget slot");
 auto createSuccess=ledger.createRequired("live","required",&liveHandle,
     [](const auto &,const auto &,auto *out){*out=91;return EVROverlayError_VROverlayError_None;});
 check(createSuccess==EVROverlayError_VROverlayError_None && ledger.count()==1,
       "successful OpenVR creation is tracked once");
 check(!ledger.destroy(liveHandle,[](auto){return EVROverlayError_VROverlayError_RequestFailed;}) &&
           ledger.count()==1,
       "failed OpenVR destruction keeps the live handle counted");
 check(ledger.destroy(liveHandle,[](auto){return EVROverlayError_VROverlayError_None;}) &&
           ledger.count()==0 &&
           !ledger.destroy(liveHandle,[](auto){return EVROverlayError_VROverlayError_None;}),
       "successful cleanup removes a handle once, including failed-constructor cleanup");

 // A failed optional restore is rate-limited until a real allocation-ledger
 // change, then receives a fresh attempt.
 Harness restoreRetry(12);
 Harness::Group retrying{"retrying"};
 retrying.failedRestores=1;
 restoreRetry.addGroup(retrying,.5f,0);
 restoreRetry.budget.reconcileWalls();
 check(retrying.restoreCalls==1 && !restoreRetry.allocated(retrying),
       "failed wall restore is not retried every reconcile at the same count");
 restoreRetry.allocateRequired("restore-ledger-change",1);
 restoreRetry.budget.reconcileWalls();
 check(retrying.restoreCalls==2 && restoreRetry.allocated(retrying),
       "wall restore retries after the allocation ledger changes");

 // The host's shared reporter emits one initial state, then coalesces rapid
 // transitions into the final state after the one-second window.
 WallTransitionLog log;
 WallTransitionSummary summary;
 const auto logStart=WallTransitionLog::Clock::time_point{}+std::chrono::seconds(5);
 check(log.record(true,10,20,logStart,summary) && summary.restored && summary.count==10,
       "first wall transition is reported immediately");
 check(!log.record(false,6,20,logStart+std::chrono::milliseconds(100),summary) &&
           !log.record(true,10,20,logStart+std::chrono::milliseconds(500),summary),
       "rapid wall changes are coalesced per window");
 check(log.flush(logStart+std::chrono::seconds(1),false,summary) &&
           summary.restored && summary.count==10 && summary.budget==20,
       "coalesced logging eventually exposes the final wall state and counts");

 Harness dropFailure(14);
 Harness::Group failedDrop{"failed-drop"},fallbackDrop{"fallback-drop"};
 dropFailure.addGroup(failedDrop,3.f,0);
 dropFailure.addGroup(fallbackDrop,2.f,0);
 failedDrop.failDrop=true;
 dropFailure.allocateRequired("drop-fallback",7);
 check(dropFailure.allocated(failedDrop)&&!dropFailure.allocated(fallbackDrop)&&
           dropFailure.budget.count()==11,
       "a failed wall destruction does not prevent dropping the next eligible group");

 Harness focusSelection(12);
 Harness::Group oldFocus{"old-focus"},newFocus{"new-focus"};
 focusSelection.addGroup(oldFocus,3.f,1);
 focusSelection.addGroup(newFocus,.1f,0);
 std::vector<WallPriority> priorities{{&oldFocus,"old-focus",3.f,1},
                                      {&newFocus,"new-focus",.1f,0}};
 HostFocus selected;
 selected.activate("old-focus");
 focusSelection.budget.updateWallPriorities(priorities,selected.active);
 selected.activate("new-focus");
 focusSelection.budget.updateWallPriorities(priorities,selected.active);
 focusSelection.allocateRequired("after-host-selection",5);
 check(!focusSelection.allocated(oldFocus)&&focusSelection.allocated(newFocus),
       "host focus activation refreshes protection before a required allocation");

 // Required allocations first drop the angularly furthest eligible wall;
 // equal angles use least-recent focus, and the focused group's walls survive.
 Harness order(20);
 Harness::Group recent{"recent"},stale{"stale"},near{"near"};
 order.addGroup(recent,2.4f,9);order.addGroup(stale,2.4f,1);order.addGroup(near,.4f,0);
 order.allocateRequired("required",8);
 order.allocateRequired("pressure-a",1);
 check(!order.allocated(stale)&&order.allocated(recent)&&order.allocated(near),
       "budget drops furthest wall group, then least recent focus");
 order.allocateRequired("pressure-b",4);
 check(!order.allocated(recent)&&order.allocated(near),"budget drops angularly furthest before nearer wall");
 order.budget.updateWalls(&near,.4f,0,true);
 order.allocateRequired("protected",4);
 check(order.allocated(near)&&order.budget.count()>20,
       "focused optional walls survive and required overlays may exceed budget");
 for(auto h:order.required)check(order.live.count(h)==1,"required overlay remains live under budget pressure");

 // Re-add only whole groups under total budget minus four, in focused/nearest
 // order. Focus changes alone never evict an allocated group.
 Harness hysteresis(20);
 Harness::Group waiting{"waiting"};
 hysteresis.addGroup(waiting,.3f,0);
 hysteresis.allocateRequired("fill",20);
 check(!hysteresis.allocated(waiting)&&hysteresis.budget.count()==20,
       "required allocations evict optional walls at the configured limit");
 hysteresis.freeRequired(7);
 hysteresis.budget.updateWalls(&waiting,.3f,0,true);
 hysteresis.budget.reconcileWalls();
 check(!hysteresis.allocated(waiting)&&hysteresis.budget.count()==13,
       "wall restoration keeps four slots of hysteresis");
 size_t restoreEvents=hysteresis.restored.size();
 for(int i=0;i<20;++i) {
  hysteresis.budget.updateWalls(&waiting,.3f,uint64_t(i),i%2==0);
  hysteresis.budget.reconcileWalls();
 }
 check(!hysteresis.allocated(waiting)&&hysteresis.restored.size()==restoreEvents,
       "repeated focus changes do not flicker walls under pressure");
 hysteresis.freeRequired(1);
 hysteresis.budget.reconcileWalls();
 check(hysteresis.allocated(waiting)&&hysteresis.budget.count()==16,
       "walls restore as a whole group at budget minus four");

 Harness restore(20);
 Harness::Group farther{"farther"},closer{"closer"},focused{"focused"};
 restore.addGroup(farther,2.5f,8);restore.addGroup(closer,.2f,5);restore.addGroup(focused,1.f,1);
 restore.allocateRequired("restore-pressure",20);
 check(!restore.allocated(farther)&&!restore.allocated(closer)&&!restore.allocated(focused),
       "all optional groups can yield to required overlays");
 restore.freeRequired(16);
 restore.restored.clear();
 restore.budget.updateWalls(&farther,2.5f,8,false);
 restore.budget.updateWalls(&closer,.2f,5,false);
 restore.budget.updateWalls(&focused,1.f,1,true);
 restore.budget.reconcileWalls();
 check(restore.restored==std::vector<std::string>({"focused","closer","farther"}),
       "wall restoration prioritizes focus, then gaze proximity");

 // Tie the policy check to the existing workspace high-water helpers. The
 // fully split workspace is 115; 77 required overlays plus four complete wall
 // groups and the dashboard pair must finish at an exact total of 95.
 Harness nine(96,2);
 std::array<Harness::Group,9> windows;
 Placement splitPlacement;splitPlacement.width=1.6f;splitPlacement.bendA=.1f;splitPlacement.bendB=.2f;
 const int splitWindowHighWater=windowOverlayCount(bendShape(splitPlacement,1.f));
 const int fullWorkspaceHighWater=workspaceOverlayCount(9,true,4,true,9,true);
 const int requiredPerWindow=splitWindowHighWater-4;
 const int requiredRetainedUI=fullWorkspaceHighWater-9*splitWindowHighWater-nine.budget.reserved();
 check(splitWindowHighWater==10&&requiredPerWindow==6&&fullWorkspaceHighWater==115&&
           requiredRetainedUI==23,
       "nine-window budget case follows production workspace and window counts");
 for(int i=0;i<9;++i) {
  windows[i].key="window"+std::to_string(i);
  nine.allocateRequired(windows[i].key+".required",requiredPerWindow);
  nine.addGroup(windows[i],float(i)*.1f,uint64_t(i),i==0);
 }
 nine.allocateRequired("retained-host-ui",requiredRetainedUI);
 int allocatedGroups=0;
 for(const auto &window:windows)if(nine.allocated(window))++allocatedGroups;
 check(nine.budget.diagnosticCount()==95 && nine.budget.count()==93 &&
           allocatedGroups==4 && nine.allocated(windows[0]),
       "nine split windows reach exact 95 high-water after five whole groups drop, focused group protected");
 check(nine.budget.totalBudget()==96&&nine.budget.reserved()==2,
       "host diagnostic counts the separate dashboard reservation");
 check(nine.required.size()==77,"all nine-window and retained UI overlays remain required");
 for(auto h:nine.required)check(nine.live.count(h)==1,"nine-window required overlays survive wall eviction");
}

inline void selfTestDepth() {
 auto check=[](bool ok,const char *why){if(!ok)throw std::runtime_error(why);};
 check(std::fabs(frameDepth-.03f)<1e-7f && pictureDepth==frameDepth && controlsDepth>frameDepth,
       "one 3 cm frame depth drives picture, walls and forward controls");
 HandleLayout l;l.width=2;l.height=1.2f;
 for(bool vertical:{false,true})for(auto amounts:{std::array<float,2>{0,0},{.08f,.08f},{.3f,.3f},{.1f,.3f}}) {
  Placement p;p.width=l.width;p.bendVertical=vertical;p.bendA=amounts[0];p.bendB=amounts[1];
  auto b=bendShape(p,l.height);auto walls=frameWalls(b,l);
  check(walls.size()==4,"four real wall overlays for every bend");
  check(walls[0].facets>=1&&walls[0].facets<=FrameWall::maxFacets,"wall facets bounded by curvature");
  if(!amounts[0]&&!amounts[1])check(walls[0].facets==1,"flat wall has one facet");
  if(b.split())check(walls[0].facets%2==0,"split wall retains centre seam");
  // Compare actual polygon chord midpoints with the exact bend, on both
  // front and rear edges. This catches coarse visible tabs independently of
  // the facet-selection formula and shader capacity.
  const auto &edge=walls[0];
  check(edge.polygon.size()<=FrameWall::maxPoints,"wall polygon fits shader point capacity");
  const float along=b.span/2+l.bezel()*.3f;
  for(int face=0;face<2;++face)for(int i=0;i<edge.facets;++i) {
    auto a=edge.polygon[face*(edge.facets+1)+i],c=edge.polygon[face*(edge.facets+1)+i+1];
    auto midpoint=edge.local;
    for(int r=0;r<3;++r)midpoint.m[r][3]+=edge.local.m[r][0]*(a[0]+c[0])/2+edge.local.m[r][1]*(a[1]+c[1])/2;
    float s=-along+2*along*(i+.5f)/edge.facets;if(face)s=-s;
    auto exact=bendPoint(b,s,-((vertical?l.width:l.height)/2+l.bezel()*.3f),face?0.f:frameDepth);
    float error=0;for(int r=0;r<3;++r)error+=std::pow(midpoint.m[r][3]-exact.m[r][3],2);
    check(std::sqrt(error)<=FrameWall::chordError+1e-6f,"actual wall chords deviate at most one millimetre");
  }
  check(windowOverlayCount(b)==(b.split()?10:7),"overlay count includes live halves and four walls");
  for(const auto &w:walls) {
    check(w.bevelRadius==frameDepth/2 && std::fabs(w.depthDirection)==1 && FrameWall::joinOverlap>0,
          "wall renderer receives full-depth pillow radius, front direction and soft join overlap");
    check(w.width>0 && w.height>=frameDepth-1e-5,"wall texture bounds include full frame depth");
  }
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
  for(int alongSign:{-1,1})for(int acrossSign:{-1,1})for(float z:{frameDepth*.1f,frameDepth*.5f,frameDepth*.9f}) {
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
 selfTestOverlayBudget();
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
 // Independently recover global title points through production surface picking.
 // Test both edges as well as centre: a translation-only check misses scaling bugs.
 for(bool vertical:{false,true})for(auto bend:{std::array<float,2>{0,0},{.1f,.1f},{.1f,.2f}})
 for(float mpp:{.0013f,.002f}) {
  window.placement.bendVertical=vertical;window.placement.bendA=bend[0];window.placement.bendB=bend[1];
  auto actual=menuStripPlacement(window,m,mpp,1);
  float recovered[3];
  for(int i=0;i<3;++i) {
   float x=actual.pressedRect.x+actual.pressedRect.w*i/2-actual.placement.width/2;
   float y=actual.height/2-actual.pressedRect.y-actual.pressedRect.h/2;
   auto pt=surfacePoint(actual,x,y);EyeRay r;
   for(int k=0;k<3;++k){r.origin[k]=pt.m[k][3]+pt.m[k][2];r.direction[k]=-pt.m[k][2];}
   auto pick=surfacePick(r,actual);check(bool(pick),"selected title roundtrips actual strip");
   recovered[i]=m.rect.x+pick.u*m.rect.w;
  }
  check(std::fabs(recovered[1]-(m.selectedRect.x+m.selectedRect.w/2))<1,
        "flat/even/split/vertical inset centre agrees within one capture point");
  check(std::fabs(recovered[2]-recovered[0]-m.selectedRect.w)<1,
        "flat/even/split/vertical inset width agrees within one capture point");
  check(std::fabs(actual.recessOffset/mpp-.8f)<1e-5,"recessed legend offset preserves capture point scale");
 }
 for(const char *r:{"[1,2,0,4]","[1,2,3]","[1,2,3,\"x\"]"}){bool bad=false;try{SurfaceMessage::parse(std::string("{\"t\":\"surface\",\"v\":1,\"seq\":1,\"visible\":false,\"selectedRect\":")+r+"}");}catch(...){bad=true;}check(bad,"malformed selected rect rejected");}
 check(!SurfaceMessage::parse(R"({"t":"surface","v":1,"seq":2,"visible":false})").hasSelectedRect,"absent selection clears pressed material");
}
}
