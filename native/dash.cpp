// frame-workspace-dash: the single "Frame Workspace" entry on the SteamVR bar
// (FW-08). It owns a dashboard overlay with SteamVR's laser input, shows the
// Workspace and every floating window, and turns presses into fixed requests
// the Mac companion carries out (start/stop the Workspace, recenter, arrange
// the Spatial Mode windows, close a window). The companion rewrites the state file every couple of seconds; an
// old file means the Mac is not connected.
//
//   frame-workspace-dash --state FILE --request-dir DIR [--images DIR] [--layout tiles|list]
//   frame-workspace-dash --render-mock OUT.png list|tiles running|stopped|offline|spatial|notice
#include "dash_ui.hpp"
#include "workspace_exit.hpp"
#include "vendor/openvr/openvr_capi.h"
#include <json-c/json.h>
#include <csignal>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <dlfcn.h>
#include <fcntl.h>
#include <poll.h>
#include <string>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

using namespace fw;

static DashState mockState(const std::string &which) {
  DashState s;
  s.macConnected = which != "offline";
  s.workspaceRunning = which == "running" || which == "spatial" || which == "notice";
  s.workspaceStopAvailable = s.workspaceRunning || !s.macConnected;
  s.spatial = which == "spatial" || which == "notice";
  if (which == "notice") s.notice = "Couldn't Open Claude: application not found";
  if (s.workspaceRunning)
    s.windows = {{"mads — grok — -zsh — 138×46", "com.frame-workspace.native.1000.panel-w105"},
                 {"Frame Workspace Preview — Google Chrome", "com.frame-workspace.native.1000.panel-w212"},
                 {"Claude", "com.frame-workspace.native.1000.panel-w33"}};
  return s;
}

// Renders the panel to a PNG; returns its buttons (empty on failure).
static std::vector<DashButton> renderPng(DashRenderer &r, const DashState &s, const std::string &hover,
                                         const std::string &layout, const std::string &out) {
  r.height = r.measure(s, layout);
  cairo_surface_t *surface = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, DashRenderer::width, r.height);
  cairo_t *cr = cairo_create(surface);
  auto buttons = r.draw(cr, s, hover, layout);
  cairo_destroy(cr);
  const std::string tmp = out + ".tmp.png";
  const bool ok = cairo_surface_write_to_png(surface, tmp.c_str()) == CAIRO_STATUS_SUCCESS &&
                  rename(tmp.c_str(), out.c_str()) == 0;
  cairo_surface_destroy(surface);
  return ok ? buttons : std::vector<DashButton>{};
}

// The bar thumbnail: a rounded tile with a wide screen and a smaller window.
static bool renderThumbnail(const std::string &out) {
  const int n = 256;
  cairo_surface_t *surface = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, n, n);
  cairo_t *c = cairo_create(surface);
  auto rounded = [&](double x, double y, double w, double h, double r) {
    cairo_new_sub_path(c);
    cairo_arc(c, x + w - r, y + r, r, -M_PI / 2, 0);
    cairo_arc(c, x + w - r, y + h - r, r, 0, M_PI / 2);
    cairo_arc(c, x + r, y + h - r, r, M_PI / 2, M_PI);
    cairo_arc(c, x + r, y + r, r, M_PI, 3 * M_PI / 2);
    cairo_close_path(c);
  };
  cairo_pattern_t *g = cairo_pattern_create_linear(0, 0, n, n);
  cairo_pattern_add_color_stop_rgb(g, 0, 0.278, 0.749, 1);
  cairo_pattern_add_color_stop_rgb(g, 1, 0.102, 0.267, 0.761);
  rounded(8, 8, n - 16, n - 16, 52);
  cairo_set_source(c, g);
  cairo_fill(c);
  cairo_pattern_destroy(g);
  cairo_set_source_rgba(c, 1, 1, 1, 0.95);
  rounded(44, 66, 168, 104, 14);
  cairo_set_line_width(c, 12);
  cairo_stroke(c);
  rounded(150, 120, 70, 54, 10);
  cairo_set_source_rgb(c, 0.13, 0.36, 0.86);
  cairo_fill_preserve(c);
  cairo_set_source_rgba(c, 1, 1, 1, 0.95);
  cairo_set_line_width(c, 9);
  cairo_stroke(c);
  rounded(98, 192, 60, 12, 6);
  cairo_fill(c);
  cairo_destroy(c);
  const bool ok = cairo_surface_write_to_png(surface, out.c_str()) == CAIRO_STATUS_SUCCESS;
  cairo_surface_destroy(surface);
  return ok;
}

// Reads the companion's last state. Fresh (rewritten within 6 s) means the Mac
// is connected; stale state still identifies a session that can be stopped
// locally while the Mac is offline.
static DashState readState(const std::string &path) {
  DashState s;
  struct stat st {};
  if (stat(path.c_str(), &st) != 0) {
    // With no Mac state, Stop is still a useful Frame-local orphan cleanup.
    s.workspaceStopAvailable = true;
    return s;
  }
  const bool fresh = time(nullptr) - st.st_mtime <= 6;
  json_object *j = json_object_from_file(path.c_str());
  if (!j) return s;
  json_object *v = nullptr;
  s.macConnected = fresh && json_object_object_get_ex(j, "connected", &v) && json_object_get_boolean(v);
  json_object *ws = nullptr;
  if (json_object_object_get_ex(j, "workspace", &ws)) {
    if (json_object_object_get_ex(ws, "running", &v)) s.workspaceRunning = json_object_get_boolean(v);
    if (json_object_object_get_ex(ws, "stopAvailable", &v)) s.workspaceStopAvailable = json_object_get_boolean(v);
    else s.workspaceStopAvailable = s.workspaceRunning;
    if (json_object_object_get_ex(ws, "busy", &v) && json_object_is_type(v, json_type_string)) {
      const std::string busy = json_object_get_string(v);
      s.workspaceBusy = busy == "starting" || busy == "stopping";
      s.busyText = busy == "stopping" ? "Stopping…" : "Starting…";
    }
    if (json_object_object_get_ex(ws, "label", &v)) s.workspaceLabel = json_object_get_string(v);
    if (json_object_object_get_ex(ws, "notice", &v) && json_object_is_type(v, json_type_string))
      s.notice = std::string(json_object_get_string(v)).substr(0, 200);
    if (json_object_object_get_ex(ws, "mode", &v) && json_object_is_type(v, json_type_string))
      s.spatial = std::string(json_object_get_string(v)) == "spatial";
    if (json_object_object_get_ex(ws, "session", &v) && json_object_is_type(v, json_type_string))
      s.session = json_object_get_string(v);
  }
  json_object *windows = nullptr;
  if (json_object_object_get_ex(j, "windows", &windows) && json_object_is_type(windows, json_type_array))
    for (size_t i = 0; i < json_object_array_length(windows) && i < 32; i++) {
      json_object *w = json_object_array_get_idx(windows, i), *t = nullptr, *k = nullptr;
      if (json_object_object_get_ex(w, "title", &t) && json_object_object_get_ex(w, "key", &k))
        s.windows.push_back({json_object_get_string(t), json_object_get_string(k)});
    }
  if (!fresh) {
    s.workspaceBusy = false;
    s.notice.clear();
    s.workspaceStopAvailable = true; // local owned-service cleanup needs no Mac link
    s.windows.clear(); // do not present stale window entries as live
  }
  json_object_put(j);
  return s;
}

static bool sameState(const DashState &a, const DashState &b) {
  if (a.macConnected != b.macConnected || a.workspaceRunning != b.workspaceRunning ||
      a.workspaceBusy != b.workspaceBusy || a.workspaceStopAvailable != b.workspaceStopAvailable ||
      a.spatial != b.spatial || a.busyText != b.busyText || a.locked != b.locked ||
      a.workspaceLabel != b.workspaceLabel || a.session != b.session || a.windows.size() != b.windows.size())
    return false;
  for (size_t i = 0; i < a.windows.size(); i++)
    if (a.windows[i].title != b.windows[i].title || a.windows[i].key != b.windows[i].key) return false;
  return true;
}

static bool writeRequest(const std::string &dir, const std::string &action, const std::string &key,
                         const std::string &session) {
  static int sequence = 0;
  const std::string file = dir + "/dash-" + std::to_string(getpid()) + "-" + std::to_string(++sequence) + ".json";
  const std::string tmp = file + ".tmp";
  FILE *f = fopen(tmp.c_str(), "w");
  if (!f) return false;
  json_object *j = json_object_new_object();
  if (!j) { fclose(f); unlink(tmp.c_str()); return false; }
  json_object_object_add(j, "action", json_object_new_string(action.c_str()));
  if (!key.empty()) json_object_object_add(j, "key", json_object_new_string(key.c_str()));
  if (!session.empty()) json_object_object_add(j, "session", json_object_new_string(session.c_str()));
  bool ok = fputs(json_object_to_json_string_ext(j, JSON_C_TO_STRING_PLAIN), f) >= 0 && fputc('\n', f) != EOF &&
            fflush(f) == 0 && fsync(fileno(f)) == 0;
  json_object_put(j);
  if (fclose(f) != 0) ok = false;
  if (ok && chmod(tmp.c_str(), 0600) != 0) ok = false;
  if (ok && rename(tmp.c_str(), file.c_str()) != 0) ok = false;
  if (!ok) unlink(tmp.c_str());
  return ok;
}


static volatile sig_atomic_t stopping = 0;

int main(int argc, char **argv) {
  if (argc == 5 && std::strcmp(argv[1], "--render-mock") == 0) {
    DashRenderer r;
    const std::string which = argv[4];
    return renderPng(r, mockState(which), which == "running" ? "close:com.frame-workspace.native.1000.panel-w212" : "",
                     argv[3], argv[2]).empty() ? 1 : 0;
  }
  std::string statePath, requestDir, images = "/tmp", layout = "tiles", exitHelper;
  for (int i = 1; i + 1 < argc; i += 2) {
    const std::string a = argv[i], v = argv[i + 1];
    if (a == "--state") statePath = v;
    else if (a == "--request-dir") requestDir = v;
    else if (a == "--images") images = v;
    else if (a == "--layout") layout = v == "list" ? "list" : "tiles";
    else if (a == "--exit-helper") exitHelper = v;
  }
  if (exitHelper.empty()) {
    const char *dataHome = std::getenv("XDG_DATA_HOME");
    const char *home = std::getenv("HOME");
    exitHelper = std::string(dataHome && *dataHome ? dataHome :
                             (home && *home ? std::string(home) + "/.local/share" : "/.local/share")) +
                 "/frame-workspace/native/workspace-exit-on-frame.py";
  }
  if (statePath.empty() || requestDir.empty()) {
    fprintf(stderr, "usage: frame-workspace-dash --state FILE --request-dir DIR [--images DIR] [--layout tiles|list]\n");
    return 2;
  }
  signal(SIGTERM, [](int) { stopping = 1; });
  signal(SIGINT, [](int) { stopping = 1; });
  void *lib = dlopen("/opt/steamvr/bin/linuxarm64/libopenvr_api.so", RTLD_NOW | RTLD_LOCAL);
  if (!lib) lib = dlopen("libopenvr_api.so", RTLD_NOW | RTLD_LOCAL);
  if (!lib) { fprintf(stderr, "[dash] OpenVR loader unavailable\n"); return 1; }
  auto init = reinterpret_cast<intptr_t (*)(EVRInitError *, EVRApplicationType)>(dlsym(lib, "VR_InitInternal"));
  auto get = reinterpret_cast<intptr_t (*)(const char *, EVRInitError *)>(dlsym(lib, "VR_GetGenericInterface"));
  auto shutdown = reinterpret_cast<void (*)()>(dlsym(lib, "VR_ShutdownInternal"));
  EVRInitError e = EVRInitError_VRInitError_None;
  init(&e, EVRApplicationType_VRApplication_Overlay);
  if (e) { fprintf(stderr, "[dash] OpenVR init failed %d\n", e); return 1; }
  auto *ov = reinterpret_cast<VR_IVROverlay_FnTable *>(get("FnTable:IVROverlay_027", &e));
  if (!ov) { fprintf(stderr, "[dash] overlay interface unavailable\n"); return 1; }
  VROverlayHandle_t main = 0, thumb = 0;
  char key[] = "com.frame-workspace.dash", name[] = "Frame Workspace";
  if (ov->CreateDashboardOverlay(key, name, &main, &thumb)) {
    fprintf(stderr, "[dash] could not create the dashboard overlay\n");
    return 1;
  }
  // Two panel images in turn, so SteamVR never reuses a cached copy of the last one.
  const std::string panelPngs[2] = {images + "/panel-a.png", images + "/panel-b.png"};
  const std::string thumbPng = images + "/thumbnail.png";
  int nextPng = 0;
  if (renderThumbnail(thumbPng)) ov->SetOverlayFromFile(thumb, const_cast<char *>(thumbPng.c_str()));
  ov->SetOverlayWidthInMeters(main, 1.6f);
  ov->SetOverlayInputMethod(main, VROverlayInputMethod_Mouse);
  fprintf(stderr, "[dash] ready\n");

  DashRenderer renderer;
  DashState state = readState(statePath), shown;
  std::string hover, shownHover = "-";
  std::vector<DashButton> buttons;
  // A press waits for the companion's next state (at most 8 s), so a second
  // press can't repeat it meanwhile.
  bool pending = false;
  std::string pendingAction;
  double lastRead = 0, pressedAt = 0;
  auto now = [] { timespec t{}; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec + t.tv_nsec / 1e9; };
  bool first = true;
  while (!stopping) {
    if (now() - lastRead > 0.25) {
      lastRead = now();
      DashState next = readState(statePath);
      if (!sameState(next, state) || now() - pressedAt > 8) pending = false;
      state = next;
    }
    DashState drawn = state;
    if (pending) {
      drawn.locked = true;
      if (pendingAction == "workspace_start" || pendingAction == "workspace_stop") {
        drawn.workspaceBusy = true;
        drawn.busyText = pendingAction == "workspace_stop" ? "Stopping…" : "Starting…";
      }
    }
    if (first || !sameState(drawn, shown) || hover != shownHover) {
      const std::string &png = panelPngs[nextPng];
      nextPng ^= 1;
      buttons = renderPng(renderer, drawn, hover, layout, png);
      if (!buttons.empty()) { // every state has at least one button, enabled or not
        HmdVector2_t scale{{float(DashRenderer::width), float(renderer.height)}};
        ov->SetOverlayMouseScale(main, &scale);
        ov->SetOverlayFromFile(main, const_cast<char *>(png.c_str()));
      }
      shown = drawn;
      shownHover = hover;
      first = false;
    }
    VREvent_t ev{};
    while (ov->PollNextOverlayEvent(main, &ev, sizeof ev)) {
      // OpenVR mouse coordinates start at the bottom left; the image starts at the top.
      const double x = ev.data.mouse.x, y = renderer.height - ev.data.mouse.y;
      if (ev.eventType == EVREventType_VREvent_MouseMove) {
        hover.clear();
        for (auto &b : buttons)
          if (b.enabled && b.contains(x, y)) hover = b.action + ":" + b.key;
      } else if (ev.eventType == EVREventType_VREvent_FocusLeave) {
        hover.clear();
      } else if (ev.eventType == EVREventType_VREvent_MouseButtonDown && !pending) {
        for (auto &b : buttons)
          if (b.enabled && b.contains(x, y)) {
            if (b.action == "workspace_stop") {
              // First persist the Mac restore request in the dashboard's normal
              // request channel. The detached helper then does only the local
              // close; failed exec leaves the durable request for the companion.
              const bool requestSaved = state.session.empty()
                  ? false : writeRequest(requestDir, b.action, b.key, state.session);
              const std::string operation = state.session.empty() ? "exit-current" :
                                            requestSaved ? "stop" : "exit";
              const bool helperStarted = spawnLocalExit(exitHelper, requestDir, state.session, operation);
              const bool fallbackSaved = requestSaved ||
                  (!state.session.empty() && writeRequest(requestDir, b.action, b.key, state.session)) ||
                  (state.session.empty() && !helperStarted && writeRequest(requestDir, b.action, b.key, state.session)) ||
                  (state.session.empty() && helperStarted);
              if (!fallbackSaved)
                fprintf(stderr, "[dash] could not save the Mac Stop request or start its local helper\n");
              else if (requestSaved && !helperStarted)
                fprintf(stderr, "[dash] Mac restore was queued; local viewer cleanup could not be started\n");
              state.workspaceRunning = false;
              state.workspaceStopAvailable = true;
              state.workspaceBusy = true;
              state.busyText = "Stopping…";
              state.windows.clear();
            } else {
              writeRequest(requestDir, b.action, b.key, state.session);
            }
            pending = true;
            pressedAt = now();
            pendingAction = b.action;
            fprintf(stderr, "[dash] %s\n", b.action.c_str());
          }
      } else if (ev.eventType == EVREventType_VREvent_Quit) {
        stopping = 1;
      }
    }
    timespec t{0, 16000000};
    nanosleep(&t, nullptr);
  }
  ov->DestroyOverlay(main);
  shutdown();
  return 0;
}
