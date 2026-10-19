// clang++ -std=c++17 -pthread native/tests_gaze_producer.cpp -o /tmp/gaze-producer-test
// Also runs under -fsanitize=thread; no graphics, runtime, or device needed.
#include "gaze_producer.hpp"
#include <algorithm>
#include <cassert>
#include <cstring>
#include <future>
#include <iostream>

using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;
static double milliseconds() {
  return std::chrono::duration<double, std::milli>(Clock::now().time_since_epoch()).count();
}
int main() {
  // This is a real producer and single-slot consumer, not a thread-flag mock.
  // Only aggregate timing/counts survive; no coordinate history is retained.
  std::mutex mutex;
  std::condition_variable wake;
  size_t count = 0;
  double newest = 0, minGap = 1e9, maxGap = 0;
  fw::GazeReading latest;
  fw::GazeProducer producer;
  const auto renderThread = std::this_thread::get_id();
  const double start = milliseconds();
  producer.start([&](double now) {
    assert(std::this_thread::get_id() != renderThread);
    return fw::diagnosticGazeReading(now, start, true, 0, 0);
  }, [&](const fw::GazeReading &reading, double sampled) {
    std::lock_guard<std::mutex> lock(mutex);
    if (count) { minGap = std::min(minGap, sampled-newest); maxGap = std::max(maxGap, sampled-newest); }
    newest = sampled; latest = reading; ++count; wake.notify_all();
  });
  {
    std::unique_lock<std::mutex> lock(mutex);
    assert(wake.wait_for(lock, 1s, [&] { return count >= 2; }));
  }
  const double blocked = milliseconds();
  std::this_thread::sleep_for(300ms); // main/render thread cannot sample or deliver
  producer.finish();
  assert(count >= 12 && count <= 22);
  assert(newest > blocked + 200 && milliseconds() - newest < 100);
  assert(minGap >= 16 && maxGap < 100); // bound cadence and unchanged freshness budget
  assert(latest.valid && latest.diagnostic && latest.u >= 0 && latest.u <= 1);
  assert(std::strcmp(producer.status(), "diagnostic-scan") == 0);
  const size_t stoppedCount = count;
  producer.finish(); // idempotent
  std::this_thread::sleep_for(25ms);
  assert(count == stoppedCount);

  // Stop during an in-flight runtime acquisition must join it before an owner
  // can destroy its OpenVR table/library. A delayed sample retains its original
  // timestamp so downstream freshness rejection still works.
  std::promise<void> entered, release;
  auto released = release.get_future().share();
  bool callbackReturned = false;
  double delayedAge = 0;
  producer.start([&](double) {
    entered.set_value(); released.wait(); callbackReturned = true;
    return fw::GazeReading{.2, .8, "valid", true, false};
  }, [&](const fw::GazeReading &, double sampled) { delayedAge = milliseconds() - sampled; });
  entered.get_future().wait();
  auto joined = std::async(std::launch::async, [&] { producer.finish(); });
  assert(joined.wait_for(120ms) == std::future_status::timeout);
  release.set_value(); joined.get();
  assert(callbackReturned && delayedAge >= 100);

  // An unavailable/throwing provider must publish invalid observations, not
  // reuse a previous valid reading or terminate the process.
  std::promise<void> invalidPublished;
  std::atomic<bool> signaled{false};
  producer.start([](double) -> fw::GazeReading { throw std::runtime_error("runtime unavailable"); },
    [&](const fw::GazeReading &reading, double) {
      assert(!reading.valid && std::strcmp(reading.status, "sampling-error") == 0);
      if (!signaled.exchange(true)) invalidPublished.set_value();
    });
  invalidPublished.get_future().wait(); producer.finish();
  assert(std::strcmp(producer.status(), "sampling-error") == 0);

  // Scope unwinding also joins before callback dependencies are destroyed.
  std::atomic<size_t> unwindingCount{0};
  try {
    fw::GazeProducer scoped;
    scoped.start([](double) { return fw::GazeReading{}; },
                 [&](const fw::GazeReading &, double) { ++unwindingCount; });
    throw std::runtime_error("render failed");
  } catch (const std::runtime_error &) {}
  const auto afterUnwind = unwindingCount.load();
  std::this_thread::sleep_for(25ms);
  assert(unwindingCount == afterUnwind);
  std::cout << "PASS producer: 300 ms blocked renderer, samples=" << count
            << " min_gap_ms=" << minGap << " max_gap_ms=" << maxGap
            << "; in-flight join, stale timestamp, restart, invalid provider, exception unwind\n";
}
