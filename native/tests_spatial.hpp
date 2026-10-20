#pragma once
#include "spatial.hpp"
namespace fw {
inline void selfTestSpatial() {
  auto check = [](bool ok, const char *why) {
    if (!ok)
      throw std::runtime_error(why);
  };
  Placement near, far;
  near.matrix = spatialIdentity(); near.matrix.m[2][3] = -1;
  far.matrix = spatialIdentity(); far.matrix.m[2][3] = -2;
  std::vector<PanelSurface> overlapping{{"near", near, 1}, {"far", far, 1}};
  EyeRay center{{0, 0, 0}, {0, 0, -1}};
  check(spatialPanelPick(center, overlapping, "far").key == "far",
        "foreground picking matches render order across different depths");
  check(spatialPanelPick(center, overlapping, "").key == "near",
        "ordinary panels still pick nearest surface");
  far.matrix.m[0][3] = 4;
  overlapping[1].placement = far;
  check(spatialPanelPick(center, overlapping, "far").key == "near",
        "outside foreground panel picks another window");
  auto state = SpatialState::parse(
      R"({"mode":"spatial","apps":[{"bundleID":"b","name":"Terminal","icon":"../bad.png","running":true,"windows":[{"key":"b.2","title":"Two"},{"key":"b.1","title":"One"}]},{"bundleID":"a","name":"Safari","windows":[{"key":"a.1","title":"Page"}]}],"switcher":{"open":true,"index":2,"seq":12,"chosen":"a.1"}})");
  check(state.apps[0].id == "b" && state.windows[0].key == "b.2" &&
            state.windows[2].key == "a.1",
        "companion ordering preserved");
  check(state.apps[0].icon.empty() && state.seq == 12 &&
            state.chosen == "a.1" && state.index == 2,
        "safe icons and chosen contract");
  auto tiles = spatialLayout(state, spatialAnchor(spatialIdentity(), 1.3f, 0),
                             state.apps[0].id);
  for (size_t i = 1; i < tiles.size(); ++i) {
    const auto &t = tiles[i];
    EyeRay ray;
    for (int k = 0; k < 3; ++k) {
      ray.origin[k] = t.surface.placement.matrix.m[k][3] +
                      t.surface.placement.matrix.m[k][2];
      ray.direction[k] = -t.surface.placement.matrix.m[k][2];
    }
    auto hit = surfacePick(ray, t.surface);
    check(hit && std::abs(hit.u - .5) < 1e-5 && std::abs(hit.v - .5) < 1e-5,
          "spatial rendered surface ray round trip");
  }
  state.spatial = false;
  check(spatialLayout(state, spatialIdentity()).empty(),
        "desktop hides all spatial UI");
  state.spatial = true;
  for (int i = 0; i < 20; ++i)
    state.windows.push_back({"extra" + std::to_string(i), "Window", "App"});
  state.index = 22;
  tiles = spatialLayout(state, spatialIdentity());
  int cards = 0;
  bool selected = false;
  for (auto &t : tiles)
    if (t.kind == SpatialTile::Window) {
      ++cards;
      selected |= t.index == 22;
    }
  check(cards == 7 && selected, "large strip keeps selection visible");
  SpatialNavigation navigation;
  state.open = true;
  state.index = 0;
  state.seq = 10;
  bool grip[2]{true, false};
  navigation.update(state, grip);
  check(state.index == 22, "previous wraps to last window beyond viewport");
  grip[0] = false;
  navigation.update(state, grip);
  grip[1] = true;
  navigation.update(state, grip);
  check(state.index == 0, "next wraps to first");
  for (int i = 0; i < 9; ++i) {
    grip[1] = false;
    navigation.update(state, grip);
    grip[1] = true;
    navigation.update(state, grip);
  }
  check(state.index == 9, "controller reaches off-screen windows");
  state.index = 2;
  navigation.reconcile(state);
  check(state.index == 9, "same-sequence app refresh keeps local index");
  state.seq = 11;
  state.index = 3;
  navigation.reconcile(state);
  check(state.index == 3, "new Mac sequence wins local traversal");
  auto ordered = state;
  auto before = spatialLayout(ordered, spatialIdentity(), "b");
  std::string dockB;
  for (auto &t : before)
    if (t.kind == SpatialTile::Dock && t.index == 0)
      dockB = t.surface.key;
  std::swap(ordered.apps[0], ordered.apps[1]);
  auto after = spatialLayout(ordered, spatialIdentity(), "b");
  bool sameDock = false, correctMenu = false;
  for (auto &t : after) {
    if (t.kind == SpatialTile::Dock && ordered.apps[t.index].id == "b")
      sameDock = t.surface.key == dockB;
    if (t.kind == SpatialTile::Menu)
      correctMenu = t.surface.key.find("menu:b:") == 0;
  }
  check(sameDock && correctMenu && spatialAppIndex(ordered, "b") == 1,
        "dock press and menu stay with bundle after reorder");
  ordered.apps.erase(ordered.apps.begin() + 1);
  auto absent = spatialLayout(ordered, spatialIdentity(), "b");
  for (auto &t : absent)
    check(t.kind != SpatialTile::Menu, "removed menu app dismisses menu");
  SpatialVisibility visibility;
  state.open = true;
  state.seq = 3;
  visibility.request(false, 3);
  visibility.reconcile(state);
  check(!state.open, "app refresh cannot reopen local selection");
  state.open = true;
  state.seq = 4;
  visibility.reconcile(state);
  check(state.open, "new keyboard sequence clears local override");
  SpatialPress press;
  std::string hits[2]{"", "dock1"};
  bool down[2]{false, true};
  check(press.update(hits, down, 0).click.empty(), "press waits for release");
  check(press.update(hits, down, 501).hold == "dock1",
        "hold opens app menu once");
  check(press.update(hits, down, 600).hold.empty(), "held menu not repeated");
  down[1] = false;
  check(press.update(hits, down, 650).click.empty(),
        "hold release never opens app");
  down[1] = true;
  press.update(hits, down, 700);
  down[1] = false;
  check(press.update(hits, down, 750).click == "dock1", "short trigger click");
  down[1] = true;
  hits[1] = "";
  press.update(hits, down, 800);
  hits[1] = "dock1";
  press.update(hits, down, 850);
  down[1] = false;
  check(press.update(hits, down, 900).click.empty(),
        "entering UI with held trigger never clicks");
  down[1] = true;
  hits[1] = "dock:b";
  press.update(hits, down, 1000);
  hits[1] = "dock:a";
  press.update(hits, down, 1100);
  down[1] = false;
  check(press.update(hits, down, 1200).click.empty(),
        "reordered app under held trigger cannot receive click");
  down[1] = true;
  hits[1] = "";
  check(press.update(hits, down, 1300).outside,
        "outside trigger dismisses menu");
  HostFocus f;
  f.feedback["one"] = {};
  f.feedback["two"] = {};
  PanelPick one{"one", .5, .5, 1}, two{"two", .5, .5, 1};
  check(!f.update(one, .05, "eye") && !f.update(one, .05, "eye"),
        "early gaze does not select");
  auto glow = f.feedback["one"].glow;
  f.update({}, .02, "eye");
  check(f.feedback["one"].glow < glow, "early gaze fades");
  for (int i = 0; i < 4; ++i)
    check(!f.update(one, .05, "eye"), "residual glow cannot shorten dwell");
  check(f.update(one, .05, "eye"), "quarter second gaze confirms");
  f.activate("one");
  for (int i = 0; i < 4; ++i)
    check(!f.update(two, .05, "eye"), "second target waits");
  check(!f.update(two, .05, "controller"),
        "input source transition restarts dwell");
  for (int i = 0; i < 3; ++i)
    f.update(two, .05, "controller");
  check(f.update(two, .05, "controller"), "controller fallback confirms");
  f.activate("two");
  check(!f.feedback["one"].focused && f.feedback["two"].focused,
        "one focused bezel only");
  f.remove("two");
  check(f.active.empty(), "removed focused panel clears state");
}
} // namespace fw
