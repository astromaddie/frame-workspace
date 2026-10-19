#pragma once
// Rewrites an H.264 stream from pic_order_cnt_type 0 to 2.
//
// The Frame's iris V4L2 decoder holds each POC-type-0 picture until the next
// access unit arrives, even when the SPS says max_num_reorder_frames = 0:
// one frame interval of delay (~17 ms at 60 fps). With POC type 2 (output
// order is decode order) it releases each picture as soon as it is decoded.
// Verified 2026-10-19 on 2880x2560 VideoToolbox streams: hold 21.0 -> 4.3 ms,
// output lag 1 -> 0, and FFmpeg decodes the rewrite bit-identically.
//
// Only the SPS POC fields and each slice header's pic_order_cnt_lsb (and
// delta_pic_order_cnt_bottom) change; CABAC slice data is copied unchanged
// after re-aligning it. POC type 2 needs every picture to be a reference
// picture or no two non-reference pictures in a row, which holds for the
// Mac's no-B-frame stream; anything this does not parse is reported, not
// guessed.
#include <cstdint>
#include <map>
#include <vector>

namespace fw {

class H264PocRewriter {
  struct Sps {
    int log2MaxFrameNum = 0, log2MaxPocLsb = 0;
    bool frameMbsOnly = true, rewrite = false;
  };
  struct Pps {
    int sps = -1;
    bool cabac = false, bottomFieldPoc = false, weighted = false;
    int weightedBipred = 0;
    bool deblockingControl = false, redundantPicCnt = false;
  };
  std::map<int, Sps> sps_;
  std::map<int, Pps> pps_;

  struct Reader {
    const std::vector<uint8_t> &d;
    size_t p = 0;
    bool bad = false;
    explicit Reader(const std::vector<uint8_t> &data) : d(data) {}
    uint32_t u(int n) {
      uint32_t v = 0;
      for (int i = 0; i < n; ++i) {
        if ((p >> 3) >= d.size()) { bad = true; return 0; }
        v = (v << 1) | ((d[p >> 3] >> (7 - (p & 7))) & 1);
        ++p;
      }
      return v;
    }
    uint32_t ue() {
      int zeros = 0;
      while (!bad && u(1) == 0)
        if (++zeros > 31) { bad = true; return 0; }
      return (1u << zeros) - 1 + u(zeros);
    }
    int32_t se() {
      uint32_t k = ue();
      return (k & 1) ? int32_t((k + 1) / 2) : -int32_t(k / 2);
    }
  };
  struct Writer {
    std::vector<uint8_t> out;
    int bits = 0;
    void bit(int b) {
      if (bits % 8 == 0) out.push_back(0);
      if (b) out.back() |= uint8_t(0x80 >> (bits % 8));
      ++bits;
    }
    void u(int n, uint32_t v) {
      for (int i = n - 1; i >= 0; --i) bit((v >> i) & 1);
    }
    void ue(uint32_t v) {
      ++v;
      int n = 0;
      for (uint32_t t = v; t; t >>= 1) ++n;
      u(n - 1, 0);
      u(n, v);
    }
    void copy(const std::vector<uint8_t> &d, size_t from, size_t to) {
      for (size_t i = from; i < to; ++i) bit((d[i >> 3] >> (7 - (i & 7))) & 1);
    }
  };

  static std::vector<uint8_t> unescape(const uint8_t *b, size_t n) {
    std::vector<uint8_t> out;
    out.reserve(n);
    int zeros = 0;
    for (size_t i = 0; i < n; ++i) {
      if (zeros >= 2 && b[i] == 3) { zeros = 0; continue; }
      out.push_back(b[i]);
      zeros = b[i] == 0 ? zeros + 1 : 0;
    }
    return out;
  }
  static void escape(const std::vector<uint8_t> &rbsp, std::vector<uint8_t> &out) {
    int zeros = 0;
    for (uint8_t x : rbsp) {
      if (zeros >= 2 && x <= 3) { out.push_back(3); zeros = 0; }
      out.push_back(x);
      zeros = x == 0 ? zeros + 1 : 0;
    }
    if (zeros >= 2) // trailing cabac_zero_words end in 00 00 03
      out.push_back(3);
  }

  bool parseSps(const std::vector<uint8_t> &rbsp, std::vector<uint8_t> &rewritten) {
    Reader r(rbsp);
    int profile = int(r.u(8));
    r.u(16);
    int id = int(r.ue());
    switch (profile) {
    case 100: case 110: case 122: case 244: case 44: case 83: case 86:
    case 118: case 128: case 138: case 139: case 134: case 135: {
      if (r.ue() == 3) r.u(1);
      r.ue(); r.ue(); r.u(1);
      if (r.u(1)) return false; // scaling matrices: not needed for the Mac stream
      break;
    }
    default: break;
    }
    Sps s;
    s.log2MaxFrameNum = int(r.ue()) + 4;
    const size_t pocStart = r.p;
    const uint32_t pocType = r.ue();
    if (pocType == 0)
      s.log2MaxPocLsb = int(r.ue()) + 4;
    else if (pocType == 1)
      return false;
    const size_t pocEnd = r.p;
    r.ue(); r.u(1); r.ue(); r.ue();
    s.frameMbsOnly = r.u(1);
    if (r.bad || s.log2MaxFrameNum > 16 || s.log2MaxPocLsb > 16 || !s.frameMbsOnly) return false;
    s.rewrite = pocType == 0;
    sps_[id] = s;
    if (!s.rewrite) { rewritten = rbsp; return true; }
    size_t last = rbsp.size() * 8;
    while (last > pocEnd && !((rbsp[(last - 1) >> 3] >> (7 - ((last - 1) & 7))) & 1)) --last;
    if (last <= pocEnd) return false;
    Writer w;
    w.copy(rbsp, 0, pocStart);
    w.ue(2);
    w.copy(rbsp, pocEnd, last - 1); // the rest, up to the rbsp_stop_one_bit
    w.bit(1);
    rewritten = std::move(w.out);
    return true;
  }
  bool parsePps(const std::vector<uint8_t> &rbsp) {
    Reader r(rbsp);
    Pps p;
    int id = int(r.ue());
    p.sps = int(r.ue());
    p.cabac = r.u(1);
    p.bottomFieldPoc = r.u(1);
    if (r.ue() != 0) return false; // slice groups
    r.ue(); r.ue();
    p.weighted = r.u(1);
    p.weightedBipred = int(r.u(2));
    r.se(); r.se(); r.se();
    p.deblockingControl = r.u(1);
    r.u(1);
    p.redundantPicCnt = r.u(1);
    if (r.bad) return false;
    pps_[id] = p;
    return true;
  }
  bool rewriteSlice(int nalRefIdc, int nalType, const std::vector<uint8_t> &rbsp,
                    std::vector<uint8_t> &rewritten, bool &changed) {
    Reader r(rbsp);
    r.ue();
    const uint32_t sliceType = r.ue() % 5;
    auto pit = pps_.find(int(r.ue()));
    if (pit == pps_.end()) return false;
    const Pps &p = pit->second;
    auto sit = sps_.find(p.sps);
    if (sit == sps_.end()) return false;
    const Sps &s = sit->second;
    if (!s.rewrite) { rewritten = rbsp; changed = false; return true; }
    r.u(s.log2MaxFrameNum);
    if (nalType == 5) r.ue();
    const size_t lsbStart = r.p;
    r.u(s.log2MaxPocLsb);
    if (p.bottomFieldPoc) r.se();
    const size_t lsbEnd = r.p;
    if (p.redundantPicCnt) r.ue();
    if (sliceType == 1) r.u(1);
    if (sliceType == 0 || sliceType == 1 || sliceType == 3)
      if (r.u(1)) { r.ue(); if (sliceType == 1) r.ue(); }
    const int lists = sliceType == 1 ? 2 : (sliceType == 0 || sliceType == 3) ? 1 : 0;
    for (int list = 0; list < lists; ++list)
      if (r.u(1))
        for (int guard = 0;; ++guard) {
          uint32_t op = r.ue();
          if (op == 3 || r.bad || guard > 64) break;
          r.ue();
        }
    if ((p.weighted && (sliceType == 0 || sliceType == 3)) || (p.weightedBipred == 1 && sliceType == 1))
      return false;
    if (nalRefIdc) {
      if (nalType == 5) { r.u(1); r.u(1); }
      else if (r.u(1))
        for (int guard = 0;; ++guard) {
          uint32_t op = r.ue();
          if (op == 0 || r.bad || guard > 64) break;
          if (op == 1 || op == 3) r.ue();
          if (op == 2) r.ue();
          if (op == 3 || op == 6) r.ue();
          if (op == 4) r.ue();
        }
    }
    if (p.cabac && sliceType != 2 && sliceType != 4) r.ue();
    r.se();
    if (sliceType == 3 || sliceType == 4) return false; // SP/SI
    if (p.deblockingControl && r.ue() != 1) { r.se(); r.se(); }
    if (r.bad || !p.cabac) return false; // the Mac stream is CABAC
    const size_t headerEnd = r.p;
    Writer w;
    w.copy(rbsp, 0, lsbStart);
    w.copy(rbsp, lsbEnd, headerEnd);
    while (w.bits % 8) w.bit(1); // cabac_alignment_one_bit
    const size_t dataStart = (headerEnd + 7) / 8;
    rewritten = std::move(w.out);
    rewritten.insert(rewritten.end(), rbsp.begin() + long(dataStart), rbsp.end());
    changed = true;
    return true;
  }

public:
  void reset() { sps_.clear(); pps_.clear(); }
  // Rewrites one Annex B access unit. False means it uses something this
  // does not handle; `out` is then unspecified.
  bool rewrite(const std::vector<uint8_t> &in, std::vector<uint8_t> &out) {
    out.clear();
    out.reserve(in.size() + 16);
    std::vector<size_t> starts;
    for (size_t i = 0; i + 2 < in.size(); ++i)
      if (in[i] == 0 && in[i + 1] == 0 && in[i + 2] == 1) { starts.push_back(i + 3); i += 2; }
    for (size_t n = 0; n < starts.size(); ++n) {
      size_t begin = starts[n];
      size_t end = n + 1 < starts.size() ? starts[n + 1] - 3 : in.size();
      if (n + 1 < starts.size() && end > begin && in[end - 1] == 0) --end; // 4-byte start code
      if (end <= begin) continue;
      const uint8_t header = in[begin];
      const int type = header & 31, refIdc = (header >> 5) & 3;
      static const uint8_t startCode[] = {0, 0, 0, 1};
      out.insert(out.end(), startCode, startCode + 4);
      out.push_back(header);
      if (type != 7 && type != 8 && type != 1 && type != 5) {
        out.insert(out.end(), in.begin() + long(begin) + 1, in.begin() + long(end));
        continue;
      }
      auto rbsp = unescape(in.data() + begin + 1, end - begin - 1);
      std::vector<uint8_t> rewritten;
      bool changed = true;
      if (type == 7) { if (!parseSps(rbsp, rewritten)) return false; }
      else if (type == 8) { if (!parsePps(rbsp)) return false; rewritten = std::move(rbsp); }
      else if (!rewriteSlice(refIdc, type, rbsp, rewritten, changed)) return false;
      escape(rewritten, out);
    }
    return true;
  }
};

} // namespace fw
