#pragma once
#include "websocket.hpp"
#include "clock_sync.hpp"
#include "host.hpp"
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
#include <fcntl.h>
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
enum class DecoderOpenReason { Initial, Reconnect, FormatChange, Live };
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
inline bool h264HasSpsAndPps(const std::vector<uint8_t> &bytes) {
  bool sps = false, pps = false;
  for (size_t i = 0; i + 3 < bytes.size();) {
    size_t prefix = 0;
    if (bytes[i] == 0 && bytes[i + 1] == 0 && bytes[i + 2] == 1)
      prefix = 3;
    else if (i + 4 <= bytes.size() && bytes[i] == 0 && bytes[i + 1] == 0 &&
             bytes[i + 2] == 0 && bytes[i + 3] == 1)
      prefix = 4;
    if (!prefix) {
      ++i;
      continue;
    }
    if (i + prefix < bytes.size()) {
      const int type = bytes[i + prefix] & 0x1f;
      sps |= type == 7;
      pps |= type == 8;
      if (sps && pps)
        return true;
    }
    i += prefix + 1;
  }
  return false;
}
struct Pipeline {
  const std::string decoderRequested;
  const std::string conversionRequested;
  std::atomic<bool> decoderActive{false};
  std::atomic<bool> streamStill{false}, decoderSessionOpen{false};
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
    if (::pipe(wakePipe) == 0)
      for (int fd : wakePipe) fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK);
    else
      wakePipe[0] = wakePipe[1] = -1;
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
      decoderOpenFailures{0}, decoderStillReleases{0}, decoderLiveOpens{0};
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
  // Keep packet ordinals parallel to `packets` so ordered still/live fences do
  // not change the packet type or the existing queue recovery contract.
  std::deque<uint64_t> packetOrdinals;
  struct StreamTransition {
    uint64_t afterPacket = 0;
    bool still = false;
    // Keep a transient still request when controls toggle before the decoder
    // can apply them. A following live request still has to release the old
    // session and wait for a fresh parameterized IDR.
    bool releaseSession = false;
  };
  std::deque<StreamTransition> streamTransitions;
  uint64_t nextPacketOrdinal = 0, processedPacketOrdinal = 0,
           skippedThroughOrdinal = 0, inFlightPacketOrdinal = 0;
  bool networkStillRequested = false; // protected by mutex
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
  std::atomic<bool> foveationNegotiated{false}, atlasNegotiated{false}, directNegotiated{false},
      gazeNegotiated{false};
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
  void dropQueuedLocked() {
    if (!packetOrdinals.empty())
      skippedThroughOrdinal =
          std::max(skippedThroughOrdinal, packetOrdinals.back());
    packets.clear();
    packetOrdinals.clear();
    if (skippedThroughOrdinal) {
      std::deque<StreamTransition> compacted;
      bool haveCompacted = false;
      StreamTransition compactedTransition;
      for (const auto &transition : streamTransitions) {
        const bool fenceWasDropped =
            transition.afterPacket <= skippedThroughOrdinal &&
            (!inFlightPacketOrdinal ||
             transition.afterPacket >= inFlightPacketOrdinal);
        if (!fenceWasDropped) {
          compacted.push_back(transition);
          continue;
        }
        if (!haveCompacted) {
          compactedTransition = transition;
          haveCompacted = true;
        } else {
          compactedTransition.still = transition.still;
          compactedTransition.releaseSession |= transition.releaseSession;
        }
      }
      if (haveCompacted) {
        compactedTransition.afterPacket = skippedThroughOrdinal;
        auto position = std::find_if(
            compacted.begin(), compacted.end(), [&](const auto &transition) {
              return transition.afterPacket > skippedThroughOrdinal;
            });
        compacted.insert(position, compactedTransition);
      }
      streamTransitions.swap(compacted);
    }
    if (!inFlightPacketOrdinal)
      processedPacketOrdinal =
          std::max(processedPacketOrdinal, skippedThroughOrdinal);
  }
  void connectionOpened(bool resumed) {
    std::lock_guard<std::mutex> lock(mutex);
    droppedEncoded += packets.size();
    dropQueuedLocked();
    if (acceptsSurfaces) { surfaceMessage = {}; surfaceRevision = 0; }
    // Keep the still request across reconnects; post-reconnect video stays
    // discarded until the server sends a live control.
    requireKeyLocked(resumed ? ResetReason::Reconnect : ResetReason::None);
    wake.notify_all();
  }
  // Text controls enter the same ordered pipeline as video. The decoder sees
  // a transition only after every accepted packet before its fence completes.
  void setStreamStill(bool still) {
    std::lock_guard<std::mutex> lock(mutex);
    if (networkStillRequested == still)
      return;
    networkStillRequested = still;
    if (!streamTransitions.empty() &&
        streamTransitions.back().afterPacket == nextPacketOrdinal) {
      // With no accepted picture between controls, retain the final mode but
      // remember that any still request still requires releasing the session.
      streamTransitions.back().still = still;
      streamTransitions.back().releaseSession |= still;
    } else {
      streamTransitions.push_back({nextPacketOrdinal, still, still});
    }
    if (!still) {
      // A reopened session must begin at an IDR. Do not ask the decoder to open
      // here: the network may still be between the live control and that IDR.
      requireKeyLocked(ResetReason::None);
      requestKey = true;
    }
    wake.notify_all();
  }
  ~Pipeline() {
    finish();
    for (int fd : wakePipe) if (fd >= 0) ::close(fd);
  }
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
    if (networkStillRequested) {
      ++droppedEncoded;
      return;
    }
    if (packets.size() >= maxPacketQueue) {
      ++packetQueueOverflows;
      droppedEncoded += packets.size();
      dropQueuedLocked();
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
    packetOrdinals.push_back(++nextPacketOrdinal);
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
  SurfaceMessage surfaceMessage;
  uint64_t surfaceRevision = 0;
  bool acceptsSurfaces = false;

  // Native content input (FW-15). Negotiated per connection: the helper's
  // hello offers `nativeInput` and a fresh epoch. Focus, edges, barriers and
  // releases are reliable and ordered; only consecutive unsent moves
  // coalesce. The host queues on its thread, the network thread numbers and
  // sends at once (the wake pipe interrupts its poll).
  struct InputGeometry {
    uint64_t revision = 0;
    uint64_t connection = 0;  // the connection it arrived on (Pipeline::inputConnection)
    uint32_t pictureSeq = 0;  // 0: applies now (a hidden surface has no picture)
    SurfaceRect rect;
    float content[4]{0, 0, 1, 1};
    bool visible = false;
    bool contains(float u, float v) const {
      return visible && u >= content[0] && v >= content[1] &&
          u <= content[0] + content[2] && v <= content[1] + content[3];
    }
    static bool parse(const Json &j, InputGeometry &g) {
      auto integer = [&](const char *name, double low, double high, double &out) {
        auto f = field(j, name);
        if (!f || !json_object_is_type(f, json_type_int)) return false;
        out = json_object_get_double(f);
        return out >= low && out <= high;
      };
      auto quad = [&](const char *name, float (&out)[4]) {
        auto a = field(j, name);
        if (!a || !json_object_is_type(a, json_type_array) || json_object_array_length(a) != 4) return false;
        for (int i = 0; i < 4; ++i) {
          auto v = json_object_array_get_idx(a, i);
          if (!json_object_is_type(v, json_type_int) && !json_object_is_type(v, json_type_double)) return false;
          const double d = json_object_get_double(v);
          if (!std::isfinite(d) || std::fabs(d) > 1000000) return false;
          out[i] = float(d);
        }
        return true;
      };
      double v = 0, revision = 0, seq = 0;
      float rect[4]{}, content[4]{};
      auto visible = field(j, "visible");
      if (!integer("v", 1, 1, v) || !integer("revision", 1, 9007199254740991.0, revision) ||
          !integer("pictureSeq", 0, UINT32_MAX, seq) || !quad("rect", rect) || !quad("contentUV", content) ||
          !visible || !json_object_is_type(visible, json_type_boolean))
        return false;
      g = {};
      g.revision = uint64_t(revision); g.pictureSeq = uint32_t(seq);
      g.rect = {rect[0], rect[1], rect[2], rect[3]};
      g.visible = json_object_get_boolean(visible);
      for (int i = 0; i < 4; ++i) g.content[i] = content[i];
      if (g.visible && (rect[2] <= 0 || rect[3] <= 0 || content[0] < 0 || content[1] < 0 || content[2] <= 0 ||
                        content[3] <= 0 || content[0] + content[2] > 1.0001f || content[1] + content[3] > 1.0001f))
        return false;
      return true;
    }
  };
  struct InputEvent {
    // Typing: a physical Mac key went down or up. PhysicalHeld: a trackpad
    // or mouse button is (ok) or is no longer held.
    enum Kind { Ack, Refused, Physical, Remote, Focused, Unfocused, Released, Reset, Typing, PhysicalHeld } kind = Ack;
    uint64_t token = 0;
    bool ok = false;
  };
  static constexpr size_t maxInputQueue = 64, maxInputEvents = 64;
  std::atomic<bool> inputNegotiated{false};
  // The helper takes `gaze-highlight` on this stream (hello `gazeHighlight`).
  std::atomic<bool> highlightNegotiated{false};
  std::atomic<uint64_t> inputConnection{0};  // increases before each connection
  std::atomic<uint64_t> inputSent{0}, inputCoalesced{0}, inputOverflows{0}, inputRefusals{0}, inputGeometries{0};
  struct InputItem { std::string body; bool move = false; uint64_t token = 0; };
  std::deque<InputItem> inputQueue;          // outMutex
  std::deque<InputEvent> inputEvents;        // outMutex
  std::deque<InputGeometry> inputGeometry;   // outMutex
  std::string inputEpoch;                    // outMutex
  int wakePipe[2]{-1, -1};
  // `body` is the message without its closing brace; the network thread adds
  // the epoch and sequence. False: not negotiated, or the queue is full (the
  // caller cancels and releases, never dropping an edge silently).
  bool sendInput(std::string body, bool move = false, uint64_t token = 0) {
    {
      std::lock_guard<std::mutex> l(outMutex);
      if (!inputNegotiated) return false;
      if (move && !inputQueue.empty() && inputQueue.back().move) {
        inputQueue.back().body = std::move(body);
        ++inputCoalesced;
      } else if (inputQueue.size() >= maxInputQueue) {
        ++inputOverflows;
        return false;
      } else
        inputQueue.push_back({std::move(body), move, token});
    }
    wakeNetwork();
    return true;
  }
  // Drops everything queued and lets go of all this connection holds.
  void releaseInput() {
    {
      std::lock_guard<std::mutex> l(outMutex);
      inputQueue.clear();
      if (inputNegotiated) inputQueue.push_back({"{\"t\":\"release\"", false, 0});
    }
    wakeNetwork();
  }
  std::vector<InputEvent> takeInputEvents() {
    std::lock_guard<std::mutex> l(outMutex);
    std::vector<InputEvent> events(inputEvents.begin(), inputEvents.end());
    inputEvents.clear();
    return events;
  }
  std::vector<InputGeometry> takeInputGeometry() {
    std::lock_guard<std::mutex> l(outMutex);
    std::vector<InputGeometry> list(inputGeometry.begin(), inputGeometry.end());
    inputGeometry.clear();
    return list;
  }
  void wakeNetwork() {
    if (wakePipe[1] >= 0) {
      const char c = 1;
      if (::write(wakePipe[1], &c, 1) < 0) { /* full: a wake is already pending */ }
    }
  }
  void inputEvent(InputEvent e) {
    std::lock_guard<std::mutex> l(outMutex);
    if (inputEvents.size() >= maxInputEvents) inputEvents.pop_front();
    inputEvents.push_back(e);
  }
  // A new or lost connection starts neutral; nothing queued is replayed.
  void resetInput() {
    inputNegotiated = false;
    highlightNegotiated = false;
    ++inputConnection;
    {
      std::lock_guard<std::mutex> l(outMutex);
      inputQueue.clear();
      inputGeometry.clear();
      inputEpoch.clear();
    }
    inputEvent({InputEvent::Reset});
  }
  std::string pendingFocus;
  void sendFocus(float u, float v) {
    std::lock_guard<std::mutex> lock(outMutex);
    pendingFocus = "{\"t\":\"focus\",\"u\":" + std::to_string(u) +
                   ",\"v\":" + std::to_string(v) + "}";
  }
  std::pair<SurfaceMessage, uint64_t> surface() {
    std::lock_guard<std::mutex> lock(mutex);
    return {surfaceMessage, surfaceRevision};
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
        if (endpoint.sendsGaze()) {
          ws.idlePollMs = 8;
          ws.serviceIdleWhileReading = true;
        }
        resetInput();
        uint64_t inputQ = 0;
        std::map<uint64_t, uint64_t> inputTokens;  // q -> host token
        ws.wakeFd = wakePipe[0];
        ws.open(endpoint, reconnect);
        connected = true;
        ++reconnects;
        connectionOpened(resumed);
        clockValid = false;
        foveationNegotiated = false; atlasNegotiated = false; directNegotiated = false;
        gazeNegotiated = false;
        std::string gazeSession;
        int directWidth = 0, directHeight = 0;
        FoveationClock gazeClock;
        { std::lock_guard<std::mutex> l(outMutex); gazeSample = {}; }
        double ping = 0, key = 0, lastGaze = 0, lastSentSample = -1;
        bool lastSentValid = false;
        ws.idle = [&] {
          double now = nowMs();
          if (inputNegotiated) {
            std::deque<InputItem> items;
            std::string epoch;
            {
              std::lock_guard<std::mutex> l(outMutex);
              items.swap(inputQueue);
              epoch = inputEpoch;
            }
            for (auto &item : items) {
              const uint64_t q = ++inputQ;
              if (item.token) {
                inputTokens[q] = item.token;
                while (inputTokens.size() > 256) inputTokens.erase(inputTokens.begin());
              }
              ws.sendText(item.body + ",\"epoch\":" + jsonString(epoch) + ",\"q\":" + std::to_string(q) + "}");
              ++inputSent;
            }
          }
          if (endpoint.sendsGaze()) {
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
          if ((directNegotiated || gazeNegotiated) && clockValid && now - lastGaze >= 16) {
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
          std::string focused;
          { std::lock_guard<std::mutex> l(outMutex); focused.swap(pendingFocus); }
          if (!focused.empty()) ws.sendText(focused);
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
            if (t == "surface" && acceptsSurfaces) {
              try {
                auto message = SurfaceMessage::parse(std::string(bytes.begin(), bytes.end()));
                message.received = receivedAt;
                std::lock_guard<std::mutex> lock(mutex);
                // Same-sequence hot/geometry updates are valid; older metadata is not.
                if (!surfaceRevision || message.seq == surfaceMessage.seq ||
                    sequenceNewer(message.seq, surfaceMessage.seq)) {
                  if (surfaceRevision && message.visible && surfaceMessage.visible &&
                      message.seq == surfaceMessage.seq && message.rect.x == surfaceMessage.rect.x &&
                      message.rect.y == surfaceMessage.rect.y && message.rect.w == surfaceMessage.rect.w &&
                      message.rect.h == surfaceMessage.rect.h)
                    message.received = surfaceMessage.received;
                  surfaceMessage = message;
                  ++surfaceRevision;
                }
              } catch (...) { /* Malformed metadata cannot expose an old picture. */ }
            } else if (t == "input-ack" || t == "input-state" || t == "input-released" || t == "input-geometry") {
              std::string epoch;
              { std::lock_guard<std::mutex> l(outMutex); epoch = inputEpoch; }
              if (!inputNegotiated || (t != "input-geometry" && str(j, "epoch") != epoch)) {
                // Another connection's or a stale message: ignored.
              } else if (t == "input-ack") {
                const double q = num(j, "q", 0);
                auto it = q >= 1 ? inputTokens.find(uint64_t(q)) : inputTokens.end();
                const bool ok = flag(j, "ok");
                if (!ok) ++inputRefusals;
                if (it != inputTokens.end()) {
                  inputEvent({InputEvent::Ack, it->second, ok});
                  inputTokens.erase(it);
                } else if (!ok)
                  inputEvent({InputEvent::Refused});
              } else if (t == "input-state") {
                const auto owner = str(j, "owner");
                if (owner == "physical") inputEvent({InputEvent::Physical});
                else if (owner == "remote") inputEvent({InputEvent::Remote});
                auto focused = field(j, "focused");
                if (focused && json_object_is_type(focused, json_type_boolean))
                  inputEvent({json_object_get_boolean(focused) ? InputEvent::Focused : InputEvent::Unfocused});
                auto typed = field(j, "typing");
                if (typed && json_object_is_type(typed, json_type_boolean) && json_object_get_boolean(typed))
                  inputEvent({InputEvent::Typing});
                auto held = field(j, "physicalHeld");
                if (held && json_object_is_type(held, json_type_boolean))
                  inputEvent({InputEvent::PhysicalHeld, 0, bool(json_object_get_boolean(held))});
              } else if (t == "input-released") {
                inputEvent({InputEvent::Released});
              } else {
                InputGeometry g;
                if (InputGeometry::parse(j, g)) {
                  g.connection = inputConnection;
                  ++inputGeometries;
                  std::lock_guard<std::mutex> l(outMutex);
                  if (inputGeometry.size() >= 16) inputGeometry.pop_front();
                  inputGeometry.push_back(g);
                }
              }
            } else if (t == "hello") {
              auto r = str(j, "r");
              if (r.empty() || r.size() > 256)
                throw std::runtime_error("invalid reconnect key");
              reconnect = r;
              {
                // Native content input, offered by Spatial sources only.
                auto epoch = str(j, "inputEpoch");
                auto offered = field(j, "nativeInput");
                if (offered && json_object_is_type(offered, json_type_int) && num(j, "nativeInput") == 1 &&
                    !epoch.empty() && epoch.size() <= 64 &&
                    epoch.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_") ==
                        std::string::npos) {
                  {
                    std::lock_guard<std::mutex> l(outMutex);
                    if (inputEpoch != epoch) inputQueue.clear();
                    inputEpoch = epoch;
                  }
                  inputNegotiated = true;
                  auto highlight = field(j, "gazeHighlight");
                  highlightNegotiated = highlight && json_object_is_type(highlight, json_type_int) &&
                      num(j, "gazeHighlight") == 1;
                  // Input goes out promptly, even while a picture is arriving.
                  ws.idlePollMs = std::min(ws.idlePollMs, 8);
                  ws.serviceIdleWhileReading = true;
                }
              }
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
              if (endpoint.gaze && !endpoint.foveation) {
                // Optional: a companion without gaze-directed detail simply
                // streams full detail, so this is not an error.
                auto nonce = str(j, "gazeSession");
                const bool offered = json_object_is_type(field(j, "gaze"), json_type_int) && num(j, "gaze") == 1;
                if (offered && !nonce.empty() && nonce.size() <= 64 &&
                    nonce.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-") == std::string::npos) {
                  gazeSession = nonce;
                  gazeNegotiated = true;
                }
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
                if (endpoint.sendsGaze()) {
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
            } else if (t == "still") {
              setStreamStill(true);
            } else if (t == "live") {
              setStreamStill(false);
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
        resetInput();
        clockValid = false;
        foveationNegotiated = false; atlasNegotiated = false; directNegotiated = false; gazeNegotiated = false;
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
    const bool pocRewriteRequested = pocRewrite;
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
    bool awaitingLiveKey = false;
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
      decoderSessionOpen = false;
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
      decoderSessionOpen = true;
      if (reason == DecoderOpenReason::Live)
        ++decoderLiveOpens;
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
    auto releaseForStill = [&]() {
      const bool hadSession = context != nullptr;
      if (context && submitted && !stop) {
        const double started = nowMs();
        double lastOutput = started;
        uint64_t lastDecoded = decoded.load();
        // Some V4L2 drivers publish a completed final surface shortly after
        // the last receive returns EAGAIN. Give that output a short bounded
        // window, then release the session even if the driver keeps waiting.
        const double maxWaitMs = decoderRequested == "v4l2" ? 100 : 0;
        const double quietWaitMs = decoderRequested == "v4l2" ? 30 : 0;
        do {
          int result = drain();
          if (result != AVERROR(EAGAIN) && result != AVERROR_EOF && !stop) {
            if (decoderRequested == "v4l2") {
              fail("still-drain", result);
            } else {
              ++errors;
              ++decoderReceiveErrors;
              fprintf(stderr,
                      "[native] decoder recoverable stage=still-drain code=%d; requesting IDR\n",
                      result);
              requestKey = true;
              std::lock_guard<std::mutex> lock(mutex);
              requireKeyLocked(ResetReason::DecoderError);
              droppedEncoded += packets.size();
              dropQueuedLocked();
            }
            break;
          }
          uint64_t currentDecoded = decoded.load();
          if (currentDecoded != lastDecoded) {
            lastDecoded = currentDecoded;
            lastOutput = nowMs();
          }
          if (nowMs() - lastOutput >= quietWaitMs)
            break;
          std::this_thread::sleep_for(std::chrono::milliseconds(1));
        } while (nowMs() - started < maxWaitMs && !stop);
      }
      avcodec_free_context(&context);
      decoderSessionOpen = false;
      decoderActive = false;
      submitted = false;
      timing.clear();
      pocRewriter.reset();
      pocRewrite = pocRewriteRequested;
      if (hadSession)
        ++decoderStillReleases;
    };
    auto completePacket = [&](uint64_t ordinal) {
      {
        std::lock_guard<std::mutex> lock(mutex);
        inFlightPacketOrdinal = 0;
        processedPacketOrdinal = std::max(
            processedPacketOrdinal,
            std::max(ordinal, skippedThroughOrdinal));
      }
      wake.notify_all();
    };
    auto rejectLiveKey = [&](uint64_t ordinal) {
      ++droppedEncoded;
      requestKey = true;
      {
        std::lock_guard<std::mutex> lock(mutex);
        requireKeyLocked(ResetReason::None);
        droppedEncoded += packets.size();
        dropQueuedLocked();
      }
      completePacket(ordinal);
    };
    while (!stop) {
      Packet p;
      uint64_t packetOrdinal = 0;
      RecoveryAction action = RecoveryAction::KeepContext;
      bool havePacket = false;
      bool haveTransition = false, transitionStill = false;
      bool transitionReleaseSession = false;
      {
        std::unique_lock<std::mutex> l(mutex);
        wake.wait_for(l, std::chrono::milliseconds(5),
                      [&] {
                        return stop || !packets.empty() ||
                               (!streamTransitions.empty() &&
                                streamTransitions.front().afterPacket <=
                                    processedPacketOrdinal);
                      });
        if (!inFlightPacketOrdinal)
          processedPacketOrdinal =
              std::max(processedPacketOrdinal, skippedThroughOrdinal);
        if (!streamTransitions.empty() &&
            streamTransitions.front().afterPacket <= processedPacketOrdinal) {
          transitionStill = streamTransitions.front().still;
          transitionReleaseSession =
              streamTransitions.front().releaseSession;
          streamTransitions.pop_front();
          haveTransition = true;
        } else if (!packets.empty()) {
          p = std::move(packets.front());
          packets.pop_front();
          packetOrdinal = packetOrdinals.front();
          packetOrdinals.pop_front();
          inFlightPacketOrdinal = packetOrdinal;
          havePacket = true;
        }
      }
      if (stop)
        break;
      if (haveTransition) {
        if (transitionReleaseSession)
          releaseForStill();
        if (transitionStill) {
          awaitingLiveKey = false;
          streamStill = true;
        } else {
          awaitingLiveKey = true;
          streamStill = false;
        }
        continue;
      }
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
      if (decoderRequested == "v4l2" || !context) {
        int parsed = headers.parse(p.bytes);
        if (parsed < 0) {
          if (awaitingLiveKey) {
            rejectLiveKey(packetOrdinal);
            continue;
          }
          fail("h264-headers", parsed);
          break;
        }
        if (awaitingLiveKey &&
            (!p.key || headers.parser->key_frame != 1 ||
             !h264HasSpsAndPps(p.bytes))) {
          rejectLiveKey(packetOrdinal);
          continue;
        }
        if (p.layout.present) {
          if (p.resetReason == ResetReason::Reconnect) cropHeaders = H264CropHeaders{};
          if (!cropHeaders.parse(p.bytes, packetCrop) ||
              packetCrop.width() != headers.parser->width ||
              packetCrop.height() != headers.parser->height ||
              packetCrop.codedWidth != headers.parser->coded_width ||
              packetCrop.codedHeight != headers.parser->coded_height ||
              !p.layout.matches(packetCrop.width(), packetCrop.height())) {
            if (awaitingLiveKey) {
              rejectLiveKey(packetOrdinal);
              continue;
            }
            fail("h264-crop", AVERROR_INVALIDDATA);
            break;
          }
        }
        bool changed = decoderRequested == "v4l2" && context &&
                       (headers.parser->width != inputWidth ||
                                   headers.parser->height != inputHeight ||
                                   headers.parser->coded_width != codedWidth ||
                                   headers.parser->coded_height != codedHeight);
        const bool requiresIdr = !context || changed ||
                                 p.resetReason != ResetReason::None;
        if (requiresIdr &&
            (!p.key || headers.parser->key_frame != 1)) {
          if (awaitingLiveKey) {
            rejectLiveKey(packetOrdinal);
            continue;
          }
          ++droppedEncoded;
          requestKey = true;
          std::lock_guard<std::mutex> lock(mutex);
          requireKeyLocked(changed ? ResetReason::FormatChange
                                   : p.resetReason);
          droppedEncoded += packets.size();
          dropQueuedLocked();
          // This packet was consumed by the decoder worker; finish its fence
          // after dropping any later packets under the same queue lock.
          inFlightPacketOrdinal = 0;
          processedPacketOrdinal = std::max(
              processedPacketOrdinal,
              std::max(packetOrdinal, skippedThroughOrdinal));
          wake.notify_all();
          continue;
        }
        action = recoveryAction(
            decoderRequested == "v4l2", context != nullptr, changed,
            p.resetReason);
        if (action == RecoveryAction::ReopenContext) {
          // A real reconnect or format change needs a clean driver context.
          // Queue overflow alone recovers at the validated IDR below.
          DecoderOpenReason reason = !context
                                         ? (awaitingLiveKey
                                                ? DecoderOpenReason::Live
                                                : p.resetReason == ResetReason::Reconnect
                                                      ? DecoderOpenReason::Reconnect
                                                      : DecoderOpenReason::Initial)
                                         : p.resetReason == ResetReason::Reconnect
                                               ? DecoderOpenReason::Reconnect
                                               : DecoderOpenReason::FormatChange;
          if (!open(reason, context &&
                                 (changed || p.resetReason ==
                                                ResetReason::FormatChange)))
            break;
          submitted = false;
          if (reason == DecoderOpenReason::Live)
            awaitingLiveKey = false;
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
          {
            std::lock_guard<std::mutex> lock(mutex);
            requireKeyLocked(ResetReason::FormatChange);
            droppedEncoded += packets.size();
            dropQueuedLocked();
          }
          completePacket(packetOrdinal);
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
        {
          std::lock_guard<std::mutex> l(mutex);
          requireKeyLocked(ResetReason::DecoderError);
          droppedEncoded += packets.size();
          dropQueuedLocked();
        }
        completePacket(packetOrdinal);
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
        {
          std::lock_guard<std::mutex> l(mutex);
          requireKeyLocked(ResetReason::DecoderError);
          droppedEncoded += packets.size();
          dropQueuedLocked();
        }
      }
      completePacket(packetOrdinal);
    }
    sws_freeContext(sws);
    av_packet_free(&avp);
    av_frame_free(&frame);
    avcodec_free_context(&context);
    decoderSessionOpen = false;
  }
};
} // namespace fw
