#include "overlay.hpp"
#include "gaze_producer.hpp"
#include "host.hpp"
#include "tests_host.hpp"
#include "chrome_preview.hpp"
#include "spatial_render.hpp"
#include "scene_render.hpp"
#include "tests_spatial.hpp"
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
    const float hx = l.width / 2, hy = l.height / 2, o = l.handleOffset();
    require(l.zone(0, 0) == Handle::None && l.zone(hx - 0.01f, hy - 0.01f) == Handle::None, "inside: no handle");
    require(l.zone(l.pillX(), l.rowY()) == Handle::Pill, "under the window: the move pill");
    require(l.zone(l.closeX(), l.rowY()) == Handle::Close, "left of the pill: the close disc");
    require(l.closeX() + l.discDiameter() / 2 < l.pillX() - l.pillWidth() / 2, "disc and pill are apart");
    require(l.zone(hx + o, 0) == Handle::BarRight && l.zone(-hx - o, 0.02f) == Handle::BarLeft, "sides: curve grips");
    require(l.zone(hx + o - 0.6f * l.arm(), hy + o) == Handle::CornerTR, "along a corner's top arm");
    require(l.zone(-hx - o, -hy - o + 0.6f * l.arm()) == Handle::CornerBL, "along a corner's side arm");
    require(l.zone(hx + o - 0.29f * l.bend(), hy + o - 0.29f * l.bend()) == Handle::CornerTR, "on a corner's bend");
    require(l.zone(hx + l.reach() + 0.05f, 0) == Handle::None, "far outside: nothing");
    const unsigned right = l.revealed(hx + 0.05f, 0);
    require(right == ((1u << handleIndex(Handle::CornerTR)) | (1u << handleIndex(Handle::CornerBR)) |
                      (1u << handleIndex(Handle::BarRight))), "past the right edge: its corners and grip slide out");
    require(l.revealed(0, 0) == 0 && l.revealed(hx + l.reach() + 0.05f, 0) == 0, "inside or far away: none");
    require(l.chromeWidth() > l.frameWidth() && l.chromeHeight() / 2 > -l.rowY(), "the controls layer covers them all");
    require(l.zone(0, hy + o) == Handle::BarTop && l.zone(0.03f, -hy - o) == Handle::BarBottom, "top and bottom grips");
    require(l.zone(l.pillX(), l.rowY()) == Handle::Pill, "the pill still wins below the bottom grip");
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
    {
      const Placement one = drag.update(pulled);
      require(std::fabs(one.bendB - 0.5f) < 1e-3 && one.bendA == 0 && one.curvature == 0 && !one.bendVertical,
              "side grip with the trigger: only its own half bends");
    }
    drag.start(Handle::BarRight, c0, start, true);
    require(std::fabs(drag.update(pulled).curvature - 0.5f) < 1e-3, "side grip with the grip button: both halves");
    drag.start(Handle::BarTop, c0, start);
    {
      const Placement top = drag.update(pulled);
      require(top.bendVertical && top.bendB > 0.4f && top.bendA == 0, "top grip: the top half bends, top to bottom");
      drag.start(Handle::BarLeft, c0, top);
      const Placement back = drag.update(c0);
      require(!back.bendVertical && back.bendA == 0 && back.bendB == 0, "changing direction starts flat");
    }
    {
      // Bends: a point on the surface, hit back along its normal, is the same
      // window position, for every shape and both layers in front.
      auto check = [&](BendShape b, const char *what) {
        for (float z : {0.f, 0.015f, 0.03f})
          for (float fx : {-0.55f, -0.3f, -0.01f, 0.01f, 0.2f, 0.52f})
            for (float fy : {-0.45f, 0.f, 0.3f}) {
              const float x = fx * (b.vertical ? 2.4f : b.span), y = fy * (b.vertical ? b.span : 2.13f);
              const HmdMatrix34_t m = bendPointXY(b, x, y, z);
              const float o[3] = {m.m[0][3] + m.m[0][2] * 2, m.m[1][3] + m.m[1][2] * 2, m.m[2][3] + m.m[2][2] * 2};
              const float d[3] = {-m.m[0][2], -m.m[1][2], -m.m[2][2]};
              float hx2 = 0, hy2 = 0;
              require(bendHit(b, o, d, z, hx2, hy2) && std::fabs(hx2 - x) < 2e-3f && std::fabs(hy2 - y) < 2e-3f, what);
            }
        // the halves meet smoothly at the centre line
        const HmdMatrix34_t l0 = bendPoint(b, -1e-4f, 0, 0), r0 = bendPoint(b, 1e-4f, 0, 0);
        double gap = 0;
        for (int r = 0; r < 3; r++) gap += std::fabs(l0.m[r][3] - r0.m[r][3]) + std::fabs(l0.m[r][2] - r0.m[r][2]);
        require(gap < 1e-3, "halves meet at the centre line, tangent");
        // each overlay sits on the surface at its middle
        for (int i = 0; i < b.segments(); i++) {
          const BendSegment seg = bendSegment(b, i, b.span, 0.015f);
          const float mid = b.segments() == 2 ? (i == 0 ? -b.span / 4 + 0.001f : b.span / 4 - 0.001f) : 0;
          const HmdMatrix34_t m = bendPoint(b, mid, 0, 0.015f);
          double off = 0;
          for (int r = 0; r < 3; r++) off += std::fabs(seg.local.m[r][3] - m.m[r][3]);
          require(off < 1e-4 && seg.width > 0 && seg.curvature >= 0 && seg.curvature <= 1, "overlays on the surface");
        }
      };
      check(BendShape{false, 2.4f, {0.2f, 0.2f}}, "even side-to-side bend: hits round-trip");
      check(BendShape{false, 2.4f, {0.f, 0.35f}}, "right half only: hits round-trip");
      check(BendShape{false, 2.4f, {0.5f, 0.1f}}, "uneven halves: hits round-trip");
      check(BendShape{true, 2.13f, {0.3f, 0.3f}}, "even top-to-bottom bend: hits round-trip");
      check(BendShape{true, 2.13f, {0.f, 0.4f}}, "top half only: hits round-trip");
      check(BendShape{false, 2.4f, {0.f, 0.f}}, "flat: hits round-trip");
      require(bendSegment(BendShape{false, 2.4f, {0.f, 0.3f}}, 0, 2.4f, 0).bounds.uMax > 0.5f &&
              bendSegment(BendShape{false, 2.4f, {0.f, 0.3f}}, 1, 2.4f, 0).bounds.uMin < 0.5f &&
              bendSegment(BendShape{false, 2.4f, {0.f, 0.3f}}, 0, 2.4f, 0).bounds.uMax < 0.501f,
              "halves split the texture at its middle, overlapping slightly");
      Placement saved = start;
      saved.bendVertical = true;
      saved.bendA = 0.1f;
      saved.bendB = 0.4f;
      Placement loaded;
      require(parsePlacement(placementJSON(saved), loaded) && loaded.bendVertical &&
              std::fabs(loaded.bendA - 0.1f) < 1e-4f && std::fabs(loaded.bendB - 0.4f) < 1e-4f,
              "pose files keep the bend");
    }
  }
  selfTestHost();
  selfTestSpatial();
  std::cout
      << "native self-test passed: URL allowlist, wire framing, initial/overflow/software and hardware reconnect/resize recovery policy, bounded reset PTS metadata, strict unknown PTS, newest picture, spatial placement, VR handles, bends, host bookkeeping, gaze picking, focus feedback, spatial layout and trigger gestures\n";
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
struct PanelOptions {
  std::string urlFile, controlFile, stateFile, poseFile, dumpPath, overlayName, initialPose, actionManifest,
      requestDir, spatialStateFile, hostGeneration,
      key = "com.frame-workspace.native.workspace", decoderMode = "software", conversionMode = "cpu",
      presentMode = "paced", handoffMode = "fence", testYuvFormat, testYuvRange = "limited",
      testYuvColorSpace = "bt601", testYuvPattern = "sharp", exitReason = "running";
  PanelRole role = PanelRole::Window;
  bool test = false, noVR = false, testYuvOracle = false, foveation = false, gaze = false,
      diagnosticGaze = false, diagnosticScan = false, pocRewrite = true;
  double diagnosticU = 0, diagnosticV = 0, seconds = 0;
  uint32_t dumpSequence = 0;
  int dumpWidth = 0, dumpHeight = 0;
};
class PanelRunner : public PanelOptions {
public:
  Endpoint endpoint;
  Overlay overlay;
  Pipeline pipe;
  GazeProducer gazeProducer;
  bool pendingRecenter = false, stopped = false;
  int result = 1;
  double start = 0, lastState = 0, lastLog = 0, lastControl = 0, lastPattern = 0, previousState = 0;
  uint64_t uploads = 0, lastUploads = 0, lastDecoded = 0, submitErrors = 0;
  int w = 0, h = 0, encodedW = 0, encodedH = 0;
  bool foveationActive = false;
  double uploadMs = 0, ageMs = 0, decodeFps = 0, uploadFps = 0;
  bool ready = false, dumped = false, surfaceConfigured = false;
  SurfaceMessage surface;
  uint64_t surfaceRevision = 0;
  uint32_t uploadedSeq = 0;
  double uploadedReceived = 0;
  bool auxiliary() const { return role != PanelRole::Window; }
  void refreshSurface() {
    if (!auxiliary()) return;
    auto next = pipe.surface();
    surface = next.first; surfaceRevision = next.second;
    overlay.showSurface(surfaceConfigured && ready &&
                        surfacePictureReady(surface, uploadedSeq, uploadedReceived));
  }
  double revision = -1, controlResultRevision = -1;
  Overlay::CurvatureResult curvatureResult;
  std::shared_ptr<Picture> retryPicture;
  explicit PanelRunner(const PanelOptions &options, std::shared_ptr<OverlaySession> session = {})
      : PanelOptions(options), endpoint(test ? Endpoint{} : parseURL(readFile(urlFile))),
        overlay(noVR, key, (foveation || gaze) && !diagnosticGaze, overlayName, actionManifest, std::move(session), role),
        pipe(decoderMode, conversionMode) {
    overlay.handoff = handoffMode;
    overlay.present = presentMode;
    endpoint.foveation = foveation; endpoint.gaze = gaze;
    pipe.acceptsSurfaces = auxiliary();
    if (!auxiliary()) {
      bool persisted = loadPlacement(poseFile, overlay.placement);
      // A new panel's first position (for example beside the Workspace) when
      // it has no saved pose of its own.
      if (!persisted && !initialPose.empty()) {
        if (!parsePlacement(initialPose, overlay.placement))
          throw std::runtime_error("invalid initial pose");
        persisted = true;
      }
      pendingRecenter = !persisted;
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
    }
    pipe.pocRewrite = pocRewrite;
    if (!test)
      pipe.start(endpoint);
    start = nowMs(); lastState = lastLog = previousState = start;
    // Destruction order is producer, pipeline, overlay, including exceptions.
    // No gaze acquisition or delivery depends on the render loop making progress.
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
    atomicWrite(stateFile, state("connecting"));
  }
  std::string state(const char *status) {
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
           "\",\"hostGeneration\":" + jsonString(hostGeneration) +
           ",\"hostPid\":" + std::to_string(hostGeneration.empty() ? 0 : getpid()) +
           ",\"role\":" + jsonString(roleName(role)) +
           ",\"surfaceVisible\":" + (overlay.surfaceVisible ? "true" : "false") +
           ",\"surfaceSeq\":" + std::to_string(surface.seq) +
           ",\"surfaceRevision\":" + std::to_string(surfaceRevision) +
           ",\"uploadedSeq\":" + std::to_string(uploadedSeq) +
           ",\"surfaceHeight\":" + std::to_string(overlay.panelHeight()) +
           ",\"textureWidth\":" + std::to_string(w) +
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
           ",\"streamStill\":" + (pipe.streamStill ? "true" : "false") +
           ",\"decoderSessionOpen\":" + (pipe.decoderSessionOpen ? "true" : "false") +
           ",\"decoderStillReleases\":" + std::to_string(pipe.decoderStillReleases) +
           ",\"decoderLiveOpens\":" + std::to_string(pipe.decoderLiveOpens) +
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
  }
  bool tick(bool sharedEvents = true, bool wait = true, unsigned allowedHands = 3, bool consumeSqueeze = false) {
    if (interrupted || pipe.stop || (seconds > 0 && nowMs() - start >= seconds * 1000)) return false;
    double now = nowMs();
    refreshSurface();
    if (!overlay.events(sharedEvents)) {
      exitReason = overlay.exitReason;
      return false;
    }
    if (overlay.updatePlacementInput(allowedHands, consumeSqueeze))
      atomicWrite(poseFile, placementJSON(overlay.placement));
    if (overlay.closeRequested) {
      overlay.closeRequested = false;
      // A fixed, named request for the companion, which polls this folder;
      // it closes this panel's stream (or stops the Workspace).
      if (!requestDir.empty()) {
        static int sequence = 0;
        const std::string file = requestDir + "/close-" + std::to_string(getpid()) + "-" +
                                 std::to_string(++sequence) + ".json";
        try {
          atomicWrite(file, "{\"action\":\"close\",\"key\":\"" + key + "\"}");
        } catch (const std::exception &) {
          fprintf(stderr, "[native] could not write the close request\n");
        }
      }
    }
    if (now - lastControl > 100) {
      lastControl = now;
      if (!controlFile.empty()) {
        try {
          auto j = json(readFile(controlFile));
          double r = num(j, "revision", -1);
          if (r >= 0 && r != revision && auxiliary()) {
            revision = controlResultRevision = r;
            if (flag(j, "stop")) { exitReason = "control-stop"; return false; }
          } else if (r >= 0 && r != revision) {
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
              // {"bend":{"vertical":bool,"a":0-1,"b":0-1}}: each half on its own
              // (a: left or bottom, b: right or top), as the grips do.
              bool bent = false;
              if (auto bend = field(j, "bend"))
                if (json_object_is_type(bend, json_type_object)) {
                  auto part = [&](const char *name, double &out) {
                    json_object *v = nullptr;
                    if (!json_object_object_get_ex(bend, name, &v) ||
                        !(json_object_is_type(v, json_type_double) || json_object_is_type(v, json_type_int)))
                      return false;
                    out = json_object_get_double(v);
                    return std::isfinite(out) && out >= 0 && out <= 1;
                  };
                  json_object *vertical = nullptr;
                  double a = 0, b = 0;
                  if (part("a", a) && part("b", b)) {
                    json_object_object_get_ex(bend, "vertical", &vertical);
                    overlay.setBend(vertical && json_object_get_boolean(vertical), float(a), float(b));
                    bent = true;
                  }
                }
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
              if (spatial || bent || (requestedCurvature && curvatureResult.applied))
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
                return false;
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
    // The bend turned to (or from) top-to-bottom: redraw the still picture
    // turned to match, rather than waiting for the desktop to change.
    if (!picture)
      picture = overlay.restagePicture();
    std::vector<Overlay::Completion> done;
    auto finished = [&] {
      for (auto &c : done) {
        const auto &picture = c.picture;
        if (c.dropped)
          continue;
        if (c.submitted) {
          ready = true;
          uploadedSeq = picture->seq; uploadedReceived = picture->received;
          refreshSurface();
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
    if (wait) {
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
    return true;
  }
  int finish(const std::string &reason = "") {
    if (stopped) return result;
    stopped = true;
    if (!reason.empty()) exitReason = reason;
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
    result = uploads && !pipe.decoderError ? 0 : 1;
    return result;
  }
  ~PanelRunner() {
    try { finish(); } catch (...) { gazeProducer.finish(); pipe.finish(); }
  }
};
static int runHost(const std::string &folder, const std::string &stateFile, const PanelOptions &defaults) {
  auto session = std::make_shared<OverlaySession>(defaults.noVR);
  if (!defaults.actionManifest.empty()) session->initInput(defaults.actionManifest);
  struct Entry { PanelSpec spec; std::unique_ptr<PanelRunner> panel; };
  std::map<std::string, Entry> entries;
  PanelBook book;
  HostFocus focus;
  MenuStripReveal menuReveal;
  SpatialState spatial;
  SpatialRenderer spatialRenderer(session);
  std::string spatialPath = defaults.spatialStateFile;
  if (spatialPath.empty()) {
    auto runtime = std::getenv("XDG_RUNTIME_DIR");
    if (runtime)
      spatialPath = std::string(runtime) + "/frame-workspace/spatial-ui.json";
  }
  std::string spatialRequests =
      defaults.requestDir.empty()
          ? std::filesystem::path(spatialPath.empty() ? folder : spatialPath)
                    .parent_path()
                    .string() +
                "/requests"
          : defaults.requestDir;
  HmdMatrix34_t anchor = spatialAnchor(Placement{}.matrix, 1.3f, 0);
  bool anchored = false, buttonWas = false;
  SpatialPress press;
  std::string menuID;
  SpatialNavigation navigation;
  double lastSpatial = -1000, lastFrame = nowMs();
  std::set<int64_t> chosenSequences;
  SpatialVisibility visibility;
  std::string lastSpatialRaw, gazeKey;
  double gazeSince = 0;
  double lastSpatialDraw = -1000;
  std::vector<SpatialTile> tiles;
  auto emit = [&](const std::string &body) {
    static uint64_t serial = 0;
    atomicWrite(spatialRequests + "/spatial-" + std::to_string(getpid()) + "-" +
                    std::to_string(++serial) + ".json",
                body);
  };
  auto select = [&](const std::string &key) {
    for (auto &[path, e] : entries)
      if (e.panel && e.spec.role == PanelRole::Window && e.spec.key == key && e.panel->overlay.interactive()) {
        auto &o = e.panel->overlay;
        if (!o.recenter())
          return false;
        o.apply();
        atomicWrite(e.spec.poseFile, placementJSON(o.placement));
        emit("{\"action\":\"switch\",\"key\":" + jsonString(key) + "}");
        e.panel->pipe.sendFocus(.5f, .5f);
        focus.activate(key);
        for (auto &[otherPath, other] : entries) if (other.panel && other.spec.role == PanelRole::Window)
          other.panel->overlay.setSpatialForeground(other.spec.key == key);
        spatial.open = false;
        visibility.request(false, spatial.seq);
        menuID.clear();
        return true;
      }
    return false;
  };
  std::set<std::string> invalid;
  double lastScan = -1000, lastState = -1000, lastFocusAttempt = -1000, start = nowMs();
  uint64_t requests = 0;
  std::string reason = "timer", gazeStatus = "unavailable";
  auto writeHostState = [&](const char *status) {
    size_t running = 0;
    for (const auto &[path, entry] : entries) if (entry.panel) ++running;
    atomicWrite(stateFile, "{\"version\":1,\"state\":" + jsonString(status) +
        ",\"ready\":" + (std::string(status) == "hosting" ? "true" : "false") +
        ",\"hostPid\":" + std::to_string(getpid()) + ",\"panels\":" + std::to_string(entries.size()) +
        ",\"runningPanels\":" + std::to_string(running) + ",\"activeKey\":" + jsonString(focus.active) +
        ",\"spatialMode\":" + (spatial.spatial ? std::string("true") : "false") +
        ",\"switcherOpen\":" + (spatial.open ? std::string("true") : "false") +
        ",\"spatialTiles\":" + std::to_string(tiles.size()) +
        ",\"gazeState\":" + jsonString(gazeStatus) + ",\"exitReason\":" + jsonString(reason) + "}");
  };
  auto remove = [&](const std::string &path) {
    auto it = entries.find(path);
    if (it != entries.end()) {
      if (it->second.panel) {
        focus.remove(it->second.spec.key);
        it->second.panel->finish("host-remove");
      }
      // Release only this panel's GL/VR resources, keeping the session alive.
      entries.erase(it);
    }
    book.removed(path);
  };
  auto panelError = [&](const PanelSpec &s, const char *error) {
    atomicWrite(s.stateFile, "{\"version\":1,\"state\":\"error\",\"ready\":false,\"overlayKey\":" + jsonString(s.key) +
        ",\"hostGeneration\":" + jsonString(s.generation) + ",\"hostPid\":" + std::to_string(getpid()) +
        ",\"exitReason\":" + jsonString(error) + "}");
  };
  writeHostState("hosting");
  while (!interrupted && (defaults.seconds == 0 || nowMs() - start < defaults.seconds * 1000)) {
    const double now = nowMs();
    if (!session->events()) { reason = session->exitReason; break; }
    if (now - lastScan >= 100) {
      lastScan = now;
      std::map<std::string, PanelSpec> desired;
      std::error_code error;
      std::filesystem::directory_iterator dir(folder, error), end;
      // An unavailable directory is not a command to remove every panel.
      bool scanned = !error;
      for (; scanned && dir != end; dir.increment(error)) {
        if (error) { scanned = false; break; }
        const auto &file = *dir;
        if (file.path().extension() != ".json") continue;
        const auto path = file.path().string();
        try {
          if (!file.is_regular_file() || file.is_symlink()) throw std::runtime_error("not a spec file");
          desired[path] = PanelSpec::parse(readFile(path));
          invalid.erase(path);
        } catch (...) {
          if (invalid.insert(path).second) fprintf(stderr, "[native-host] invalid panel spec (contents redacted)\n");
          auto old = entries.find(path);
          if (old != entries.end()) desired[path] = old->second.spec;
        }
      }
      if (error) scanned = false;
      if (scanned) {
        for (auto it = invalid.begin(); it != invalid.end();) {
          if (!std::filesystem::exists(*it, error)) it = invalid.erase(it); else ++it;
        }
        auto changes = book.plan(desired);
        for (const auto &path : changes.remove) remove(path);
        for (const auto &path : changes.add) {
          const auto &spec = desired.at(path);
          bool duplicate = false, duplicateState = false;
          for (const auto &[other, entry] : entries) {
            const auto &s = entry.spec;
            duplicateState |= spec.stateFile == s.stateFile;
            duplicate |= spec.key == s.key || spec.stateFile == s.stateFile || spec.controlFile == s.controlFile ||
                         (!spec.poseFile.empty() && spec.poseFile == s.poseFile) || spec.urlFile == s.urlFile;
          }
          Entry entry{spec, {}};
          try {
            if (duplicate) throw std::runtime_error("duplicate panel resources");
            auto options = defaults;
            options.urlFile = spec.urlFile; options.key = spec.key; options.overlayName = spec.name;
            options.controlFile = spec.controlFile; options.stateFile = spec.stateFile; options.poseFile = spec.poseFile;
            options.initialPose = spec.initialPose; options.requestDir = spec.requestDir; options.hostGeneration = spec.generation;
            options.decoderMode = spec.decoder; options.conversionMode = spec.conversion;
            options.foveation = spec.foveation; options.gaze = spec.gaze; options.role = spec.role;
            // The host duration limits the process, rather than each later panel.
            options.seconds = 0;
            entry.panel = std::make_unique<PanelRunner>(options, session);
          } catch (...) {
            // A malformed stream or exhausted decoder must leave other panels alive.
            // Never copy exception text here: parser errors may contain credentials.
            fprintf(stderr, "[native-host] panel startup failed (endpoint redacted)\n");
            // A duplicate state path belongs to the existing owner.
            if (!duplicateState) panelError(spec, duplicate ? "host-panel-conflict" : "host-panel-start-failed");
          }
          entries.emplace(path, std::move(entry));
          book.added(path, spec);
        }
      }
    }
    if (now - lastSpatial >= 50) {
      lastSpatial = now;
      try {
        auto raw = readFile(spatialPath, 1024 * 1024);
        if (raw != lastSpatialRaw) {
          auto next = SpatialState::parse(raw);
          lastSpatialRaw = raw;
          visibility.reconcile(next);
          navigation.reconcile(next);
          spatial = std::move(next);
          if (!spatial.spatial)
            menuID.clear();
          if (spatial.spatial && !spatial.chosen.empty() &&
              !chosenSequences.count(spatial.seq)) {
            chosenSequences.insert(spatial.seq);
            select(spatial.chosen);
          }
        }
      } catch (...) {
      } // Atomic replacement may be temporarily unavailable.
    }
    if (spatial.spatial) {
      TrackedDevicePose_t head{};
      if (!session->noVR)
        session->system->GetDeviceToAbsoluteTrackingPose(
            ETrackingUniverseOrigin_TrackingUniverseStanding, 0, &head, 1);
      if (session->noVR) {
        head.bPoseIsValid = true;
        head.mDeviceToAbsoluteTracking = spatialIdentity();
      }
      if (head.bPoseIsValid) {
        auto next = spatialAnchor(head.mDeviceToAbsoluteTracking, 1.3f, 0);
        float dot =
            next.m[0][2] * anchor.m[0][2] + next.m[2][2] * anchor.m[2][2];
        if (!anchored || dot < std::cos(.8f)) {
          anchor = next;
          anchored = true;
        }
      }
    } else
      anchored = false;
    bool button = session->hand[0].switcher || session->hand[1].switcher;
    if (button && !buttonWas && spatial.spatial) {
      spatial.open = !spatial.open;
      visibility.request(spatial.open, spatial.seq);
      try {
        emit("{\"action\":\"switcher\",\"verb\":" +
             jsonString(spatial.open ? "open" : "close") + "}");
      } catch (...) {
      }
    }
    buttonWas = button;
    bool grip[2]{session->hand[0].valid&&session->hand[0].squeeze,session->hand[1].valid&&session->hand[1].squeeze};
    navigation.update(spatial,grip);
    if(spatialAppIndex(spatial,menuID)<0)menuID.clear();
    tiles = spatialLayout(spatial, anchor, menuID);
    // Host-owned auxiliary poses follow the focused bezel and the actual D1 shelf.
    PanelRunner *focused = nullptr, *menubar = nullptr, *status = nullptr;
    for (auto &[path, e] : entries) if (e.panel) {
      e.panel->refreshSurface();
      if (e.spec.role == PanelRole::Window && e.spec.key == focus.active) focused = e.panel.get();
      if (e.spec.role == PanelRole::Menubar) menubar = e.panel.get();
      if (e.spec.role == PanelRole::Status) status = e.panel.get();
    }
    const float mpp = focused && focused->w > 0 ? focused->overlay.placement.width * 2 / focused->w : .001f;
    bool openMenu = false;
    for (auto &[path, e] : entries)
      if (e.panel && e.spec.role == PanelRole::Menu && e.panel->surfaceConfigured &&
          surfacePictureReady(e.panel->surface, e.panel->uploadedSeq, e.panel->uploadedReceived) &&
          e.panel->surface.anchor == "menubar") openMenu = true;
    menuReveal.update(menubar && menubar->surface.visible && menubar->surface.hot, openMenu, now, float((now-lastFrame)/1000));
    auto place = [&](PanelRunner *panel, const PanelSurface &surface, bool valid) {
      if (!panel) return;
      panel->surfaceConfigured = spatial.spatial && valid && surface.height > 0 && surface.placement.width > 0;
      if (panel->surfaceConfigured) panel->overlay.placeSurface(surface);
      panel->refreshSurface();
    };
    PanelSurface menuAnchor, statusAnchor;
    if (menubar) {
      if (focused) menuAnchor = menuStripPlacement(
          {focus.active, focused->overlay.placement, focused->overlay.panelHeight()},
          menubar->surface, mpp, menuReveal.wide);
      place(menubar, menuAnchor, focused != nullptr);
    }
    if (status) {
      statusAnchor = statusStripPlacement(tiles, status->surface, .001f);
      place(status, statusAnchor, statusAnchor.height > 0);
    }
    for (auto &[path, e] : entries) if (e.panel && e.spec.role == PanelRole::Menu) {
      auto &p = *e.panel;
      bool fromStatus = p.surface.anchor == "status";
      PanelSurface origin = fromStatus ? statusAnchor : menuAnchor;
      bool valid = fromStatus ? status && status->surfaceConfigured : menubar && menubar->surfaceConfigured;
      if (p.surface.anchor == "none" && focused) {
        origin = {focus.active, focused->overlay.placement, focused->overlay.panelHeight()};
        valid = true;
      }
      place(&p, surfacePlacement(origin, p.surface, fromStatus ? .001f : mpp), valid);
    }
    std::vector<PanelSurface> auxiliaries;
    for (auto &[path, e] : entries) if (e.panel && e.panel->auxiliary() && e.panel->overlay.interactive())
      auxiliaries.push_back({e.spec.key, e.panel->overlay.placement, e.panel->overlay.panelHeight(), e.spec.role});
    auto auxiliaryHit = [&](const EyeRay &ray) { return bool(pickPanel(ray, auxiliaries)); };
    int hot = -1;
    PanelPick uiHit;
    int pointer = -1;
    for (int hand : {1, 0})
      if (session->hand[hand].valid) {
        if (auxiliaryHit(session->hand[hand].ray())) continue;
        int h = spatialPick(session->hand[hand].ray(), tiles, &uiHit);
        if (h >= 0) {
          hot = h;
          pointer = hand;
          break;
        }
      }
    bool grab = pointer >= 0 && session->hand[pointer].grab;
    std::string handHits[2];
    bool down[2]{};
    for (int hand = 0; hand < 2; ++hand) {
      down[hand] = session->hand[hand].valid && session->hand[hand].grab;
      if (session->hand[hand].valid && !auxiliaryHit(session->hand[hand].ray())) {
        int t = spatialPick(session->hand[hand].ray(), tiles);
        if (t >= 0)
          handHits[hand] = tiles[t].surface.key;
      }
    }
    auto gesture = press.update(handHits, down, now);
    const bool dismissedMenu = gesture.outside && !menuID.empty();
    if(gesture.outside)menuID.clear();
    for (const auto &t : tiles) {
      if (gesture.hold == t.surface.key && t.kind == SpatialTile::Dock)
        menuID = spatial.apps[t.index].id;
      if (gesture.click != t.surface.key)
        continue;
      try {
        if (t.kind == SpatialTile::Window)
          select(spatial.windows[t.index].key);
        else if (t.kind == SpatialTile::Shelf)menuID.clear();
        else {
          int app = t.kind == SpatialTile::Dock ? t.index : spatialAppIndex(spatial,menuID);
          if (app >= 0 && app < int(spatial.apps.size())) {
            auto &a = spatial.apps[app];
            std::string verb = t.kind == SpatialTile::Dock ? "open"
                               : t.index == 0              ? "show_windows"
                               : t.index == 1 ? (a.hidden ? "unhide" : "hide")
                                              : "quit";
            emit("{\"action\":\"app\",\"verb\":" + jsonString(verb) +
                 ",\"bundleID\":" + jsonString(a.id) + "}");
            menuID.clear();
          }
        }
      } catch (...) {
      }
    }
    tiles = spatialLayout(spatial, anchor, menuID);
    if (now - lastSpatialDraw >= 33) {
      lastSpatialDraw = now;
      spatialRenderer.draw(
          spatial, tiles, hot, uiHit.u, uiHit.v, grab,
          std::filesystem::path(spatialPath).parent_path().string() + "/icons",
          [&](const std::string &key) {
            for (auto &[p, e] : entries)
              if (e.panel && e.spec.key == key)
                return SpatialPicture{e.panel->overlay.spatialTexture(),
                                      e.panel->overlay.spatialTransposed()};
            return SpatialPicture{};
          });
    }
    std::string handOwner[2];
    for (int hand = 0; hand < 2; ++hand) {
      if (session->hand[hand].valid && auxiliaryHit(session->hand[hand].ray())) continue;
      if(dismissedMenu || (spatial.spatial&&spatial.open&&grip[hand]))continue;
      if (session->hand[hand].valid &&
          spatialPick(session->hand[hand].ray(), tiles) >= 0)
        continue;
      float nearest = 0;
      bool nearestForeground = false;
      for (const auto &[path, entry] : entries) {
        if (!entry.panel || entry.spec.role != PanelRole::Window) continue;
        const auto &o = entry.panel->overlay;
        if (o.draggingHand() == hand) { handOwner[hand] = path; break; }
        if (!session->hand[hand].valid) continue;
        float distance = o.interactionDistance(session->hand[hand].pose);
        const bool foreground = o.spatialForeground;
        if (distance > 0 && (handOwner[hand].empty() ||
            (foreground && !nearestForeground) ||
            (foreground == nearestForeground && distance < nearest))) {
          nearest = distance; nearestForeground = foreground; handOwner[hand] = path;
        }
      }
    }
    std::vector<PanelSurface> surfaces;
    for (auto &[path, entry] : entries) {
      if (!entry.panel) continue;
      auto &panel = *entry.panel;
      try {
        unsigned hands = (handOwner[0] == path ? 1u : 0u) | (handOwner[1] == path ? 2u : 0u);
        if (!panel.tick(false, false, hands, spatial.spatial && spatial.open)) {
          panel.finish();
          focus.remove(entry.spec.key); entry.panel.reset(); continue;
        }
        if (entry.spec.role == PanelRole::Window && panel.ready && panel.overlay.interactive())
          surfaces.push_back({entry.spec.key, panel.overlay.placement, panel.overlay.panelHeight()});
      } catch (...) {
        fprintf(stderr, "[native-host] panel failed (endpoint redacted)\n");
        focus.remove(entry.spec.key);
        // Join stream workers before destroying the panel's GL resources.
        entry.panel.reset();
        panelError(entry.spec, "host-panel-error");
      }
    }
    auxiliaries.clear();
    for (auto &[path, e] : entries) if (e.panel && e.panel->auxiliary() && e.panel->overlay.interactive())
      auxiliaries.push_back({e.spec.key, e.panel->overlay.placement, e.panel->overlay.panelHeight(), e.spec.role});
    EyeRay ray, eyes[2];
    gazeStatus = session->eyes ? session->eyes->sample(eyes) : "unavailable";
    bool eye = gazeStatus == "valid" && binocularRay(eyes, ray);
    if (gazeStatus == "valid" && !eye) gazeStatus = "eye-invalid";
    PanelPick hit;
    std::string source;
    if (eye) {
      hit = spatialPanelPick(ray, surfaces, focus.active); source = "eye";
    } else {
      source = "controller";
      // Right hand wins when both point at a window, matching placement input.
      for (int hand : {1, 0}) if (session->hand[hand].valid) {
          if (auxiliaryHit(session->hand[hand].ray()) || spatialPick(session->hand[hand].ray(), tiles) >= 0) {
            hit = {};
            break;
          }
          hit = spatialPanelPick(session->hand[hand].ray(), surfaces, focus.active);
          if (hit)
            break;
        }
    }
    // Foreground UI consumes gaze before underlying windows can build focus.
    if (eye && (auxiliaryHit(ray) || spatialPick(ray, tiles) >= 0))
      hit = {};
    for (auto &[path, e] : entries)
      if (e.panel && e.spec.role == PanelRole::Window)
        focus.feedback.try_emplace(e.spec.key);
    if (focus.update(hit, float((now - lastFrame) / 1000), source) &&
        now - lastFocusAttempt >= 100) {
      lastFocusAttempt = now;
      for (auto &[path, e] : entries)
        if (e.panel && e.spec.key == hit.key)
          try {
            atomicWrite(e.spec.requestDir + "/focus-" + std::to_string(getpid()) + "-" + std::to_string(++requests) + ".json", "{\"action\":\"focus\",\"key\":" + jsonString(hit.key) + ",\"u\":" +
                      std::to_string(hit.u) + ",\"v\":" + std::to_string(hit.v) + "}");
            if (spatial.spatial) e.panel->pipe.sendFocus(hit.u, hit.v);
            focus.activate(hit.key);
            for (auto &[otherPath, other] : entries) if (other.panel && other.spec.role == PanelRole::Window)
              other.panel->overlay.setSpatialForeground(other.spec.key == hit.key);
          } catch (...) {
            focus.feedback[hit.key].lose();
          }
    }
    for (auto &[path, e] : entries)
      if (e.panel && e.spec.role == PanelRole::Window)
        e.panel->overlay.focusFeedback = focus.feedback[e.spec.key];
    lastFrame = now;
    if (spatial.open && eye && !auxiliaryHit(ray)) {
      PanelPick pick;
      int tile = spatialPick(ray, tiles, &pick);
      std::string next = tile >= 0 && tiles[tile].kind == SpatialTile::Window
                             ? spatial.windows[tiles[tile].index].key
                             : "";
      if (next != gazeKey) {
        gazeKey = next;
        gazeSince = now;
      }
      if (!next.empty() && now - gazeSince >= 250) {
        try {
          select(next);
        } catch (...) {
        }
        gazeKey.clear();
      }
    } else {
      gazeKey.clear();
      gazeSince = now;
    }
    if (now - lastState >= 1000) { writeHostState("hosting"); lastState = now; }
    // One short wait for the host, independent of its panel count; every live
    // stream still has its own bounded network and decode workers.
    std::this_thread::sleep_for(std::chrono::milliseconds(4));
  }
  if (interrupted) reason = "signal-" + std::to_string(interrupted);
  for (auto &[path, entry] : entries) if (entry.panel) entry.panel->finish(reason);
  entries.clear();
  writeHostState("stopped");
  return 0;
}
int main(int argc, char **argv) {
  std::string urlFile, controlFile, stateFile, poseFile, dumpPath, overlayName, initialPose, actionManifest,
      requestDir, hostDir, spatialStateFile,
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
      else if (a == "--host-dir")
        hostDir = value();
      else if (a == "--spatial-state")
        spatialStateFile = value();
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
      else if (a == "--request-dir")
        requestDir = value();
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
      } else if (a == "--render-scene") {
        return renderSceneCommand(argc, argv, i);
      } else if (a == "--render-chrome") {
        // Diagnostic: --render-chrome rest|all|hover|held|close|close-held OUT.ppm
        // draws the window chrome (bezel and controls) for a 1.2 m window, flat.
        std::string scene = value(), outPath = value();
        Overlay gl(true, "com.frame-workspace.native.preview");
        if (!renderChromePreview(scene, 1.2f, outPath))
          throw std::runtime_error("--render-chrome: unknown scene or unwritable output");
        return 0;
      } else if (a == "--render-spatial-style") {
        auto scene = value(), out = value();
        auto session = std::make_shared<OverlaySession>(true);
        if (scene == "menubar-slim" || scene == "menubar-wide" || scene == "status" || scene == "menu") {
          const int w = scene == "menubar-slim" ? 420 : scene == "menubar-wide" ? 600 : 360;
          const int h = scene == "menu" ? 240 : scene == "menubar-slim" ? 36 : 60;
          SpatialRaster raster;
          auto image = raster.surface(0, w, h), cr = raster.create(image);
          raster.rgba(cr, .22, .18, .28, 1);
          raster.fontSize(cr, scene == "menubar-slim" ? 14 : 20);
          auto text = [&](double x, double y, const char *label) {
            raster.move(cr, x, y); raster.text(cr, label);
          };
          if (scene == "menu") {
            text(18, 38, "New Window"); text(18, 82, "Open…");
            text(18, 126, "Recent Items"); text(18, 170, "Close Window");
            text(18, 214, "Save…");
          } else if (scene == "status") text(16, 37, "Wi-Fi   Battery   10:21");
          else {
            text(14, scene == "menubar-slim" ? 23 : 37,
                 "Finder    File    Edit    View    Go    Window    Help");
          }
          raster.flush(image);
          GLuint textures[2]; glGenTextures(2, textures);
          glBindTexture(GL_TEXTURE_2D, textures[0]);
          glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
          glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
          glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_BGRA, GL_UNSIGNED_BYTE, raster.data(image));
          SurfaceRenderer material; material.render(textures[0], textures[1], w, h);
          glFinish();
          std::vector<unsigned char> pixels(size_t(w)*h*4);
          glBindTexture(GL_TEXTURE_2D, textures[1]);
          glGetTexImage(GL_TEXTURE_2D, 0, GL_BGRA, GL_UNSIGNED_BYTE, pixels.data());
          memcpy(raster.data(image), pixels.data(), pixels.size());
          void *cairo = dlopen("libcairo.so.2", RTLD_NOW | RTLD_LOCAL);
          if (!cairo) cairo = dlopen("/opt/homebrew/lib/libcairo.2.dylib", RTLD_NOW | RTLD_LOCAL);
          if (!cairo) cairo = dlopen("libcairo.2.dylib", RTLD_NOW | RTLD_LOCAL);
          using Write = int (*)(void *, const char *);
          using Dirty = void (*)(void *);
          auto write = cairo ? reinterpret_cast<Write>(dlsym(cairo, "cairo_surface_write_to_png")) : nullptr;
          auto dirty = cairo ? reinterpret_cast<Dirty>(dlsym(cairo, "cairo_surface_mark_dirty")) : nullptr;
          bool ok = write && dirty;
          if (ok) { dirty(image); ok = write(image, out.c_str()) == 0; }
          if (cairo) dlclose(cairo);
          glDeleteTextures(2, textures); raster.destroy(cr); raster.surfaceDestroy(image);
          if (!ok) throw std::runtime_error("surface PNG output failed");
          return 0;
        }
        SpatialRenderer renderer(session);
        SpatialState st;
        st.spatial = true;
        st.open = true;
        st.index = 1;
        for (auto name : {"Safari", "Terminal", "Code", "Finder"}) {
          st.apps.push_back({name, name, "", true, false});
          st.windows.push_back({name, "Window", name});
        }
        if (!spatialStateFile.empty())
          st = SpatialState::parse(readFile(spatialStateFile, 1024 * 1024));
        auto tiles =
            spatialLayout(st, spatialAnchor(spatialIdentity(), 1.3f, 0),
                          scene == "app-menu" && st.apps.size()>1 ? st.apps[1].id : "");
        if (scene != "rest" && scene != "hover" && scene != "held" &&
            scene != "app-menu")
          throw std::runtime_error("unknown spatial scene");
        // Asymmetric fixture through the same texture sampling as live panels:
        // red upper left, blue lower right, with a white top bar. Alternate
        // cards are physically quarter-turned just like vertical-bend output.
        GLuint fixture[2];
        glGenTextures(2, fixture);
        const int w = 240, h = 160;
        std::vector<unsigned char> pixels(w * h * 4), turned(w * h * 4);
        for (int y = 0; y < h; ++y)
          for (int x = 0; x < w; ++x) {
            auto k = (y * w + x) * 4;
            bool bar = y > 12 && y < 22 && x > 12 && x < 150;
            pixels[k] = bar ? 240 : x < w / 2 ? 150 : 30;
            pixels[k + 1] = bar ? 240 : y < h / 2 ? 65 : 100;
            pixels[k + 2] = bar ? 240 : x >= w / 2 ? 180 : 50;
            pixels[k + 3] = 255;
            for (int c = 0; c < 4; ++c)
              turned[(x * h + (h - 1 - y)) * 4 + c] = pixels[k + c];
          }
        for (int i = 0; i < 2; ++i) {
          glBindTexture(GL_TEXTURE_2D, fixture[i]);
          glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
          glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
          glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, i ? h : w, i ? w : h, 0,
                       GL_RGBA, GL_UNSIGNED_BYTE,
                       i ? turned.data() : pixels.data());
        }
        renderer.draw(
            st, tiles, scene == "rest" ? -1 : 2, .45f, .35f, scene == "held",
            std::filesystem::path(spatialStateFile).parent_path().string() +
                "/icons",
            [&](const std::string &key) {
              size_t i = 0;
              for (; i < st.windows.size() && st.windows[i].key != key; ++i) {
              }
              return SpatialPicture{fixture[i % 2], bool(i % 2)};
            });
        if (!renderer.dump(out, tiles))
          throw std::runtime_error("spatial diagnostic output failed");
        glDeleteTextures(2, fixture);
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
            << "frame-workspace-native --host-dir DIR [--state-file HOST_STATE] | --url-file PATH [--control-file PATH] "
               "[--state-file PATH] [--pose-file PATH] [--overlay-key KEY] [--overlay-name NAME] [--initial-pose JSON] "
               "[--decoder software|v4l2] [--conversion cpu|gpu] [--gaze] [--handoff fence|finish] [--present paced|immediate] [--v4l2-poc-rewrite on|off] [--foveation] "
               "[--seconds N] [--dump PATH.ppm [--dump-seq N] "
               "[--dump-size WIDTHxHEIGHT]] "
               "\nDiagnostics: --diagnostic-gaze U,V|scan (requires --foveation; synthetic, not eye tracking) | --self-test | --render-spatial-style menubar-slim|menubar-wide|status|menu OUT.png | rest|hover|held|app-menu OUT.ppm | --spatial-state PATH | --render-chrome SCENE OUT.ppm | --render-scene SCENE.json OUT.png [--eye x,y,z] [--look yaw,pitch] [--fov deg] [--size WxH] | "
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
    signal(SIGTERM, signalHandler);
    signal(SIGINT, signalHandler);
    signal(SIGPIPE, SIG_IGN);
    av_log_set_level(AV_LOG_ERROR);
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
    PanelOptions options;
    options.urlFile = urlFile;
    options.controlFile = controlFile;
    options.stateFile = stateFile;
    options.poseFile = poseFile;
    options.dumpPath = dumpPath;
    options.overlayName = overlayName;
    options.initialPose = initialPose;
    options.actionManifest = actionManifest;
    options.requestDir = requestDir;
    options.spatialStateFile = spatialStateFile;
    options.key = key;
    options.decoderMode = decoderMode;
    options.conversionMode = conversionMode;
    options.presentMode = presentMode;
    options.handoffMode = handoffMode;
    options.testYuvFormat = testYuvFormat;
    options.testYuvRange = testYuvRange;
    options.testYuvColorSpace = testYuvColorSpace;
    options.testYuvPattern = testYuvPattern;
    options.test = test;
    options.noVR = noVR;
    options.testYuvOracle = testYuvOracle;
    options.foveation = foveation;
    options.gaze = gaze;
    options.diagnosticGaze = diagnosticGaze;
    options.diagnosticScan = diagnosticScan;
    options.pocRewrite = pocRewrite;
    options.diagnosticU = diagnosticU;
    options.diagnosticV = diagnosticV;
    options.seconds = seconds;
    options.dumpSequence = dumpSequence;
    options.dumpWidth = dumpWidth;
    options.dumpHeight = dumpHeight;
    if (!hostDir.empty()) {
      if (!urlFile.empty() || diagnosticGaze || foveation || gaze)
        throw std::runtime_error("host streams use per-panel specs");
      return runHost(hostDir, stateFile, options);
    }
    PanelRunner panel(options);
    while (panel.tick()) {}
    return panel.finish();
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
