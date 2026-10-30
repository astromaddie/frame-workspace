#include "content_input.hpp"
#include "workspace_mouse.hpp"
#include "workspace_ui_layout.hpp"
#include <cassert>
#include <iostream>

int main() {
  fw::MouseGazeRestore restore;restore.requested("a",1,10);
  assert(!restore.acknowledge("a",1,11,11)); // motion already applied wins
  restore.requested("a",2,11);
  assert(!restore.acknowledge("a",2,11,12)); // queued newer motion wins too
  restore.requested("a",3,12);
  assert(!restore.acknowledge("b",3,12,12));
  assert(restore.acknowledge("a",3,12,12));
  assert(!restore.acknowledge("a",3,12,12)); // one ACK cannot snap twice
  auto dock=fw::workspaceDock(true,true,true,true);
  assert(dock.hit(100.f/840,80.f/184)=="recenter");
  assert(dock.hit(300.f/840,80.f/184)=="add-window");
  assert(dock.hit(650.f/840,80.f/184)=="stop");
  auto picker=fw::workspaceDock(false,true,false,true,true,{"First","Second","Third","Fourth","Fifth"});
  assert(picker.hit(400.f/840,80.f/picker.height)=="admit:0");
  assert(picker.hit(700.f/840,280.f/picker.height)=="catalog-next");
  auto frame=fw::workspaceFrame(1.6f,"Title",true,true);
  assert(frame.hit(.5f,.5f).empty()); // captured content opening stays transparent to input
  assert(frame.hit(42.f/1100,90.f/frame.height)=="corner:-1:1");
  assert(frame.hit(1058.f/1100,90.f/frame.height)=="corner:1:1");
  fw::ContentInput input;
  fw::ContentHit a{fw::ContentConsumer::Content, "a", false, .3f, .4f};
  fw::ContentHit b{fw::ContentConsumer::Content, "b", false, .7f, .2f};
  using C = fw::ContentCommand;
  assert(input.mouseHover(a, 100).empty()); // pure highlight, no focus or Mac move
  auto commands = input.mouseButton(a, 0, true, 110);
  assert(commands.size() == 1 && commands[0].kind == C::Focus);
  auto token = commands[0].token;
  assert(input.mouseHover(b, 120).empty()); // held input cannot transfer target
  input.acknowledge("a", token, true);
  commands = input.drain();
  assert(commands.size() == 2 && commands[0].kind == C::Move && commands[1].kind == C::Down);
  assert(commands[1].key == "a" && commands[1].u == .3f);
  assert(input.mouseHover(b, 130).empty());
  commands = input.mouseButton(b, 0, false, 140);
  assert(commands.size() == 1 && commands[0].kind == C::Up && commands[0].key == "a");
  assert(!input.suppressGaze()); // host idle timer can permit dwell after mouse release
  assert(input.mouseHover(b, 150).empty());
  commands = input.mouseWheel(b, 0, 12, 160);
  assert(commands.size() == 1 && commands[0].kind == C::Barrier);
  input.acknowledge("a", commands[0].token, true);
  commands = input.drain(); assert(commands.size() == 1 && commands[0].kind == C::Focus);
  input.acknowledge("b", commands[0].token, true);
  commands = input.drain();
  assert(commands.size() == 2 && commands[1].kind == C::Wheel && commands[1].dy == 12);
  commands = input.cancelAll(); assert(!commands.empty() && !input.buttonHeld());
  assert(input.mouseHover(a, 170).empty());
  commands = input.mouseButton(a, 0, true, 180);
  input.acknowledge("a", commands[0].token, false);
  assert(input.mouseButton(a, 0, false, 190).empty()); // refused down never produces orphan up
  HmdMatrix34_t head{{{1,0,0,0},{0,1,0,1.6f},{0,0,1,0}}};
  fw::WorkspacePointer pointer; pointer.begin("s", "h", head);
  const auto original = pointer.point;
  auto movedHead = head; movedHead.m[0][3] += .5f;
  assert(pointer.motion("s","h",1,0,0,movedHead,100));
  assert(pointer.point==original);
  assert(pointer.point == original); // head lean itself never moves the point
  assert(pointer.motion("s", "h", 2, 100, 50, movedHead, 200));
  assert(std::fabs(pointer.point[0] - original[0] - .1f) < 1e-6f);
  assert(!pointer.motion("s", "h", 1, 999, 999, movedHead, 210));
  assert(!pointer.motion("old", "h", 3, 999, 999, movedHead, 210));
  assert(!pointer.gazeMayDwell(1199, false, false, false));
  assert(pointer.gazeMayDwell(1200, false, false, false));
  assert(!pointer.gazeMayDwell(1200, true, false, false));
  fw::Placement pose; pose.matrix.m[1][3]=1.6f;
  fw::MousePlacementGesture gesture;
  auto rayTo=[&](float x,float y,float z) { return fw::EyeRay{{0,1.6f,0},{x,y-1.6f,z}}; };
  auto startRay=rayTo(0,1.6f,-2), movedRay=rayTo(.1f,1.55f,-2);
  assert(gesture.begin(fw::MousePlacementGesture::Kind::Move,pose,head,startRay,1.2f));
  auto moved=gesture.update(movedRay);
  assert(std::fabs(moved.matrix.m[0][3]-.1f)<1e-6f);
  assert(gesture.update(movedRay).matrix.m[0][3]==moved.matrix.m[0][3]);
  assert(gesture.cancel().matrix.m[0][3]==pose.matrix.m[0][3]);
  pose.matrix.m[0][3]=1; // off-axis depth must follow head-to-window ray
  assert(gesture.begin(fw::MousePlacementGesture::Kind::Depth,pose,head,rayTo(1,1.6f,-2),1.2f));
  auto depth=gesture.finish(rayTo(1,1.5f,-2));
  assert(depth.width==pose.width && depth.matrix.m[0][3]>1);
  assert(std::fabs(depth.matrix.m[0][3]/depth.matrix.m[2][3]+.5f)<1e-6f);
  pose.matrix.m[0][3]=0;
  assert(gesture.begin(fw::MousePlacementGesture::Kind::Depth,pose,head,startRay,1.2f));
  auto wheel=gesture.update(startRay,120);
  assert(wheel.distance>pose.distance && wheel.width==pose.width);
  assert(gesture.cancel().matrix.m[2][3]==pose.matrix.m[2][3]); // pending wheel cancellation restores without save

  for(int sx:{-1,1})for(int sy:{-1,1}) {
    const float cx=sx*pose.width/2,cy=1.6f+sy*.6f;
    assert(gesture.begin(fw::MousePlacementGesture::Kind::Resize,pose,head,rayTo(cx,cy,-2),1.2f,sx,sy));
    auto resized=gesture.finish(rayTo(cx+sx*.2f,cy+sy*.1f,-2));
    assert(std::fabs(resized.width-pose.width-.2f)<1e-6f);
    assert(resized.distance==pose.distance && resized.matrix.m[2][3]==pose.matrix.m[2][3]);
    assert(std::fabs(resized.matrix.m[0][3]-sx*resized.width/2+sx*pose.width/2)<1e-6f);
    assert(std::fabs(resized.matrix.m[1][3]-sy*(resized.width/2)/2-(1.6f-sy*.6f))<1e-6f);
  }
  std::cout << "Mouse arbiter: hover, focus ACK, fixed drag target, wheel barrier, cancellation passed\n";
}
