#pragma once
// Dock clicks (FW-15 D1), apart from drawing and from the host's wiring: the
// request a click writes (with an id the later hops log), the pressed and
// "launching" feedback on the shelf's icons, and delivery of a panel the
// companion chose (an app opened that already has one). Plain data and timing,
// so self-tests can run it.
#include "host.hpp"
#include <algorithm>
#include <cmath>
#include <set>
#include <string>
#include <vector>

namespace fw {

inline constexpr float dockPressScale = .88f;        // an icon under a held trigger shrinks to this
inline constexpr double dockBounceMs = 620;          // one hop of the launching bounce
inline constexpr float dockBounceHeight = .55f;      // a hop rises this far, as a fraction of the icon's height
inline constexpr double dockLaunchMaxMs = 20000;     // a launch that never gets an answer stops bouncing here
inline constexpr double dockAckedMaxMs = 12000;      // ... and this long after the Mac says it opened the app
inline constexpr double dockChosenMaxMs = 5000;      // a chosen panel that cannot be selected is dropped after this

// How high an icon is lifted, 0..1, `ms` into a launch: hops that start at once
// (a click is answered the same frame) and come to rest on the shelf between.
inline float dockBounce(double ms) {
  if (!(ms >= 0)) return 0;
  const double t = std::fmod(ms, dockBounceMs) / dockBounceMs; // 0..1 within one hop
  return float(4 * t * (1 - t));
}

// What the companion said happened to a request (the state file's `acks`).
struct DockAck {
  std::string id, error;
  bool ok = true;
};

// The body a dock click writes. `selected` is the panel the host already brought
// forward, so the companion does not do it again.
inline std::string dockRequest(const std::string &verb, const std::string &bundle, const std::string &id,
                               const std::string &selected = "") {
  return "{\"action\":\"app\",\"verb\":" + jsonString(verb) + ",\"bundleID\":" + jsonString(bundle) +
         ",\"id\":" + jsonString(id) + (selected.empty() ? "" : ",\"selected\":" + jsonString(selected)) + "}";
}
// Request ids are unique across host restarts (the pid) and within a run (the count).
struct DockIds {
  unsigned long pid = 0;
  unsigned long serial = 0;
  std::string next() { return "d" + std::to_string(pid) + "-" + std::to_string(++serial); }
};

// One launch the user asked for: the icon bounces until the app's panel appears or
// an error comes back.
struct DockLaunch {
  std::string id, bundle;
  double since = 0, ackedAt = 0;
  int panels = 0;       // the app's panels when it was clicked
  bool acked = false;
};
enum class DockEnd { Panel, Error, Timeout };
struct DockEnded {
  DockLaunch launch;
  DockEnd why = DockEnd::Timeout;
  std::string error;
};

struct DockFeedback {
  std::string pressed;                // the tile key a held trigger is on, or empty
  std::vector<DockLaunch> launches;
  void launch(const std::string &id, const std::string &bundle, double now, int panels) {
    launches.erase(std::remove_if(launches.begin(), launches.end(),
                                  [&](const DockLaunch &l) { return l.bundle == bundle; }), launches.end());
    launches.push_back({id, bundle, now, 0, panels, false});
  }
  // The app's panel was brought forward: nothing more to wait for.
  void finish(const std::string &bundle) {
    launches.erase(std::remove_if(launches.begin(), launches.end(),
                                  [&](const DockLaunch &l) { return l.bundle == bundle; }), launches.end());
  }
  bool launching(const std::string &bundle) const {
    for (const auto &l : launches)
      if (l.bundle == bundle) return true;
    return false;
  }
  double since(const std::string &bundle) const {
    for (const auto &l : launches)
      if (l.bundle == bundle) return l.since;
    return 0;
  }
  // Called every frame with the panels each app has now (a count by bundle ID)
  // and the companion's acknowledgements. Returns the launches that ended.
  template <class Count>
  std::vector<DockEnded> observe(const std::vector<DockAck> &acks, Count panelsOf, double now) {
    std::vector<DockEnded> ended;
    for (auto it = launches.begin(); it != launches.end();) {
      DockEnded end{*it, DockEnd::Timeout, ""};
      bool done = false;
      for (const auto &ack : acks)
        if (ack.id == it->id) {
          if (!ack.ok) {
            end.why = DockEnd::Error;
            end.error = ack.error;
            done = true;
          } else if (!it->acked) {
            it->acked = true;
            it->ackedAt = now;
          }
        }
      if (!done && panelsOf(it->bundle) > it->panels) {
        end.why = DockEnd::Panel;
        done = true;
      }
      if (!done && (now - it->since >= dockLaunchMaxMs || (it->acked && now - it->ackedAt >= dockAckedMaxMs))) done = true;
      if (done) {
        end.launch = *it;
        ended.push_back(end);
        it = launches.erase(it);
      } else {
        ++it;
      }
    }
    return ended;
  }
};

// A panel the companion chose (the state's `chosen`, once per switcher sequence): the
// host takes it when it can. A panel that is not ready yet (no picture, no head pose)
// is tried again each frame for a few seconds instead of being lost.
struct DockChosen {
  std::set<int64_t> taken;
  int64_t seq = -1;
  double since = 0;
  bool failed = false;   // the first attempt of this sequence did not take it
  enum Result { Idle, Taken, Waiting, GaveUp };
  template <class Select>
  Result step(bool spatial, const std::string &chosen, int64_t sequence, double now, Select select) {
    if (!spatial || chosen.empty() || taken.count(sequence)) return Idle;
    if (sequence != seq) {
      seq = sequence;
      since = now;
      failed = false;
    }
    if (select(chosen)) {
      taken.insert(sequence);
      return Taken;
    }
    failed = true;
    if (now - since >= dockChosenMaxMs) {
      taken.insert(sequence);
      return GaveUp;
    }
    return Waiting;
  }
};

} // namespace fw
