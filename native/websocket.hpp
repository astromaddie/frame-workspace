#pragma once
#include "protocol.hpp"
#include <arpa/inet.h>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <functional>
#include <netinet/tcp.h>
#include <openssl/evp.h>
#include <openssl/rand.h>
#include <openssl/sha.h>
#include <poll.h>
#include <sys/socket.h>
#include <system_error>
#include <unistd.h>
namespace fw {
inline double nowMs() {
  using namespace std::chrono;
  return duration<double, std::milli>(steady_clock::now().time_since_epoch())
      .count();
}
inline std::string base64(const unsigned char *b, int n) {
  std::string s(4 * ((n + 2) / 3), '\0');
  EVP_EncodeBlock(reinterpret_cast<unsigned char *>(&s[0]), b, n);
  return s;
}
class WebSocket {
  int fd = -1;
  std::atomic<bool> &stop;
  double lastByte = 0, lastReadService = 0;
  void readBytes(void *ptr, size_t n) {
    auto *p = static_cast<uint8_t *>(ptr);
    while (n) {
      if (stop)
        throw std::runtime_error("stopped");
      // A large video message can arrive continuously for hundreds of
      // milliseconds without a poll timeout. Keep the opt-in gaze/control
      // backchannel alive while assembling that message, too.
      if (serviceIdleWhileReading && idle &&
          nowMs() - lastReadService >= idlePollMs) {
        lastReadService = nowMs();
        idle();
      }
      pollfd f[2]{{fd, POLLIN, 0}, {wakeFd, POLLIN, 0}};
      int r = poll(f, wakeFd >= 0 ? 2 : 1, idlePollMs);
      if (r < 0 && errno == EINTR)
        continue;
      if (r < 0)
        throw std::runtime_error("socket poll failed");
      if (wakeFd >= 0 && (f[1].revents & POLLIN)) {
        // Queued input goes out now rather than at the next poll timeout.
        char drain[64];
        while (::read(wakeFd, drain, sizeof drain) > 0) {
        }
        if (idle) idle();
        lastReadService = nowMs();
        if (!(f[0].revents & (POLLIN | POLLHUP | POLLERR)))
          continue;
      }
      if (!(f[0].revents & (POLLIN | POLLHUP | POLLERR))) {
        if (idle)
          idle();
        lastReadService = nowMs();
        if (nowMs() - lastByte > 15000)
          throw std::runtime_error("stream timeout");
        continue;
      }
      ssize_t got = recv(fd, p, n, 0);
      if (got <= 0)
        throw std::runtime_error("stream disconnected");
      lastByte = nowMs();
      p += got;
      n -= got;
    }
  }
  void writeBytes(const uint8_t *p, size_t n) {
    double start = nowMs();
    while (n) {
      if (stop)
        throw std::runtime_error("stopped");
      ssize_t sent = send(fd, p, n, MSG_NOSIGNAL | MSG_DONTWAIT);
      if (sent < 0 &&
          (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) {
        if (nowMs() - start > 1000)
          throw std::runtime_error("stream send timeout");
        pollfd f{fd, POLLOUT, 0};
        poll(&f, 1, 50);
        continue;
      }
      if (sent <= 0)
        throw std::runtime_error("stream send failed");
      p += sent;
      n -= sent;
    }
  }

public:
  int idlePollMs = 100; // legacy cadence; foveated gaze opts into 8 ms
  bool serviceIdleWhileReading = false;
  // Readable when the owner has queued input to send (non-blocking pipe).
  int wakeFd = -1;
  std::function<void()> idle;
  explicit WebSocket(std::atomic<bool> &stopped) : stop(stopped) {}
  ~WebSocket() {
    if (fd >= 0)
      close(fd);
  }
  void open(const Endpoint &e, const std::string &reconnect) {
    fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0)
      throw std::runtime_error("socket unavailable");
    const int one = 1;
    if (setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one)) < 0)
      throw std::system_error(errno, std::generic_category(), "TCP_NODELAY");
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(e.port);
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    if (connect(fd, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)))
      throw std::runtime_error("loopback connection failed");
    lastByte = nowMs();
    unsigned char random[16];
    if (RAND_bytes(random, 16) != 1)
      throw std::runtime_error("random source failed");
    auto key = base64(random, 16);
    std::string request =
        "GET " + e.path(reconnect) + " HTTP/1.1\r\nHost: " + e.host + ":" +
        std::to_string(e.port) +
        "\r\nUpgrade: websocket\r\nConnection: "
        "Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: " +
        key + "\r\n\r\n";
    writeBytes(reinterpret_cast<const uint8_t *>(request.data()),
               request.size());
    std::string reply;
    while (reply.find("\r\n\r\n") == std::string::npos) {
      char c;
      readBytes(&c, 1);
      reply += c;
      if (reply.size() > 8192)
        throw std::runtime_error("invalid upgrade");
    }
    if (reply.rfind("HTTP/1.1 101 ", 0) != 0)
      throw std::runtime_error("stream authorization or upgrade rejected");
    auto hashInput = key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
    unsigned char digest[SHA_DIGEST_LENGTH];
    SHA1(reinterpret_cast<const unsigned char *>(hashInput.data()),
         hashInput.size(), digest);
    auto expected = base64(digest, sizeof(digest));
    bool accepted = false;
    size_t pos = reply.find("\r\n") + 2;
    while (pos < reply.size()) {
      auto end = reply.find("\r\n", pos);
      auto line = reply.substr(pos, end - pos);
      auto c = line.find(':');
      if (c != std::string::npos) {
        auto name = line.substr(0, c);
        std::transform(name.begin(), name.end(), name.begin(),
                       [](unsigned char x) { return std::tolower(x); });
        auto value = line.substr(c + 1);
        while (!value.empty() && value.front() == ' ')
          value.erase(0, 1);
        if (name == "sec-websocket-accept" && value == expected)
          accepted = true;
      }
      pos = end + 2;
    }
    if (!accepted)
      throw std::runtime_error("invalid websocket accept");
  }
  void sendFrame(uint8_t opcode, const std::vector<uint8_t> &data) {
    if (data.size() > 65535)
      throw std::runtime_error("outgoing message too large");
    std::vector<uint8_t> b{uint8_t(0x80 | opcode)};
    if (data.size() < 126)
      b.push_back(uint8_t(0x80 | data.size()));
    else {
      b.push_back(0x80 | 126);
      b.push_back(data.size() >> 8);
      b.push_back(data.size());
    }
    unsigned char mask[4];
    if (RAND_bytes(mask, 4) != 1)
      throw std::runtime_error("random source failed");
    b.insert(b.end(), mask, mask + 4);
    for (size_t i = 0; i < data.size(); i++)
      b.push_back(data[i] ^ mask[i % 4]);
    writeBytes(b.data(), b.size());
  }
  void sendText(const std::string &s) { sendFrame(1, {s.begin(), s.end()}); }
  std::pair<uint8_t, std::vector<uint8_t>> receive() {
    std::vector<uint8_t> assembled;
    uint8_t type = 0;
    for (;;) {
      uint8_t h[2];
      readBytes(h, 2);
      if (h[0] & 0x70 || h[1] & 0x80)
        throw std::runtime_error("invalid websocket flags");
      uint8_t op = h[0] & 15;
      uint64_t n = h[1] & 127;
      if (n == 126) {
        uint8_t v[2];
        readBytes(v, 2);
        n = (uint64_t(v[0]) << 8) | v[1];
      } else if (n == 127) {
        uint8_t v[8];
        readBytes(v, 8);
        n = 0;
        for (auto c : v)
          n = (n << 8) | c;
      }
      if (n > 16 * 1024 * 1024 || assembled.size() + n > 16 * 1024 * 1024)
        throw std::runtime_error("websocket message too large");
      if (op >= 8 && (!(h[0] & 0x80) || n > 125))
        throw std::runtime_error("invalid websocket control");
      std::vector<uint8_t> payload(n);
      readBytes(payload.data(), n);
      if (op == 8)
        throw std::runtime_error("stream closed");
      if (op == 9) {
        sendFrame(10, payload);
        continue;
      }
      if (op == 10)
        continue;
      if (op != 0 && op != 1 && op != 2)
        throw std::runtime_error("invalid websocket opcode");
      if (op) {
        if (type)
          throw std::runtime_error("interleaved websocket fragments");
        type = op;
      } else if (!type)
        throw std::runtime_error("unexpected websocket continuation");
      assembled.insert(assembled.end(), payload.begin(), payload.end());
      if (h[0] & 0x80)
        return {type, std::move(assembled)};
    }
  }
};
} // namespace fw
