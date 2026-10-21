#pragma once
#include "vendor/openvr/openvr_capi.h"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <string>
#include <unordered_set>
#include <vector>

namespace fw {
// The dashboard and thumbnail live in a separate SteamVR process. Keep two
// slots reserved for them when applying the host's configurable total budget.
inline constexpr int dashboardOverlayReserve = 2;
inline constexpr int defaultNativeOverlayBudget = 96;
inline bool frameWallGroupActive(const VROverlayHandle_t handles[4]) {
  return handles[0] && handles[1] && handles[2] && handles[3];
}
inline int nativeOverlayBudgetFromEnvironment() {
  const char *raw = std::getenv("FRAME_NATIVE_OVERLAY_BUDGET");
  if (!raw || !*raw) return defaultNativeOverlayBudget;
  char *end = nullptr;
  long value = std::strtol(raw, &end, 10);
  if (end == raw || *end || value < 1 || value > 128) {
    std::fprintf(stderr, "[native-host] invalid FRAME_NATIVE_OVERLAY_BUDGET; using %d\n",
                 defaultNativeOverlayBudget);
    return defaultNativeOverlayBudget;
  }
  return int(value);
}

struct WallPriority {
  const void *owner = nullptr;
  std::string key;
  float gazeAngle = 3.14159265f;
  uint64_t focusOrder = 0;
};

struct WallTransitionSummary {
  bool restored = false;
  int count = 0;
  int budget = 0;
};

// Coalesces rapid wall changes per window. A caller flushes pending summaries
// from the host loop, so the final state is still logged after pressure ends.
class WallTransitionLog {
public:
  using Clock = std::chrono::steady_clock;
private:
  bool emitted_ = false, pending_ = false;
  Clock::time_point lastEmission_{};
  WallTransitionSummary pendingSummary_;
public:
  bool flush(Clock::time_point now, bool force, WallTransitionSummary &summary) {
    if (!pending_ || (emitted_ && !force && now - lastEmission_ < std::chrono::seconds(1)))
      return false;
    summary = pendingSummary_;
    pending_ = false;
    emitted_ = true;
    lastEmission_ = now;
    return true;
  }
  bool record(bool restored, int count, int budget, Clock::time_point now,
              WallTransitionSummary &summary) {
    pending_ = true;
    pendingSummary_ = {restored, count, budget};
    return flush(now, false, summary);
  }
};

// Tracks every overlay created by the native host and treats a window's four
// frame walls as one optional group. Create/destroy backends are supplied by
// OverlaySession, which keeps the policy testable without an OpenVR runtime.
class OverlayBudget {
public:
  using Handle = VROverlayHandle_t;
  using Create = std::function<EVROverlayError(const std::string &, const std::string &, Handle *)>;
  using Destroy = std::function<EVROverlayError(Handle)>;
private:
  struct WallGroup {
    const void *owner = nullptr;
    std::string key;
    int overlays = 4;
    float gazeAngle = 3.14159265f;
    uint64_t focusOrder = 0;
    bool focused = false, allocated = false;
    bool retryBlocked = false;
    size_t retryBlockedAtCount = 0;
    std::function<bool()> drop, restore;
  };
  int totalBudget_ = defaultNativeOverlayBudget;
  int reserve_ = dashboardOverlayReserve;
  size_t count_ = 0;
  std::unordered_set<Handle> live_;
  std::vector<WallGroup> walls_;
  const void *restoring_ = nullptr;
  size_t restoreLimit_ = 0;
  bool overflowLogged_ = false;

  int hostLimit() const { return std::max(0, totalBudget_ - reserve_); }
  int reportedCount() const { return int(count_) + reserve_; }
  WallGroup *group(const void *owner) {
    auto it = std::find_if(walls_.begin(), walls_.end(), [&](const auto &g) { return g.owner == owner; });
    return it == walls_.end() ? nullptr : &*it;
  }
  void track(Handle handle) {
    if (handle && live_.insert(handle).second) ++count_;
  }
  bool destroyTracked(Handle handle, const Destroy &destroy) {
    if (!handle || !live_.count(handle)) return false;
    EVROverlayError error = destroy(handle);
    if (error != EVROverlayError_VROverlayError_None) return false;
    live_.erase(handle);
    --count_;
    return true;
  }
  void dropForRoom(size_t requested) {
    std::unordered_set<const void *> failedThisPass;
    while (count_ + requested > size_t(hostLimit())) {
      WallGroup *candidate = nullptr;
      for (auto &g : walls_) {
        if (!g.allocated || g.focused || failedThisPass.count(g.owner)) continue;
        if (!candidate || g.gazeAngle > candidate->gazeAngle ||
            (g.gazeAngle == candidate->gazeAngle && g.focusOrder < candidate->focusOrder) ||
            (g.gazeAngle == candidate->gazeAngle && g.focusOrder == candidate->focusOrder && g.key < candidate->key))
          candidate = &g;
      }
      if (!candidate) break;
      if (!candidate->drop || !candidate->drop()) {
        failedThisPass.insert(candidate->owner);
        continue;
      }
      candidate->allocated = false;
    }
  }
public:
  explicit OverlayBudget(int totalBudget = defaultNativeOverlayBudget,
                         int reserve = dashboardOverlayReserve)
      : totalBudget_(std::max(1, std::min(totalBudget, 128))),
        reserve_(std::max(0, reserve)) {}

  size_t count() const { return count_; }
  int totalBudget() const { return totalBudget_; }
  int reserved() const { return reserve_; }
  int diagnosticCount() const { return reportedCount(); }
  bool wallsAllocated(const void *owner) const {
    auto it = std::find_if(walls_.begin(), walls_.end(), [&](const auto &g) { return g.owner == owner; });
    return it != walls_.end() && it->allocated;
  }
  bool canRestoreWalls(const void *owner) const {
    auto it = std::find_if(walls_.begin(), walls_.end(), [&](const auto &g) { return g.owner == owner; });
    return it != walls_.end() && !it->allocated &&
           count_ + size_t(it->overlays) <= size_t(std::max(0, hostLimit() - 4));
  }
  bool restoringWalls(const void *owner) const { return restoring_ == owner; }

  EVROverlayError createRequired(const std::string &key, const std::string &name,
                                Handle *handle, const Create &create) {
    dropForRoom(1);
    EVROverlayError error = create(key, name, handle);
    if (error == EVROverlayError_VROverlayError_None) {
      track(*handle);
      if (reportedCount() > totalBudget_ && !overflowLogged_) {
        overflowLogged_ = true;
        std::fprintf(stderr,
                     "[native-host] required overlay exceeds budget key=%s count=%d budget=%d reserved=%d\n",
                     key.c_str(), reportedCount(), totalBudget_, reserve_);
      }
    }
    return error;
  }
  EVROverlayError createOptional(const void *owner, const std::string &key,
                                 const std::string &name, Handle *handle,
                                 const Create &create) {
    if (!restoringWalls(owner) || count_ >= restoreLimit_)
      return EVROverlayError_VROverlayError_InvalidParameter;
    EVROverlayError error = create(key, name, handle);
    if (error == EVROverlayError_VROverlayError_None) track(*handle);
    return error;
  }
  bool destroy(Handle handle, const Destroy &destroy) {
    return destroyTracked(handle, destroy);
  }

  void registerWalls(const void *owner, std::string key, std::function<bool()> drop,
                     std::function<bool()> restore) {
    if (group(owner)) return;
    walls_.push_back({owner, std::move(key), 4, 3.14159265f, 0, false, false, false, 0,
                      std::move(drop), std::move(restore)});
    reconcileWalls();
  }
  void unregisterWalls(const void *owner) {
    auto it = std::find_if(walls_.begin(), walls_.end(), [&](const auto &g) { return g.owner == owner; });
    if (it == walls_.end()) return;
    if (it->allocated && it->drop) it->drop();
    walls_.erase(it);
  }
  void updateWalls(const void *owner, float gazeAngle, uint64_t focusOrder, bool focused) {
    auto *g = group(owner);
    if (!g) return;
    g->gazeAngle = std::isfinite(gazeAngle) ? gazeAngle : 3.14159265f;
    g->focusOrder = focusOrder;
    g->focused = focused;
  }
  void updateWallPriorities(const std::vector<WallPriority> &priorities,
                            const std::string &focusedKey) {
    for (const auto &priority : priorities)
      updateWalls(priority.owner, priority.gazeAngle, priority.focusOrder,
                  priority.key == focusedKey);
  }
  void reconcileWalls() {
    std::vector<WallGroup *> candidates;
    for (auto &g : walls_) if (!g.allocated) candidates.push_back(&g);
    std::sort(candidates.begin(), candidates.end(), [](const auto *a, const auto *b) {
      if (a->focused != b->focused) return a->focused > b->focused;
      if (a->gazeAngle != b->gazeAngle) return a->gazeAngle < b->gazeAngle;
      if (a->focusOrder != b->focusOrder) return a->focusOrder > b->focusOrder;
      return a->key < b->key;
    });
    for (auto *g : candidates) {
      if (g->retryBlocked && g->retryBlockedAtCount == count_) continue;
      if (!canRestoreWalls(g->owner) || !g->restore) continue;
      restoring_ = g->owner;
      restoreLimit_ = count_ + size_t(g->overlays);
      bool restored = g->restore();
      restoring_ = nullptr;
      restoreLimit_ = 0;
      if (restored) {
        g->allocated = true;
        g->retryBlocked = false;
      } else {
        // A failed optional restore should not make the host retry an OpenVR
        // allocation every frame. Any ledger change makes the group eligible
        // for a fresh attempt.
        g->retryBlocked = true;
        g->retryBlockedAtCount = count_;
      }
    }
  }
};
} // namespace fw
