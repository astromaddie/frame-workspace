#pragma once
#include "host.hpp"
namespace fw {
inline void selfTestHost() {
  auto check = [](bool ok, const char *message) { if (!ok) throw std::runtime_error(message); };
  const std::string raw = R"({"version":1,"generation":"fc12345678","urlFile":"/tmp/panel/view.url","overlayKey":"panel.a","overlayName":"A","controlFile":"/tmp/panel/control.json","stateFile":"/tmp/panel/state.json","poseFile":"/tmp/panel/pose.json","requestDir":"/tmp/requests","decoder":"v4l2","conversion":"gpu"})";
  auto a = PanelSpec::parse(raw), b = a;
  b.key = "panel.b"; b.generation = "fc98765432";
  PanelBook book;
  std::map<std::string, PanelSpec> desired{{"a.json", a}, {"b.json", b}};
  auto plan = book.plan(desired);
  check(plan.remove.empty() && plan.add == std::vector<std::string>{"a.json", "b.json"}, "host add bookkeeping");
  for (const auto &path : plan.add) book.added(path, desired.at(path));
  check(book.plan(desired).add.empty(), "unchanged and terminal host generations do not restart");
  desired.erase("a.json"); desired["b.json"].generation = "next";
  plan = book.plan(desired);
  check(plan.remove == std::vector<std::string>{"a.json", "b.json"} && plan.add == std::vector<std::string>{"b.json"},
        "host remove and replace before add");
  for (const auto &path : plan.remove) book.removed(path);
  for (const auto &path : plan.add) book.added(path, desired.at(path));
  check(book.generations.size() == 1 && book.plan(desired).add.empty(), "host replacement retains one owner");
  desired.clear(); plan = book.plan(desired);
  check(plan.remove.size() == 1 && plan.add.empty(), "empty host releases its last panel");
  for (auto bad : {std::string("{}"), std::string("[]"), std::string("null"),
                  std::string(R"({"version":2})")}) {
    bool rejected = false;
    try { PanelSpec::parse(bad); } catch (...) { rejected = true; }
    check(rejected, "invalid host spec rejected");
  }
  for (const auto &change : std::vector<std::pair<std::string, std::string>>{
           {"overlayKey", "bad\"key"}, {"generation", "bad/key"}, {"urlFile", "relative.url"},
           {"stateFile", ""}, {"decoder", "auto"}, {"conversion", "auto"}, {"initialPose", "{}"}}) {
    auto j = json(raw); json_object_object_add(j.get(), change.first.c_str(), json_object_new_string(change.second.c_str()));
    bool rejected = false;
    try { PanelSpec::parse(json_object_to_json_string(j.get())); } catch (...) { rejected = true; }
    check(rejected, "invalid host spec field rejected");
  }
  PanelSurface flat{"flat", {}, 1.2f};
  flat.placement.width = 2; flat.placement.matrix = {{{1,0,0,0}, {0,1,0,0}, {0,0,1,-2}}};
  EyeRay ray{{0,0,0}, {0,0,-1}};
  auto hit = surfacePick(ray, flat);
  check(hit && std::fabs(hit.u-.5f)<1e-6 && std::fabs(hit.v-.5f)<1e-6 && std::fabs(hit.distance-2)<1e-6,
        "host flat gaze centre and distance");
  ray.origin[0] = -.8f; ray.origin[1] = .45f;
  hit = surfacePick(ray, flat);
  check(hit && std::fabs(hit.u-.1f)<1e-6 && std::fabs(hit.v-.125f)<1e-6, "gaze UV is right and down from top left");
  ray.origin[0] = 1.1f; check(!surfacePick(ray, flat), "gaze outside window ignored");
  ray = {{0,0,-3}, {0,0,1}}; check(!surfacePick(ray, flat), "back face gaze ignored");
  ray = {{0,0,0}, {1,0,0}}; check(!surfacePick(ray, flat), "parallel gaze ignored");
  ray.direction[0] = NAN; check(!surfacePick(ray, flat), "invalid gaze ignored");
  // Every bend, including uneven and vertical halves, round-trips original UV
  // through a translated/rotated standing-space panel.
  for (bool vertical : {false, true}) for (const auto &amounts : std::vector<std::array<float,2>>{
           {0,0}, {.2f,.2f}, {0,.35f}, {.4f,.1f}}) {
    PanelSurface bent = flat;
    bent.placement.bendVertical = vertical;
    bent.placement.bendA = amounts[0]; bent.placement.bendB = amounts[1];
    const float c = std::cos(.4f), s = std::sin(.4f);
    bent.placement.matrix = {{{c,0,s,1.3f}, {0,1,0,1.6f}, {-s,0,c,-3}}};
    for (float u : {.1f,.49f,.51f,.9f}) for (float v : {.15f,.7f}) {
      auto local = bendPointXY(bendShape(bent.placement, bent.height), (u-.5f)*2, (.5f-v)*bent.height, 0);
      auto world = multiply(bent.placement.matrix, local);
      EyeRay eye;
      for (int i=0; i<3; ++i) { eye.origin[i]=world.m[i][3]+world.m[i][2]; eye.direction[i]=-world.m[i][2]; }
      auto pick = surfacePick(eye, bent);
      check(pick && std::fabs(pick.u-u)<1e-4 && std::fabs(pick.v-v)<1e-4 && std::fabs(pick.distance-1)<1e-4,
            "host gaze roundtrips rotated asymmetric and vertical bends");
    }
  }
  PanelSurface near = flat; near.key = "near"; near.placement.matrix.m[2][3] = -1;
  ray = {{0,0,0}, {0,0,-3}};
  hit = pickPanel(ray, {flat,near});
  check(hit.key == "near" && std::fabs(hit.distance-1)<1e-6, "nearest overlapping window wins with normalized ray");
  // Adjacent panels: small jitter across a seam keeps the active panel.
  PanelSurface right = flat; right.key = "right"; right.placement.matrix.m[0][3] = 2;
  ray = {{1.005f,0,0},{0,0,-1}};
  check(pickPanel(ray,{flat,right},"flat").key == "flat", "gaze edge hysteresis retains active window");
  ray.origin[0] = 1.04f;
  check(pickPanel(ray,{flat,right},"flat").key == "right", "gaze past edge hysteresis selects neighbour");
  EyeRay eyes[2]{{{-.03f,0,0},{.03f,0,-1}}, {{.03f,0,0},{-.03f,0,-1}}}, combined;
  check(binocularRay(eyes,combined) && std::fabs(combined.origin[0])<1e-6 && std::fabs(combined.direction[0])<1e-6 &&
        combined.direction[2] == -1, "binocular gaze combines both eyes");
  FocusDwell dwell;
  PanelPick one{"flat",.2f,.3f,2}, two{"right",.7f,.8f,2};
  check(!dwell.settled(one,0,"eye") && !dwell.settled(one,179,"eye") && dwell.settled(one,180,"eye"),
        "gaze focus waits for dwell");
  dwell.activated(one);
  check(!dwell.settled(one,200,"eye") && !dwell.settled(two,250,"eye") && !dwell.settled({},400,"eye") &&
        !dwell.settled(two,450,"eye") && !dwell.settled(two,600,"controller") && !dwell.settled(two,779,"controller") &&
        dwell.settled(two,780,"controller"), "dwell resets after misses and tracking source changes");
  dwell.activated(two); dwell.removed("right");
  check(dwell.active.empty() && !dwell.settled(two,900,"eye"), "removed focused panel clears focus identity");
}
} // namespace fw
