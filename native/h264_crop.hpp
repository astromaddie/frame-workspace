#pragma once
#include <algorithm>
#include <array>
#include <cstdint>
#include <vector>

namespace fw {
// Visible bounds are immutable packet metadata, never borrowed from a newer
// decoder context. Only progressive 8-bit 4:2:0 is eligible for driver repair.
struct H264Crop {
  int codedWidth = 0, codedHeight = 0, left = 0, top = 0, right = 0, bottom = 0;
  AVColorSpace colorSpace = AVCOL_SPC_UNSPECIFIED;
  AVColorRange colorRange = AVCOL_RANGE_MPEG;
  bool valid() const {
    return codedWidth > 0 && codedHeight > 0 && codedWidth <= 4096 &&
           codedHeight <= 4096 && left >= 0 && top >= 0 && right >= 0 &&
           bottom >= 0 && left <= codedWidth && right <= codedWidth &&
           top <= codedHeight && bottom <= codedHeight && !(left % 2 || top % 2 || right % 2 || bottom % 2) &&
           left + right < codedWidth && top + bottom < codedHeight;
  }
  int width() const { return codedWidth - left - right; }
  int height() const { return codedHeight - top - bottom; }
};
struct H264CropHeaders {
  struct Bits {
    std::vector<uint8_t> data;
    size_t position = 0;
    bool ok = true;
    Bits(const uint8_t *p, size_t n, size_t limit = 4096) {
      unsigned zeros = 0;
      for (size_t i = 0; i < n && data.size() < limit; ++i) {
        if (zeros >= 2 && p[i] == 3) {
          if (i + 1 >= n || p[i + 1] > 3) { ok = false; break; }
          zeros = 0;
          continue;
        }
        data.push_back(p[i]);
        zeros = p[i] == 0 ? zeros + 1 : 0;
      }
    }
    uint32_t bits(unsigned n) {
      if (!ok || n > 32 || position + n > data.size() * 8) {
        ok = false; return 0;
      }
      uint32_t v = 0;
      while (n--) { v = (v << 1) | ((data[position / 8] >> (7 - position % 8)) & 1); ++position; }
      return v;
    }
    uint32_t ue() {
      unsigned zeros = 0;
      while (ok && !bits(1)) { if (++zeros > 24) { ok = false; return 0; } }
      return ok ? ((1u << zeros) - 1) + bits(zeros) : 0;
    }
    int se() { uint32_t v = ue(); return v & 1 ? int((v + 1) / 2) : -int(v / 2); }
  };
  std::array<H264Crop, 32> sps{};
  std::array<int, 256> pps;
  H264CropHeaders() { pps.fill(-1); }
  bool parseSps(Bits &b) {
    unsigned profile = b.bits(8); b.bits(8); b.bits(8);
    unsigned id = b.ue();
    if (!b.ok || id >= sps.size()) return false;
    sps[id] = {};
    unsigned chroma = 1, depthY = 0, depthC = 0;
    if (profile == 100 || profile == 110 || profile == 122 || profile == 244 ||
        profile == 44 || profile == 83 || profile == 86 || profile == 118 ||
        profile == 128 || profile == 138 || profile == 139 || profile == 134 || profile == 135) {
      chroma = b.ue();
      if (chroma > 3) return false;
      if (chroma == 3) b.bits(1);
      depthY = b.ue(); depthC = b.ue(); b.bits(1);
      if (b.bits(1)) {
        for (unsigned i = 0; i < (chroma == 3 ? 12u : 8u) && b.ok; ++i) {
          if (!b.bits(1)) continue;
          int last = 8, next = 8;
          for (unsigned j = 0; j < (i < 6 ? 16u : 64u) && b.ok; ++j) {
            if (next) next = (last + b.se() + 256) % 256;
            if (next) last = next;
          }
        }
      }
    } else if (profile != 66 && profile != 77 && profile != 88) return false;
    b.ue(); unsigned poc = b.ue();
    if (poc == 0) b.ue();
    else if (poc == 1) {
      b.bits(1); b.se(); b.se(); unsigned n = b.ue();
      if (n > 256) return false;
      while (n--) b.se();
    } else if (poc != 2) return false;
    b.ue(); b.bits(1);
    unsigned mbw = b.ue(), mbh = b.ue(); bool progressive = b.bits(1);
    if (!progressive) b.bits(1);
    b.bits(1);
    unsigned l = 0, r = 0, t = 0, d = 0;
    if (b.bits(1)) { l = b.ue(); r = b.ue(); t = b.ue(); d = b.ue(); }
    if (!b.ok || chroma != 1 || depthY || depthC || !progressive ||
        mbw >= 256 || mbh >= 256 || l > 2048 || r > 2048 || t > 2048 || d > 2048) return false;
    H264Crop c{int((mbw + 1) * 16), int((mbh + 1) * 16), int(l * 2), int(t * 2), int(r * 2), int(d * 2)};
    // FFmpeg's lightweight parser and QCOM AVFrames both omit VUI colors.
    // Parse the bounded VUI prefix so range/matrix follow this SPS and PTS.
    if (b.bits(1)) {
      if (b.bits(1)) { unsigned aspect = b.bits(8); if (aspect == 255) { b.bits(16); b.bits(16); } }
      if (b.bits(1)) b.bits(1);
      if (b.bits(1)) {
        b.bits(3); c.colorRange = b.bits(1) ? AVCOL_RANGE_JPEG : AVCOL_RANGE_MPEG;
        if (b.bits(1)) { b.bits(8); b.bits(8); c.colorSpace = AVColorSpace(b.bits(8)); }
      }
    }
    if (!b.ok || !c.valid()) return false;
    sps[id] = c;
    return true;
  }
  // Read only parameter sets and the bounded slice prefix. Slice payload is
  // beyond that prefix is never copied/unescaped; Annex-B scanning is linear.
  bool parse(const std::vector<uint8_t> &data, H264Crop &crop) {
    int active = -1;
    auto start = [&](size_t i) {
      return i + 3 <= data.size() && data[i] == 0 && data[i + 1] == 0 && data[i + 2] == 1;
    };
    for (size_t i = 0; i + 3 < data.size();) {
      if (!start(i)) { ++i; continue; }
      size_t n = i + 3, end = n + 1;
      while (end + 3 <= data.size() && !start(end)) ++end;
      if (end + 3 > data.size()) end = data.size();
      unsigned type = data[n] & 31;
      if (data[n] & 128) return false;
      if (active >= 0 && (type == 7 || type == 8)) return false;
      if (type == 7 || type == 8 || type == 1 || type == 5) {
        size_t length = end - n - 1;
        if ((type == 7 || type == 8) && length > 4096) return false;
        Bits b(data.data() + n + 1, length, type == 1 || type == 5 ? 64 : 4096);
        if (type == 7) { if (!parseSps(b)) return false; }
        else if (type == 8) {
          unsigned p = b.ue(), s = b.ue();
          if (!b.ok || p >= pps.size() || s >= sps.size()) return false;
          pps[p] = int(s);
        } else {
          b.ue(); b.ue(); unsigned p = b.ue();
          if (!b.ok || p >= pps.size() || pps[p] < 0 || !sps[pps[p]].valid()) return false;
          if (active >= 0 && active != pps[p]) return false;
          active = pps[p]; crop = sps[active];
        }
      }
      i = end;
    }
    return active >= 0 && crop.valid();
  }
};
// Select the visible rectangle only when all dimensions agree. An already
// cropped software frame is accepted; coded frames require exact SPS bounds.
inline bool normalizeH264Crop(AVFrame *frame, const H264Crop &crop,
                              int width, int height) {
  if (!crop.valid() || crop.width() != width || crop.height() != height) return false;
  const bool tagged = frame->crop_left || frame->crop_top || frame->crop_right || frame->crop_bottom;
  if (frame->width == width && frame->height == height) return !tagged;
  if (frame->width != crop.codedWidth || frame->height != crop.codedHeight ||
      (frame->format != AV_PIX_FMT_NV12 && frame->format != AV_PIX_FMT_YUV420P && frame->format != AV_PIX_FMT_YUVJ420P)) return false;
  if (tagged && (frame->crop_left != size_t(crop.left) || frame->crop_top != size_t(crop.top) ||
                 frame->crop_right != size_t(crop.right) || frame->crop_bottom != size_t(crop.bottom))) return false;
  frame->crop_left = crop.left; frame->crop_top = crop.top;
  frame->crop_right = crop.right; frame->crop_bottom = crop.bottom;
  return av_frame_apply_cropping(frame, AV_FRAME_CROP_UNALIGNED) == 0 &&
         frame->width == width && frame->height == height;
}
} // namespace fw
