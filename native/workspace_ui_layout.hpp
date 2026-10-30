#pragma once
#include <string>
#include <vector>
#include <cmath>
namespace fw {
struct WorkspaceWidget {
  float x=0,y=0,w=0,h=0;
  std::string action,label;
  bool selected=false;
  bool contains(float px,float py)const{return px>=x&&px<=x+w&&py>=y&&py<=y+h;}
};
struct WorkspacePicture {
  int width=840,height=184;
  std::vector<WorkspaceWidget> widgets;
  std::string hit(float u,float v)const {
    for(auto i=widgets.rbegin();i!=widgets.rend();++i)
      if(!i->action.empty()&&i->contains(u*width,v*height))return i->action;
    return {};
  }
};
inline WorkspacePicture workspaceDock(bool arrange,bool menu,bool placing,bool active, bool choosing=false,
    const std::vector<std::string> &choices={},size_t page=0,const std::string &notice="") {
  WorkspacePicture p;
  p.widgets={{12,120,200,52,"work","Work",!arrange},{220,120,200,52,"arrange","Arrange",arrange},
             {428,120,400,52,"workspace",active?"Workspace  ·  Mouse connected":"Workspace  ·  Connecting…",menu}};
  if(menu||placing) {
    p.widgets.push_back({12,58,220,52,"recenter","Recenter window"});
    p.widgets.push_back({240,58,160,52,"add-window","Add window"});
    p.widgets.push_back({408,58,150,52,"cancel","Cancel",placing});
    p.widgets.push_back({566,58,262,52,"stop","Return to Mac"});
  }
  if(choosing) {
    const size_t first=page*4;
    const size_t rows=std::min<size_t>(4,choices.size()>first?choices.size()-first:0);
    const float navigation=64+float(rows)*50,shift=navigation;
    p.height+=int(shift);
    for(auto &w:p.widgets)w.y+=shift;
    p.widgets.push_back({12,8,816,42,"",notice.empty()?"Choose a Mac window":notice});
    for(size_t i=first;i<choices.size()&&i<first+4;++i)
      p.widgets.push_back({12,58+float(i-first)*50,816,44,"admit:"+std::to_string(i),choices[i]});
    p.widgets.push_back({12,navigation,184,44,"catalog-back","Back"});
    p.widgets.push_back({204,navigation,200,44,"catalog-refresh","Refresh"});
    if(page>0)p.widgets.push_back({412,navigation,200,44,"catalog-prev","Previous"});
    if(first+4<choices.size())p.widgets.push_back({620,navigation,208,44,"catalog-next","Next"});
  }
  return p;
}
inline WorkspacePicture workspaceFrame(float aspect,const std::string &title,bool arrange,bool reduced) {
  WorkspacePicture p; p.width=1100;p.height=int(std::lround(1000/std::max(.1f,aspect)))+132;
  const float bottom=p.height-42.f;
  // The 1000px content opening is transparent. All handles have visible bounds.
  p.widgets.push_back({50,6,1000,46,arrange?"move":"",title.empty()?"Mac window":title});
  if(reduced)p.widgets.push_back({710,58,334,32,"","Reduced quality · budget"});
  if(arrange) {
    p.widgets.push_back({20,68,44,44,"corner:-1:1",""});
    p.widgets.push_back({1036,68,44,44,"corner:1:1",""});
    p.widgets.push_back({20,bottom-20,44,44,"corner:-1:-1",""});
    p.widgets.push_back({1036,bottom-20,44,44,"corner:1:-1",""});
    p.widgets.push_back({210,bottom+2,226,36,"depth","Depth / wheel"});
    p.widgets.push_back({444,bottom+2,104,36,"rotate-left","Rotate −"});
    p.widgets.push_back({556,bottom+2,104,36,"rotate-right","Rotate +"});
    p.widgets.push_back({668,bottom+2,104,36,"curve-less","Curve −"});
    p.widgets.push_back({780,bottom+2,104,36,"curve-more","Curve +"});
  }
  return p;
}
inline WorkspacePicture workspaceBeacon(const std::string &direction) {
  WorkspacePicture p;p.width=340;p.height=64;
  p.widgets={{0,0,340,64,"beacon:"+direction,direction.find("Behind")!=std::string::npos?"Behind · Click to recenter":"Click to recenter"}};return p;
}
} // namespace fw
