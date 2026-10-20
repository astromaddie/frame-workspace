#!/usr/bin/env python3
"""Exercise the production Pipeline decoder lifecycle without GL or a headset."""
import json
import os
import pathlib
import shlex
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent


def split_access_units(bitstream):
    starts = []
    i = 0
    while i + 3 <= len(bitstream):
        if bitstream[i:i + 4] == b"\x00\x00\x00\x01":
            starts.append((i, 4))
            i += 4
        elif bitstream[i:i + 3] == b"\x00\x00\x01":
            starts.append((i, 3))
            i += 3
        else:
            i += 1
    nals = []
    for index, (start, prefix) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(bitstream)
        if start + prefix < end:
            nals.append((bitstream[start:end], bitstream[start + prefix] & 0x1f))
    groups = []
    current = []
    saw_aud = False
    for nal, nal_type in nals:
        if nal_type == 9:
            if saw_aud and current:
                groups.append(current)
                current = []
            saw_aud = True
        current.append((nal, nal_type))
    if current:
        groups.append(current)
    return [(b"".join(nal for nal, _ in group),
             any(nal_type == 5 for _, nal_type in group))
            for group in groups]


HARNESS = r"""
#include "video.hpp"
#include <libavutil/log.h>
#include <fstream>
#include <iostream>

using namespace fw;

static std::vector<uint8_t> readBytes(const char *path) {
  std::ifstream file(path, std::ios::binary);
  return {std::istreambuf_iterator<char>(file), {}};
}

static std::vector<uint8_t> withoutParameterSets(
    const std::vector<uint8_t> &bytes) {
  std::vector<std::pair<size_t, size_t>> starts;
  for (size_t i = 0; i + 3 < bytes.size();) {
    size_t prefix = 0;
    if (bytes[i] == 0 && bytes[i + 1] == 0 && bytes[i + 2] == 1)
      prefix = 3;
    else if (bytes[i] == 0 && bytes[i + 1] == 0 && bytes[i + 2] == 0 &&
             bytes[i + 3] == 1)
      prefix = 4;
    if (!prefix) {
      ++i;
      continue;
    }
    starts.emplace_back(i, prefix);
    i += prefix;
  }
  std::vector<uint8_t> out;
  for (size_t i = 0; i < starts.size(); ++i) {
    const auto [start, prefix] = starts[i];
    const size_t end = i + 1 < starts.size() ? starts[i + 1].first : bytes.size();
    if (start + prefix >= end) continue;
    const int type = bytes[start + prefix] & 0x1f;
    if (type != 7 && type != 8)
      out.insert(out.end(), bytes.begin() + start, bytes.begin() + end);
  }
  return out;
}

static void require(bool condition, const char *message) {
  if (!condition) throw std::runtime_error(message);
}

static bool waitFor(const std::function<bool()> &predicate, int timeoutMs = 4000) {
  const auto end = std::chrono::steady_clock::now() +
                   std::chrono::milliseconds(timeoutMs);
  while (std::chrono::steady_clock::now() < end) {
    if (predicate()) return true;
    std::this_thread::sleep_for(std::chrono::milliseconds(2));
  }
  return predicate();
}

static void enqueue(Pipeline &pipeline, const std::vector<uint8_t> &bytes,
                    uint32_t seq, bool key) {
  Packet p;
  p.bytes = bytes;
  p.seq = seq;
  p.key = key;
  p.received = nowMs();
  pipeline.enqueue(std::move(p));
}

int main(int argc, char **argv) {
  try {
    av_log_set_level(AV_LOG_QUIET);
    if (argc < 9) throw std::runtime_error("expected encoded access units");
    std::vector<std::vector<uint8_t>> units;
    for (int i = 1; i < argc; ++i) units.push_back(readBytes(argv[i]));
    require(units.size() >= 16, "encoded sequence is too short");
    require(h264HasSpsAndPps(units[0]), "initial IDR lacks SPS/PPS");
    require(h264HasSpsAndPps(units[10]), "resume IDR lacks SPS/PPS");

    Pipeline pipeline("software", "cpu");
    pipeline.pocRewrite = false;
    pipeline.decoder = std::thread([&] { pipeline.decodeLoop(); });

    // The transition must wait for every packet already accepted, including
    // the final picture immediately before the text control.
    for (size_t i = 0; i < 5; ++i)
      enqueue(pipeline, units[i], uint32_t(i + 1), i == 0);
    pipeline.setStreamStill(true);
    require(waitFor([&] {
              return pipeline.streamStill && !pipeline.decoderSessionOpen &&
                     pipeline.decoderStillReleases == 1;
            }), "still did not release the initial decoder session");
    require(pipeline.decoded == 5, "still crossed the final packet boundary");
    require(!pipeline.pocRewrite,
            "still reset over the explicitly disabled POC rewrite setting");
    std::shared_ptr<Picture> held;
    {
      std::lock_guard<std::mutex> lock(pipeline.mutex);
      held = pipeline.newest;
    }
    require(held && held->seq == 5, "still did not retain the last picture");

    // A reconnect does not cancel still. Binary video remains dropped until a
    // later live control, after which the first parameterized IDR can resume.
    pipeline.connectionOpened(true);
    enqueue(pipeline, units[5], 55, true);
    std::this_thread::sleep_for(std::chrono::milliseconds(30));
    require(pipeline.streamStill && !pipeline.decoderSessionOpen &&
                pipeline.decoderStillReleases == 1,
            "reconnect resumed video before a live control");
    {
      std::lock_guard<std::mutex> lock(pipeline.mutex);
      require(pipeline.newest == held,
              "reconnect while still replaced the held picture");
    }

    const uint64_t decodedBeforeStillTraffic = pipeline.decoded;
    const uint64_t droppedBeforeStillTraffic = pipeline.droppedEncoded;
    for (size_t i = 5; i < 8; ++i)
      enqueue(pipeline, units[i], uint32_t(50 + i), false);
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    require(pipeline.decoded == decodedBeforeStillTraffic,
            "video decoded while the stream was still");
    require(pipeline.droppedEncoded >= droppedBeforeStillTraffic + 3,
            "video arriving while still was not discarded");
    {
      std::lock_guard<std::mutex> lock(pipeline.mutex);
      require(pipeline.newest == held, "still traffic replaced the held picture");
    }

    pipeline.setStreamStill(false);
    require(waitFor([&] { return !pipeline.streamStill; }),
            "live transition was not applied");
    const uint64_t decodedBeforeResume = pipeline.decoded;
    enqueue(pipeline, units[6], 98, false);
    std::this_thread::sleep_for(std::chrono::milliseconds(30));
    require(!pipeline.decoderSessionOpen && pipeline.decoderLiveOpens == 0 &&
                pipeline.decoded == decodedBeforeResume,
            "live opened before an IDR with SPS/PPS");

    // A key-marked IDR without parameter sets is insufficient even though the
    // parser still remembers the previous session's SPS and PPS.
    std::vector<uint8_t> idrWithoutHeaders = withoutParameterSets(units[10]);
    require(!idrWithoutHeaders.empty() && !h264HasSpsAndPps(idrWithoutHeaders),
            "headerless IDR fixture was not constructed");
    enqueue(pipeline, idrWithoutHeaders, 99, true);
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
    require(!pipeline.decoderSessionOpen && pipeline.decoderLiveOpens == 0 &&
                pipeline.decoded == decodedBeforeResume,
            "headerless IDR opened the decoder");

    enqueue(pipeline, units[10], 100, true);
    require(waitFor([&] {
              return pipeline.decoderSessionOpen && pipeline.decoderLiveOpens == 1 &&
                     pipeline.decoded == decodedBeforeResume + 1;
            }), "valid live IDR did not recreate the decoder");
    require(!pipeline.pocRewrite,
            "live reset over the explicitly disabled POC rewrite setting");
    {
      std::lock_guard<std::mutex> lock(pipeline.mutex);
      require(pipeline.newest && pipeline.newest->seq == 100,
              "resumed picture did not reach the production picture queue");
    }

    pipeline.setStreamStill(true);
    require(waitFor([&] {
              return pipeline.streamStill && !pipeline.decoderSessionOpen &&
                     pipeline.decoderStillReleases == 2;
            }), "still did not release the resumed session");
    pipeline.finish();
    require(pipeline.decoderLiveOpens == 1 && pipeline.decoderStillReleases == 2,
            "lifecycle counters do not match applied transitions");

    // Queue both controls before the decoder starts. This makes the rapid
    // still/live ordering deterministic, with a keyframe behind both fences.
    Pipeline rapid("software", "cpu");
    for (size_t i = 0; i < 5; ++i)
      enqueue(rapid, units[i], uint32_t(i + 1), i == 0);
    rapid.setStreamStill(true);
    rapid.setStreamStill(false);
    for (int i = 0; i < 5000; ++i) {
      rapid.setStreamStill(true);
      rapid.setStreamStill(false);
    }
    require(rapid.streamTransitions.size() == 1 &&
                !rapid.streamTransitions.front().still &&
                rapid.streamTransitions.front().releaseSession,
            "rapid no-packet controls grew or lost their required release");
    enqueue(rapid, units[5], 200, true);
    rapid.decoder = std::thread([&] { rapid.decodeLoop(); });
    require(waitFor([&] {
              return rapid.decoderStillReleases == 1 &&
                     rapid.decoderLiveOpens == 1 && rapid.decoded == 6 &&
                     rapid.decoderSessionOpen && !rapid.streamStill;
            }), "rapid still/live controls were not applied in order");
    {
      std::lock_guard<std::mutex> lock(rapid.mutex);
      require(rapid.newest && rapid.newest->seq == 200,
              "rapid live keyframe did not reach the picture queue");
    }
    rapid.finish();
    std::cout << "still/live production Pipeline lifecycle passed; decoded="
              << pipeline.decoded + rapid.decoded << " still_releases="
              << pipeline.decoderStillReleases + rapid.decoderStillReleases
              << " live_opens=" << pipeline.decoderLiveOpens + rapid.decoderLiveOpens
              << " dropped=" << pipeline.droppedEncoded + rapid.droppedEncoded
              << '\n';
    return 0;
  } catch (const std::exception &error) {
    std::cerr << error.what() << '\n';
    return 1;
  }
}
"""


with tempfile.TemporaryDirectory(prefix="fw-still-test-") as temporary:
    temp = pathlib.Path(temporary)
    encoded = subprocess.check_output([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=size=160x96:rate=30", "-frames:v", "21", "-pix_fmt",
        "yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-tune",
        "zerolatency", "-bf", "0", "-g", "5", "-keyint_min", "5",
        "-sc_threshold", "0", "-x264-params", "aud=1:repeat-headers=1",
        "-f", "h264", "pipe:1",
    ])
    units = split_access_units(encoded)
    if len(units) != 21 or not all(units[index][1] for index in (0, 5, 10, 15, 20)):
        raise AssertionError({"access_units": len(units), "idr_indices": [
            index for index, (_, key) in enumerate(units) if key]})
    paths = []
    for index, (unit, _) in enumerate(units):
        path = temp / f"au-{index:02d}.h264"
        path.write_bytes(unit)
        paths.append(path)
    source = temp / "tests_still.cpp"
    binary = temp / "tests_still"
    source.write_text(HARNESS)
    flags = subprocess.check_output([
        "pkg-config", "--cflags", "--libs", "libavcodec", "libavutil",
        "libswscale", "json-c", "openssl",
    ], text=True)
    compiler = shlex.split(os.environ.get("CXX", "clang++"))
    subprocess.run(compiler + ["-std=c++17", "-O1", "-g", "-Wall", "-Wextra",
                               "-Wpedantic", "-Wno-unused-function", "-pthread",
                               "-I", str(ROOT / "native"),
                               str(source), "-o", str(binary)] + shlex.split(flags),
                   check=True, cwd=ROOT)
    result = subprocess.run([str(binary), *(str(path) for path in paths)],
                            check=False, text=True, capture_output=True, timeout=20)
    if result.returncode:
        raise AssertionError({"returncode": result.returncode,
                              "stdout": result.stdout, "stderr": result.stderr})
    print(json.dumps({"test": "production Pipeline still/live decoder lifecycle",
                      "result": "passed", "stdout": result.stdout.strip(),
                      "stderr": result.stderr.strip()}, sort_keys=True))
