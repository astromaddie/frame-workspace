#pragma once
// Content input (FW-15): picture-only picking on every bend and the pure
// arbiter, driven with synthetic hand samples.
#include "content_input.hpp"
#include "spatial.hpp"
#include "video.hpp"
#include <array>
namespace fw {
inline void selfTestContentPick() {
  auto check = [](bool ok, const char *why) { if (!ok) throw std::runtime_error(why); };
  PanelSurface flat{"flat", {}, 1.2f};
  flat.placement.width = 2; flat.placement.matrix = {{{1,0,0,0}, {0,1,0,0}, {0,0,1,-2}}};
  auto pick = surfacePick({{0,0,0}, {0,0,-1}}, flat);
  check(pick && pick.content, "window centre is content");
  pick = surfacePick({{.995f,0,0}, {0,0,-1}}, flat);
  check(pick && pick.content && pick.u > .99f, "the picture's own edge is content");
  for (float x : {1.012f, 1.03f}) {
    pick = surfacePick({{x,0,0}, {0,0,-1}}, flat);
    check(!pick.content, "the bezel and frame wall are never content");
  }
  check(!surfacePick({{0,.62f,0}, {0,0,-1}}, flat).content, "above the picture is not content");
  // Each bend, uneven halves and top-to-bottom, through a rotated pose: the
  // laser lands on the original UV, including either side of the seam.
  for (bool vertical : {false, true}) for (const auto &amounts : std::vector<std::array<float,2>>{
           {0,0}, {.25f,.25f}, {0,.4f}, {.45f,.1f}}) {
    PanelSurface bent = flat;
    bent.placement.bendVertical = vertical;
    bent.placement.bendA = amounts[0]; bent.placement.bendB = amounts[1];
    const float c = std::cos(-.3f), s = std::sin(-.3f);
    bent.placement.matrix = {{{c,0,s,-.7f}, {0,1,0,1.4f}, {-s,0,c,-2.5f}}};
    for (float u : {.02f,.499f,.501f,.98f}) for (float v : {.03f,.499f,.501f,.97f}) {
      auto local = bendPointXY(bendShape(bent.placement, bent.height), (u-.5f)*2, (.5f-v)*bent.height, pictureDepth);
      auto world = multiply(bent.placement.matrix, local);
      EyeRay laser;
      // From 60 cm out and a little to the side, as a hand would point.
      for (int i = 0; i < 3; ++i) {
        laser.origin[i] = world.m[i][3] + .6f * world.m[i][2] + .1f * world.m[i][0];
        laser.direction[i] = world.m[i][3] - laser.origin[i];
      }
      auto hit = surfacePick(laser, bent);
      check(hit && hit.content && std::fabs(hit.u-u) < 2e-4 && std::fabs(hit.v-v) < 2e-4,
            "content UV round-trips flat, even, uneven and vertical bends at seams and edges");
    }
    // Just past the side edge, on the bent bezel: never content.
    auto local = bendPointXY(bendShape(bent.placement, bent.height), vertical ? 0 : 1.015f, vertical ? .615f : 0, pictureDepth);
    auto world = multiply(bent.placement.matrix, local);
    EyeRay laser;
    for (int i = 0; i < 3; ++i) { laser.origin[i] = world.m[i][3] + world.m[i][2]; laser.direction[i] = -world.m[i][2]; }
    check(!surfacePick(laser, bent).content, "a bent bezel is not content");
  }
  // M1's inset rim is chrome, its picture is content.
  auto metadata = SurfaceMessage::parse(R"({"t":"surface","v":1,"seq":1,"visible":true,"rect":[0,0,612,36],)"
                                        R"("anchorRect":[0,0,612,36],"anchor":"menubar","menusWidth":612})");
  PanelSurface window{"window", {}, .8f};
  window.placement.width = 1.2f; window.placement.matrix = spatialIdentity();
  auto strip = menuStripPlacement(window, metadata, .001f, 1);
  strip.key = "menubar";
  auto through = [&](float x, float y) {
    auto p = surfacePoint(strip, x, y, 0);
    EyeRay r;
    for (int i = 0; i < 3; ++i) { r.origin[i] = p.m[i][3] + p.m[i][2]; r.direction[i] = -p.m[i][2]; }
    return surfacePick(r, strip);
  };
  const float top = strip.height / 2, left = -strip.placement.width / 2;
  auto inside = through(left + strip.inset + .1f, top - strip.inset - .01f);
  check(inside && inside.content && std::fabs(inside.u - .1f / .612f) < 2e-3, "menu strip picture maps at point scale");
  check(!through(left + .003f, top - strip.inset - .01f).content, "menu strip rim is not content");
  // Menu scale from the window's logical width: a 2400-point window capped
  // from 4800 to 1920 pixels was 2.5x too large with the decoded estimate.
  check(std::fabs(windowMetresPerPoint(1.2f, 1920, 2400) - .0005f) < 1e-9f &&
        std::fabs(windowMetresPerPoint(1.2f, 1920) / windowMetresPerPoint(1.2f, 1920, 2400) - 2.5f) < 1e-4f,
        "menu point scale follows the logical width under a capture cap");
  check(std::fabs(windowMetresPerPoint(1.2f, 1600, 800, .5f) - .00075f) < 1e-9f,
        "content showing child windows past the frame scales by the window's share");
  check(windowMetresPerPoint(1.2f, 1600, NAN) == 1.2f * 2 / 1600 && windowMetresPerPoint(1.2f, 0) == .001f,
        "without geometry the uncapped estimate remains");
  // Geometry from the helper: padding outside the content rectangle.
  Pipeline::InputGeometry g;
  auto ok = Pipeline::InputGeometry::parse(json(R"({"t":"input-geometry","v":1,"revision":9,"pictureSeq":44,)"
      R"("rect":[100,200,800,600],"contentUV":[0,0.25,1,0.5],"visible":true})"), g);
  check(ok && g.revision == 9 && g.pictureSeq == 44 && g.contains(.5f, .5f) && !g.contains(.5f, .1f),
        "input geometry parses; padding is not content");
  for (const auto *bad : {R"({"v":2,"revision":1,"pictureSeq":0,"rect":[0,0,1,1],"contentUV":[0,0,1,1],"visible":true})",
                          R"({"v":1,"revision":0,"pictureSeq":0,"rect":[0,0,1,1],"contentUV":[0,0,1,1],"visible":true})",
                          R"({"v":1,"revision":1,"pictureSeq":-1,"rect":[0,0,1,1],"contentUV":[0,0,1,1],"visible":true})",
                          R"({"v":1,"revision":1,"pictureSeq":0,"rect":[0,0,0,1],"contentUV":[0,0,1,1],"visible":true})",
                          R"({"v":1,"revision":1,"pictureSeq":0,"rect":[0,0,1,1],"contentUV":[0,0,1.5,1],"visible":true})",
                          R"({"v":1,"revision":1,"pictureSeq":0,"rect":[0,0,1,1],"contentUV":[0,0,1,1]})"})
    check(!Pipeline::InputGeometry::parse(json(bad), g), "malformed input geometry rejected");
  check(Pipeline::InputGeometry::parse(json(R"({"v":1,"revision":2,"pictureSeq":0,"rect":[0,0,0,0],)"
                                            R"("contentUV":[0,0,1,1],"visible":false})"), g) && !g.contains(.5f, .5f),
        "a hidden surface addresses nothing");
}

// Drives ContentInput frame by frame at 90 Hz.
struct ContentSim {
  ContentInput input;
  ContentFrame frame;
  double t = 1000;
  float angle[2]{};
  std::vector<ContentCommand> log, all;  // `all` survives clear()
  ContentSim() { for (auto &h : frame.hand) { h.valid = true; h.direction[2] = -1; } }
  std::vector<ContentCommand> step(double ms = 11) {
    t += ms; frame.now = t;
    auto c = input.update(frame);
    log.insert(log.end(), c.begin(), c.end());
    all.insert(all.end(), c.begin(), c.end());
    return c;
  }
  void aim(int h, const std::string &key, float u, float v, bool aux = false) {
    frame.hand[h].hit = {ContentConsumer::Content, key, aux, u, v};
  }
  void off(int h, ContentConsumer c = ContentConsumer::None) { frame.hand[h].hit = ContentHit{}; frame.hand[h].hit.consumer = c; }
  // Rotates the laser a little each frame: deliberate pointing.
  void sweep(int h, int frames = 6, float step = .01f) {
    for (int i = 0; i < frames; ++i) {
      angle[h] += step;
      frame.hand[h].direction[0] = std::sin(angle[h]); frame.hand[h].direction[2] = -std::cos(angle[h]);
      this->step();
    }
  }
  void ackAll(bool ok = true) {
    for (int round = 0; round < 4; ++round) {
      bool any = false;
      for (auto &c : std::vector<ContentCommand>(all))
        if ((c.kind == ContentCommand::Focus || c.kind == ContentCommand::Barrier) && c.token && !acked(c.token)) {
          input.acknowledge(c.key, c.token, ok); done.push_back(c.token); any = true;
        }
      step();
      if (!any) break;
    }
  }
  std::vector<uint64_t> done;
  bool acked(uint64_t token) const { return std::find(done.begin(), done.end(), token) != done.end(); }
  int count(ContentCommand::Kind kind, const std::string &key = "") const {
    int n = 0;
    for (const auto &c : log) if (c.kind == kind && (key.empty() || c.key == key)) ++n;
    return n;
  }
  std::string kinds() const {  // e.g. "F:a M:a D:a U:a"
    static const char *names = "FMDUWBRK";
    std::string s;
    for (const auto &c : log) {
      if (c.kind == ContentCommand::Keepalive) continue;
      if (!s.empty()) s += ' ';
      s += names[c.kind]; s += ':'; s += c.key;
    }
    return s;
  }
  void clear() { log.clear(); }
};

inline void selfTestContentArbiter() {
  auto check = [](bool ok, const char *why) { if (!ok) throw std::runtime_error(why); };
  {
    // A resting laser never takes the pointer; deliberate motion does.
    ContentSim s;
    s.aim(1, "a", .3f, .4f);
    for (int i = 0; i < 60; ++i) s.step();
    check(s.log.empty() && !s.input.pointerOwned(), "a stationary ray over content sends nothing");
    s.sweep(1, 2, .002f);
    check(s.log.empty(), "hand tremor is not pointing");
    s.sweep(1);
    check(s.input.pointerOwned() && s.input.pointerHand() == 1 && s.kinds() == "F:a", "deliberate pointing focuses first");
    check(s.log[0].intent == "pointer" && s.log[0].deliberate && s.input.suppressGaze(), "pointer focus suppresses gaze");
    s.ackAll();
    check(s.kinds() == "F:a M:a" && s.input.target() == "a", "the move follows the acknowledgement");
    check(s.input.takeConfirmed() == std::vector<std::string>{"a"}, "confirmed focus reported once");
    s.clear();
    s.aim(1, "a", .31f, .4f); s.step();
    s.frame.hand[1].trigger = true; s.aim(1, "a", .32f, .41f); s.step();
    s.aim(1, "a", .35f, .45f); s.step();
    s.frame.hand[1].trigger = false; s.step();
    check(s.kinds() == "M:a M:a D:a M:a U:a", "hover, press, drag motion and release in order");
    check(s.log[2].button == 0 && std::fabs(s.log[2].u - .32f) < 1e-6 && std::fabs(s.log[4].u - .35f) < 1e-6,
          "the trigger is the left button at the laser point");
    s.clear();
    s.frame.hand[1].secondary = true; s.step(); s.frame.hand[1].secondary = false; s.step();
    check(s.kinds() == "D:a U:a" && s.log[0].button == 2, "the secondary button is a right click");
    // Leaving content: the pointer yields after a short grace.
    s.clear(); s.off(1);
    for (int i = 0; i < 20; ++i) s.step();
    check(!s.input.pointerOwned() && s.log.empty() && !s.input.suppressGaze(), "gaze resumes once the laser leaves");
  }
  {
    // Foreground consumers keep their presses: nothing transfers in later.
    for (auto consumer : {ContentConsumer::Ui, ContentConsumer::Handle, ContentConsumer::None}) {
      ContentSim s;
      s.aim(1, "a", .5f, .5f); s.sweep(1); s.ackAll(); s.clear();
      s.off(1, consumer); s.step();
      s.frame.hand[1].trigger = true; s.step();
      s.aim(1, "a", .6f, .5f); s.sweep(1, 10);
      check(s.log.empty(), "while a shelf, handle or bezel press is held the laser does not hover content");
      s.frame.hand[1].trigger = false; s.sweep(1);
      check(s.count(ContentCommand::Down) == 0 && s.count(ContentCommand::Up) == 0,
            "a press on the shelf, a handle or the bezel never clicks content");
      check(s.count(ContentCommand::Move) > 0, "after release the laser hovers again");
    }
  }
  {
    // One Mac cursor: the second hand cannot press or steal while one holds.
    ContentSim s;
    s.aim(1, "a", .5f, .5f); s.sweep(1); s.ackAll(); s.clear();
    s.frame.hand[1].trigger = true; s.step();
    s.aim(0, "a", .1f, .1f); s.sweep(0);
    s.frame.hand[0].trigger = true; s.step(); s.frame.hand[0].trigger = false; s.step();
    s.frame.hand[1].trigger = false; s.step();
    check(s.count(ContentCommand::Down) == 1 && s.count(ContentCommand::Up) == 1, "only the first hand's press clicks");
    for (const auto &c : s.log) check(c.kind != ContentCommand::Move || c.u == .5f, "the other hand never moves the cursor");
    // Both hands start pointing together: the right hand wins.
    ContentSim t;
    t.aim(0, "a", .2f, .2f); t.aim(1, "b", .8f, .8f);
    for (int i = 0; i < 6; ++i) {
      for (int h = 0; h < 2; ++h) {
        t.angle[h] += .01f;
        t.frame.hand[h].direction[0] = std::sin(t.angle[h]); t.frame.hand[h].direction[2] = -std::cos(t.angle[h]);
      }
      t.step();
    }
    check(t.input.pointerHand() == 1 && t.kinds() == "F:b", "simultaneous pointing: right hand wins");
  }
  {
    // Cross-window drag: barrier the source, raise the drop window with drag
    // intent, carry the held motion there and let go once.
    ContentSim s;
    s.aim(1, "a", .5f, .5f); s.sweep(1); s.ackAll(); s.clear();
    s.frame.hand[1].trigger = true; s.step();
    s.aim(1, "a", .9f, .5f); s.step();
    s.off(1); for (int i = 0; i < 30; ++i) s.step();  // a gap between panels freezes
    check(s.input.buttonHeld() && s.kinds() == "D:a M:a", "a ray gap mid-drag sends nothing, keeps the button");
    s.aim(1, "b", .1f, .5f); s.step();
    check(s.kinds() == "D:a M:a B:a", "entering the drop window barriers the source first");
    s.aim(1, "b", .15f, .5f); s.step();
    s.ackAll();
    check(s.kinds() == "D:a M:a B:a F:b M:b", "then raises the drop window and continues there");
    check(s.log[3].intent == "drag" && std::fabs(s.log[4].u - .15f) < 1e-6, "drag intent, newest point");
    s.aim(1, "b", .2f, .5f); s.step();
    s.aim(1, "menubar", .2f, .5f, true); s.step();
    check(s.count(ContentCommand::Focus, "menubar") == 0 && s.count(ContentCommand::Barrier, "b") == 0,
          "a drag does not leave into a strip");
    s.aim(1, "a", .8f, .5f); s.step(); s.ackAll();
    s.aim(1, "a", .7f, .5f); s.step();
    s.frame.hand[1].trigger = false; s.step();
    check(s.count(ContentCommand::Down) == 1 && s.count(ContentCommand::Up) == 1 && s.log.back().kind == ContentCommand::Up &&
          s.log.back().key == "a", "re-entering the source carries on; a single up at the drop");
    check(s.count(ContentCommand::Up, "b") == 0, "no up on leaving a window");
  }
  {
    // Release during the handoff: the up waits for the drop window.
    ContentSim s;
    s.aim(1, "a", .5f, .5f); s.sweep(1); s.ackAll(); s.clear();
    s.frame.hand[1].trigger = true; s.step();
    s.aim(1, "b", .3f, .3f); s.step();
    s.frame.hand[1].trigger = false; s.step();
    check(s.count(ContentCommand::Up) == 0, "no up before the drop window is ready");
    s.ackAll();
    check(s.kinds() == "D:a B:a F:b M:b U:b", "focus, motion, then the up at the drop");
  }
  {
    // A drop window that will not come forward: let go where the drag was.
    ContentSim s;
    s.aim(1, "a", .5f, .5f); s.sweep(1); s.ackAll(); s.clear();
    s.frame.hand[1].trigger = true; s.step();
    s.aim(1, "b", .3f, .3f); s.step();
    for (const auto &c : std::vector<ContentCommand>(s.log)) if (c.kind == ContentCommand::Barrier) s.input.acknowledge(c.key, c.token, true);
    s.step();
    for (const auto &c : std::vector<ContentCommand>(s.log)) if (c.kind == ContentCommand::Focus) s.input.acknowledge(c.key, c.token, false);
    s.frame.hand[1].trigger = false; s.step();
    check(s.count(ContentCommand::Up, "a") == 1 && s.count(ContentCommand::Up, "b") == 0, "failed drop releases at the source");
  }
  {
    // Press before the target is ready: the down waits; a failed focus drops it.
    ContentSim s;
    s.aim(1, "a", .5f, .5f); s.sweep(1); s.clear();
    s.frame.hand[1].trigger = true; s.step(); s.frame.hand[1].trigger = false; s.step();
    check(s.count(ContentCommand::Down) == 0, "no down before focus");
    s.ackAll();
    check(s.kinds() == "M:a D:a U:a", "a quick click completes in order after focus");
    ContentSim f;
    f.aim(1, "a", .5f, .5f); f.sweep(1); f.clear();
    f.frame.hand[1].trigger = true; f.step();
    for (int i = 0; i < 80; ++i) f.step();  // no answer: times out
    f.frame.hand[1].trigger = false; f.step();
    check(f.count(ContentCommand::Down) == 0 && f.count(ContentCommand::Up) == 0, "an unanswered focus never clicks");
  }
  {
    // Barrier timeout: a silent old stream cannot block the new target.
    ContentSim s;
    s.aim(1, "a", .5f, .5f); s.sweep(1); s.ackAll(); s.clear();
    s.aim(1, "b", .5f, .5f); s.step();
    for (int i = 0; i < 45; ++i) s.step();
    check(s.kinds() == "B:a F:b", "focus follows a timed-out barrier");
  }
  {
    // Gaze focus over the native path, and its suppression.
    ContentSim s;
    check(s.input.gazeFocus("a", .4f, .6f, s.t), "gaze focus allowed while idle");
    auto c = s.input.drain();
    check(c.size() == 1 && c[0].kind == ContentCommand::Focus && c[0].intent == "gaze" && !c[0].deliberate,
          "gaze focus restores memory, not deliberate");
    s.input.acknowledge("a", c[0].token, true); s.step();
    check(s.input.target() == "a" && s.count(ContentCommand::Move) == 0, "gaze focus sends no move");
    s.aim(1, "a", .5f, .5f); s.sweep(1);
    check(s.input.suppressGaze() && !s.input.gazeFocus("b", .5f, .5f, s.t), "controller pointing suppresses gaze");
    s.frame.hand[1].trigger = true; s.step();
    s.off(1); for (int i = 0; i < 40; ++i) s.step();
    check(s.input.suppressGaze(), "a held button suppresses gaze");
    s.frame.hand[1].trigger = false; s.step();
    for (int i = 0; i < 40; ++i) s.step();
    check(!s.input.suppressGaze(), "gaze resumes after release");
    s.frame.menuOpen = true; s.step();
    check(s.input.suppressGaze(), "an open menu suppresses gaze");
    // With a menu open, hover reaches menus but does not raise other windows.
    s.frame.activeWindow = "a"; s.clear();
    s.aim(1, "b", .5f, .5f); s.sweep(1);
    check(s.count(ContentCommand::Focus) == 0 && s.count(ContentCommand::Barrier) == 0, "menu open: no raise on hover");
    s.aim(1, "menu0", .5f, .5f, true); s.sweep(1); s.ackAll();
    check(s.kinds().rfind("B:a F:menu0 M:menu0", 0) == 0, "menu open: the menu itself is reachable");
    s.clear();
    s.aim(1, "b", .5f, .5f); s.step();
    s.frame.hand[1].trigger = true; s.step(); s.ackAll(); s.frame.hand[1].trigger = false; s.step();
    check(s.count(ContentCommand::Down, "b") == 1, "menu open: a press on another window still goes there");
  }
  {
    // Thumbstick scroll: dead zone, acceleration, fractions carried over.
    ContentSim s;
    s.aim(1, "a", .5f, .5f); s.sweep(1); s.ackAll(); s.clear();
    s.frame.hand[1].stick[1] = .1f;
    for (int i = 0; i < 90; ++i) s.step();
    check(s.count(ContentCommand::Wheel) == 0, "inside the dead zone nothing scrolls");
    s.frame.hand[1].stick[1] = .25f;
    for (int i = 0; i < 91; ++i) s.step();
    float slow = 0;
    for (const auto &c : s.log) if (c.kind == ContentCommand::Wheel) { slow += c.dy; check(c.dy == std::trunc(c.dy), "whole pixels"); }
    const float tSlow = (.25f - ContentInput::stickDeadZone) / (1 - ContentInput::stickDeadZone);
    const float expectSlow = -(ContentInput::minScroll + (ContentInput::maxScroll - ContentInput::minScroll) * tSlow * tSlow);
    check(std::fabs(slow - expectSlow * 1.001f) < 2, "a slight push scrolls slowly, fractions accumulate to the integral");
    s.clear(); s.frame.hand[1].stick[1] = 1;
    for (int i = 0; i < 91; ++i) s.step();
    float fast = 0;
    for (const auto &c : s.log) if (c.kind == ContentCommand::Wheel) fast += c.dy;
    check(std::fabs(fast + ContentInput::maxScroll * 1.001f) < 2, "full forward scrolls up at the top speed");
    s.clear(); s.frame.hand[1].stick[1] = 0; s.frame.hand[1].stick[0] = -1;
    for (int i = 0; i < 10; ++i) s.step();
    float dx = 0;
    for (const auto &c : s.log) if (c.kind == ContentCommand::Wheel) { dx += c.dx; check(c.key == "a" && c.dy == 0, "horizontal"); }
    check(dx < 0, "stick left scrolls left");
    s.frame.hand[1].stick[0] = 0; s.off(1); s.clear();
    s.frame.hand[1].stick[1] = 1;
    for (int i = 0; i < 10; ++i) s.step();
    check(s.count(ContentCommand::Wheel) == 0, "no scroll off content");
  }
  {
    // Tracking loss: a held button lets go at the last point; then neutral.
    ContentSim s;
    s.aim(1, "a", .5f, .5f); s.sweep(1); s.ackAll(); s.clear();
    s.frame.hand[1].trigger = true; s.aim(1, "a", .6f, .6f); s.step();
    s.frame.hand[1].valid = false; s.step();
    check(s.kinds() == "M:a D:a U:a" && s.log.back().u == .6f && !s.input.buttonHeld() && !s.input.pointerOwned(),
          "tracking loss releases at the last point");
    s.frame.hand[1].valid = true; s.step(); s.step();
    check(s.count(ContentCommand::Down) == 1, "returning tracking with the trigger held does not press again");
  }
  {
    // Keepalive while held goes to the stream that owns the button.
    ContentSim s;
    s.aim(1, "a", .5f, .5f); s.sweep(1); s.ackAll(); s.clear();
    s.frame.hand[1].trigger = true; s.step();
    s.aim(1, "b", .5f, .5f); s.step(); s.ackAll();
    for (int i = 0; i < 100; ++i) s.step();
    check(s.count(ContentCommand::Keepalive, "a") >= 2 && s.count(ContentCommand::Keepalive, "b") == 0,
          "held input stays alive on its owner stream");
    // That stream resets: the helper released it; no stray up later.
    s.input.streamReset("a"); s.clear();
    for (int i = 0; i < 60; ++i) s.step();
    s.frame.hand[1].trigger = false; s.step();
    check(s.count(ContentCommand::Up) == 0 && s.count(ContentCommand::Keepalive) == 0 && s.count(ContentCommand::Move) == 0,
          "a reset owner stream ends the drag without replay");
  }
  {
    // The trackpad takes over: only deliberate laser motion takes it back.
    ContentSim s;
    s.aim(1, "a", .5f, .5f); s.sweep(1); s.ackAll(); s.clear();
    s.input.yieldToPhysical();
    check(!s.input.pointerOwned() && s.input.target().empty(), "trackpad motion ends remote hover");
    for (int i = 0; i < 30; ++i) s.step();
    s.sweep(1, 2, .01f);
    check(s.log.empty(), "small motion after a trackpad takeover does nothing");
    s.sweep(1, 6, .01f);
    check(s.kinds() == "F:a" && s.log[0].deliberate, "clear motion refocuses deliberately");
    s.ackAll(); s.clear();
    s.input.yieldToPhysical();
    s.frame.hand[1].trigger = true; s.step(); s.ackAll(); s.frame.hand[1].trigger = false; s.step();
    check(s.kinds() == "F:a M:a D:a U:a", "a press is deliberate too");
    // A pose jump is not pointing.
    ContentSim j;
    j.aim(1, "a", .5f, .5f);
    j.frame.hand[1].direction[0] = std::sin(1.f); j.frame.hand[1].direction[2] = -std::cos(1.f); j.step();
    check(j.log.empty(), "a tracking jump does not take the pointer");
  }
  {
    // Focus lost elsewhere, refusals, overflow and cancellation.
    ContentSim s;
    s.aim(1, "a", .5f, .5f); s.sweep(1); s.ackAll(); s.clear();
    s.input.focusLost("a"); s.sweep(1);
    check(s.count(ContentCommand::Focus, "a") == 1, "focus moved away: focus again before moving");
    s.ackAll(); s.clear();
    s.input.refused("a"); s.sweep(1);
    check(s.count(ContentCommand::Focus, "a") == 1, "a refused press refocuses first");
    s.ackAll(); s.clear();
    s.frame.hand[1].trigger = true; s.step();
    s.input.overflow("a"); s.clear();
    s.frame.hand[1].trigger = false; s.step();
    check(s.count(ContentCommand::Up) == 0, "after an overflow release nothing more is sent");
    ContentSim c;
    c.aim(1, "a", .5f, .5f); c.sweep(1); c.ackAll();
    c.frame.hand[1].trigger = true; c.step();
    c.aim(1, "b", .5f, .5f); c.step(); c.ackAll();
    auto released = c.input.cancelAll();
    int a = 0, b = 0;
    for (const auto &r : released) { check(r.kind == ContentCommand::Release, "cancel only releases"); a += r.key == "a"; b += r.key == "b"; }
    check(a == 1 && b == 1 && !c.input.buttonHeld() && !c.input.pointerOwned() && c.input.target().empty(),
          "Stop releases the source and drop streams once and starts neutral");
  }
}
inline void selfTestContent() {
  selfTestContentPick();
  selfTestContentArbiter();
}
} // namespace fw
