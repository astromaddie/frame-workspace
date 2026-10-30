#pragma once
#include "websocket.hpp"
#include "host.hpp"
#include <deque>
#include <mutex>
#include <thread>

namespace fw {
struct WorkspaceWindowChoice {std::string label,selection;};
struct WorkspaceMousePacket {
  uint64_t sequence = 0;
  std::string kind;
  double x = 0, y = 0;
  float dx = 0, dy = 0;
  int button = 0;
};
// Exactly one persistent, authenticated workspace socket. No reconnect/replay:
// socket loss is a cancellation, and only a freshly issued Start ticket restarts.
class WorkspaceControlClient {
  std::atomic<bool> quitting{false}, requestStop{false};
  std::thread worker;
  std::mutex mutex;
  std::deque<WorkspaceMousePacket> incoming;
  std::deque<std::string> commands;
  std::vector<WorkspaceWindowChoice> choices;
  std::string catalogNotice;
  uint64_t commandSequence=0;
  std::string pendingBindings, sessionValue, hostValue;
  bool pendingReady = false;
  uint64_t lastSequence = 0;
  std::atomic<uint64_t> receivedSequence{0};
  std::atomic<double> lastHostFrame{0};
public:
  std::atomic<bool> connected{false}, active{false}, failed{false};
  std::string ticket;
  ~WorkspaceControlClient() { quitting = true; if (worker.joinable()) worker.join(); }
  std::pair<std::string,std::string> epochs() {
    std::lock_guard<std::mutex> lock(mutex); return {sessionValue, hostValue};
  }
  uint64_t latestSequence() const {return receivedSequence.load();}
  void hostFrame(double now) { lastHostFrame = now; }
  void bind(const std::string &panels, bool recoveryReady) {
    std::lock_guard<std::mutex> lock(mutex);
    pendingBindings = panels; pendingReady = recoveryReady;
  }
  void requestWindows() {
    std::lock_guard<std::mutex> lock(mutex);
    if(commands.size()<8){commands.push_back("\"t\":\"workspace-windows\"");catalogNotice="Loading Mac windows…";}
  }
  void admit(const std::string &selection) {
    std::lock_guard<std::mutex> lock(mutex);
    if(commands.size()<8){commands.push_back("\"t\":\"workspace-admit\",\"window\":"+selection);catalogNotice="Adding selected window…";}
  }
  std::pair<std::vector<WorkspaceWindowChoice>,std::string> catalog() {
    std::lock_guard<std::mutex> lock(mutex);return {choices,catalogNotice};
  }
  void stop() { active = false; requestStop = true; }
  std::vector<WorkspaceMousePacket> take() {
    std::lock_guard<std::mutex> lock(mutex);
    std::vector<WorkspaceMousePacket> result(incoming.begin(),incoming.end()); incoming.clear(); return result;
  }
  void start(const Endpoint &endpoint) {
    if (worker.joinable()) throw std::runtime_error("workspace connection already used");
    ticket = endpoint.workspaceTicket;
    hostValue = std::to_string(getpid()) + "-" + std::to_string(uint64_t(nowMs()*1000));
    lastHostFrame = nowMs();
    worker = std::thread([this, endpoint] {
      try {
        WebSocket ws(quitting); ws.idlePollMs = 8; ws.serviceIdleWhileReading = true;
        ws.open(endpoint, "", "/workspace/control?ticket=" + quote(endpoint.workspaceTicket));
        std::string session, sentBindings;
        uint64_t heartbeat = 0,bindingsQ=0,bindingsAck=0; double lastBindings=0; double lastHeartbeat = 0, lastServerReply=nowMs();
        ws.idle = [&] {
          const double now = nowMs();
          if (requestStop || (active && (now-lastHostFrame.load()>1000 || now-lastServerReply>1500))) {
            if (!session.empty()) ws.sendText("{\"t\":\"workspace-stop\",\"session\":"+jsonString(session)+"}");
            throw std::runtime_error("workspace stopped");
          }
          std::string bindings; bool recovery = false;
          { std::lock_guard<std::mutex> lock(mutex); bindings=pendingBindings; recovery=pendingReady; }
          if (!session.empty() && recovery && !bindings.empty() && (bindings != sentBindings || (bindingsAck!=bindingsQ && now-lastBindings>=250))) {
            if(bindings!=sentBindings)++bindingsQ;lastBindings=now;
            ws.sendText("{\"t\":\"workspace-ready\",\"session\":"+jsonString(session)+",\"host\":"+
                jsonString(hostValue)+",\"recoveryReady\":true,\"bindingsQ\":"+std::to_string(bindingsQ)+",\"panels\":"+bindings+"}");
            sentBindings = bindings;
          }
          if(active) {
            std::deque<std::string> work;
            {std::lock_guard<std::mutex> lock(mutex);work.swap(commands);}
            for(const auto &command:work)ws.sendText("{"+command+",\"session\":"+jsonString(session)+",\"host\":"+
                jsonString(hostValue)+",\"q\":"+std::to_string(++commandSequence)+"}");
          }
          if (active && now-lastHeartbeat>=250) {
            lastHeartbeat=now;
            ws.sendText("{\"t\":\"workspace-heartbeat\",\"session\":"+jsonString(session)+",\"host\":"+
                jsonString(hostValue)+",\"q\":"+std::to_string(++heartbeat)+"}");
          }
        };
        while (!quitting) {
          auto message=ws.receive();
          if (message.first != 1) continue;
          auto j=json(std::string(message.second.begin(),message.second.end()));
          const auto type=str(j,"t");
          if (type=="workspace-hello" && session.empty()) {
            session=str(j,"session"); if (session.empty()) throw std::runtime_error("invalid workspace epoch");
            { std::lock_guard<std::mutex> lock(mutex); sessionValue=session; }
            connected=true;
          } else if (type=="workspace-started" && str(j,"session")==session && str(j,"host")==hostValue) {active=true;lastServerReply=nowMs();}
          else if (type=="workspace-ready" && str(j,"session")==session && str(j,"host")==hostValue) bindingsAck=uint64_t(num(j,"bindingsQ"));
          else if (type=="workspace-heartbeat" && str(j,"session")==session && str(j,"host")==hostValue) lastServerReply=nowMs();
          else if(type=="workspace-windows" && str(j,"session")==session && str(j,"host")==hostValue) {
            std::vector<WorkspaceWindowChoice> next;auto windows=field(j,"windows");
            if(windows&&json_object_is_type(windows,json_type_array))for(size_t i=0;i<json_object_array_length(windows)&&i<128;++i) {
              auto row=json_object_array_get_idx(windows,i);Json item(json_object_get(row),json_object_put);
              if(str(item,"stamp").empty()||num(item,"id")<=0||num(item,"pid")<=0)continue;
              next.push_back({str(item,"app")+" · "+str(item,"title"),json_object_to_json_string_ext(row,JSON_C_TO_STRING_PLAIN)});
            }
            std::lock_guard<std::mutex> lock(mutex);choices=std::move(next);catalogNotice=choices.empty()?"No other eligible Mac windows":"Choose one window to add";
          }else if(type=="workspace-admitted" && str(j,"session")==session && str(j,"host")==hostValue) {
            std::lock_guard<std::mutex> lock(mutex);catalogNotice=str(j,"message");
          }
          else if (type=="workspace-stopped") throw std::runtime_error("workspace stopped");
          else if (type=="mouse" && active && str(j,"session")==session && str(j,"host")==hostValue) {
            auto q=field(j,"q");
            if (!q || !json_object_is_type(q,json_type_int) || json_object_get_int64(q)<=0) throw std::runtime_error("invalid mouse sequence");
            const uint64_t sequence=uint64_t(json_object_get_int64(q));
            if (sequence<=lastSequence) continue;
            WorkspaceMousePacket p; p.sequence=sequence;p.kind=str(j,"kind");
            if (p.kind!="motion" && p.kind!="down" && p.kind!="up" && p.kind!="wheel") throw std::runtime_error("invalid mouse event");
            auto coordinate=[&](const char *key) {
              auto value=field(j,key);
              if (!value || (!json_object_is_type(value,json_type_int) && !json_object_is_type(value,json_type_double)) || !std::isfinite(json_object_get_double(value))) throw std::runtime_error("invalid mouse coordinate");
              return json_object_get_double(value);
            };
            p.x=coordinate("x");p.y=coordinate("y");p.button=int(num(j,"button"));
            if (p.kind=="wheel") {p.dx=float(coordinate("dx"));p.dy=float(coordinate("dy"));}
            std::lock_guard<std::mutex> lock(mutex);
            if (p.kind=="motion" && !incoming.empty() && incoming.back().kind=="motion") incoming.back()=p;
            else if (incoming.size()<256) incoming.push_back(p);
            else throw std::runtime_error("mouse event queue overflow");
            lastSequence=sequence;receivedSequence=sequence;
          }
        }
      } catch (...) { failed=true; }
      active=false;connected=false;
      std::lock_guard<std::mutex> lock(mutex);incoming.clear();
    });
  }
};
} // namespace fw
