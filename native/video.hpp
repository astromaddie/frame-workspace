#pragma once
#include "websocket.hpp"
#include "clock_sync.hpp"
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/imgutils.h>
#include <libswscale/swscale.h>
}
#include "h264_crop.hpp"
#include "h264_poc.hpp"
#include <condition_variable>
#include <utility>

#include <deque>
#include <mutex>
#include <thread>
namespace fw {
struct Picture {
  enum class Format { RGBA, NV12, YUV420P };
  int width = 0, height = 0;
  FoveationLayout layout;
  int outputWidth() const { return layout.present ? layout.width : width; }
  int outputHeight() const { return layout.present ? layout.height : height; }
  uint32_t seq = 0;
  double received = 0, decoded = 0;
  Format format = Format::RGBA;
  AVColorSpace colorSpace = AVCOL_SPC_UNSPECIFIED;
  AVColorRange colorRange = AVCOL_RANGE_UNSPECIFIED;
  std::vector<uint8_t> rgba;
  // GPU conversion pictures own compact copies of decoder planes. No pointer
  // into an AVFrame survives av_frame_unref() or a decoder-context reopen.
  std::vector<uint8_t> y, u, v, uv;
};
struct PacketTiming {
  uint32_t seq;
  double received;
  FoveationLayout layout;
  H264Crop crop{};
  double sent = 0; // handed to the decoder
};
enum class RecoveryAction {
  KeepContext,
  ReopenContext,
  FlushContext,
  SameContextIdrBarrier
};
enum class DecoderOpenReason { Initial, Reconnect, FormatChange };
enum class TimestampStatus { Mapped, Stale, Unknown };
inline RecoveryAction recoveryAction(bool hardware, bool contextReady,
                                     bool sizeChanged,
                                     ResetReason reason) {
  if (!contextReady || sizeChanged)
    return RecoveryAction::ReopenContext;
  if (reason == ResetReason::Reconnect ||
      reason == ResetReason::FormatChange)
    return hardware ? RecoveryAction::ReopenContext
                    : RecoveryAction::FlushContext;
  if (reason == ResetReason::Overflow)
    return hardware ? RecoveryAction::SameContextIdrBarrier
                    : RecoveryAction::FlushContext;
  if (reason == ResetReason::DecoderError)
    return hardware ? RecoveryAction::ReopenContext
                    : RecoveryAction::FlushContext;
  return RecoveryAction::KeepContext;
}
inline bool staleOutputToken(int64_t pts, int64_t staleThroughToken) {
  // Packet tokens start at one. Zero, negative and AV_NOPTS_VALUE were never
  // issued by this pipeline and must remain unknown even after a barrier.
  return pts > 0 && staleThroughToken > 0 && pts <= staleThroughToken;
}
inline void retireTimingsThrough(std::map<int64_t, PacketTiming> &timing,
                                 int64_t token) {
  timing.erase(timing.begin(), timing.upper_bound(token));
}
inline void retireTimingHistory(std::map<int64_t, PacketTiming> &timing,
                               int64_t token, int64_t &staleThroughToken) {
  if (token > staleThroughToken)
    staleThroughToken = token;
  retireTimingsThrough(timing, staleThroughToken);
}
inline TimestampStatus timestampStatus(
    int64_t pts, int64_t staleThroughToken,
    const std::map<int64_t, PacketTiming> &timing) {
  if (staleOutputToken(pts, staleThroughToken))
    return TimestampStatus::Stale;
  return timing.count(pts) ? TimestampStatus::Mapped
                           : TimestampStatus::Unknown;
}
// Shared by the live decoder and --inspect-h264: parse complete Annex-B
// access units with FFmpeg, preserving SPS/PPS across frames and size changes.
struct H264Headers {
  AVCodecParserContext *parser = av_parser_init(AV_CODEC_ID_H264);
  AVCodecContext *context = avcodec_alloc_context3(nullptr);
  H264Headers() {
    if (!parser || !context)
      throw std::runtime_error("H264 parser unavailable");
    parser->flags |= PARSER_FLAG_COMPLETE_FRAMES;
    context->codec_id = AV_CODEC_ID_H264;
    context->codec_type = AVMEDIA_TYPE_VIDEO;
    context->pkt_timebase = AVRational{1, 1000000};
  }
  ~H264Headers() {
    av_parser_close(parser);
    avcodec_free_context(&context);
  }
  H264Headers(const H264Headers &) = delete;
  H264Headers &operator=(const H264Headers &) = delete;
  int parse(const std::vector<uint8_t> &bytes) {
    // av_parser_parse2 requires AV_INPUT_BUFFER_PADDING_SIZE zero bytes.
    std::vector<uint8_t> padded = bytes;
    padded.resize(bytes.size() + AV_INPUT_BUFFER_PADDING_SIZE, 0);
    uint8_t *output = nullptr;
    int outputSize = 0;
    parser->width = parser->height = parser->coded_width =
        parser->coded_height = 0;
    parser->key_frame = -1;
    int used =
        av_parser_parse2(parser, context, &output, &outputSize, padded.data(),
                         bytes.size(), AV_NOPTS_VALUE, AV_NOPTS_VALUE, 0);
    if (used < 0)
      return used;
    if (used != int(bytes.size()) || outputSize != int(bytes.size()) ||
        parser->width < 1 || parser->height < 1 ||
        parser->coded_width < parser->width ||
        parser->coded_height < parser->height || parser->coded_width > 4096 ||
        parser->coded_height > 4096)
      return AVERROR_INVALIDDATA;
    return 0;
  }
};
struct Pipeline {
  const std::string decoderRequested;
  const std::string conversionRequested;
  std::atomic<bool> decoderActive{false};
  std::atomic<const char *> conversionActive{"pending"};
  std::atomic<int> conversionFallbacks{0}, decoderPixelFormat{-1},
      decoderColorSpace{AVCOL_SPC_UNSPECIFIED},
      decoderColorRange{AVCOL_RANGE_UNSPECIFIED};
  std::atomic<int> decoderError{0}, inputWidth{0}, inputHeight{0},
      codedWidth{0}, codedHeight{0};
  std::atomic<const char *> decoderErrorStage{"none"}, stopReason{"running"};
  explicit Pipeline(std::string mode = "software", std::string conversion = "cpu")
      : decoderRequested(std::move(mode)),
        conversionRequested(std::move(conversion)) {
    if (decoderRequested != "software" && decoderRequested != "v4l2")
      throw std::runtime_error("decoder must be software or v4l2");
    if (conversionRequested != "cpu" && conversionRequested != "gpu")
      throw std::runtime_error("conversion must be cpu or gpu");
  }
  const char *codecName() const {
    return decoderRequested == "v4l2" ? "h264_v4l2m2m" : "h264";
  }
  std::string decoderIdentity() const {
    return std::string("libavcodec ") + codecName() +
           (decoderActive ? " active" : " pending");
  }
  std::atomic<bool> stop{false}, requestKey{false}, focus{false},
      connected{false};
  std::atomic<uint64_t> received{0}, decoded{0}, droppedEncoded{0},
      droppedDecoded{0}, errors{0}, reconnects{0}, networkErrors{0},
      serverErrors{0}, decoderSendErrors{0}, decoderReceiveErrors{0},
      decoderFatalErrors{0}, packetQueueOverflows{0}, droppedAwaitingIdr{0},
      overflowRecoveryIdrs{0}, staleDriverFramesDropped{0},
      decoderOpenAttempts{0}, decoderInitialOpens{0},
      decoderReconnectOpens{0}, decoderFormatReopens{0},
      decoderOpenFailures{0};
  std::atomic<uint32_t> lastOverflowRecoverySeq{0};
  std::atomic<uint64_t> decoderTimingHighWater{0}, packetQueueHighWater{0},
      emptyReceives{0};
  std::atomic<double> decodeMs{0}, sendMs{0}, convertMs{0}, planeCopyMs{0},
      decoderOpenMs{0}, decoderOpenMsTotal{0}, clockOffset{0},
      packetQueueWaitMs{0}, emptyReceiveMaxMs{0}, emptyReceiveTotalMs{0};
  // Per-state-interval maxima (the owner resets them after each state write):
  // one packet's parse/send/receive/copy step, and the gap between packet
  // arrivals. They distinguish decoder stalls from network delivery bursts.
  std::atomic<double> decodeStepMaxMs{0}, arrivalGapMaxMs{0}, decoderHoldMaxMs{0},
      decoderHoldMs{0};
  std::atomic<int64_t> decoderOutputLag{0}, decoderOutputLagMax{0};
  int64_t lastSentToken = 0;
  bool pocRewrite = true;
  H264PocRewriter pocRewriter;
  std::vector<uint8_t> rewrittenBytes;
  double lastArrival = 0;
  static void raiseMax(std::atomic<double> &value, double sample) {
    double current = value.load();
    while (sample > current && !value.compare_exchange_weak(current, sample)) {
    }
  }
  std::atomic<bool> clockValid{false};
  std::mutex mutex, outMutex;
  std::condition_variable wake, pictureReady;
  std::deque<Packet> packets;
  std::shared_ptr<Picture> newest;
  std::deque<std::string> outbound;
  struct GazeSample {
    double u = 0, v = 0, sampled = 0;
    bool valid = false, diagnostic = false;
  };
  GazeSample gazeSample;
  void gaze(double u, double v, bool valid, double sampled, bool diagnostic = false) {
    std::lock_guard<std::mutex> l(outMutex);
    gazeSample = {u, v, sampled, valid && std::isfinite(u) && std::isfinite(v) &&
        u >= 0 && u <= 1 && v >= 0 && v <= 1 && std::isfinite(sampled), diagnostic};
  }
  std::atomic<bool> foveationNegotiated{false}, atlasNegotiated{false}, directNegotiated{false};
  bool needKey = true;
  // Absorb transport bursts without throwing away an H.264 reference chain.
  // Frame Wi-Fi delivery gaps reached ~210 ms at p95 (about 13 packets at
  // 60 fps), which overflowed eight. This remains bounded; a real backlog
  // still recovers at an IDR.
  static constexpr size_t maxPacketQueue = 16;
  ResetReason pendingResetReason = ResetReason::None;
  std::thread net, decoder;
  static int resetPriority(ResetReason reason) {
    switch (reason) {
    case ResetReason::Reconnect:
      return 2;
    case ResetReason::FormatChange:
      return 3;
    case ResetReason::DecoderError:
      return 2;
    case ResetReason::Overflow:
      return 1;
    case ResetReason::None:
      return 0;
    }
    return 0;
  }
  void requireKeyLocked(ResetReason reason) {
    needKey = true;
    if (resetPriority(reason) > resetPriority(pendingResetReason))
      pendingResetReason = reason;
  }
  void connectionOpened(bool resumed) {
    std::lock_guard<std::mutex> lock(mutex);
    droppedEncoded += packets.size();
    packets.clear();
    requireKeyLocked(resumed ? ResetReason::Reconnect : ResetReason::None);
  }
  ~Pipeline() { finish(); }
  void finish() {
    stop = true;
    wake.notify_all();
    if (net.joinable())
      net.join();
    if (decoder.joinable())
      decoder.join();
  }
  void enqueue(Packet p) {
    std::lock_guard<std::mutex> lock(mutex);
    ++received;
    if (lastArrival > 0)
      raiseMax(arrivalGapMaxMs, p.received - lastArrival);
    lastArrival = p.received;
    if (packets.size() >= maxPacketQueue) {
      ++packetQueueOverflows;
      droppedEncoded += packets.size();
      packets.clear();
      requireKeyLocked(ResetReason::Overflow);
      requestKey = true;
    }
    if (needKey && !p.key) {
      ++droppedEncoded;
      ++droppedAwaitingIdr;
      requestKey = true;
      return;
    }
    if (needKey) {
      p.resetReason = pendingResetReason;
      pendingResetReason = ResetReason::None;
      needKey = false;
    }
    packets.push_back(std::move(p));
    if (packets.size() > packetQueueHighWater)
      packetQueueHighWater = packets.size();
    wake.notify_one();
  }
  std::shared_ptr<Picture> take() {
    std::lock_guard<std::mutex> lock(mutex);
    return std::exchange(newest, {});
  }
  void publish(std::shared_ptr<Picture> p) {
    {
      std::lock_guard<std::mutex> lock(mutex);
      if (newest)
        ++droppedDecoded;
      newest = std::move(p);
    }
    pictureReady.notify_one();
  }
  // Lets the GL owner sleep until a decoded picture arrives instead of
  // polling on a fixed interval; it still wakes for events and control.
  void waitForPicture(double ms) {
    std::unique_lock<std::mutex> lock(mutex);
    pictureReady.wait_for(lock, std::chrono::microseconds(int64_t(ms * 1000)),
                          [&] { return newest || stop; });
  }
  void report(const Picture &p, double uploaded) {
    if (!clockValid)
      return;
    double o = clockOffset;
    std::lock_guard<std::mutex> l(outMutex);
    if (outbound.size() < 8)
      outbound.push_back("{\"t\":\"fd\",\"f\":[[" + std::to_string(p.seq) +
                         "," + std::to_string((p.decoded + o) * 1000) + "," +
                         std::to_string((uploaded + o) * 1000) +
                         ",0]],\"drop\":0}");
  }
  void start(const Endpoint &e) {
    decoder = std::thread([this] { decodeLoop(); });
    net = std::thread([this, e] { networkLoop(e); });
  }
  void networkLoop(const Endpoint &endpoint) {
    std::string reconnect;
    int failed = 0;
    while (!stop) {
      try {
        WebSocket ws(stop);
        bool resumed = !reconnect.empty();
        if (endpoint.foveation) {
          ws.idlePollMs = 8;
          ws.serviceIdleWhileReading = true;
        }
        ws.open(endpoint, reconnect);
        connected = true;
        ++reconnects;
        connectionOpened(resumed);
        clockValid = false;
        foveationNegotiated = false; atlasNegotiated = false; directNegotiated = false;
        std::string gazeSession;
        int directWidth = 0, directHeight = 0;
        FoveationClock gazeClock;
        { std::lock_guard<std::mutex> l(outMutex); gazeSample = {}; }
        double ping = 0, key = 0, lastGaze = 0, lastSentSample = -1;
        bool lastSentValid = false;
        ws.idle = [&] {
          double now = nowMs();
          if (endpoint.foveation) {
            double estimate = 0;
            const bool valid = gazeClock.estimate(now, estimate);
            if (valid) clockOffset = estimate;
            clockValid = valid;
          }
          if (now - ping > 2000) {
            ws.sendText("{\"t\":\"ping\",\"c\":" + std::to_string(now * 1000) +
                        "}");
            ping = now;
          }
          if (requestKey && now - key > 500) {
            requestKey = false;
            key = now;
            ws.sendText("{\"t\":\"key-frame\"}");
          }
          if (focus.exchange(false))
            ws.sendText("{\"t\":\"focus\"}");
          if (directNegotiated && clockValid && now - lastGaze >= 16) {
            GazeSample g;
            { std::lock_guard<std::mutex> l(outMutex); g = gazeSample; }
            const bool valid = g.valid && now >= g.sampled && now - g.sampled <= 100;
            if (g.sampled > lastSentSample || (lastSentValid && !valid)) {
            ws.sendText("{\"t\":\"gaze\",\"version\":1,\"session\":\"" + gazeSession +
                "\",\"valid\":" + (valid ? "true" : "false") +
                ",\"u\":" + std::to_string(valid ? g.u : 0) + ",\"v\":" + std::to_string(valid ? g.v : 0) +
                ",\"sampleTime\":" + std::to_string(int64_t(g.sampled * 1000)) +
                ",\"time\":" + std::to_string((g.sampled + clockOffset.load()) * 1000) +
                ",\"source\":\"" + (g.diagnostic ? "diagnostic" : "eye") + "\"}");
            lastGaze = now; lastSentSample = g.sampled; lastSentValid = valid;
            }
          }
          std::deque<std::string> pending;
          {
            std::lock_guard<std::mutex> l(outMutex);
            pending.swap(outbound);
          }
          for (auto &s : pending)
            ws.sendText(s);
        };
        while (!stop) {
          ws.idle();
          auto [type, bytes] = ws.receive();
          double receivedAt = nowMs();
          if (type == 2) {
            if (endpoint.foveation && !directNegotiated)
              throw std::runtime_error("video before direct capture negotiation");
            auto p = packet(bytes, foveationNegotiated, atlasNegotiated, directNegotiated);
            if (directNegotiated) {
              if (directWidth && (directWidth != p.layout.width || directHeight != p.layout.height))
                throw std::runtime_error("direct capture dimensions changed");
              directWidth = p.layout.width; directHeight = p.layout.height;
            }
            p.received = receivedAt;
            std::string ack = "{\"t\":\"rx\",\"s\":" + std::to_string(p.seq);
            if (clockValid)
              ack += ",\"r\":" +
                     std::to_string((receivedAt + clockOffset.load()) * 1000);
            ws.sendText(ack + "}");
            enqueue(std::move(p));
          } else {
            auto j = json(std::string(bytes.begin(), bytes.end()));
            auto t = str(j, "t");
            if (t == "hello") {
              auto r = str(j, "r");
              if (r.empty() || r.size() > 256)
                throw std::runtime_error("invalid reconnect key");
              reconnect = r;
              if (endpoint.foveation &&
                  (!json_object_is_type(field(j, "foveation"), json_type_int) || num(j, "foveation") != 1 ||
                   !json_object_is_type(field(j, "direct"), json_type_int) || num(j, "direct") != 1)) {
                ++errors; ++serverErrors;
                stopReason = "direct-capture-unavailable";
                stop = true;
                wake.notify_all();
                fprintf(stderr, "[native] companion does not support direct foveated capture\n");
                break;
              }
              if (endpoint.foveation) {
                auto nonce = str(j, "gazeSession");
                if (nonce.empty() || nonce.size() > 64 ||
                    nonce.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-") != std::string::npos)
                  throw std::runtime_error("invalid gaze session");
                gazeSession = nonce;
                foveationNegotiated = true;
                directNegotiated = true;
              }
              ws.sendText("{\"t\":\"ack\"}");
              failed = 0;
            } else if (t == "pong") {
              double c = num(j, "c") / 1000, a = num(j, "a") / 1000;
              if (c > 0 && a > 0 && receivedAt - c >= 0 &&
                  receivedAt - c < 1000) {
                if (endpoint.foveation) {
                  gazeClock.observe(c, a, receivedAt);
                  double estimate = 0;
                  const bool valid = gazeClock.estimate(receivedAt, estimate);
                  if (valid) clockOffset = estimate;
                  clockValid = valid;
                } else {
                  clockOffset = a - (c + receivedAt) / 2;
                  clockValid = true;
                }
                ws.sendText("{\"t\":\"clock\",\"rtt\":" +
                            std::to_string(receivedAt - c) + ",\"dec\":\"" +
                            decoderIdentity() + "\"}");
              }
            } else if (t == "close" || t == "closed") {
              stopReason = "server-close";
              stop = true;
              wake.notify_all();
            } else if (t == "error") {
              ++errors;
              ++serverErrors;
              fprintf(
                  stderr,
                  "[native] server reported stream error (text redacted)\n");
            }
          }
        }
      } catch (const std::exception &) {
        connected = false;
        clockValid = false;
        foveationNegotiated = false; atlasNegotiated = false; directNegotiated = false;
        if (stop)
          break;
        ++errors;
        ++networkErrors;
        ++failed;
        fprintf(stderr,
                "[native] stream unavailable; retry=%d (endpoint redacted)\n",
                failed);
        if (reconnect.empty() && failed >= 3) {
          stopReason = "stream-start-failed";
          stop = true;
          wake.notify_all();
          break;
        }
        for (int i = 0; i < std::min(failed, 6) * 10 && !stop; i++)
          std::this_thread::sleep_for(std::chrono::milliseconds(100));
      }
    }
    connected = false;
  }
  void decodeLoop() {
    const AVCodec *codec = avcodec_find_decoder_by_name(codecName());
    AVCodecContext *context = nullptr;
    AVFrame *frame = av_frame_alloc();
    AVPacket *avp = av_packet_alloc();
    SwsContext *sws = nullptr;
    std::map<int64_t, PacketTiming> timing;
    int64_t token = 0;
    // -1 means no recovery barrier exists yet; in particular, PTS 0 must not
    // be silently accepted as stale before the first overflow recovery.
    int64_t staleThroughToken = -1;
    bool submitted = false;
    H264Headers headers;
    H264CropHeaders cropHeaders;
    auto fail = [&](const char *stage, int code) {
      decoderError = code;
      decoderErrorStage = stage;
      stopReason = "decoder-error";
      ++errors;
      ++decoderFatalErrors;
      stop = true;
      wake.notify_all();
      char detail[AV_ERROR_MAX_STRING_SIZE]{};
      av_strerror(code, detail, sizeof(detail));
      fprintf(stderr,
              "[native] decoder=%s stage=%s failed=%d (%s); no fallback\n",
              codecName(), stage, code, detail);
    };
    auto open = [&](DecoderOpenReason reason, bool formatReopen = false) {
      const double openStart = nowMs();
      ++decoderOpenAttempts;
      if (reason == DecoderOpenReason::Initial)
        ++decoderInitialOpens;
      else if (reason == DecoderOpenReason::Reconnect)
        ++decoderReconnectOpens;
      if (formatReopen)
        ++decoderFormatReopens;
      auto recordOpenTime = [&] {
        const double elapsed = nowMs() - openStart;
        decoderOpenMs = elapsed;
        decoderOpenMsTotal = decoderOpenMsTotal.load() + elapsed;
      };
      av_frame_unref(frame);
      avcodec_free_context(&context);
      timing.clear();
      decoderActive = false;
      context = codec ? avcodec_alloc_context3(codec) : nullptr;
      if (!context) {
        ++decoderOpenFailures;
        recordOpenTime();
        fail("allocate", AVERROR(ENOMEM));
        return false;
      }
      // V4L2 converts packet timestamps through pkt_timebase to timeval. Use a
      // unique microsecond token so delayed output maps to its own receive
      // time.
      context->pkt_timebase = AVRational{1, 1000000};
      context->flags |= AV_CODEC_FLAG_LOW_DELAY;
      if (decoderRequested == "software") {
        context->thread_count = 2;
        context->thread_type = FF_THREAD_SLICE;
      } else {
        // Request CPU-mappable NV12, not opaque DRM_PRIME surfaces. Conversion
        // remains in the existing swscale path for a controlled comparison.
        context->pix_fmt = AV_PIX_FMT_NV12;
        context->width = headers.parser->width;
        context->height = headers.parser->height;
        context->coded_width = headers.parser->coded_width;
        context->coded_height = headers.parser->coded_height;
        context->profile = headers.context->profile;
        context->level = headers.context->level;
        context->framerate = headers.context->framerate;
        inputWidth = context->width;
        inputHeight = context->height;
        codedWidth = context->coded_width;
        codedHeight = context->coded_height;
        fprintf(stderr,
                "[native] decoder configure codec=%s size=%dx%d coded=%dx%d "
                "format=NV12 profile=%d level=%d\n",
                codecName(), context->width, context->height,
                context->coded_width, context->coded_height, context->profile,
                context->level);
      }
      int result = avcodec_open2(context, codec, nullptr);
      if (result < 0) {
        ++decoderOpenFailures;
        recordOpenTime();
        fail("open", result);
        return false;
      }
      recordOpenTime();
      fprintf(stderr, "[native] decoder initialized requested=%s codec=%s\n",
              decoderRequested.c_str(), codecName());
      return true;
    };
    if (!codec || !frame || !avp) {
      fail("unavailable", codec ? AVERROR(ENOMEM) : AVERROR_DECODER_NOT_FOUND);
    } else if (decoderRequested == "software") {
      open(DecoderOpenReason::Initial);
    }
    // Hardware open is deferred until the first IDR has provided valid SPS
    // dimensions. Drain all available output before asking the decoder for more
    // input. A delayed frame must never inherit the newest packet's
    // sequence/time.
    auto drain = [&]() {
      int result = AVERROR(EAGAIN);
      while (!stop) {
        double before = nowMs();
        result = avcodec_receive_frame(context, frame);
        double receiveMs = nowMs() - before;
        if (result < 0) {
          if (result == AVERROR(EAGAIN)) {
            ++emptyReceives;
            emptyReceiveTotalMs = emptyReceiveTotalMs.load() + receiveMs;
            if (receiveMs > emptyReceiveMaxMs)
              emptyReceiveMaxMs = receiveMs;
          }
          break;
        }
        decodeMs = sendMs.load() + receiveMs;
        TimestampStatus timestamp =
            timestampStatus(frame->pts, staleThroughToken, timing);
        if (timestamp == TimestampStatus::Stale) {
          ++staleDriverFramesDropped;
          av_frame_unref(frame);
          continue;
        }
        auto it = timing.find(frame->pts);
        if (timestamp == TimestampStatus::Unknown || it == timing.end()) {
          fail("output-timestamp", AVERROR_INVALIDDATA);
          break;
        }
        if (frame->width < 1 || frame->height < 1 || frame->width > 4096 ||
            frame->height > 4096 ||
            !sws_isSupportedInput(AVPixelFormat(frame->format))) {
          fail("output-format", AVERROR_INVALIDDATA);
          break;
        }
        // QCOM returns coded dimensions with zero crop fields. Repair only
        // negotiated pictures, from their own validated SPS/PTS snapshot.
        if (it->second.layout.present && decoderRequested == "v4l2" &&
            !normalizeH264Crop(frame, it->second.crop,
                               it->second.layout.encodedWidth,
                               it->second.layout.encodedHeight)) {
          fail("output-crop", AVERROR_INVALIDDATA);
          break;
        }
        if (it->second.layout.present && decoderRequested == "v4l2") {
          const auto &metadata = it->second.crop;
          if (metadata.colorSpace != AVCOL_SPC_UNSPECIFIED)
            frame->colorspace = metadata.colorSpace;
          frame->color_range = metadata.colorRange;
        }
        double convert = nowMs();
        auto pic = std::make_shared<Picture>();
        pic->width = frame->width;
        pic->height = frame->height;
        pic->layout = it->second.layout;
        if (!pic->layout.matches(frame->width, frame->height)) {
          fail("output-layout", AVERROR_INVALIDDATA);
          break;
        }
        pic->seq = it->second.seq;
        pic->received = it->second.received;
        // How long the decoder held this picture, and how many later packets
        // it needed first (0 = released by its own packet).
        if (it->second.sent > 0)
          raiseMax(decoderHoldMaxMs, nowMs() - it->second.sent);
        decoderHoldMs = it->second.sent > 0 ? nowMs() - it->second.sent : 0;
        decoderOutputLag = lastSentToken - frame->pts;
        if (decoderOutputLag > decoderOutputLagMax)
          decoderOutputLagMax = decoderOutputLag.load();
        pic->colorSpace = frame->colorspace;
        pic->colorRange = frame->color_range;
        timing.erase(it);
        decoderPixelFormat = frame->format;
        decoderColorSpace = frame->colorspace;
        decoderColorRange = frame->color_range;
        bool gpuFormat = frame->format == AV_PIX_FMT_NV12 ||
                         frame->format == AV_PIX_FMT_YUV420P ||
                         frame->format == AV_PIX_FMT_YUVJ420P;
        bool useGpu = conversionRequested == "gpu" && gpuFormat;
        if (useGpu) {
          const int chromaWidth = (pic->width + 1) / 2;
          const int chromaHeight = (pic->height + 1) / 2;
          auto copyPlane = [](std::vector<uint8_t> &dst, const uint8_t *src,
                              int stride, int rowBytes, int rows) {
            if (!src || std::abs(stride) < rowBytes)
              return false;
            dst.resize(size_t(rowBytes) * rows);
            for (int row = 0; row < rows; ++row)
              memcpy(dst.data() + size_t(row) * rowBytes,
                     src + ptrdiff_t(row) * stride, rowBytes);
            return true;
          };
          pic->format = frame->format == AV_PIX_FMT_NV12
                            ? Picture::Format::NV12
                            : Picture::Format::YUV420P;
          if (frame->format == AV_PIX_FMT_YUVJ420P &&
              pic->colorRange == AVCOL_RANGE_UNSPECIFIED)
            pic->colorRange = AVCOL_RANGE_JPEG;
          bool copied = copyPlane(pic->y, frame->data[0], frame->linesize[0],
                                  pic->width, pic->height);
          if (pic->format == Picture::Format::NV12)
            copied = copied &&
                     copyPlane(pic->uv, frame->data[1], frame->linesize[1],
                               chromaWidth * 2, chromaHeight);
          else
            copied = copied &&
                     copyPlane(pic->u, frame->data[1], frame->linesize[1],
                               chromaWidth, chromaHeight) &&
                     copyPlane(pic->v, frame->data[2], frame->linesize[2],
                               chromaWidth, chromaHeight);
          planeCopyMs = nowMs() - convert;
          convertMs = planeCopyMs.load();
          if (!copied) {
            fail("output-plane-layout", AVERROR_INVALIDDATA);
            break;
          }
          conversionActive = "gpu";
        } else {
          planeCopyMs = 0;
          if (conversionRequested == "gpu") {
            ++conversionFallbacks;
            conversionActive = "cpu-fallback";
          } else {
            conversionActive = "cpu";
          }
          pic->rgba.resize(size_t(pic->width) * pic->height * 4);
          // Convert independent tiles independently: chroma interpolation must
          // never use the unrelated tile on the other side of the atlas seam.
          const int tileWidth = pic->layout.atlas ? pic->width / 2 : pic->width;
          if (pic->layout.atlas && frame->format != AV_PIX_FMT_NV12 &&
              frame->format != AV_PIX_FMT_YUV420P && frame->format != AV_PIX_FMT_YUVJ420P) {
            fail("atlas-conversion-format", AVERROR_INVALIDDATA); break;
          }
          sws = sws_getCachedContext(sws, tileWidth, pic->height,
                                     AVPixelFormat(frame->format), tileWidth,
                                     pic->height, AV_PIX_FMT_RGBA, SWS_BILINEAR,
                                     nullptr, nullptr, nullptr);
          if (!sws) {
            fail("conversion-context", AVERROR(ENOMEM));
            break;
          }
          int cs = frame->colorspace == AVCOL_SPC_BT709 ? SWS_CS_ITU709
                                                        : SWS_CS_ITU601;
          auto coefficients = sws_getCoefficients(cs);
          sws_setColorspaceDetails(sws, coefficients,
                                   pic->colorRange == AVCOL_RANGE_JPEG,
                                   coefficients, 1, 0, 1 << 16, 1 << 16);
          bool converted = true;
          for (int tile = 0; tile < (pic->layout.atlas ? 2 : 1); ++tile) {
            const int x = tile * tileWidth;
            const uint8_t *src[] = {frame->data[0], frame->data[1], frame->data[2], frame->data[3]};
            if (tile) {
              src[0] += x;
              src[1] += frame->format == AV_PIX_FMT_NV12 ? x : x / 2;
              if (src[2]) src[2] += x / 2;
            }
            uint8_t *dst[] = {pic->rgba.data() + x * 4};
            int strides[] = {pic->width * 4};
            if (sws_scale(sws, src, frame->linesize, 0, pic->height, dst, strides) != pic->height)
              converted = false;
          }
          if (!converted) { fail("conversion", AVERROR_INVALIDDATA); break; }
          convertMs = nowMs() - convert;
        }
        pic->decoded = nowMs();
        if (!decoderActive.exchange(true))
          fprintf(stderr,
                  "[native] decoder active requested=%s codec=%s format=%d "
                  "conversion=%s size=%dx%d\n",
                  decoderRequested.c_str(), codecName(), frame->format,
                  conversionActive.load(), pic->width, pic->height);
        ++decoded;
        publish(std::move(pic));
        // Release the driver buffer before a format change or context reopen.
        av_frame_unref(frame);
      }
      av_frame_unref(frame);
      return result;
    };
    while (!stop) {
      Packet p;
      RecoveryAction action = RecoveryAction::KeepContext;
      bool havePacket = false;
      {
        std::unique_lock<std::mutex> l(mutex);
        wake.wait_for(l, std::chrono::milliseconds(5),
                      [&] { return stop || !packets.empty(); });
        if (!packets.empty()) {
          p = std::move(packets.front());
          packets.pop_front();
          havePacket = true;
        }
      }
      if (stop)
        break;
      if (!havePacket) {
        // V4L2 may complete output after the final packet of a static desktop.
        if (submitted && decoderRequested == "v4l2") {
          int result = drain();
          if (result != AVERROR(EAGAIN) && result != AVERROR_EOF && !stop)
            fail("receive", result);
        }
        continue;
      }
      packetQueueWaitMs = nowMs() - p.received;
      const double stepStart = nowMs();
      H264Crop packetCrop;
      if (decoderRequested == "v4l2") {
        int parsed = headers.parse(p.bytes);
        if (parsed < 0) {
          fail("h264-headers", parsed);
          break;
        }
        if (p.layout.present) {
          if (p.resetReason == ResetReason::Reconnect) cropHeaders = H264CropHeaders{};
          if (!cropHeaders.parse(p.bytes, packetCrop) ||
              packetCrop.width() != headers.parser->width ||
              packetCrop.height() != headers.parser->height ||
              packetCrop.codedWidth != headers.parser->coded_width ||
              packetCrop.codedHeight != headers.parser->coded_height ||
              !p.layout.matches(packetCrop.width(), packetCrop.height())) {
            fail("h264-crop", AVERROR_INVALIDDATA);
            break;
          }
        }
        bool changed = context && (headers.parser->width != inputWidth ||
                                   headers.parser->height != inputHeight ||
                                   headers.parser->coded_width != codedWidth ||
                                   headers.parser->coded_height != codedHeight);
        const bool requiresIdr = !context || changed ||
                                 p.resetReason != ResetReason::None;
        if (requiresIdr &&
            (!p.key || headers.parser->key_frame != 1)) {
          ++droppedEncoded;
          requestKey = true;
          std::lock_guard<std::mutex> lock(mutex);
          requireKeyLocked(changed ? ResetReason::FormatChange
                                   : p.resetReason);
          droppedEncoded += packets.size();
          packets.clear();
          continue;
        }
        action = recoveryAction(
            decoderRequested == "v4l2", context != nullptr, changed,
            p.resetReason);
        if (action == RecoveryAction::ReopenContext) {
          // A real reconnect or format change needs a clean driver context.
          // Queue overflow alone recovers at the validated IDR below.
          DecoderOpenReason reason = !context
                                         ? DecoderOpenReason::Initial
                                         : p.resetReason == ResetReason::Reconnect
                                               ? DecoderOpenReason::Reconnect
                                               : DecoderOpenReason::FormatChange;
          if (!open(reason, context &&
                                 (changed || p.resetReason ==
                                                ResetReason::FormatChange)))
            break;
          submitted = false;
        }
        if (action == RecoveryAction::SameContextIdrBarrier) {
          // IDR starts a new H.264 reference state. Old V4L2 output may still
          // surface afterward, so retire its bounded PTS metadata and ignore
          // delayed frames through this token instead of reopening the device.
          retireTimingHistory(timing, token, staleThroughToken);
        }
      } else if (p.resetReason != ResetReason::None && submitted) {
        const RecoveryAction action =
            recoveryAction(false, context != nullptr, false, p.resetReason);
        if (action != RecoveryAction::FlushContext) {
          fail("recovery-policy", AVERROR_BUG);
          break;
        }
        // Software reconnects keep the context and flush it. Retire every
        // previously issued timestamp before the flush so delayed output
        // cannot grow the timing map across repeated reconnects.
        retireTimingHistory(timing, token, staleThroughToken);
        avcodec_flush_buffers(context);
        submitted = false;
      }
      av_packet_unref(avp);
      // The Frame's V4L2 decoder holds POC-type-0 pictures for one frame
      // interval; rewrite to POC type 2 so each is released when decoded
      // (h264_poc.hpp). Anything the rewriter cannot handle turns it off and
      // restarts at the next IDR, never decoding a half-rewritten sequence.
      const std::vector<uint8_t> *bytes = &p.bytes;
      if (decoderRequested == "v4l2" && pocRewrite) {
        if (pocRewriter.rewrite(p.bytes, rewrittenBytes)) {
          bytes = &rewrittenBytes;
        } else {
          pocRewrite = false;
          fprintf(stderr, "[native] POC rewrite unsupported for this stream; using it unchanged from the next IDR\n");
          ++droppedEncoded;
          requestKey = true;
          std::lock_guard<std::mutex> lock(mutex);
          requireKeyLocked(ResetReason::FormatChange);
          droppedEncoded += packets.size();
          packets.clear();
          continue;
        }
      }
      int result = av_new_packet(avp, int(bytes->size()));
      if (result < 0) {
        fail("packet-allocation", result);
        break;
      }
      memcpy(avp->data, bytes->data(), bytes->size());
      avp->pts = avp->dts = ++token;
      if (p.key)
        avp->flags |= AV_PKT_FLAG_KEY;
      // Bound metadata independently of compressed and decoded picture queues.
      if (timing.size() >= 64) {
        fail("inflight-limit", AVERROR(ENOBUFS));
        break;
      }
      timing[token] = {p.seq, p.received, p.layout, packetCrop};
      auto highWater = decoderTimingHighWater.load();
      if (timing.size() > highWater)
        decoderTimingHighWater = timing.size();
      double before = nowMs();
      timing[token].sent = before;
      lastSentToken = token;
      result = avcodec_send_packet(context, avp);
      sendMs = nowMs() - before;
      if (result == AVERROR(EAGAIN)) {
        uint64_t count = decoded;
        int drained = drain();
        if (stop)
          break;
        if (drained != AVERROR(EAGAIN) || decoded == count) {
          fail("send-receive-progress",
               drained == AVERROR(EAGAIN) ? AVERROR_BUG : drained);
          break;
        }
        before = nowMs();
        result = avcodec_send_packet(context, avp);
        sendMs = sendMs.load() + nowMs() - before;
      }
      if (result < 0) {
        if (decoderRequested == "v4l2") {
          fail("send", result);
          break;
        }
        ++errors;
        ++decoderSendErrors;
        fprintf(stderr,
                "[native] decoder recoverable stage=send code=%d; requesting IDR\n",
                result);
        requestKey = true;
        std::lock_guard<std::mutex> l(mutex);
        requireKeyLocked(ResetReason::DecoderError);
        droppedEncoded += packets.size();
        packets.clear();
        continue;
      }
      submitted = true;
      if (p.resetReason == ResetReason::Overflow &&
          decoderRequested == "v4l2" &&
          action == RecoveryAction::SameContextIdrBarrier) {
        ++overflowRecoveryIdrs;
        lastOverflowRecoverySeq = p.seq;
      }
      result = drain();
      raiseMax(decodeStepMaxMs, nowMs() - stepStart);
      if (result != AVERROR(EAGAIN) && result != AVERROR_EOF && !stop) {
        if (decoderRequested == "v4l2") {
          fail("receive", result);
          break;
        }
        ++errors;
        ++decoderReceiveErrors;
        fprintf(stderr,
                "[native] decoder recoverable stage=receive code=%d; requesting IDR\n",
                result);
        requestKey = true;
        std::lock_guard<std::mutex> l(mutex);
        requireKeyLocked(ResetReason::DecoderError);
        droppedEncoded += packets.size();
        packets.clear();
      }
    }
    sws_freeContext(sws);
    av_packet_free(&avp);
    av_frame_free(&frame);
    avcodec_free_context(&context);
  }
};
} // namespace fw
