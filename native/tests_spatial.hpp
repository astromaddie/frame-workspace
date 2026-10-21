#pragma once
#include "spatial.hpp"
namespace fw {
inline void selfTestSpatial() {
  auto check = [](bool ok, const char *why) {
    if (!ok)
      throw std::runtime_error(why);
  };
  const std::string spec = R"({"version":1,"generation":"test","urlFile":"/tmp/view","overlayKey":"panel.test","controlFile":"/tmp/control","stateFile":"/tmp/state","poseFile":"/tmp/pose","requestDir":"/tmp/requests"})";
  check(PanelSpec::parse(spec).role == PanelRole::Window, "default window role");
  for (const auto &role : {"window", "menubar", "status", "menu"}) {
    auto j = json(spec);
    json_object_object_add(j.get(), "role", json_object_new_string(role));
    check(PanelSpec::parse(json_object_to_json_string(j.get())).role == panelRole(role), "panel role parsing");
  }
  for (const auto &bad : {"other", "Window", "", "true"}) {
    auto j = json(spec);
    json_object_object_add(j.get(), "role", json_object_new_string(bad));
    bool rejected = false;
    try { PanelSpec::parse(json_object_to_json_string(j.get())); } catch (...) { rejected = true; }
    check(rejected, "invalid role rejected");
  }
  for (auto value : {"null", "true", "1", "[]", "{}"}) {
    auto j = json(spec), invalid = json(value);
    json_object_object_add(j.get(), "role", json_object_get(invalid.get()));
    bool rejected = false;
    try { PanelSpec::parse(json_object_to_json_string(j.get())); } catch (...) { rejected = true; }
    check(rejected, "non-string panel role rejected");
  }
  auto metadata = SurfaceMessage::parse(R"({"t":"surface","v":1,"seq":7,"visible":true,"rect":[10,20,600,36],"anchor":"menubar","anchorRect":[10,20,600,36],"menusWidth":600,"hot":true})");
  metadata.received = 10;
  check(!surfacePictureReady(metadata, 7, 11) && !surfacePictureReady(metadata, 6, 11) &&
        !surfacePictureReady(metadata, 8, 9) && surfacePictureReady(metadata, 8, 11),
        "surface requires uploaded newer sequence arriving after message");
  auto hidden = SurfaceMessage::parse(R"({"t":"surface","v":1,"seq":8,"visible":false})");
  check(!surfacePictureReady(hidden, 9, 12) && sequenceNewer(0, UINT32_MAX), "hide and sequence wrap");
  for (const auto &bad : {R"({"t":"surface","v":2,"seq":0,"visible":false})",
                         R"({"t":"surface","v":1,"seq":-1,"visible":false})",
                         R"({"t":"surface","v":1,"seq":1.5,"visible":false})",
                         R"({"t":"surface","v":1,"seq":1,"visible":true,"rect":[0,0,0,36]})"}) {
    bool rejected = false;
    try { SurfaceMessage::parse(bad); } catch (...) { rejected = true; }
    check(rejected, "malformed surface metadata rejected");
  }
  PanelSurface window{"window", {}, .8f};
  window.placement.width = 1.2f; window.placement.matrix = spatialIdentity();
  auto slim = menuStripPlacement(window, metadata, .001f, 0);
  auto wide = menuStripPlacement(window, metadata, .001f, 1);
  check(std::abs(slim.placement.width - .42f) < 1e-6 && std::abs(slim.height - .0216f) < 1e-6 &&
        std::abs(wide.placement.width - .6f) < 1e-6 && std::abs(wide.height - .036f) < 1e-6,
        "menu strip slim and full point scale");
  check(std::abs(wide.placement.matrix.m[0][3] - wide.placement.width/2 + .6f) < 1e-6 &&
        wide.placement.matrix.m[1][3] > .4f, "menu strip follows top left bezel");
  MenuStripReveal reveal;
  reveal.update(true, false, 0, .12f);
  check(reveal.wide > .8f, "menu strip uses quick chrome reveal");
  reveal.update(false, true, 800, .1f);
  reveal.update(false, false, 1799, .1f);
  check(reveal.wide == 1, "visible menu and one second grace hold wide");
  reveal.update(false, false, 1801, .1f);
  reveal.update(false, false, 1901, .1f);
  check(reveal.wide == 0, "menu strip slims after grace");
  SpatialState shelfState; shelfState.spatial = true;
  shelfState.apps.push_back({"app", "App", "", true, false});
  auto shelfTiles = spatialLayout(shelfState, spatialIdentity());
  auto status = statusStripPlacement(shelfTiles, metadata, .001f);
  check(status.height > 0 && status.placement.matrix.m[0][3] > .3f &&
        std::abs(status.placement.matrix.m[1][3] + .48f) < 1e-6,
        "status strip uses actual shelf right end");
  check(statusStripPlacement({}, metadata, .001f).height == 0, "no detached status without shelf");
  metadata.rect = {40, 62, 200, 120}; metadata.anchorRect = {10,20,600,36};
  auto dropdown = surfacePlacement(wide, metadata, .001f);
  check(std::abs(dropdown.placement.matrix.m[0][3] - (wide.placement.matrix.m[0][3] - .3f + .03f + .1f)) < 1e-6 &&
        std::abs(dropdown.placement.matrix.m[1][3] - (wide.placement.matrix.m[1][3] - .018f - .006f - .06f)) < 1e-6 &&
        std::abs(dropdown.placement.matrix.m[2][3] - wide.placement.matrix.m[2][3] - .01f) < 1e-6,
        "surface offset uses global anchor bottom and one centimetre front");
  metadata.anchor = "status";
  auto above = surfacePlacement(status, metadata, .001f);
  check(above.placement.matrix.m[1][3] - above.height/2 > status.placement.matrix.m[1][3] + status.height/2,
        "status anchored menu opens above shelf");
  metadata.rect.y = 20;
  above = surfacePlacement(status, metadata, .001f);
  check(above.placement.matrix.m[1][3] - above.height/2 >= status.placement.matrix.m[1][3] + status.height/2 - 1e-6,
        "padded status panel cannot open below shelf");
  window.placement.matrix.m[2][3] = -2;
  auto auxiliary = window; auxiliary.key = "auxiliary"; auxiliary.role = PanelRole::Menu;
  auxiliary.placement.matrix.m[2][3] = -1;
  EyeRay focusRay{{0,0,0},{0,0,-1}};
  check(spatialPanelPick(focusRay, {window, auxiliary}, "auxiliary").key == "window" &&
        pickPanel(focusRay, {auxiliary}).key == "auxiliary" && !focusSurface(auxiliary),
        "auxiliary takes hits but is excluded from focus surfaces");
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
