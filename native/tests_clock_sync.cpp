// Standalone host check; no GL, FFmpeg, OpenVR or device required.
#include "clock_sync.hpp"
#include <cassert>
#include <iostream>
int main() {
  fw::FoveationClock clock;
  double offset = 0;
  assert(!clock.estimate(1000, offset));
  assert(!clock.observe(1000, 6000, 999));
  assert(!clock.observe(1000, NAN, 1010));
  assert(clock.observe(1000, 6005, 1010)); // true host offset5000ms, RTT10ms
  assert(clock.estimate(1010, offset) && offset == 5000);
  assert(clock.observe(2000, 7001, 2070)); // pong queued69ms: naive bias-34ms
  assert(clock.estimate(2070, offset) && offset == 5000);
  assert(!clock.observe(3000, 8500, 3500)); // request queued500ms: naive bias+250ms
  assert(clock.estimate(3500, offset) && offset == 5000);
  assert(!clock.observe(4000, 9001, 4500)); // pong queued499ms: naive bias-249ms
  assert(clock.estimate(4500, offset) && offset == 5000);
  // Every fresh sample retains its actual host age despite congested resyncs.
  for (int i = 0; i < 120; ++i) {
    double sampled = 5000 + i * (1000.0 / 60), arrival = sampled + 10;
    assert(clock.estimate(arrival, offset));
    double age = arrival + 5000 - (sampled + offset);
    assert(age == 10 && age >= -50 && age <= 150);
  }
  assert(!clock.estimate(62071, offset)); // all accepted observations expired
  assert(clock.observe(63000, 68005, 63010));
  assert(clock.estimate(63010, offset) && offset == 5000);
  fw::FoveationClock reconnect;
  assert(!reconnect.estimate(63010, offset));
  assert(reconnect.observe(64000, 69005, 64010));
  assert(reconnect.estimate(64010, offset) && offset == 5000);
  // Better RTT may legitimately adjust mapping; sample ordering is separate.
  assert(reconnect.observe(65000, 70001, 65002));
  assert(reconnect.estimate(65002, offset) && offset == 5000);
  std::cout << "PASS bounded low-RTT clock: asymmetric queued pongs, exact freshness, expiry and reconnect\n";
}
