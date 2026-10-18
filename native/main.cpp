#include "overlay.hpp"
#include <array>
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
  Pipeline initial;
  Packet initialIdr;
  initialIdr.key = true;
  initial.enqueue(initialIdr);
  require(initial.packets.size() == 1 &&
              initial.packets.front().resetReason == ResetReason::None,
          "initial IDR does not request a reset");
  Pipeline pipe;
  for (size_t i = 0; i < Pipeline::maxPacketQueue + 5; i++) {
    Packet q;
    q.key = i == 0;
    q.seq = i;
    pipe.enqueue(std::move(q));
  }
  require(pipe.packets.empty() && pipe.needKey && pipe.requestKey &&
              pipe.packetQueueOverflows == 1 && pipe.droppedAwaitingIdr > 0,
          "queue recovery");
  Packet idr;
  idr.key = true;
  pipe.enqueue(idr);
  require(pipe.packets.size() == 1 &&
              pipe.packets.front().resetReason == ResetReason::Overflow,
          "overflow IDR recovery reason");
  Pipeline reconnect;
  reconnect.connectionOpened(false);
  Packet connectedIdr;
  connectedIdr.key = true;
  reconnect.enqueue(connectedIdr);
  require(reconnect.packets.front().resetReason == ResetReason::None,
          "initial connection keeps the initial IDR policy");
  reconnect.connectionOpened(true);
  Packet resumedIdr;
  resumedIdr.key = true;
  reconnect.enqueue(resumedIdr);
  require(reconnect.packets.front().resetReason == ResetReason::Reconnect,
          "real reconnect carries an explicit reset reason");
  require(recoveryAction(true, false, false, ResetReason::None) ==
              RecoveryAction::ReopenContext &&
              recoveryAction(true, true, false, ResetReason::Overflow) ==
                  RecoveryAction::SameContextIdrBarrier &&
              recoveryAction(true, true, false, ResetReason::Reconnect) ==
                  RecoveryAction::ReopenContext &&
              recoveryAction(true, true, true, ResetReason::Overflow) ==
                  RecoveryAction::ReopenContext &&
              recoveryAction(false, true, false, ResetReason::Reconnect) ==
                  RecoveryAction::FlushContext &&
              recoveryAction(false, true, false, ResetReason::FormatChange) ==
                  RecoveryAction::FlushContext &&
              recoveryAction(false, true, false, ResetReason::Overflow) ==
                  RecoveryAction::FlushContext,
          "initial, overflow, reconnect, resize recovery policy");
  std::map<int64_t, PacketTiming> softwareReconnectTiming{
      {1, {1, 1}}, {2, {2, 2}}, {3, {3, 3}}};
  int64_t softwareReconnectBarrier = -1;
  const RecoveryAction softwareReconnectAction =
      recoveryAction(false, true, false, ResetReason::Reconnect);
  if (softwareReconnectAction == RecoveryAction::FlushContext)
    retireTimingHistory(softwareReconnectTiming, 3,
                        softwareReconnectBarrier);
  require(softwareReconnectAction == RecoveryAction::FlushContext &&
              softwareReconnectTiming.empty() &&
              softwareReconnectBarrier == 3 &&
              timestampStatus(2, softwareReconnectBarrier,
                             softwareReconnectTiming) ==
                  TimestampStatus::Stale &&
              timestampStatus(0, softwareReconnectBarrier,
                             softwareReconnectTiming) ==
                  TimestampStatus::Unknown,
          "software reconnect flush retires prior timestamp metadata");
  std::map<int64_t, PacketTiming> timestamps{{1, {1, 1}}, {2, {2, 2}},
                                              {3, {3, 3}}, {4, {4, 4}}};
  retireTimingsThrough(timestamps, 3);
  require(timestamps.size() == 1 && timestamps.count(4) &&
              timestampStatus(2, 3, timestamps) == TimestampStatus::Stale &&
              timestampStatus(0, -1, timestamps) == TimestampStatus::Unknown &&
              timestampStatus(0, 3, timestamps) == TimestampStatus::Unknown &&
              timestampStatus(4, 3, timestamps) == TimestampStatus::Mapped &&
              timestampStatus(AV_NOPTS_VALUE, 3, timestamps) ==
                  TimestampStatus::Unknown &&
              timestampStatus(5, 3, timestamps) == TimestampStatus::Unknown,
          "stale PTS retirement, recovery IDR, and strict unknown PTS");
  auto a = std::make_shared<Picture>();
  a->seq = 1;
  auto b = std::make_shared<Picture>();
  b->seq = 2;
  pipe.publish(a);
  pipe.publish(b);
  require(pipe.take()->seq == 2 && pipe.droppedDecoded == 1, "newest picture");
  std::cout
      << "native self-test passed: URL allowlist, wire framing, initial/overflow/software and hardware reconnect/resize recovery policy, bounded reset PTS metadata, strict unknown PTS, newest picture\n";
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
static std::shared_ptr<Picture> yuvPattern(const std::string &fmt,
                                           const std::string &range,
                                           const std::string &space,
                                           const std::string &pattern) {
  auto p = std::make_shared<Picture>();
  p->width = 320;
  p->height = 180;
  p->format = fmt == "nv12" ? Picture::Format::NV12 : Picture::Format::YUV420P;
  p->colorRange = range == "full" ? AVCOL_RANGE_JPEG : AVCOL_RANGE_MPEG;
  p->colorSpace = space == "bt709" ? AVCOL_SPC_BT709 : AVCOL_SPC_SMPTE170M;
  p->y.resize(size_t(p->width) * p->height);
  const int cw = (p->width + 1) / 2, ch = (p->height + 1) / 2;
  const bool full = range == "full";
  const bool ramp = pattern == "ramp";
  auto values = [&](int x, int y) {
    if (!ramp) {
      int tile = (x / (p->width / 2) + 2 * (y / (p->height / 2))) % 4;
      int lum = full ? tile * 85 : 16 + tile * 73;
      int u = full ? 24 + tile * 68 : 32 + tile * 64;
      int v = full ? 232 - tile * 68 : 224 - tile * 64;
      return std::array<uint8_t, 3>{uint8_t(lum), uint8_t(u), uint8_t(v)};
    }
    int lum = full ? x * 255 / (p->width - 1)
                   : 16 + x * 219 / (p->width - 1);
    int chromaX = x / 2, chromaY = y / 2;
    int u = full ? chromaX * 255 / (cw - 1)
                 : 16 + chromaX * 224 / (cw - 1);
    int v = full ? 255 - chromaY * 255 / (ch - 1)
                 : 240 - chromaY * 224 / (ch - 1);
    return std::array<uint8_t, 3>{uint8_t(lum), uint8_t(u), uint8_t(v)};
  };
  for (int y = 0; y < p->height; ++y)
    for (int x = 0; x < p->width; ++x)
      p->y[size_t(y) * p->width + x] = values(x, y)[0];
  if (p->format == Picture::Format::NV12) {
    p->uv.resize(size_t(cw) * ch * 2);
    for (int y = 0; y < ch; ++y)
      for (int x = 0; x < cw; ++x) {
        auto c = values(x * 2, y * 2);
        size_t i = (size_t(y) * cw + x) * 2;
        p->uv[i] = c[1];
        p->uv[i + 1] = c[2];
      }
  } else {
    p->u.resize(size_t(cw) * ch);
    p->v.resize(size_t(cw) * ch);
    for (int y = 0; y < ch; ++y)
      for (int x = 0; x < cw; ++x) {
        auto c = values(x * 2, y * 2);
        size_t i = size_t(y) * cw + x;
        p->u[i] = c[1];
        p->v[i] = c[2];
      }
  }
  p->received = p->decoded = nowMs();
  return p;
}
static void convertYuvOracle(Picture &p) {
  const int cw = (p.width + 1) / 2;
  const uint8_t *planes[4] = {p.y.data(), nullptr, nullptr, nullptr};
  int strides[4] = {p.width, 0, 0, 0};
  AVPixelFormat sourceFormat = AV_PIX_FMT_YUV420P;
  if (p.format == Picture::Format::NV12) {
    sourceFormat = AV_PIX_FMT_NV12;
    planes[1] = p.uv.data();
    strides[1] = cw * 2;
  } else {
    planes[1] = p.u.data();
    planes[2] = p.v.data();
    strides[1] = strides[2] = cw;
  }
  p.rgba.resize(size_t(p.width) * p.height * 4);
  uint8_t *output[4] = {p.rgba.data(), nullptr, nullptr, nullptr};
  int outputStrides[4] = {p.width * 4, 0, 0, 0};
  SwsContext *context = sws_getContext(
      p.width, p.height, sourceFormat, p.width, p.height, AV_PIX_FMT_RGBA,
      SWS_BILINEAR, nullptr, nullptr, nullptr);
  if (!context)
    throw std::runtime_error("test swscale context allocation failed");
  int colorSpace = p.colorSpace == AVCOL_SPC_BT709 ? SWS_CS_ITU709 : SWS_CS_ITU601;
  const int *coefficients = sws_getCoefficients(colorSpace);
  int result = sws_setColorspaceDetails(
      context, coefficients, p.colorRange == AVCOL_RANGE_JPEG, coefficients, 1,
      0, 1 << 16, 1 << 16);
  if (result < 0 || sws_scale(context, planes, strides, 0, p.height, output,
                              outputStrides) != p.height) {
    sws_freeContext(context);
    throw std::runtime_error("test swscale conversion failed");
  }
  sws_freeContext(context);
  p.format = Picture::Format::RGBA;
  p.y.clear();
  p.u.clear();
  p.v.clear();
  p.uv.clear();
}
int main(int argc, char **argv) {
  std::string urlFile, controlFile, stateFile, poseFile, dumpPath,
      key = "com.frame-workspace.native.workspace", decoderMode = "software",
      conversionMode = std::getenv("FRAME_WORKSPACE_CONVERSION")
                          ? std::getenv("FRAME_WORKSPACE_CONVERSION")
                          : "cpu",
      testYuvFormat, testYuvRange = "limited", testYuvColorSpace = "bt601",
      testYuvPattern = "sharp",
      exitReason = "running";
  std::vector<std::string> inspectFiles;
  bool test = false, noVR = false, testYuvOracle = false;
  uint32_t dumpSequence = 0;
  int dumpWidth = 0, dumpHeight = 0;
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
      else if (a == "--conversion")
        conversionMode = value();
      else if (a == "--dump")
        dumpPath = value();
      else if (a == "--dump-seq") {
        auto parsed = std::stoul(value());
        if (parsed > UINT32_MAX)
          throw std::runtime_error("invalid dump sequence");
        dumpSequence = uint32_t(parsed);
      } else if (a == "--dump-size") {
        std::string size = value();
        size_t separator = size.find('x');
        if (separator == std::string::npos ||
            size.find('x', separator + 1) != std::string::npos)
          throw std::runtime_error("dump size must be WIDTHxHEIGHT");
        auto parsedWidth = std::stoul(size.substr(0, separator));
        auto parsedHeight = std::stoul(size.substr(separator + 1));
        if (!parsedWidth || !parsedHeight || parsedWidth > 4096 ||
            parsedHeight > 4096)
          throw std::runtime_error("invalid dump size");
        dumpWidth = int(parsedWidth);
        dumpHeight = int(parsedHeight);
      }
      else if (a == "--seconds")
        seconds = std::stod(value());
      else if (a == "--test-pattern")
        test = true;
      else if (a == "--test-yuv-format") {
        test = true;
        testYuvFormat = value();
      } else if (a == "--test-yuv-range")
        testYuvRange = value();
      else if (a == "--test-yuv-colorspace")
        testYuvColorSpace = value();
      else if (a == "--test-yuv-pattern")
        testYuvPattern = value();
      else if (a == "--test-yuv-oracle")
        testYuvOracle = true;
      else if (a == "--no-vr")
        noVR = true;
      else if (a == "--help") {
        std::cout
            << "frame-workspace-native --url-file PATH [--control-file PATH] "
               "[--state-file PATH] [--pose-file PATH] [--overlay-key KEY] "
               "[--decoder software|v4l2] [--conversion cpu|gpu] "
               "[--seconds N] [--dump PATH.ppm [--dump-seq N] "
               "[--dump-size WIDTHxHEIGHT]] "
               "\nDiagnostics: --self-test | "
               "--test-pattern [--no-vr] [--seconds N] | "
               "--test-yuv-format nv12|yuv420p --conversion gpu "
               "[--test-yuv-range limited|full] "
               "[--test-yuv-colorspace bt601|bt709] "
               "[--test-yuv-pattern sharp|ramp] [--test-yuv-oracle] "
               "[--dump PATH.ppm]\n";
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
    if (conversionMode != "cpu" && conversionMode != "gpu")
      throw std::runtime_error("conversion must be cpu or gpu");
    if (!testYuvFormat.empty() && testYuvFormat != "nv12" &&
        testYuvFormat != "yuv420p")
      throw std::runtime_error("test YUV format must be nv12 or yuv420p");
    if (testYuvRange != "limited" && testYuvRange != "full")
      throw std::runtime_error("test YUV range must be limited or full");
    if (testYuvColorSpace != "bt601" && testYuvColorSpace != "bt709")
      throw std::runtime_error("test YUV colorspace must be bt601 or bt709");
    if (testYuvPattern != "sharp" && testYuvPattern != "ramp")
      throw std::runtime_error("test YUV pattern must be sharp or ramp");
    if (!testYuvFormat.empty() && conversionMode != "gpu" && !testYuvOracle)
      throw std::runtime_error("test YUV input requires --conversion gpu");
    if (testYuvOracle && (testYuvFormat.empty() || conversionMode != "cpu"))
      throw std::runtime_error("YUV oracle requires --test-yuv-format and --conversion cpu");
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
    Pipeline pipe(decoderMode, conversionMode);
    if (!test)
      pipe.start(endpoint);
    double start = nowMs(), lastState = start, lastLog = start, lastControl = 0,
           lastPattern = 0, previousState = start;
    uint64_t uploads = 0, lastUploads = 0, lastDecoded = 0, submitErrors = 0;
    int w = 0, h = 0;
    double uploadMs = 0, ageMs = 0, decodeFps = 0, uploadFps = 0;
    bool ready = false, dumped = false;
    double revision = -1, controlResultRevision = -1;
    Overlay::CurvatureResult curvatureResult;
    std::shared_ptr<Picture> retryPicture;
    auto state = [&](const char *status) {
      auto curvature = overlay.curvatureReadback();
      const std::string curvatureValue =
          curvature.readable ? std::to_string(curvature.value) : "null";
      const std::string resultValue =
          curvatureResult.hasValue ? std::to_string(curvatureResult.value)
                                   : "null";
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
             ",\"planeCopyMs\":" + std::to_string(pipe.planeCopyMs) +
             ",\"gpuConvertMs\":" + std::to_string(overlay.gpuConvertMs) +
             ",\"uploadMs\":" + std::to_string(uploadMs) +
             ",\"syncMs\":" + std::to_string(overlay.syncMs) +
             ",\"submitMs\":" + std::to_string(overlay.submitMs) +
             ",\"queueAgeMs\":" + std::to_string(ageMs) +
             ",\"droppedEncoded\":" + std::to_string(pipe.droppedEncoded) +
             ",\"droppedDecoded\":" + std::to_string(pipe.droppedDecoded) +
             ",\"queueOverflows\":" +
                 std::to_string(pipe.packetQueueOverflows) +
             ",\"packetQueueLimit\":" +
                 std::to_string(Pipeline::maxPacketQueue) +
             ",\"packetQueueHighWater\":" +
                 std::to_string(pipe.packetQueueHighWater) +
             ",\"packetQueueWaitMs\":" +
                 std::to_string(pipe.packetQueueWaitMs) +
             ",\"emptyReceives\":" + std::to_string(pipe.emptyReceives) +
             ",\"emptyReceiveMaxMs\":" +
                 std::to_string(pipe.emptyReceiveMaxMs) +
             ",\"emptyReceiveTotalMs\":" +
                 std::to_string(pipe.emptyReceiveTotalMs) +
             ",\"droppedAwaitingIdr\":" +
                 std::to_string(pipe.droppedAwaitingIdr) +
             ",\"overflowRecoveryIdrs\":" +
                 std::to_string(pipe.overflowRecoveryIdrs) +
             ",\"lastOverflowRecoverySeq\":" +
                 std::to_string(pipe.lastOverflowRecoverySeq) +
             ",\"staleDriverFramesDropped\":" +
                 std::to_string(pipe.staleDriverFramesDropped) +
             ",\"decoderTimingHighWater\":" +
                 std::to_string(pipe.decoderTimingHighWater) +
             ",\"decoderOpenAttempts\":" +
                 std::to_string(pipe.decoderOpenAttempts) +
             ",\"decoderInitialOpens\":" +
                 std::to_string(pipe.decoderInitialOpens) +
             ",\"decoderReconnectOpens\":" +
                 std::to_string(pipe.decoderReconnectOpens) +
             ",\"decoderFormatReopens\":" +
                 std::to_string(pipe.decoderFormatReopens) +
             ",\"decoderOpenFailures\":" +
                 std::to_string(pipe.decoderOpenFailures) +
             ",\"decoderOpenMs\":" + std::to_string(pipe.decoderOpenMs) +
             ",\"decoderOpenTotalMs\":" +
                 std::to_string(pipe.decoderOpenMsTotal) +
             ",\"errors\":" + std::to_string(pipe.errors + submitErrors) +
             ",\"networkErrors\":" + std::to_string(pipe.networkErrors) +
             ",\"serverErrors\":" + std::to_string(pipe.serverErrors) +
             ",\"decoderSendErrors\":" +
                 std::to_string(pipe.decoderSendErrors) +
             ",\"decoderReceiveErrors\":" +
                 std::to_string(pipe.decoderReceiveErrors) +
             ",\"decoderFatalErrors\":" +
                 std::to_string(pipe.decoderFatalErrors) +
             ",\"submitErrors\":" + std::to_string(submitErrors) +
             ",\"submitError\":" + std::to_string(overlay.lastSubmitError) +
             ",\"decoder\":\"" + pipe.decoderIdentity() + "\"" +
             ",\"decoderRequested\":\"" + decoderMode + "\"" +
             ",\"conversionRequested\":\"" + conversionMode + "\"" +
             ",\"conversionActive\":\"" + pipe.conversionActive.load() +
             "\"" + ",\"conversionFallbacks\":" +
                 std::to_string(pipe.conversionFallbacks) +
             ",\"decoderPixelFormat\":" +
                 std::to_string(pipe.decoderPixelFormat) +
             ",\"decoderColorSpace\":" +
                 std::to_string(pipe.decoderColorSpace) +
             ",\"decoderColorRange\":" +
                 std::to_string(pipe.decoderColorRange) +
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
             ",\"curvature\":{\"readable\":" +
                 (curvature.readable ? "true" : "false") +
             ",\"value\":" + curvatureValue +
             ",\"errorCode\":" + std::to_string(curvature.errorCode) +
             "},\"controlResultRevision\":" +
                 std::to_string(controlResultRevision) +
             ",\"curvatureResult\":{\"supported\":" +
                 (curvatureResult.supported ? "true" : "false") +
             ",\"applied\":" +
                 (curvatureResult.applied ? "true" : "false") +
             ",\"requested\":" + std::to_string(curvatureResult.requested) +
             ",\"value\":" + resultValue +
             ",\"errorCode\":" +
                 std::to_string(curvatureResult.errorCode) + "}" +
             ",\"controlRevision\":" + std::to_string(revision) +
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
              auto requestedCurvature = field(j, "curvature");
              bool validCurvature = true;
              double curvature = 0;
              if (requestedCurvature) {
                if (!json_object_is_type(requestedCurvature, json_type_int) &&
                    !json_object_is_type(requestedCurvature, json_type_double)) {
                  validCurvature = false;
                } else {
                  curvature = json_object_get_double(requestedCurvature);
                  validCurvature = std::isfinite(curvature) && curvature >= 0 &&
                                   curvature <= 1;
                }
              }
              if (!validCurvature) {
                curvatureResult = Overlay::CurvatureResult{};
                curvatureResult.errorCode = -3;
                revision = controlResultRevision = r;
              } else {
                const bool placementChanged = field(j, "width") || field(j, "distance");
                if (placementChanged)
                  overlay.resize(num(j, "width", overlay.placement.width),
                                 num(j, "distance", overlay.placement.distance));
                if (requestedCurvature)
                  curvatureResult = overlay.setCurvature(float(curvature));
                if (flag(j, "recenter"))
                  pendingRecenter = true;
                if (flag(j, "focus")) {
                  pipe.focus = true;
                  overlay.focus();
                }
                if (placementChanged)
                  atomicWrite(poseFile, placementJSON(overlay.placement));
                revision = controlResultRevision = r;
                if (flag(j, "stop")) {
                  exitReason = "control-stop";
                  break;
                }
              }
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
        if (testYuvFormat.empty())
          picture = pattern(uploads);
        else {
          picture = yuvPattern(testYuvFormat, testYuvRange,
                               testYuvColorSpace, testYuvPattern);
          if (testYuvOracle)
            convertYuvOracle(*picture);
        }
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
          if (!dumpPath.empty() && !dumped &&
              (!dumpSequence || picture->seq == dumpSequence) &&
              (!dumpWidth ||
               (picture->width == dumpWidth && picture->height == dumpHeight))) {
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
