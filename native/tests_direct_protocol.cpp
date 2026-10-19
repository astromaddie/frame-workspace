// Exercise the production packet parser without a decoder, GL or a headset.
#include "protocol.hpp"
#include <cassert>
#include <iostream>

int main() {
  using namespace fw;
  Endpoint endpoint;
  endpoint.foveation = true;
  const auto path = endpoint.path("");
  assert(path.find("&direct=1") != std::string::npos);
  assert(path.find("atlas") == std::string::npos);

  std::vector<uint8_t> bytes(38, 0);
  bytes[0] = 0x81; bytes[17] = 1; bytes[18] = 3; bytes[37] = 7;
  auto put = [&](size_t at, int value) {
    bytes[at] = value >> 8; bytes[at + 1] = value & 255;
  };
  auto rejects = [&](bool negotiated, bool atlas, bool direct) {
    bool rejected = false;
    try { packet(bytes, negotiated, atlas, direct); }
    catch (const std::runtime_error &) { rejected = true; }
    assert(rejected);
  };
  put(21, 2880); put(23, 2560); put(25, 2880); put(27, 2560);
  put(33, 1440); put(35, 1280);
  for (auto origin : {std::pair<int,int>{0,0}, {1440,0}, {0,1280},
                      {1440,1280}, {720,640}, {614,424}}) {
    put(29, origin.first); put(31, origin.second);
    const auto p = packet(bytes, true, false, true);
    assert(p.layout.direct && !p.layout.warped && !p.layout.atlas);
    assert(p.layout.matches(2880,2560) && p.bytes.size() == 1 && p.bytes[0] == 7);
    assert(p.layout.left == origin.first && p.layout.top == origin.second);
  }
  // Neither the old generic capability nor atlas authorizes direct frames.
  rejects(false,false,false); rejects(true,false,false); rejects(true,true,false);
  // Odd chroma origins, out-of-bounds cores and coded-size reductions fail.
  for (auto change : {std::pair<size_t,int>{29,615}, {31,425}, {29,1442},
                      {31,1282}, {25,2160}, {27,1280}, {33,1438}, {35,1278}}) {
    auto saved = bytes;
    put(change.first,change.second); rejects(true,false,true); bytes = saved;
  }
  // A direct session never accepts legacy packing or full-quality fallback.
  bytes[18] = 2; put(27,1280);
  rejects(true,true,true);
  bytes[18] = 1; put(25,2160); put(27,1920);
  rejects(true,false,true);
  bytes[18] = 0; put(25,2880); put(27,2560);
  put(29,0); put(31,0); put(33,0); put(35,0);
  rejects(true,false,true);
  assert(!packet(bytes,true).layout.direct); // old peers retain mode0 support
  bytes.resize(18); bytes[0] = 1;
  rejects(true,false,true);
  assert(!packet(bytes).layout.present); // feature-off framing unchanged
  std::cout << "direct protocol: PASS\n";
}
