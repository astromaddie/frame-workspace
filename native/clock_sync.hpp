#pragma once
#include <array>
#include <cmath>
#include <cstddef>
#include <limits>

namespace fw {
// A congested pong's midpoint can be biased by half its RTT. Foveation uses
// the lowest-RTT observation from a bounded recent window, never the latest
// arbitrary queue delay. <=80ms RTT bounds initial clock uncertainty to40ms,
// leaving margin inside the host's unchanged50ms future guard.
class FoveationClock {
  struct Sample { double offset = 0, rtt = 0, received = -1; };
  std::array<Sample, 32> samples{};
  std::size_t next = 0;
public:
  bool observe(double sent, double host, double received) {
    const double rtt = received - sent;
    if (!std::isfinite(sent) || !std::isfinite(host) || !std::isfinite(received) ||
        sent <= 0 || host <= 0 || rtt < 0 || rtt > 80) return false;
    samples[next] = {host - (sent + received) / 2, rtt, received};
    next = (next + 1) % samples.size();
    return true;
  }
  bool estimate(double now, double &offset) const {
    if (!std::isfinite(now)) return false;
    double bestRtt = std::numeric_limits<double>::infinity(), latest = -1;
    bool found = false;
    for (const auto &s : samples) {
      if (s.received < 0 || now < s.received || now - s.received > 60000) continue;
      if (s.rtt < bestRtt || (s.rtt == bestRtt && s.received > latest)) {
        bestRtt = s.rtt; latest = s.received; offset = s.offset; found = true;
      }
    }
    return found;
  }
};
} // namespace fw
