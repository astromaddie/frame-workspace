#pragma once
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <utility>

namespace fw {
struct GazeReading {
  double u = 0, v = 0;
  // Status strings must have static lifetime. Coordinates stay in the current
  // reading and Pipeline's single newest-sample slot, never in a history.
  const char *status = "unavailable";
  bool valid = false, diagnostic = false;
};
inline GazeReading diagnosticGazeReading(double now, double start, bool scan,
                                        double u, double v) {
  if (scan) {
    u = .5 + .3 * std::sin((now - start) * .0015707963267948966);
    v = .5 + .3 * std::cos((now - start) * .0015707963267948966);
  }
  return {u, v, scan ? "diagnostic-scan" : "diagnostic", true, true};
}

// Owned by the main thread, declared after its sample/publish dependencies.
// Sample must not touch SDL/GL; publish only replaces the newest sample. Neither
// callback performs rendering or network I/O. finish() joins any in-flight
// sample before those dependencies (including OpenVR) can be destroyed.
class GazeProducer {
  std::thread worker;
  std::mutex mutex;
  std::condition_variable wake;
  bool stopping = false;
  std::atomic<const char *> latestStatus{"disabled"};
public:
  GazeProducer() = default;
  GazeProducer(const GazeProducer &) = delete;
  GazeProducer &operator=(const GazeProducer &) = delete;
  ~GazeProducer() { finish(); }
  const char *status() const { return latestStatus.load(); }

  template <class Sample, class Publish>
  void start(Sample sample, Publish publish) {
    if (worker.joinable()) throw std::logic_error("gaze producer already started");
    stopping = false;
    latestStatus = "starting";
    worker = std::thread([this, sample = std::move(sample), publish = std::move(publish)] {
      using Clock = std::chrono::steady_clock;
      const auto period = std::chrono::duration_cast<Clock::duration>(
          std::chrono::duration<double>(1.0 / 60));
      std::unique_lock<std::mutex> lock(mutex);
      while (!stopping) {
        lock.unlock();
        const auto began = Clock::now();
        // Timestamp before acquisition: a delayed runtime call must not make
        // an old observation appear fresh to the unchanged transport guard.
        const double sampled = std::chrono::duration<double, std::milli>(
            began.time_since_epoch()).count();
        GazeReading reading;
        try { reading = sample(sampled); }
        catch (...) { reading.status = "sampling-error"; }
        publish(reading, sampled);
        latestStatus = reading.status;
        auto next = began + period;
        const auto completed = Clock::now();
        if (next < completed) next = completed + period; // Never catch up in bursts.
        lock.lock();
        wake.wait_until(lock, next, [this] { return stopping; });
      }
    });
  }
  void finish() {
    { std::lock_guard<std::mutex> lock(mutex); stopping = true; }
    wake.notify_all();
    if (worker.joinable()) worker.join();
  }
};
} // namespace fw
