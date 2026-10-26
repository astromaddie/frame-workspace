#pragma once
// Pointer, click, drag and scroll from the controllers into Mac window
// content (FW-15). One arbiter, called once per host frame: it owns the
// single Mac cursor, latches each press to the hand and consumer it started
// on, and orders target changes across independent streams (barrier the old
// stream, focus the new one, then move and press there). Pure: the host turns
// its commands into stream messages and feeds acknowledgements back.
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <deque>
#include <string>
#include <vector>

namespace fw {

// What a hand's laser is on this frame, in host priority order: placement
// handles and the shelf/switcher/auxiliary chrome first, then content.
enum class ContentConsumer { None, Ui, Handle, Content };

struct ContentHit {
  ContentConsumer consumer = ContentConsumer::None;
  std::string key;          // the stream's panel, for Content
  bool auxiliary = false;   // menu bar strip, status band or a menu slot
  float u = 0, v = 0;       // picture position, right and down from top left
};

struct ContentHandSample {
  bool valid = false, trigger = false, secondary = false;
  float stick[2]{};         // x right, y up (forward)
  float direction[3]{};     // the laser's direction, for deliberate motion
  ContentHit hit;
};

struct ContentFrame {
  double now = 0;           // ms
  ContentHandSample hand[2];
  // A Mac menu is open: hover may move within it and the strips, but does
  // not raise another window (a press still does).
  bool menuOpen = false;
  std::string activeWindow; // the host's focused window panel
};

struct ContentCommand {
  enum Kind { Focus, Move, Down, Up, Wheel, Barrier, Release, Keepalive } kind = Move;
  std::string key;
  std::string intent = "pointer";  // Focus: gaze, pointer or drag
  bool deliberate = false;         // Focus may take the pointer back from the trackpad
  float u = 0, v = 0, dx = 0, dy = 0;
  int button = 0;                  // 0 left, 2 right
  uint64_t token = 0;              // Focus and Barrier are acknowledged
};

class ContentInput {
public:
  static constexpr double barrierTimeoutMs = 400, focusTimeoutMs = 700, failureBackoffMs = 500;
  static constexpr double offContentGraceMs = 150, idleReleaseMs = 4000, keepaliveMs = 500;
  // Laser motion that counts as deliberate pointing: about 1.5 degrees
  // within a quarter second. A controller resting on the couch never gets there.
  static constexpr float deliberateRadians = .026f, reacquireRadians = .06f;
  static constexpr double motionDecayMs = 250;
  static constexpr float maxFrameRadians = .35f;
  static constexpr float stickDeadZone = .18f, minScroll = 120.f, maxScroll = 2400.f, maxWheelStep = 400.f;

  // Commands for this frame, in order, after any acknowledgements fed in
  // since the last call.
  std::vector<ContentCommand> update(const ContentFrame &f) {
    const double dt = last > 0 ? std::clamp(f.now - last, 0.0, 100.0) : 0.0;
    last = f.now;
    menuOpen = f.menuOpen;
    expireWait(f.now);
    for (int h = 0; h < 2; ++h) trackMotion(h, f.hand[h], dt);
    for (int h = 0; h < 2; ++h) edges(h, f);
    choosePointer(f);
    hover(f);
    scroll(f, dt);
    if (held >= 0 && !heldDead && !downKey.empty() && f.now - lastKeepalive >= keepaliveMs) {
      lastKeepalive = f.now;
      push({ContentCommand::Keepalive, downKey});
    }
    for (int h = 0; h < 2; ++h) {
      prevTrigger[h] = f.hand[h].valid && f.hand[h].trigger;
      prevSecondary[h] = f.hand[h].valid && f.hand[h].secondary;
    }
    return std::exchange(out, {});
  }

  // A Focus or Barrier answered by the helper.
  void acknowledge(const std::string &key, uint64_t token, bool ok) {
    if (!token || token != waitToken) return;
    if (phase == Phase::Barrier && key == from) startFocus(last);
    else if (phase == Phase::Focusing && key == to) ok ? focusReady() : focusFailed(last);
  }
  // A press the helper refused (stale geometry, focus moved, trackpad held):
  // the target must be focused again before anything else goes there.
  void refused(const std::string &key) {
    if (key == ready) ready.clear();
    if (held >= 0 && downKey == key) heldDead = true;
  }
  // The stream reconnected, closed or its panel went: it starts neutral, and
  // the helper has already released whatever that connection held.
  void streamReset(const std::string &key) {
    pending.erase(std::remove_if(pending.begin(), pending.end(),
                                 [&](const ContentCommand &c) { return c.key == key; }), pending.end());
    if (held >= 0 && (downKey == key || dragKey == key)) heldDead = true;
    if (macDown && macDownKey == key) macDown = false;  // the helper let go on disconnect
    if (ready == key) ready.clear();
    if (phase == Phase::Barrier && from == key) startFocus(last);
    else if (phase != Phase::Idle && to == key) { phase = Phase::Idle; waitToken = 0; to.clear(); }
    if (lastKey == key) hasPoint = false;
  }
  // The window lost Mac focus some other way (switcher, Cmd-Tab, gaze over
  // the legacy path): positions under it would land on whatever covers it.
  void focusLost(const std::string &key) { if (key == ready && held < 0) ready.clear(); }
  // The physical trackpad took over: hover stops at once and comes back only
  // through deliberate controller activity.
  void yieldToPhysical() {
    if (held >= 0) return;  // a held drag carries on
    owner = -1; yielded = true; yieldMotion = 0;
    ready.clear();
  }
  // The stream's ordered queue overflowed: no edge may be silently lost, so
  // ownership ends and the stream is released.
  void overflow(const std::string &key) {
    // The caller has already cleared the queue and released the stream.
    if (held >= 0) heldDead = true;
    macDown = false;
    streamReset(key);
    owner = -1;
  }
  // Spatial Mode stopped or the host is closing: release every stream this
  // arbiter could be holding and start neutral.
  std::vector<ContentCommand> cancelAll() {
    std::vector<std::string> keys;
    for (const auto &k : {ready, from, to, downKey, dragKey})
      if (!k.empty() && std::find(keys.begin(), keys.end(), k) == keys.end()) keys.push_back(k);
    for (const auto &k : keys) push({ContentCommand::Release, k});
    auto result = std::exchange(out, {});
    *this = ContentInput{};
    return result;
  }
  // Gaze dwell focus over the native path: raise and restore that window's
  // remembered cursor. Ignored while the controller owns the pointer.
  bool gazeFocus(const std::string &key, float u, float v, double now) {
    if (suppressGaze() || key == ready || (key == failedKey && now - failedAt < failureBackoffMs)) return false;
    lastKey = key; lastU = u; lastV = v; hasPoint = true;
    request(key, "gaze", false, now);
    return true;
  }

  // Gaze dwell waits while the controller points, a button is held, a target
  // change is in flight or a Mac menu is open; it resumes afterwards.
  bool suppressGaze() const { return owner >= 0 || held >= 0 || phase != Phase::Idle || menuOpen; }
  bool pointerOwned() const { return owner >= 0; }
  int pointerHand() const { return owner; }
  bool buttonHeld() const { return held >= 0; }
  bool waiting() const { return phase != Phase::Idle; }
  const std::string &target() const { return ready; }
  // Commands produced outside update() (gaze focus) for sending now.
  std::vector<ContentCommand> drain() { return std::exchange(out, {}); }
  // Window keys whose focus the helper confirmed since the last call.
  std::vector<std::string> takeConfirmed() { return std::exchange(confirmed, {}); }

private:
  enum class Phase { Idle, Barrier, Focusing };
  struct Latch { bool active = false, content = false; };
  Latch latch[2][2];  // [hand][trigger, secondary]
  bool prevTrigger[2]{}, prevSecondary[2]{}, needsNeutral[2]{};
  float lastDir[2][3]{};
  bool hasDir[2]{};
  float motion[2]{};        // decayed recent laser rotation, radians
  bool deliberate[2]{};
  int owner = -1;
  double ownerActive = 0, offSince = -1, last = 0;
  bool yielded = false, menuOpen = false;
  float yieldMotion = 0;
  // Target handoff.
  Phase phase = Phase::Idle;
  std::string ready, from, to, toIntent = "pointer";
  bool toDeliberate = false;
  uint64_t waitToken = 0, nextToken = 1;
  double waitSince = 0;
  std::deque<ContentCommand> pending;  // edges waiting for the new target
  std::string failedKey;
  double failedAt = -1e9;
  // The last valid content point (a ray gap freezes here).
  std::string lastKey;
  float lastU = 0, lastV = 0;
  bool hasPoint = false;
  std::string sentKey;
  float sentU = -1, sentV = -1;
  // The one held content button.
  int held = -1, heldHand = -1;
  bool heldDead = false, dragAuxiliary = false;
  std::string downKey, dragKey;  // where the down went, where the drag is now
  double lastKeepalive = 0;
  float scrollAcc[2]{};
  // A down reached the Mac and its up has not been sent yet.
  bool macDown = false;
  std::string macDownKey;
  std::vector<ContentCommand> out;
  std::vector<std::string> confirmed;

  void push(ContentCommand c) {
    if (c.kind == ContentCommand::Down) { macDown = true; macDownKey = c.key; }
    if (c.kind == ContentCommand::Up || c.kind == ContentCommand::Release) macDown = false;
    out.push_back(std::move(c));
  }

  void trackMotion(int h, const ContentHandSample &s, double dt) {
    motion[h] *= float(std::exp(-dt / motionDecayMs));
    if (!s.valid) { hasDir[h] = false; deliberate[h] = false; return; }
    if (hasDir[h]) {
      float dot = 0;
      for (int i = 0; i < 3; ++i) dot += lastDir[h][i] * s.direction[i];
      const float angle = std::acos(std::clamp(dot, -1.f, 1.f));
      // A pose jump (tracking glitch, a controller put down) is not pointing.
      if (angle > maxFrameRadians) motion[h] = 0;
      else if (std::isfinite(angle)) {
        motion[h] += angle;
        if (yielded && s.hit.consumer == ContentConsumer::Content) yieldMotion += angle;
      }
    }
    for (int i = 0; i < 3; ++i) lastDir[h][i] = s.direction[i];
    hasDir[h] = true;
    const bool stick = std::hypot(s.stick[0], s.stick[1]) > stickDeadZone;
    deliberate[h] = motion[h] > deliberateRadians || stick;
  }

  void edges(int h, const ContentFrame &f) {
    const auto &s = f.hand[h];
    const bool trig = s.valid && s.trigger, sec = s.valid && s.secondary;
    const bool now[2]{trig, sec}, before[2]{prevTrigger[h], prevSecondary[h]};
    // Tracking lost with a button down: that press ended. A button still
    // held when tracking returns is not a new press; it must come up first.
    if (!s.valid && (before[0] || before[1])) needsNeutral[h] = true;
    if (needsNeutral[h] && s.valid && !s.trigger && !s.secondary) needsNeutral[h] = false;
    for (int slot = 0; slot < 2; ++slot) {
      const int button = slot == 0 ? 0 : 2;
      if (now[slot] && !before[slot]) { if (!needsNeutral[h]) press(h, slot, button, s, f.now); }
      else if (!now[slot] && before[slot]) release(h, slot, button, f.now);
    }
  }

  void press(int h, int slot, int button, const ContentHandSample &s, double now) {
    auto &l = latch[h][slot];
    l = {true, false};
    // A press belongs to whatever it started on; nothing transfers in later.
    if (s.hit.consumer != ContentConsumer::Content || held >= 0) return;
    l.content = true;
    held = button; heldHand = h; heldDead = false;
    owner = h; ownerActive = now; yielded = false; offSince = -1;
    downKey = dragKey = s.hit.key; dragAuxiliary = s.hit.auxiliary;
    lastKey = s.hit.key; lastU = s.hit.u; lastV = s.hit.v; hasPoint = true;
    lastKeepalive = now;
    scrollAcc[0] = scrollAcc[1] = 0;
    ContentCommand down{ContentCommand::Down, s.hit.key};
    down.button = button; down.u = s.hit.u; down.v = s.hit.v;
    if (phase == Phase::Idle && ready == s.hit.key) {
      move(s.hit.key, s.hit.u, s.hit.v);
      push(down);
      return;
    }
    // Waits for its window; a change already in flight finishes first, so two
    // raises never race on separate streams.
    pending.push_back(down);
    if (phase == Phase::Idle) request(s.hit.key, "pointer", true, now);
  }

  void release(int h, int slot, int button, double now) {
    auto &l = latch[h][slot];
    const bool content = l.active && l.content;
    l = {};
    if (!content || held != button || heldHand != h) return;
    if (!heldDead && hasPoint) {
      ContentCommand up{ContentCommand::Up, {}};
      up.button = button; up.u = lastU; up.v = lastV;
      auto down = std::find_if(pending.begin(), pending.end(),
                               [](const ContentCommand &c) { return c.kind == ContentCommand::Down; });
      if (down != pending.end()) { up.key = down->key; pending.push_back(up); }  // waits behind its down
      else if (phase != Phase::Idle) { up.key = to; pending.push_back(up); }     // the drop window is coming
      else { up.key = ready.empty() ? downKey : ready; push(up); }  // focus lost mid-drag: where it went down
    }
    held = heldHand = -1; heldDead = false; downKey.clear(); dragKey.clear();
    ownerActive = now;
  }

  bool latchedElsewhere(int h) const {
    for (const auto &l : latch[h]) if (l.active && !l.content) return true;
    return false;
  }

  void choosePointer(const ContentFrame &f) {
    if (held >= 0) { owner = heldHand; return; }
    if (owner >= 0) {
      const auto &s = f.hand[owner];
      if (!s.valid || latchedElsewhere(owner)) owner = -1;
      else if (s.hit.consumer != ContentConsumer::Content) {
        if (offSince < 0) offSince = f.now;
        if (f.now - offSince > offContentGraceMs) owner = -1;
      } else offSince = -1;
      if (owner >= 0 && deliberate[owner]) ownerActive = f.now;
      if (owner >= 0 && f.now - ownerActive > idleReleaseMs) owner = -1;
    }
    if (owner >= 0) return;
    if (yielded && yieldMotion < reacquireRadians) return;
    for (int h : {1, 0}) {  // the right hand wins a tie, as in placement
      const auto &s = f.hand[h];
      if (s.valid && !latchedElsewhere(h) && s.hit.consumer == ContentConsumer::Content && deliberate[h]) {
        owner = h; ownerActive = f.now; offSince = -1;
        if (yielded) { yielded = false; ready.clear(); }
        return;
      }
    }
  }

  void hover(const ContentFrame &f) {
    if (owner < 0 || (held >= 0 && heldDead)) return;
    const auto &s = f.hand[owner];
    if (s.hit.consumer != ContentConsumer::Content) return;  // a gap freezes the last point
    const auto &key = s.hit.key;
    if (held >= 0) {
      // A drag continues across windows on the shared display, never into a
      // strip or menu (or out of one).
      if (key != dragKey && (s.hit.auxiliary || dragAuxiliary)) return;
      lastKey = key; lastU = s.hit.u; lastV = s.hit.v; hasPoint = true;
      if (phase == Phase::Idle && key == ready) { move(key, s.hit.u, s.hit.v); return; }
      if (phase == Phase::Idle) { request(key, "drag", true, f.now); dragKey = key; }
      return;
    }
    // While a menu is open the strips and menus stay reachable, but resting
    // over another window does not raise it and dismiss the menu.
    if (menuOpen && !s.hit.auxiliary && key != ready && key != f.activeWindow) return;
    lastKey = key; lastU = s.hit.u; lastV = s.hit.v; hasPoint = true;
    if (phase != Phase::Idle) return;
    if (key == ready) { move(key, s.hit.u, s.hit.v); return; }
    if (key == failedKey && f.now - failedAt < failureBackoffMs) return;
    request(key, "pointer", true, f.now);
  }

  void move(const std::string &key, float u, float v) {
    if (key == sentKey && std::fabs(u - sentU) < 1e-4f && std::fabs(v - sentV) < 1e-4f) return;
    ContentCommand m{ContentCommand::Move, key};
    m.u = u; m.v = v;
    push(m);
    sentKey = key; sentU = u; sentV = v;
  }

  void scroll(const ContentFrame &f, double dt) {
    const bool usable = owner >= 0 && held < 0 && phase == Phase::Idle && !ready.empty() &&
        f.hand[owner].hit.consumer == ContentConsumer::Content && f.hand[owner].hit.key == ready;
    const float x = usable ? f.hand[owner].stick[0] : 0, y = usable ? f.hand[owner].stick[1] : 0;
    const float r = std::hypot(x, y);
    if (!usable || !std::isfinite(r) || r <= stickDeadZone) { scrollAcc[0] = scrollAcc[1] = 0; return; }
    const float t = std::min(1.f, (r - stickDeadZone) / (1 - stickDeadZone));
    const float speed = minScroll + (maxScroll - minScroll) * t * t;  // pixels per second
    // Stick forward scrolls up (towards the top); right scrolls right.
    scrollAcc[0] += float(speed * x / r * dt / 1000);
    scrollAcc[1] += float(-speed * y / r * dt / 1000);
    const float dx = std::clamp(std::trunc(scrollAcc[0]), -maxWheelStep, maxWheelStep);
    const float dy = std::clamp(std::trunc(scrollAcc[1]), -maxWheelStep, maxWheelStep);
    if (dx == 0 && dy == 0) return;
    scrollAcc[0] -= dx; scrollAcc[1] -= dy;
    ContentCommand w{ContentCommand::Wheel, ready};
    w.dx = dx; w.dy = dy; w.u = f.hand[owner].hit.u; w.v = f.hand[owner].hit.v;
    push(w);
  }

  // A different stream becomes the target: drain the old one first.
  void request(const std::string &key, const std::string &intent, bool deliberateFocus, double now) {
    to = key; toIntent = intent; toDeliberate = deliberateFocus;
    if (!ready.empty() && ready != key) {
      from = ready;
      ready.clear();
      phase = Phase::Barrier;
      waitToken = nextToken++;
      waitSince = now;
      push({ContentCommand::Barrier, from, "pointer", false, 0, 0, 0, 0, 0, waitToken});
      return;
    }
    ready.clear();
    startFocus(now);
  }

  void startFocus(double now) {
    phase = Phase::Focusing;
    waitToken = nextToken++;
    waitSince = now;
    ContentCommand c{ContentCommand::Focus, to, toIntent, toDeliberate};
    c.token = waitToken;
    if (hasPoint && lastKey == to) { c.u = lastU; c.v = lastV; } else { c.u = c.v = .5f; }
    push(c);
  }

  void focusReady() {
    phase = Phase::Idle; waitToken = 0;
    ready = to; from.clear();
    confirmed.push_back(ready);
    if (held >= 0) dragKey = ready;
    sentKey.clear();
    // Gaze restored the window's own cursor; pointer and drag continue from
    // the laser.
    if (toIntent == "gaze") { sentKey = ready; sentU = lastU; sentV = lastV; }
    else if (hasPoint && lastKey == ready) move(ready, lastU, lastV);
    std::deque<ContentCommand> rest;
    for (auto &c : pending) if (c.key == ready) push(c); else rest.push_back(c);
    pending.swap(rest);
    if (!pending.empty()) request(pending.front().key, "pointer", true, last);
  }

  void focusFailed(double now) {
    failedKey = to; failedAt = now;
    phase = Phase::Idle; waitToken = 0;
    const std::string fallback = from;
    to.clear(); from.clear();
    // A drag whose drop window would not come forward stays where it was and
    // lets go there; a press that never reached the Mac has nothing to let go.
    if (macDown) {
      ready = fallback;
      if (held >= 0) dragKey = fallback.empty() ? macDownKey : fallback;
      std::deque<ContentCommand> ups;
      for (auto &c : pending)
        if (c.kind == ContentCommand::Up) { c.key = fallback.empty() ? macDownKey : fallback; ups.push_back(c); }
      pending.clear();
      for (auto &c : ups) push(c);
    } else {
      pending.clear();
      if (held >= 0) heldDead = true;
    }
  }

  void expireWait(double now) {
    if (phase == Phase::Barrier && now - waitSince > barrierTimeoutMs) startFocus(now);  // old stream silent
    else if (phase == Phase::Focusing && now - waitSince > focusTimeoutMs) focusFailed(now);
  }
};

} // namespace fw
