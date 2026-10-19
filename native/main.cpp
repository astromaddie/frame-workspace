#include "overlay.hpp"
#include "gaze_producer.hpp"
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
  const float projection[16]{2,0,.2f,0, 0,3,-.1f,0, 0,0,-1.002f,-.2002f, 0,0,-1,0};
  const float eye[12]{1,0,0,-.03f, 0,1,0,0, 0,0,1,0};
  const float head[12]{1,0,0,1, 0,1,0,2, 0,0,1,3};
  EyeRay ray;
  require(eyeRay(projection,eye,head,.3f,.5f,ray) &&
          std::abs(ray.origin[0]-.97f)<1e-5 && std::abs(ray.origin[1]-2)<1e-5 &&
          ray.direction[0]>0 && ray.direction[1]>0 && ray.direction[2]<0,
          "off-axis NDC to eye ray and standing origin");
  double px=ray.direction[0]/-ray.direction[2],py=ray.direction[1]/-ray.direction[2];
  require(std::abs(2*px-.2-.3)<1e-5 && std::abs(3*py+.1-.5)<1e-5,
          "unprojected ray roundtrips exact projection");
  bool flipX=false,flipY=false;
  require(uvOrientation(.4f,.6f,.25f,.75f,flipX,flipY) && !flipX && !flipY &&
          uvOrientation(.6f,.4f,.75f,.25f,flipX,flipY) && flipX && flipY &&
          !uvOrientation(.4f,.4f,.25f,.75f,flipX,flipY), "runtime UV orientation calibration");
  float badProjection[16]{};
  require(!eyeRay(badProjection,eye,head,0,0,ray) &&
          !eyeRay(projection,eye,head,NAN,0,ray), "invalid eye math fallback");
  auto layoutPacket = std::vector<uint8_t>(38, 0);
  layoutPacket[0] = 0x81; layoutPacket[17] = 1; layoutPacket[18] = 1;
  auto put16 = [&](size_t offset, int value) {
    layoutPacket[offset] = value >> 8; layoutPacket[offset + 1] = value & 255;
  };
  put16(21, 640); put16(23, 480); put16(25, 480); put16(27, 360);
  put16(29, 160); put16(31, 120); put16(33, 320); put16(35, 240);
  auto warped = packet(layoutPacket, true);
  require(warped.layout.valid() && warped.layout.matches(480, 360) &&
          !warped.layout.matches(640, 480) && warped.bytes.size() == 1,
          "validated foveated extension");
  for (int x = 160; x < 480; ++x)
    require(FoveationLayout::compress(x + .5, 160, 320) == x + .5 - 80,
            "focal pixels retain exact sample centers");
  require(FoveationLayout::compress(0, 160, 320) == 0 &&
          FoveationLayout::compress(640, 160, 320) == 480,
          "warp spans both source edges");
  for (int index : {0, 17, 18, 19, 20, 21, 25, 29, 33}) {
    auto bad = layoutPacket;
    bad[index] = 255;
    bool rejected = false;
    try { packet(bad, true); } catch (...) { rejected = true; }
    require(rejected, "malformed layout accepted");
  }
  bool unnegotiated = false;
  try { packet(layoutPacket); } catch (...) { unnegotiated = true; }
  require(unnegotiated, "unnegotiated extension accepted");
  // Atlas is a separately negotiated mode; legacy v1 senders stay valid.
  layoutPacket[18] = 2;
  put16(21, 644); put16(23, 484); put16(25, 644); put16(27, 242);
  put16(29, 322); put16(31, 242); put16(33, 322); put16(35, 242);
  auto atlasPacket = packet(layoutPacket, true, true);
  require(atlasPacket.layout.atlas && atlasPacket.layout.valid() &&
          atlasPacket.layout.matches(644,242), "exact half atlas supports divisible4-not8 corners");
  bool atlasUnnegotiated = false;
  try { packet(layoutPacket, true); } catch (...) { atlasUnnegotiated = true; }
  require(atlasUnnegotiated, "unnegotiated atlas accepted by axis-only session");
  for (const auto &change : std::vector<std::pair<size_t,int>>{{25,642},{27,244},{29,321},{29,324},{31,244},{33,320},{35,240}}) {
    auto saved = layoutPacket;
    put16(change.first,change.second);
    bool rejected = false;
    try { packet(layoutPacket,true,true); } catch (...) { rejected = true; }
    require(rejected, "malformed atlas geometry accepted");
    layoutPacket = saved;
  }
  std::map<int64_t, PacketTiming> layouts;
  layouts[10] = {1, 1, warped.layout};
  auto moved = warped.layout; moved.left = 0; moved.top = 0;
  layouts[11] = {2, 2, moved};
  require(layouts.at(10).layout.left == 160 && layouts.at(11).layout.left == 0,
          "delayed decoder output retains its own layout");
  int64_t layoutBarrier = 0;
  retireTimingHistory(layouts, 10, layoutBarrier);
  require(timestampStatus(10, layoutBarrier, layouts) == TimestampStatus::Stale &&
          timestampStatus(11, layoutBarrier, layouts) == TimestampStatus::Mapped &&
          layouts.at(11).layout.left == 0, "layout follows IDR timestamp barrier");
  // Crop repair accepts only exact coded/visible bounds from the slice's SPS.
  // Exercise nonzero left/top as well as QCOM's bottom-only padding case.
  auto cropSps = [](unsigned id, unsigned left, unsigned right,
                    unsigned top, unsigned bottom, bool fullRange = false) {
    std::vector<bool> bits;
    auto put = [&](uint32_t value, unsigned count) {
      for (unsigned i = count; i; --i) bits.push_back((value >> (i - 1)) & 1);
    };
    auto ue = [&](uint32_t value) {
      ++value; unsigned count = 0;
      for (uint32_t v = value; v; v >>= 1) ++count;
      put(0, count - 1); put(value, count);
    };
    put(66, 8); put(0, 8); put(31, 8); ue(id);
    ue(0); ue(0); ue(0); ue(1); put(0, 1);
    ue(30); ue(22); put(1, 1); put(1, 1); put(1, 1);
    ue(left); ue(right); ue(top); ue(bottom);
    put(1,1); put(0,1); put(0,1); put(1,1); // VUI, no aspect/overscan, video signal
    put(5,3); put(fullRange,1); put(1,1); put(1,8); put(1,8); put(1,8); put(1,1);
    while (bits.size() % 8) bits.push_back(false);
    std::vector<uint8_t> out{0,0,1,0x67};
    unsigned zeros = 0;
    for (size_t i = 0; i < bits.size(); i += 8) {
      uint8_t byte = 0;
      for (unsigned j = 0; j < 8; ++j) byte = (byte << 1) | bits[i+j];
      if (zeros >= 2 && byte <= 3) { out.push_back(3); zeros = 0; }
      out.push_back(byte); zeros = byte == 0 ? zeros + 1 : 0;
    }
    return out;
  };
  H264CropHeaders cropParser;
  H264Crop crop;
  auto cropBytes = cropSps(0,2,6,1,3);
  // PPS0 references SPS0, then a later SPS1 must not replace that mapping.
  const uint8_t pps0[]{0,0,1,0x68,0xc0};
  cropBytes.insert(cropBytes.end(), std::begin(pps0), std::end(pps0));
  auto otherSps = cropSps(1,0,8,0,4,true);
  cropBytes.insert(cropBytes.end(), otherSps.begin(), otherSps.end());
  const uint8_t slice0[]{0,0,1,0x65,0xb8};
  cropBytes.insert(cropBytes.end(), std::begin(slice0), std::end(slice0));
  require(cropParser.parse(cropBytes,crop) && crop.left == 4 && crop.top == 2 &&
          crop.width() == 480 && crop.height() == 360 && crop.colorSpace == AVCOL_SPC_BT709 &&
          crop.colorRange == AVCOL_RANGE_MPEG, "slice PPS selects crop and VUI SPS");
  auto savedCrop = crop;
  auto badCrop = cropSps(0,248,0,0,0);
  badCrop.insert(badCrop.end(), std::begin(slice0), std::end(slice0));
  require(!cropParser.parse(badCrop,crop), "out-of-bounds SPS crop rejected");
  require(!cropParser.parse({0,0,1,0x67,66},crop), "truncated crop SPS rejected");
  AVFrame *cropped = av_frame_alloc();
  require(cropped != nullptr, "crop test frame");
  cropped->format = AV_PIX_FMT_NV12; cropped->width = 496; cropped->height = 368;
  require(av_frame_get_buffer(cropped,32) == 0, "crop test planes");
  auto originalY = cropped->data[0], originalUV = cropped->data[1];
  auto strideY = cropped->linesize[0], strideUV = cropped->linesize[1];
  cropped->crop_bottom = 2;
  require(!normalizeH264Crop(cropped,savedCrop,480,360), "conflicting decoder crop rejected");
  cropped->crop_bottom = 0;
  require(!normalizeH264Crop(cropped,savedCrop,480,358), "layout crop mismatch rejected");
  require(normalizeH264Crop(cropped,savedCrop,480,360) &&
          cropped->data[0] == originalY + 2*strideY + 4 &&
          cropped->data[1] == originalUV + strideUV + 4 &&
          normalizeH264Crop(cropped,savedCrop,480,360), "visible NV12 plane origins and already-cropped frame");
  av_frame_free(&cropped);
  layouts[11].crop = savedCrop;
  crop.left = 0;
  require(layouts[11].crop.left == 4, "crop snapshot remains bound to decoder PTS");
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
      {1, {1, 1, {}}}, {2, {2, 2, {}}}, {3, {3, 3, {}}}};
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
  std::map<int64_t, PacketTiming> timestamps{{1, {1, 1, {}}}, {2, {2, 2, {}}},
                                              {3, {3, 3, {}}}, {4, {4, 4, {}}}};
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
  {
    // Spatial placement: nudges are in the panel's own frame, inverse nudges
    // restore it, and many rotations stay orthonormal.
    Placement original, p;
    original.matrix = {{{0.8f, 0, 0.6f, -1.2f}, {0, 1, 0, 1.6f}, {-0.6f, 0, 0.8f, -1.6f}}};
    p = original;
    require(movePlacement(p, 0.5f, -0.2f, 0.3f), "move accepted");
    require(std::fabs(p.matrix.m[0][3] - (-1.2f + 0.8f * 0.5f + 0.6f * 0.3f)) < 1e-5 &&
                std::fabs(p.matrix.m[1][3] - 1.4f) < 1e-5 && std::fabs(p.distance - 1.7f) < 1e-5,
            "move follows the panel's right, up and normal");
    movePlacement(p, -0.5f, 0.2f, -0.3f);
    rotatePlacement(p, 30, -15, 10);
    rotatePlacement(p, 0, 0, -10);
    rotatePlacement(p, 0, 15, 0);
    rotatePlacement(p, -30, 0, 0);
    double worst = 0;
    for (int r = 0; r < 3; r++)
      for (int c = 0; c < 4; c++)
        worst = std::max(worst, double(std::fabs(p.matrix.m[r][c] - original.matrix.m[r][c])));
    require(worst < 1e-4, "inverse rotations and moves restore the placement");
    for (int i = 0; i < 1000; i++)
      rotatePlacement(p, 7, 3, -2);
    for (int a = 0; a < 3; a++)
      for (int b = 0; b < 3; b++) {
        double dot = 0;
        for (int r = 0; r < 3; r++)
          dot += p.matrix.m[r][a] * p.matrix.m[r][b];
        require(std::fabs(dot - (a == b)) < 1e-4, "rotation stays orthonormal");
      }
    Placement far = original;
    far.matrix.m[0][3] = 9.5f;
    require(!movePlacement(far, 0.8f, 0, 0), "moves beyond 10 m are refused");
  }
  {
    // VR handles: zones around a 2.4 x 2.13 m window, and the drag maths.
    HandleLayout l;
    const float hx = l.width / 2, hy = l.height / 2, m = l.margin();
    require(l.zone(0, 0) == Handle::None && l.zone(hx - 0.01f, hy - 0.01f) == Handle::None, "inside: no handle");
    require(l.zone(0, -hy - 0.5f * m) == Handle::Pill, "below the centre: pill");
    require(l.zone(l.pillWidth() + 0.05f, -hy - 0.5f * m) == Handle::None, "below but off the pill");
    require(l.zone(hx + 0.5f * m, 0) == Handle::BarRight && l.zone(-hx - 0.5f * m, 0.3f) == Handle::BarLeft, "sides: curve bars");
    require(l.zone(hx + 0.5f * m, -hy - 0.5f * m) == Handle::CornerBR && l.zone(-hx - 0.3f * m, hy + 0.3f * m) == Handle::CornerTL,
            "corners");
    require(l.zone(hx + 2 * m, 0) == Handle::None, "far outside: nothing");
    Placement start;  // 2 m ahead of the origin, facing it
    start.matrix = {{{1, 0, 0, 0}, {0, 1, 0, 1.5f}, {0, 0, 1, -2}}};
    // Controller at the origin pointing ahead (-z) at the pill.
    HmdMatrix34_t c0 = {{{1, 0, 0, 0.1f}, {0, 1, 0, 1.0f}, {0, 0, 1, 0}}};
    HandleDrag drag;
    drag.start(Handle::Pill, c0, start);
    const double a = 30 * M_PI / 180;
    HmdMatrix34_t c1 = {{{float(std::cos(a)), 0, float(std::sin(a)), 0.6f}, {0, 1, 0, 1.2f},
                         {float(-std::sin(a)), 0, float(std::cos(a)), -0.3f}}};
    Placement moved = drag.update(c1);
    auto before = multiply(inverseRigid(c0), start.matrix), after = multiply(inverseRigid(c1), moved.matrix);
    double worst = 0;
    for (int r = 0; r < 3; r++)
      for (int c = 0; c < 4; c++) worst = std::max(worst, double(std::fabs(before.m[r][c] - after.m[r][c])));
    require(worst < 1e-4, "pill: the window follows the controller rigidly");
    // Corner: the ray hit moves from 1.0 m to 1.5 m from the centre.
    HmdMatrix34_t aimA = {{{1, 0, 0, 0.6f}, {0, 1, 0, 1.5f + 0.8f}, {0, 0, 1, 0}}};
    HmdMatrix34_t aimB = {{{1, 0, 0, 0.9f}, {0, 1, 0, 1.5f + 1.2f}, {0, 0, 1, 0}}};
    drag.start(Handle::CornerTR, aimA, start);
    require(std::fabs(drag.update(aimB).width - start.width * 1.5f) < 1e-3, "corner: resize about the centre");
    // Bar: pulled 0.3 m towards the viewer on a 2.4 m window.
    drag.start(Handle::BarRight, c0, start);
    HmdMatrix34_t pulled = c0;
    pulled.m[2][3] += 0.3f;
    require(std::fabs(drag.update(pulled).curvature - 0.5f) < 1e-3, "bar: pulling it curves the window");
    std::vector<uint8_t> image;
    int high = 0;
    drawHandle(l, Handle::None, false, 256, image, high);
    require(std::all_of(image.begin(), image.end(), [](uint8_t v) { return v == 0; }), "no handle: transparent");
    drawHandle(l, Handle::Pill, true, 256, image, high);
    size_t lit = 0, blue = 0;
    for (size_t i = 0; i < image.size(); i += 4)
      if (image[i + 3] > 128) { ++lit; blue += image[i + 2] > image[i] * 2; }
    require(lit > 20 && blue == lit, "held pill is drawn in Steam blue");
  }
  std::cout
      << "native self-test passed: URL allowlist, wire framing, initial/overflow/software and hardware reconnect/resize recovery policy, bounded reset PTS metadata, strict unknown PTS, newest picture, spatial placement, VR handles\n";
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
  std::string urlFile, controlFile, stateFile, poseFile, dumpPath, overlayName, initialPose, actionManifest,
      key = "com.frame-workspace.native.workspace", decoderMode = "software",
      conversionMode = std::getenv("FRAME_WORKSPACE_CONVERSION")
                          ? std::getenv("FRAME_WORKSPACE_CONVERSION")
                          : "cpu",
      presentMode = std::getenv("FRAME_WORKSPACE_PRESENT")
                        ? std::getenv("FRAME_WORKSPACE_PRESENT")
                        : "paced",
      handoffMode = std::getenv("FRAME_WORKSPACE_HANDOFF")
                        ? std::getenv("FRAME_WORKSPACE_HANDOFF")
                        : "fence",
      testYuvFormat, testYuvRange = "limited", testYuvColorSpace = "bt601",
      testYuvPattern = "sharp",
      exitReason = "running";
  std::vector<std::string> inspectFiles;
  bool test = false, noVR = false, testYuvOracle = false;
  bool foveation = false, gaze = false, diagnosticGaze = false, diagnosticScan = false;
  bool pocRewrite = !std::getenv("FRAME_WORKSPACE_V4L2_POC_REWRITE") ||
                    std::string(std::getenv("FRAME_WORKSPACE_V4L2_POC_REWRITE")) != "off";
  double diagnosticU = 0, diagnosticV = 0;
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
      else if (a == "--overlay-name")
        overlayName = value();
      else if (a == "--initial-pose")
        initialPose = value();
      else if (a == "--action-manifest")
        actionManifest = value();
      else if (a == "--inspect-h264")
        inspectFiles.push_back(value());
      else if (a == "--decoder")
        decoderMode = value();
      else if (a == "--conversion")
        conversionMode = value();
      else if (a == "--handoff")
        handoffMode = value();
      else if (a == "--present")
        presentMode = value();
      else if (a == "--v4l2-poc-rewrite") {
        auto v = value();
        if (v != "on" && v != "off") throw std::runtime_error("--v4l2-poc-rewrite must be on or off");
        pocRewrite = v == "on";
      } else if (a == "--draw-handle") {
        // Diagnostic: --draw-handle pill|corner|bar rest|held OUT.ppm draws one
        // handle on grey around a 2.4 m Workspace outline.
        std::string which = value(), state = value(), outPath = value();
        Handle h = which == "pill" ? Handle::Pill : which == "corner" ? Handle::CornerBR : which == "bar" ? Handle::BarRight : Handle::None;
        HandleLayout l;
        std::vector<uint8_t> image;
        int high = 0;
        const int wide = 900;
        drawHandle(l, h, state == "held", wide, image, high);
        std::ofstream f(outPath, std::ios::binary);
        f << "P6\n" << wide << " " << high << "\n255\n";
        const float mpp = l.chromeWidth() / wide;
        for (int py = 0; py < high; py++)
          for (int px = 0; px < wide; px++) {
            const float x = (px + 0.5f) * mpp - l.chromeWidth() / 2, y = l.chromeHeight() / 2 - (py + 0.5f) * mpp;
            const bool window = std::fabs(x) <= l.width / 2 && std::fabs(y) <= l.height / 2;
            const uint8_t *p = &image[(size_t(py) * wide + px) * 4];
            const float a = p[3] / 255.f, base = window ? 52.f : 18.f;
            for (int k = 0; k < 3; k++) f.put(char(std::lround(base * (1 - a) + p[k] * a)));
          }
        return 0;
      } else if (a == "--rewrite-poc") {
        // Diagnostic: rewrite an Annex B file to POC type 2 (one access unit
        // per call is not required; the rewriter splits on start codes).
        std::string in = value(), outPath = value();
        auto data = readFile(in, 256 * 1024 * 1024);
        std::vector<uint8_t> bytes(data.begin(), data.end()), out;
        H264PocRewriter rewriter;
        if (!rewriter.rewrite(bytes, out)) throw std::runtime_error("POC rewrite unsupported");
        std::ofstream(outPath, std::ios::binary).write(reinterpret_cast<const char *>(out.data()), long(out.size()));
        return 0;
      }
      else if (a == "--foveation")
        foveation = true;
      else if (a == "--gaze")
        gaze = true;
      else if (a == "--diagnostic-gaze") {
        auto uv = value();
        if (uv == "scan") { diagnosticGaze = true; diagnosticScan = true; continue; }
        size_t comma = uv.find(',');
        if (comma == std::string::npos) throw std::runtime_error("gaze requires U,V");
        size_t aUsed = 0, bUsed = 0;
        diagnosticU = std::stod(uv.substr(0, comma), &aUsed);
        diagnosticV = std::stod(uv.substr(comma + 1), &bUsed);
        if (aUsed != comma || bUsed != uv.size() - comma - 1 ||
            !std::isfinite(diagnosticU) || !std::isfinite(diagnosticV) ||
            diagnosticU < 0 || diagnosticU > 1 || diagnosticV < 0 || diagnosticV > 1)
          throw std::runtime_error("invalid diagnostic gaze");
        diagnosticGaze = true;
      }
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
               "[--state-file PATH] [--pose-file PATH] [--overlay-key KEY] [--overlay-name NAME] [--initial-pose JSON] "
               "[--decoder software|v4l2] [--conversion cpu|gpu] [--gaze] [--handoff fence|finish] [--present paced|immediate] [--v4l2-poc-rewrite on|off] [--foveation] "
               "[--seconds N] [--dump PATH.ppm [--dump-seq N] "
               "[--dump-size WIDTHxHEIGHT]] "
               "\nDiagnostics: --diagnostic-gaze U,V|scan (requires --foveation; synthetic, not eye tracking) | --self-test | "
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
    if (diagnosticGaze && !foveation && !gaze) throw std::runtime_error("diagnostic gaze requires --foveation or --gaze");
    if (foveation && gaze) throw std::runtime_error("--gaze and --foveation are alternatives");
    if (diagnosticGaze) fprintf(stderr, "[native] SYNTHETIC diagnostic gaze enabled; not eye tracking\n");
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
    if (handoffMode != "fence" && handoffMode != "finish")
      throw std::runtime_error("handoff must be fence or finish");
    if (presentMode != "paced" && presentMode != "immediate")
      throw std::runtime_error("present must be paced or immediate");
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
    endpoint.foveation = foveation;
    endpoint.gaze = gaze;
    if (overlayName.size() > 64 || overlayName.find_first_of("\r\n\t") != std::string::npos)
      throw std::runtime_error("invalid overlay name");
    // VR placement input: the action manifest installed beside the binary
    // (input/actions.json), unless --action-manifest names another or "none".
    if (actionManifest.empty()) {
      char exe[4096];
      ssize_t n = readlink("/proc/self/exe", exe, sizeof exe - 1);
      if (n > 0) {
        exe[n] = 0;
        std::string dir(exe);
        dir = dir.substr(0, dir.find_last_of('/'));
        std::ifstream probe(dir + "/input/actions.json");
        if (probe) actionManifest = dir + "/input/actions.json";
      }
    } else if (actionManifest == "none")
      actionManifest.clear();
    Overlay overlay(noVR, key, (foveation || gaze) && !diagnosticGaze, overlayName, actionManifest);
    overlay.handoff = handoffMode;
    overlay.present = presentMode;
    bool persisted = loadPlacement(poseFile, overlay.placement);
    // A new panel's first position (for example beside the Workspace) when
    // it has no saved pose of its own.
    if (!persisted && !initialPose.empty()) {
      if (!parsePlacement(initialPose, overlay.placement))
        throw std::runtime_error("invalid initial pose");
      persisted = true;
    }
    bool pendingRecenter = !persisted;
    if (pendingRecenter)
      pendingRecenter = !overlay.recenter();
    overlay.apply();
    // Curvature is part of the saved placement; re-apply it (OpenVR readback
    // confirms it, and an unsupported runtime just stays flat).
    if (overlay.placement.curvature > 0) {
      const float saved = overlay.placement.curvature;
      if (!overlay.setCurvature(saved).applied)
        overlay.placement.curvature = saved; // keep the user's choice for a later runtime
    }
    if (!pendingRecenter)
      atomicWrite(poseFile, placementJSON(overlay.placement));
    Pipeline pipe(decoderMode, conversionMode);
    pipe.pocRewrite = pocRewrite;
    if (!test)
      pipe.start(endpoint);
    double start = nowMs(), lastState = start, lastLog = start, lastControl = 0,
           lastPattern = 0, previousState = start;
    // Destruction order is producer, pipeline, overlay, including exceptions.
    // No gaze acquisition or delivery depends on the render loop making progress.
    GazeProducer gazeProducer;
    if (foveation || gaze) {
      gazeProducer.start([&](double sampled) {
        if (diagnosticGaze)
          return diagnosticGazeReading(sampled, start, diagnosticScan, diagnosticU, diagnosticV);
        GazeReading reading;
        reading.status = overlay.gaze(reading.u, reading.v);
        reading.valid = std::strcmp(reading.status, "valid") == 0;
        return reading;
      }, [&](const GazeReading &reading, double sampled) {
        pipe.gaze(reading.u, reading.v, reading.valid, sampled, reading.diagnostic);
      });
    }
    uint64_t uploads = 0, lastUploads = 0, lastDecoded = 0, submitErrors = 0;
    int w = 0, h = 0, encodedW = 0, encodedH = 0;
    bool foveationActive = false;
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
             ",\"encodedWidth\":" + std::to_string(encodedW) +
             ",\"encodedHeight\":" + std::to_string(encodedH) +
             ",\"foveationActive\":" + (foveationActive ? "true" : "false") +
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
             ",\"decodeStepMaxMs\":" + std::to_string(pipe.decodeStepMaxMs.exchange(0)) +
             ",\"decoderHoldMs\":" + std::to_string(pipe.decoderHoldMs) +
             ",\"decoderHoldMaxMs\":" + std::to_string(pipe.decoderHoldMaxMs.exchange(0)) +
             ",\"decoderOutputLag\":" + std::to_string(pipe.decoderOutputLag) +
             ",\"decoderOutputLagMax\":" + std::to_string(pipe.decoderOutputLagMax.exchange(0)) +
             ",\"arrivalGapMaxMs\":" + std::to_string(pipe.arrivalGapMaxMs.exchange(0)) +
             ",\"handoff\":\"" + overlay.handoff +
             "\",\"gpuLatencyMs\":" + std::to_string(overlay.gpuLatencyMs) +
             ",\"handoffBlockingWaits\":" + std::to_string(overlay.blockingWaits) +
             ",\"present\":\"" + (overlay.paced() ? std::string("paced") : std::string("immediate")) +
             "\",\"pacedDrops\":" + std::to_string(overlay.pacedDrops) +
             ",\"pacedFallbacks\":" + std::to_string(overlay.pacedFallbacks) +
             ",\"presentGaps\":[" + std::to_string(overlay.presentGaps[1]) + "," +
                 std::to_string(overlay.presentGaps[2]) + "," + std::to_string(overlay.presentGaps[3]) + "," +
                 std::to_string(overlay.presentGaps[4]) + "]" +
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
             ",\"gazeState\":\"" + gazeProducer.status() + "\"" +
             ",\"foveationRequested\":" + (foveation ? "true" : "false") +
             ",\"foveationNegotiated\":" + (pipe.foveationNegotiated ? "true" : "false") +
             ",\"gazeNegotiated\":" + (pipe.gazeNegotiated ? "true" : "false") +
             ",\"diagnosticGaze\":" + (diagnosticGaze ? "true" : "false") +
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
      if (overlay.updatePlacementInput())
        atomicWrite(poseFile, placementJSON(overlay.placement));
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
                // Spatial nudges in the panel's own frame: {"move":[x,y,z]}
                // meters (right, up, toward the viewer) and {"rotate":[yaw,
                // pitch, roll]} degrees. Each is bounded per command.
                auto triple = [&](const char *name, double limit, double out[3]) {
                  auto a = field(j, name);
                  if (!a || !json_object_is_type(a, json_type_array) || json_object_array_length(a) != 3)
                    return false;
                  for (int i = 0; i < 3; ++i) {
                    auto v = json_object_array_get_idx(a, i);
                    if (!json_object_is_type(v, json_type_int) && !json_object_is_type(v, json_type_double))
                      return false;
                    out[i] = json_object_get_double(v);
                    if (!std::isfinite(out[i]) || std::fabs(out[i]) > limit)
                      return false;
                  }
                  return true;
                };
                double delta[3];
                bool spatial = false;
                if (triple("rotate", 90, delta)) {
                  overlay.rotate(float(delta[0]), float(delta[1]), float(delta[2]));
                  spatial = true;
                }
                if (triple("move", 2, delta))
                  spatial = overlay.move(float(delta[0]), float(delta[1]), float(delta[2])) || spatial;
                if (spatial || (requestedCurvature && curvatureResult.applied))
                  atomicWrite(poseFile, placementJSON(overlay.placement));
                if (flag(j, "recenter"))
                  pendingRecenter = true;
                if (auto requested = field(j, "present"))
                  if (json_object_is_type(requested, json_type_string)) {
                    std::string mode = json_object_get_string(requested);
                    if (mode == "paced" || mode == "immediate")
                      overlay.present = mode;
                  }
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
      std::vector<Overlay::Completion> done;
      auto finished = [&] {
        for (auto &c : done) {
          const auto &picture = c.picture;
          if (c.dropped)
            continue;
          if (c.submitted) {
            ready = true;
            ++uploads;
            w = picture->outputWidth();
            h = picture->outputHeight();
            encodedW=picture->width;encodedH=picture->height;foveationActive=picture->layout.direct || picture->layout.warped;
            ageMs = nowMs() - picture->received;
            pipe.report(*picture, nowMs());
            retryPicture.reset();
            if (!dumpPath.empty() && !dumped &&
                (!dumpSequence || picture->seq == dumpSequence) &&
                (!dumpWidth ||
                 (picture->outputWidth() == dumpWidth && picture->outputHeight() == dumpHeight))) {
              overlay.dump(dumpPath, c);
              dumped = true;
            }
          } else {
            ++submitErrors;
            retryPicture = picture;
          }
        }
        done.clear();
      };
      if (picture) {
        double before = nowMs();
        overlay.stage(std::move(picture), done);
        uploadMs = nowMs() - before;
      }
      overlay.complete(0, done);
      overlay.presentReady(done);
      finished();
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
      // Sleep until the GPU finishes in-flight work or a new picture arrives,
      // rather than a fixed interval after every upload.
      if (overlay.waiting()) {
        // A finished picture waits for its paced refresh; a new arrival
        // still wakes the loop.
        pipe.waitForPicture(overlay.untilNextSlotMs(overlay.busy() ? 2 : 8));
        overlay.complete(0, done);
        overlay.presentReady(done);
        finished();
      } else if (overlay.busy()) {
        overlay.complete(2, done);
        finished();
      } else
        pipe.waitForPicture(test ? 4 : 8);
    }
    if (exitReason == "running") {
      if (interrupted)
        exitReason = "signal-" + std::to_string(interrupted);
      else if (pipe.stop)
        exitReason = pipe.stopReason.load();
      else
        exitReason = "timer";
    }
    gazeProducer.finish();
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
