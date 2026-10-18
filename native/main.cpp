#include "overlay.hpp"
#include <cassert>
#include <csignal>
#include <iostream>
#include <utility>
using namespace fw;
static volatile sig_atomic_t interrupted = 0;
static void signalHandler(int number) { interrupted = number; }
static void require(bool ok, const char *message) {
  if (!ok)
    throw std::runtime_error(message);
}
static int selfTest() {
  auto e = parseURL(
      "http://127.0.0.1:9123/"
      "view?src=window%3A42&t=secret&codec=h264&max=4096&fps=30&bpp=.14");
  require(e.source == "window:42" && e.port == 9123 && e.max == 4096,
          "URL decode");
  require(e.path("reconnect").find("secret") == std::string::npos,
          "ticket replaced");
  for (auto s : {"http://evil.test/view?src=test&t=s",
                 "http://localhost.evil/view?src=test&t=s",
                 "http://127.0.0.1:0/view?src=test&t=s",
                 "http://127.0.0.1/view?src=test&t=%0d",
                 "http://localhost/view?src=a&src=b&t=s",
                 "http://localhost/view?src=a&t=s&max=9999999999999",
                 "http://localhost/view?src=a&t=s&bpp=nan"}) {
    bool rejected = false;
    try {
      parseURL(s);
    } catch (...) {
      rejected = true;
    }
    require(rejected, "unsafe URL accepted");
  }
  std::vector<uint8_t> bytes(18);
  bytes[0] = 1;
  bytes[9] = 0x12;
  bytes[10] = 0x34;
  bytes[11] = 0x56;
  bytes[12] = 0x78;
  bytes[17] = 7;
  auto p = packet(bytes);
  require(p.key && p.seq == 0x12345678 && p.bytes[0] == 7, "wire packet");
  Pipeline pipe;
  for (int i = 0; i < 8; i++) {
    Packet q;
    q.key = i == 0;
    q.seq = i;
    pipe.enqueue(std::move(q));
  }
  require(pipe.packets.empty() && pipe.needKey && pipe.requestKey,
          "queue recovery");
  Packet idr;
  idr.key = true;
  pipe.enqueue(idr);
  require(pipe.packets.size() == 1 && pipe.packets.front().reset,
          "IDR recovery");
  auto a = std::make_shared<Picture>();
  a->seq = 1;
  auto b = std::make_shared<Picture>();
  b->seq = 2;
  pipe.publish(a);
  pipe.publish(b);
  require(pipe.take()->seq == 2 && pipe.droppedDecoded == 1, "newest picture");
  std::cout
      << "native self-test passed: URL allowlist, wire framing, reconnect "
         "credential isolation, bounded queue, IDR recovery, newest picture\n";
  return 0;
}
static std::shared_ptr<Picture> pattern(uint32_t seq) {
  auto p = std::make_shared<Picture>();
  p->width = 1920;
  p->height = 1080;
  p->seq = seq;
  p->rgba.resize(size_t(p->width) * p->height * 4);
  for (int y = 0; y < p->height; y++)
    for (int x = 0; x < p->width; x++) {
      auto i = (size_t(y) * p->width + x) * 4;
      bool grid = x % 100 < 2 || y % 100 < 2;
      bool bar = (x / 30) == int(seq % 64);
      p->rgba[i] = grid ? 255 : (x < 960 ? 220 : 30);
      p->rgba[i + 1] = grid ? 255 : (y < 540 ? 180 : 30);
      p->rgba[i + 2] = bar ? 255 : 30;
      p->rgba[i + 3] = 255;
    }
  p->received = p->decoded = nowMs();
  return p;
}
int main(int argc, char **argv) {
  std::string urlFile, controlFile, stateFile, poseFile, dumpPath,
      key = "com.frame-workspace.native.workspace", decoderMode = "software",
      exitReason = "running";
  std::vector<std::string> inspectFiles;
  bool test = false, noVR = false;
  double seconds = 0;
  try {
    for (int i = 1; i < argc; i++) {
      std::string a = argv[i];
      auto value = [&]() {
        if (++i >= argc)
          throw std::runtime_error("missing option value");
        return std::string(argv[i]);
      };
      if (a == "--self-test")
        return selfTest();
      else if (a == "--url-file")
        urlFile = value();
      else if (a == "--control-file")
        controlFile = value();
      else if (a == "--state-file")
        stateFile = value();
      else if (a == "--pose-file")
        poseFile = value();
      else if (a == "--overlay-key")
        key = value();
      else if (a == "--inspect-h264")
        inspectFiles.push_back(value());
      else if (a == "--decoder")
        decoderMode = value();
      else if (a == "--dump")
        dumpPath = value();
      else if (a == "--seconds")
        seconds = std::stod(value());
      else if (a == "--test-pattern")
        test = true;
      else if (a == "--no-vr")
        noVR = true;
      else if (a == "--help") {
        std::cout
            << "frame-workspace-native --url-file PATH [--control-file PATH] "
               "[--state-file PATH] [--pose-file PATH] [--overlay-key KEY] "
               "[--decoder software|v4l2] [--seconds N] [--dump "
               "PATH.ppm]\nDiagnostics: --self-test | "
               "--test-pattern [--no-vr] [--seconds N]\n";
        return 0;
      } else
        throw std::runtime_error("unknown option");
    }
    if (key.empty() || key.size() > 128 ||
        key.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUV"
                              "WXYZ0123456789._-") != std::string::npos)
      throw std::runtime_error("invalid overlay key");
    if (!std::isfinite(seconds) || seconds < 0 || seconds > 86400)
      throw std::runtime_error("invalid duration");
    if (decoderMode != "software" && decoderMode != "v4l2")
      throw std::runtime_error("decoder must be software or v4l2");
    if (!inspectFiles.empty()) {
      H264Headers headers;
      for (const auto &path : inspectFiles) {
        auto data = readFile(path, 16 * 1024 * 1024);
        int result =
            headers.parse(std::vector<uint8_t>(data.begin(), data.end()));
        if (result < 0)
          throw std::runtime_error("H264 header inspection failed");
        std::cout << "{\"width\":" << headers.parser->width
                  << ",\"height\":" << headers.parser->height
                  << ",\"codedWidth\":" << headers.parser->coded_width
                  << ",\"codedHeight\":" << headers.parser->coded_height
                  << ",\"keyFrame\":" << headers.parser->key_frame << "}\n";
      }
      return 0;
    }
    Endpoint endpoint;
    if (!test)
      endpoint = parseURL(readFile(urlFile));
    signal(SIGTERM, signalHandler);
    signal(SIGINT, signalHandler);
    signal(SIGPIPE, SIG_IGN);
    av_log_set_level(AV_LOG_ERROR);
    Overlay overlay(noVR, key);
    bool persisted = loadPlacement(poseFile, overlay.placement);
    bool pendingRecenter = !persisted;
    if (pendingRecenter)
      pendingRecenter = !overlay.recenter();
    overlay.apply();
    if (!pendingRecenter)
      atomicWrite(poseFile, placementJSON(overlay.placement));
    Pipeline pipe(decoderMode);
    if (!test)
      pipe.start(endpoint);
    double start = nowMs(), lastState = start, lastLog = start, lastControl = 0,
           lastPattern = 0, previousState = start;
    uint64_t uploads = 0, lastUploads = 0, lastDecoded = 0, submitErrors = 0;
    int w = 0, h = 0;
    double uploadMs = 0, ageMs = 0, decodeFps = 0, uploadFps = 0;
    bool ready = false, dumped = false;
    double revision = -1;
    std::shared_ptr<Picture> retryPicture;
    auto state = [&](const char *status) {
      return "{\"version\":1,\"state\":\"" + std::string(status) +
             "\",\"ready\":" + (ready ? "true" : "false") +
             ",\"diagnostic\":" + (test ? "true" : "false") +
             ",\"headsetVerified\":false,\"overlayKey\":\"" + key +
             "\",\"textureWidth\":" + std::to_string(w) +
             ",\"textureHeight\":" + std::to_string(h) +
             ",\"received\":" + std::to_string(pipe.received) +
             ",\"decoded\":" + std::to_string(pipe.decoded) +
             ",\"uploaded\":" + std::to_string(uploads) +
             ",\"decodeFps\":" + std::to_string(decodeFps) +
             ",\"uploadFps\":" + std::to_string(uploadFps) +
             ",\"decodeMs\":" + std::to_string(pipe.decodeMs) +
             ",\"sendMs\":" + std::to_string(pipe.sendMs) +
             ",\"convertMs\":" + std::to_string(pipe.convertMs) +
             ",\"uploadMs\":" + std::to_string(uploadMs) +
             ",\"syncMs\":" + std::to_string(overlay.syncMs) +
             ",\"submitMs\":" + std::to_string(overlay.submitMs) +
             ",\"queueAgeMs\":" + std::to_string(ageMs) +
             ",\"droppedEncoded\":" + std::to_string(pipe.droppedEncoded) +
             ",\"droppedDecoded\":" + std::to_string(pipe.droppedDecoded) +
             ",\"errors\":" + std::to_string(pipe.errors + submitErrors) +
             ",\"submitError\":" + std::to_string(overlay.lastSubmitError) +
             ",\"decoder\":\"" + pipe.decoderIdentity() + "\"" +
             ",\"decoderRequested\":\"" + decoderMode + "\"" +
             ",\"decoderActive\":\"" + (pipe.decoderActive ? decoderMode : "") +
             "\"" + ",\"decoderCodec\":\"" + pipe.codecName() + "\"" +
             ",\"decoderInputWidth\":" + std::to_string(pipe.inputWidth) +
             ",\"decoderInputHeight\":" + std::to_string(pipe.inputHeight) +
             ",\"decoderCodedWidth\":" + std::to_string(pipe.codedWidth) +
             ",\"decoderCodedHeight\":" + std::to_string(pipe.codedHeight) +
             ",\"decoderError\":" + std::to_string(pipe.decoderError) +
             ",\"decoderErrorStage\":\"" + pipe.decoderErrorStage.load() +
             "\"" + ",\"exitReason\":\"" + exitReason +
             "\",\"connected\":" + (pipe.connected ? "true" : "false") +
             ",\"pendingRecenter\":" + (pendingRecenter ? "true" : "false") +
             ",\"placement\":" + placementJSON(overlay.placement) + "}";
    };
    atomicWrite(stateFile, state("connecting"));
    while (!interrupted && !pipe.stop &&
           (seconds == 0 || nowMs() - start < seconds * 1000)) {
      double now = nowMs();
      if (!overlay.events()) {
        exitReason = overlay.exitReason;
        break;
      }
      if (now - lastControl > 100) {
        lastControl = now;
        if (!controlFile.empty()) {
          try {
            auto j = json(readFile(controlFile));
            double r = num(j, "revision", -1);
            if (r >= 0 && r != revision) {
              revision = r;
              overlay.resize(num(j, "width", overlay.placement.width),
                             num(j, "distance", overlay.placement.distance));
              if (flag(j, "recenter"))
                pendingRecenter = true;
              if (flag(j, "focus")) {
                pipe.focus = true;
                overlay.focus();
              }
              if (flag(j, "stop")) {
                exitReason = "control-stop";
                break;
              }
              atomicWrite(poseFile, placementJSON(overlay.placement));
            }
          } catch (const std::exception &) { /* Atomic replacement can briefly
                                                be absent; no secrets logged. */
          }
        }
        if (pendingRecenter && overlay.recenter()) {
          pendingRecenter = false;
          overlay.apply();
          atomicWrite(poseFile, placementJSON(overlay.placement));
        }
      }
      auto picture = pipe.take();
      if (test && now - lastPattern >= 1000.0 / 30) {
        picture = pattern(uploads);
        lastPattern = now;
      }
      if (!picture && retryPicture && now - lastPattern > 100) {
        picture = retryPicture;
        lastPattern = now;
      }
      if (picture) {
        double before = nowMs();
        bool submitted = overlay.upload(*picture);
        uploadMs = nowMs() - before;
        if (submitted) {
          ready = true;
          ++uploads;
          w = picture->width;
          h = picture->height;
          ageMs = nowMs() - picture->received;
          pipe.report(*picture, nowMs());
          retryPicture.reset();
          if (!dumpPath.empty() && !dumped) {
            overlay.dump(dumpPath);
            dumped = true;
          }
        } else {
          ++submitErrors;
          retryPicture = picture;
        }
      }
      if (now - lastState >= 1000) {
        double dt = (now - previousState) / 1000;
        decodeFps = (pipe.decoded - lastDecoded) / dt;
        uploadFps = (uploads - lastUploads) / dt;
        lastDecoded = pipe.decoded;
        lastUploads = uploads;
        previousState = now;
        lastState = now;
        atomicWrite(stateFile, state(ready ? (test ? "diagnostic" : "streaming")
                                           : "connecting"));
      }
      if (now - lastLog >= 10000) {
        lastLog = now;
        fprintf(
            stderr,
            "[vr-perf] decoded=%llu uploaded=%llu decode_fps=%.2f "
            "upload_fps=%.2f decode_ms=%.2f convert_ms=%.2f upload_ms=%.2f "
            "queue_age_ms=%.2f dropped=%llu errors=%llu (not compositor fps)\n",
            (unsigned long long)pipe.decoded.load(),
            (unsigned long long)uploads, decodeFps, uploadFps,
            pipe.decodeMs.load(), pipe.convertMs.load(), uploadMs, ageMs,
            (unsigned long long)(pipe.droppedEncoded + pipe.droppedDecoded),
            (unsigned long long)(pipe.errors + submitErrors));
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(4));
    }
    if (exitReason == "running") {
      if (interrupted)
        exitReason = "signal-" + std::to_string(interrupted);
      else if (pipe.stop)
        exitReason = pipe.stopReason.load();
      else
        exitReason = "timer";
    }
    pipe.finish();
    ready = false;
    fprintf(stderr,
            "[native] exit reason=%s requested=%s active=%s codec=%s "
            "decoded=%llu uploaded=%llu errors=%llu\n",
            exitReason.c_str(), decoderMode.c_str(),
            pipe.decoderActive ? decoderMode.c_str() : "none", pipe.codecName(),
            (unsigned long long)pipe.decoded.load(),
            (unsigned long long)uploads,
            (unsigned long long)(pipe.errors + submitErrors));
    atomicWrite(stateFile, state(pipe.decoderError ? "error" : "stopped"));
    return uploads && !pipe.decoderError ? 0 : 1;
  } catch (const std::exception &e) {
    fprintf(stderr, "[native] %s\n", e.what());
    try {
      atomicWrite(stateFile,
                  "{\"version\":1,\"state\":\"error\",\"ready\":false}");
    } catch (...) {
    }
    return 1;
  }
}
