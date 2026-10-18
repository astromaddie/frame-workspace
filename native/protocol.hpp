#pragma once
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <json-c/json.h>
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>
namespace fw {
using Json = std::shared_ptr<json_object>;
inline Json json(const std::string &s) {
  return Json(json_tokener_parse(s.c_str()), json_object_put);
}
inline json_object *field(const Json &j, const char *k) {
  json_object *v = nullptr;
  if (j)
    json_object_object_get_ex(j.get(), k, &v);
  return v;
}
inline std::string str(const Json &j, const char *k) {
  auto v = field(j, k);
  return v && json_object_is_type(v, json_type_string)
             ? json_object_get_string(v)
             : "";
}
inline double num(const Json &j, const char *k, double fallback = 0) {
  auto v = field(j, k);
  if (!v)
    return fallback;
  double n = json_object_get_double(v);
  return std::isfinite(n) ? n : fallback;
}
inline bool flag(const Json &j, const char *k) {
  auto v = field(j, k);
  return v && json_object_get_boolean(v);
}
inline std::string readFile(const std::string &path, size_t limit = 16384) {
  std::ifstream f(path);
  if (!f)
    throw std::runtime_error("file unavailable");
  std::string s((std::istreambuf_iterator<char>(f)), {});
  if (s.size() > limit)
    throw std::runtime_error("file too large");
  return s;
}
inline std::string unquote(const std::string &s) {
  std::string r;
  for (size_t i = 0; i < s.size(); ++i) {
    unsigned char c = s[i];
    if (c == '%' && i + 2 < s.size()) {
      auto h = [](char a) -> int {
        if (a >= '0' && a <= '9')
          return a - '0';
        if (a >= 'A' && a <= 'F')
          return a - 'A' + 10;
        if (a >= 'a' && a <= 'f')
          return a - 'a' + 10;
        return -1;
      };
      int a = h(s[i + 1]), b = h(s[i + 2]);
      if (a < 0 || b < 0)
        throw std::runtime_error("invalid URL escape");
      c = a * 16 + b;
      i += 2;
    } else if (c == '+')
      c = ' ';
    if (c < 32 || c == 127)
      throw std::runtime_error("invalid URL character");
    r += char(c);
  }
  return r;
}
inline std::string quote(const std::string &s) {
  static const char *hex = "0123456789ABCDEF";
  std::string r;
  for (unsigned char c : s)
    if ((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
        (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' || c == '~')
      r += char(c);
    else {
      r += '%';
      r += hex[c >> 4];
      r += hex[c & 15];
    }
  return r;
}
struct Endpoint {
  std::string host, source, ticket;
  int port = 0, max = 4096, fps = 30;
  double bpp = .14;
  std::string path(const std::string &reconnect) const {
    return "/stream?src=" + quote(source) +
           "&codec=h264&max=" + std::to_string(max) +
           "&fps=" + std::to_string(fps) + "&bpp=" + std::to_string(bpp) +
           (reconnect.empty() ? "&t=" + quote(ticket)
                              : "&r=" + quote(reconnect));
  }
};
inline Endpoint parseURL(std::string u) {
  while (!u.empty() && (u.back() == '\n' || u.back() == '\r'))
    u.pop_back();
  if (u.size() > 8192 || u.rfind("http://", 0) != 0)
    throw std::runtime_error("expected loopback HTTP viewer URL");
  size_t slash = u.find('/', 7);
  if (slash == std::string::npos)
    throw std::runtime_error("missing viewer path");
  std::string authority = u.substr(7, slash - 7);
  size_t colon = authority.find(':');
  Endpoint e;
  e.host = authority.substr(0, colon);
  if (e.host != "localhost" && e.host != "127.0.0.1")
    throw std::runtime_error("only loopback viewer endpoints allowed");
  e.port = 80;
  if (colon != std::string::npos) {
    auto p = authority.substr(colon + 1);
    if (p.empty() || p.find_first_not_of("0123456789") != std::string::npos)
      throw std::runtime_error("invalid port");
    e.port = std::stoi(p);
  }
  if (e.port < 1 || e.port > 65535)
    throw std::runtime_error("invalid port");
  auto q = u.find('?', slash);
  if (q == std::string::npos || u.substr(slash, q - slash) != "/view" ||
      u.find('#') != std::string::npos)
    throw std::runtime_error("expected viewer URL");
  std::map<std::string, std::string> values;
  size_t pos = q + 1;
  while (pos < u.size()) {
    auto end = u.find('&', pos);
    if (end == std::string::npos)
      end = u.size();
    auto pair = u.substr(pos, end - pos);
    auto eq = pair.find('=');
    if (eq == std::string::npos)
      throw std::runtime_error("invalid query");
    auto k = unquote(pair.substr(0, eq));
    if (values.count(k))
      throw std::runtime_error("duplicate query");
    values[k] = unquote(pair.substr(eq + 1));
    pos = end + 1;
  }
  e.source = values["src"];
  e.ticket = values["t"];
  if (e.source.empty() || e.ticket.empty())
    throw std::runtime_error("missing viewer credentials");
  if (values.count("codec") && values["codec"] != "h264")
    throw std::runtime_error("native viewer requires h264");
  auto integer = [&](const char *k, int def, int lo, int hi) {
    if (!values.count(k))
      return def;
    auto v = values[k];
    if (v.empty() || v.find_first_not_of("0123456789") != std::string::npos)
      throw std::runtime_error("invalid video setting");
    int n = std::stoi(v);
    if (n < lo || n > hi)
      throw std::runtime_error("video setting out of range");
    return n;
  };
  e.max = integer("max", 4096, 320, 4096);
  e.fps = integer("fps", 30, 1, 120);
  if (values.count("bpp")) {
    size_t consumed = 0;
    e.bpp = std::stod(values["bpp"], &consumed);
    if (consumed != values["bpp"].size() || !std::isfinite(e.bpp) ||
        e.bpp < .01 || e.bpp > 2)
      throw std::runtime_error("invalid bitrate");
  }
  return e;
}
enum class ResetReason : uint8_t {
  None,
  Overflow,
  DecoderError,
  Reconnect,
  FormatChange
};
struct Packet {
  bool key = false;
  ResetReason resetReason = ResetReason::None;
  uint32_t seq = 0;
  double received = 0;
  std::vector<uint8_t> bytes;
};
inline Packet packet(const std::vector<uint8_t> &b) {
  if (b.size() < 18 || b.size() > 16 * 1024 * 1024)
    throw std::runtime_error("invalid video packet");
  Packet p;
  p.key = b[0] & 1;
  p.seq = (uint32_t(b[9]) << 24) | (uint32_t(b[10]) << 16) |
          (uint32_t(b[11]) << 8) | b[12];
  p.bytes.assign(b.begin() + 17, b.end());
  return p;
}
} // namespace fw
