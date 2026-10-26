#pragma once
#include "spatial.hpp"
namespace fw {
// The pinned, movable, magnifying shelf (FW-15 D1).
inline void selfTestShelf() {
  auto check = [](bool ok, const char *why) {
    if (!ok)
      throw std::runtime_error(why);
  };
  auto yawed = [](float yaw, float x, float y, float z) {
    auto m = spatialIdentity();
    m.m[0][0] = std::cos(yaw); m.m[0][2] = std::sin(yaw);
    m.m[2][0] = -std::sin(yaw); m.m[2][2] = std::cos(yaw);
    m.m[0][3] = x; m.m[1][3] = y; m.m[2][3] = z;
    return m;
  };
  // A ray at (x, y) of a pose frame, one metre in front, pointing at its plane.
  auto rayAt = [](const HmdMatrix34_t &pose, float x, float y) {
    EyeRay ray;
    for (int r = 0; r < 3; ++r) {
      ray.origin[r] = pose.m[r][3] + pose.m[r][0] * x + pose.m[r][1] * y + pose.m[r][2];
      ray.direction[r] = -pose.m[r][2];
    }
    return ray;
  };
  SpatialState st; st.spatial = true;
  for (const char *id : {"a", "b", "c", "d", "e"})
    st.apps.push_back({id, id, "", true, false});

  // Anchor: in front of the head, same as the old default, and pinned there.
  auto head = yawed(.4f, .3f, 1.6f, -.2f);
  auto front = shelfFrontOf(head), old = spatialAnchor(head, 1.3f, 0);
  for (int r = 0; r < 3; ++r) for (int c = 0; c < 4; ++c)
    check(std::abs(front.m[r][c] - old.m[r][c]) < 1e-6, "shelf default sits where the old anchor did");
  ShelfPin pin;
  check(pin.update(false, head) == ShelfPin::Unchanged && !pin.placed, "no placement without a head pose");
  check(pin.update(true, head) == ShelfPin::Defaulted && pin.placed, "first pose places the shelf");
  auto first = pin.pose;
  for (float yaw : {.5f, 1.4f, 3.f})
    check(pin.update(true, yawed(yaw, 2, 1.5f, 1)) == ShelfPin::Unchanged &&
          std::memcmp(&pin.pose, &first, sizeof first) == 0, "pinned shelf never follows the head");

  // Saved pose: round trip, validation like window poses, plus handedness and range.
  auto dir = std::filesystem::temp_directory_path() / ("fw-shelf-" + std::to_string(getpid()));
  std::filesystem::create_directories(dir);
  const std::string file = (dir / "panels" / "spatial-shelf-pose.json").string();
  HmdMatrix34_t loaded{};
  check(!loadShelfPose(file, loaded), "missing shelf pose is not loaded");
  auto moved = yawed(-.7f, -1.2f, 1.1f, -2.5f);
  check(saveShelfPose(file, moved) && loadShelfPose(file, loaded), "shelf pose saves beside the panel poses");
  for (int r = 0; r < 3; ++r) for (int c = 0; c < 4; ++c)
    check(std::abs(loaded.m[r][c] - moved.m[r][c]) < 1e-5, "saved shelf pose round trips");
  ShelfPin restored;
  restored.pose = loaded; restored.placed = true;
  check(restored.update(true, head) == ShelfPin::Unchanged && std::abs(restored.pose.m[0][3] + 1.2f) < 1e-5,
        "saved shelf pose wins over the default");
  auto writeRaw = [&](const std::string &text) {
    std::ofstream out(file); out << text;
  };
  const std::string good = placementJSON([&] { Placement p; p.matrix = moved; return p; }());
  writeRaw(good); check(loadShelfPose(file, loaded), "valid pose file loads");
  auto sheared = moved; sheared.m[0][1] += .3f;
  auto mirrored = moved; for (int r = 0; r < 3; ++r) mirrored.m[r][0] = -mirrored.m[r][0];
  auto far = moved; far.m[2][3] = -40;
  for (const auto &bad : {sheared, mirrored, far}) {
    Placement p; p.matrix = bad;
    writeRaw(placementJSON(p));
    check(!loadShelfPose(file, loaded), "shelf pose with a sheared, mirrored or far matrix is refused");
    check(!saveShelfPose(file, bad), "unusable shelf pose is never saved");
  }
  for (const char *junk : {"", "{", "[]", "{\"version\":2}", "{\"version\":1,\"matrix\":[1,2]}"}) {
    writeRaw(junk); check(!loadShelfPose(file, loaded), "malformed shelf pose is refused");
  }
  std::filesystem::remove_all(dir);

  // Recenter: a companion request, a runtime zero-pose reset or a call brings it back.
  ShelfPin rc;
  rc.update(true, head);
  rc.observeSequence(7);
  check(!rc.recenterPending, "first recenter counter seen only sets the baseline");
  rc.observeSequence(7);
  rc.observeSequence(3);
  check(!rc.recenterPending, "repeated or older counters do nothing");
  auto elsewhere = yawed(2.2f, 0, 1.2f, 0);
  check(rc.update(true, elsewhere) == ShelfPin::Unchanged, "no recenter before it is asked");
  rc.observeSequence(4);
  check(rc.recenterPending, "newer counter requests a recenter");
  check(rc.update(false, elsewhere) == ShelfPin::Unchanged && rc.recenterPending, "recenter waits for a head pose");
  check(rc.update(true, elsewhere) == ShelfPin::Recentered && !rc.recenterPending, "recenter places it and asks to save");
  auto fresh = shelfFrontOf(elsewhere);
  check(std::abs(rc.pose.m[0][3] - fresh.m[0][3]) < 1e-6 && std::abs(rc.pose.m[2][3] - fresh.m[2][3]) < 1e-6 &&
        std::abs(rc.pose.m[1][1] - 1) < 1e-6, "recentered shelf is level, in front of the head");
  rc.observeResets(5);
  check(!rc.recenterPending, "first zero-pose count sets the baseline");
  rc.observeResets(6);
  check(rc.recenterPending, "zero-pose reset requests a recenter");
  auto parsed = SpatialState::parse(R"({"mode":"spatial","apps":[],"switcher":{"open":false,"index":0,"seq":0},"recenterSeq":12})");
  check(parsed.recenterSeq == 12 && SpatialState::parse(R"({"mode":"spatial"})").recenterSeq == 0,
        "recenter counter is read from the spatial state");

  // Hover timing: eased, with a grace delay and a larger region while enlarged.
  ShelfHover hover;
  double now = 0;
  const float dt = 1.f / 60;
  auto tick = [&](bool small, bool large) { now += dt * 1000; return hover.update(now, dt, small, large, true, 0); };
  check(std::abs(hover.scale() - shelfRestScale) < 1e-6, "shelf rests at the small size");
  for (int i = 0; i < 30; ++i) tick(false, true);
  check(!hover.active && hover.scale() == shelfRestScale, "the larger region alone cannot enlarge a resting shelf");
  float before = hover.scale(), biggestStep = 0;
  int frames = 0;
  while (hover.scale() < 1 && frames < 100) {
    tick(true, true); ++frames;
    biggestStep = std::max(biggestStep, hover.scale() - before);
    check(hover.scale() >= before, "enlarging never reverses");
    before = hover.scale();
  }
  check(frames >= 11 && frames <= 14, "enlarging takes about 200 ms");
  check(biggestStep < .1f, "enlarging is eased, not a snap");
  for (int i = 0; i < 20; ++i) tick(false, true);
  check(hover.active && hover.scale() == 1, "the larger region holds it enlarged");
  tick(false, false);
  double left = now;
  while (now - left < 450) tick(false, false);
  check(hover.active && hover.scale() == 1, "grace delay: still full size 450 ms after the ray left");
  tick(true, true);
  check(hover.active, "ray returning inside grace keeps it enlarged");
  left = now;
  tick(false, false);
  while (now - left < 520) tick(false, false);
  check(!hover.active, "shrinks after the grace delay");
  float mid = hover.scale();
  for (int i = 0; i < 6; ++i) tick(false, false);
  check(hover.scale() < mid && hover.scale() > shelfRestScale, "shrinking is gradual");
  const float shrinking = hover.scale();
  tick(true, true);
  check(hover.active && hover.scale() >= shrinking && hover.scale() - shrinking < .05f,
        "re-entering mid-shrink reverses without a snap");
  for (int i = 0; i < 30; ++i) tick(false, false);
  now += 1000; for (int i = 0; i < 40; ++i) tick(false, false);
  check(std::abs(hover.scale() - shelfRestScale) < 1e-6 && hover.magnify == 0, "settles at rest after leaving");
  // Magnification follows the pointer without snapping and starts under it.
  ShelfHover pointed;
  pointed.update(0, dt, true, true, true, .21f);
  check(std::abs(pointed.pointer - .21f) < 1e-6 && pointed.magnify > 0 && pointed.magnify < .2f,
        "magnification starts gently where the ray lands");
  for (int i = 0; i < 30; ++i) pointed.update(i * 16.f, dt, true, true, true, .21f);
  pointed.update(500, dt, true, true, true, -.2f);
  check(pointed.pointer < .21f && pointed.pointer > -.2f, "pointer follows with light smoothing");

  // Icon magnification: the point under the pointer stays put, falloff by distance.
  const float reach = shelfIconReach * .105f;
  check(std::abs(shelfMagnifiedX(.13f, .13f, shelfIconBoost, reach) - .13f) < 1e-6, "point under pointer does not move");
  float lastX = -9;
  for (float u = -.8f; u <= .8f; u += .01f) {
    float x = shelfMagnifiedX(u, .1f, shelfIconBoost, reach);
    check(x > lastX, "magnified row keeps its order");
    lastX = x;
  }
  check(std::abs(shelfMagnifiedX(-.5f, .1f, shelfIconBoost, reach) - (-.5f - shelfIconBoost * reach / 2)) < 1e-6 &&
        std::abs(shelfMagnifiedX(.8f, .1f, shelfIconBoost, reach) - (.8f + shelfIconBoost * reach / 2)) < 1e-6,
        "far icons only shift outward as one block");
  ShelfView pinnedView; pinnedView.pinned = true; pinnedView.pose = yawed(.35f, .4f, 1.25f, -1.1f);
  auto resting = pinnedView; resting.scale = shelfRestScale;
  auto fullView = pinnedView;
  auto restTiles = spatialLayout(st, front, "", nullptr, resting);
  auto fullTiles = spatialLayout(st, front, "", nullptr, fullView);
  check(restTiles.front().kind == SpatialTile::Shelf &&
        std::abs(restTiles.front().surface.placement.width / fullTiles.front().surface.placement.width - shelfRestScale) < 1e-5 &&
        std::abs(restTiles.front().surface.height / fullTiles.front().surface.height - shelfRestScale) < 1e-5,
        "resting shelf is 60 percent of full size");
  auto defaults = spatialLayout(st, front);
  check(defaults.size() == fullTiles.size() - 1 && fullTiles.back().kind == SpatialTile::Move && !fullTiles.back().pickable &&
        std::abs(defaults.front().surface.placement.width - fullTiles.front().surface.placement.width) < 1e-6,
        "an unpinned layout is the old full-size shelf; pinned adds the move pill");
  auto widthOf = [&](const std::vector<SpatialTile> &tiles, int dock) {
    for (const auto &t : tiles) if (t.kind == SpatialTile::Dock && t.index == dock) return t.surface.placement.width;
    return 0.f;
  };
  auto bigView = pinnedView; bigView.magnify = 1; bigView.pointer = 0; // over the middle icon (c)
  auto bigTiles = spatialLayout(st, front, "", nullptr, bigView);
  const float base = widthOf(fullTiles, 2);
  check(widthOf(bigTiles, 2) > 1.3f * base && widthOf(bigTiles, 2) <= (1 + shelfIconBoost) * base + 1e-6f,
        "the icon under the pointer is magnified by about the boost");
  check(widthOf(bigTiles, 2) > widthOf(bigTiles, 1) && widthOf(bigTiles, 1) > widthOf(bigTiles, 0) &&
        std::abs(widthOf(bigTiles, 1) - widthOf(bigTiles, 3)) < 1e-5 && widthOf(bigTiles, 0) >= base - 1e-6f,
        "nearer icons grow more than distant ones");
  check(widthOf(bigTiles, 0) < 1.15f * base, "distant icons barely grow");
  auto bottomOf = [&](const std::vector<SpatialTile> &tiles, int dock) {
    for (const auto &t : tiles) if (t.kind == SpatialTile::Dock && t.index == dock) {
      auto local = multiply(inverseRigid(pinnedView.pose), t.surface.placement.matrix);
      return local.m[1][3] - t.surface.height / 2;
    }
    return 0.f;
  };
  check(std::abs(bottomOf(bigTiles, 2) - bottomOf(fullTiles, 2)) < 1e-5 &&
        std::abs(bottomOf(bigTiles, 0) - bottomOf(fullTiles, 0)) < 1e-5, "magnified icons grow upward from a level base");
  check(bigTiles.front().surface.placement.width > fullTiles.front().surface.placement.width &&
        bigTiles.front().surface.height == fullTiles.front().surface.height, "backing widens with the stretched row");
  // The status band and its picture shrink with the shelf and stay inside it.
  SurfaceMessage statusMessage = SurfaceMessage::parse(R"({"t":"surface","v":1,"seq":1,"visible":true,"rect":[0,0,186,30],"anchor":"none","anchorRect":[0,0,186,30]})");
  for (float k : {shelfRestScale, .8f, 1.f}) {
    auto view = pinnedView; view.scale = k;
    auto tiles = spatialLayout(st, front, "", &statusMessage, view);
    auto status = statusStripPlacement(tiles, statusMessage, .001f, k);
    const auto &shelf = tiles.front().surface;
    auto local = multiply(inverseRigid(shelf.placement.matrix), status.placement.matrix);
    check(std::abs(status.placement.width - .186f * k) < 1e-5 &&
          local.m[0][3] + status.placement.width / 2 <= shelf.placement.width / 2 + 1e-6f &&
          local.m[1][3] - status.height / 2 >= -shelf.height / 2 - 1e-6f,
          "status picture scales with the shelf and stays inside it");
  }
  // Picking uses the drawn geometry at every size and magnification.
  for (float k : {shelfRestScale, .7f, .85f, 1.f}) for (float amount : {0.f, .3f, 1.f}) for (float pointer : {-.2f, 0.f, .13f, .31f}) {
    auto view = pinnedView; view.scale = k; view.magnify = amount; view.pointer = pointer * k;
    auto tiles = spatialLayout(st, front, "", nullptr, view);
    for (size_t i = 0; i < tiles.size(); ++i) {
      if (tiles[i].kind != SpatialTile::Dock) continue;
      auto local = multiply(inverseRigid(view.pose), tiles[i].surface.placement.matrix);
      PanelPick pick;
      int hit = spatialPick(rayAt(view.pose, local.m[0][3], local.m[1][3]), tiles, &pick);
      check(hit == int(i) && std::abs(pick.u - .5f) < 1e-4 && std::abs(pick.v - .5f) < 1e-4,
            "a ray at an icon's drawn centre picks that icon at any size or magnification");
      // Just inside each drawn edge still picks it, just outside picks its neighbour or nothing else.
      for (float sign : {-1.f, 1.f}) {
        float edge = local.m[0][3] + sign * (tiles[i].surface.placement.width / 2 - .002f);
        check(spatialPick(rayAt(view.pose, edge, local.m[1][3]), tiles) == int(i),
              "icon edges pick the icon they bound");
      }
    }
    // Whatever the ray points at in the row picks the icon drawn there, and pointing
    // at the pointer itself picks the icon that was under it before it grew.
    float x = pointer * k;
    int under = -1;
    for (size_t i = 0; i < tiles.size(); ++i) if (tiles[i].kind == SpatialTile::Dock) {
      auto local = multiply(inverseRigid(view.pose), tiles[i].surface.placement.matrix);
      if (x >= local.m[0][3] - tiles[i].surface.placement.width / 2 && x < local.m[0][3] + tiles[i].surface.placement.width / 2) under = int(i);
    }
    int rested = -1;
    auto plain = spatialLayout(st, front, "", nullptr, [&] { auto v = view; v.magnify = 0; return v; }());
    for (size_t i = 0; i < plain.size(); ++i) if (plain[i].kind == SpatialTile::Dock) {
      auto local = multiply(inverseRigid(view.pose), plain[i].surface.placement.matrix);
      if (x >= local.m[0][3] - plain[i].surface.placement.width / 2 && x < local.m[0][3] + plain[i].surface.placement.width / 2) rested = int(i);
    }
    if (under >= 0)
      check(spatialPick(rayAt(view.pose, x, -shelfRowDrop), tiles) == under, "the ray picks the icon drawn under it");
    check(under == rested, "the icon under the pointer is the one it pointed at before magnifying");
  }
  // The move pill is never a pick target, but it is drawn and moves with the shelf.
  auto moveTile = fullTiles.back();
  check(spatialPick(rayAt(pinnedView.pose, 0, -.48f - .155f), fullTiles) != int(fullTiles.size() - 1), "pill overlay is not pickable");
  {
    auto local = multiply(inverseRigid(pinnedView.pose), moveTile.surface.placement.matrix);
    const auto &l = moveTile.layout;
    check(std::abs(local.m[0][3] + l.pillX()) < 1e-5 && local.m[1][3] < -shelfRowDrop &&
          std::abs(moveTile.surface.placement.width - l.chromeWidth()) < 1e-6, "move pill hangs centred under the full shelf");
  }

  // Moving it: reveal below the shelf, press the pill, carry it, release to save.
  ShelfMover mover;
  ShelfHandleFrame frame;
  {
    auto local = multiply(inverseRigid(pinnedView.pose), moveTile.surface.placement.matrix);
    frame = {local.m[0][3], local.m[1][3], 1.f, moveTile.layout, true};
  }
  const float pillY = frame.cy + moveTile.layout.rowY(); // pill centre in the pose frame
  const float pillXr = frame.ox + moveTile.layout.pillX();
  auto pose = pinnedView.pose;
  ShelfMover::Hand hands[2];
  auto controllerAt = [&](const HmdMatrix34_t &frameOfShelf, float x, float y) {
    HmdMatrix34_t c = frameOfShelf;
    for (int r = 0; r < 3; ++r) c.m[r][3] += frameOfShelf.m[r][0] * x + frameOfShelf.m[r][1] * y + frameOfShelf.m[r][2] * .8f;
    return c;
  };
  double t0 = 0;
  auto step = [&](double ms = 16) { t0 += ms; return mover.update(frame, pose, hands, true, false, t0, float(ms / 1000)); };
  hands[1] = {true, false, false, controllerAt(pose, 0, 0.4f)};
  for (int i = 0; i < 30; ++i) step();
  check(!mover.visible() && !mover.owns(1), "pill stays hidden while the laser is far away");
  hands[1].pose = controllerAt(pose, pillXr, pillY + .04f); // below the shelf, in the pill row zone
  for (int i = 0; i < 12; ++i) step();
  check(mover.visible() && mover.showing.amount > .99f, "pill slides out when the laser is below the shelf");
  hands[1].pose = controllerAt(pose, pillXr, pillY);
  step();
  check(mover.hot && mover.owns(1) && !mover.owns(0), "laser on the pill hands it to the shelf");
  hands[1].grab = true;
  step();
  check(mover.dragging() && mover.dragHand == 1, "trigger on the pill starts the move");
  auto startPose = pose;
  hands[1].pose = controllerAt(yawed(.2f, .3f, 0, .1f), pillXr, pillY);
  auto controllerStart = controllerAt(startPose, pillXr, pillY);
  hands[1].pose = controllerStart;
  hands[1].pose.m[0][3] += .5f; hands[1].pose.m[2][3] -= .25f; // the hand moves 50 cm right, 25 cm away
  bool ended = step();
  check(!ended && std::abs(pose.m[0][3] - startPose.m[0][3] - .5f) < 1e-4 && std::abs(pose.m[2][3] - startPose.m[2][3] + .25f) < 1e-4 &&
        std::abs(pose.m[0][0] - startPose.m[0][0]) < 1e-5, "shelf follows the controller rigidly");
  hands[1].pose.m[0][3] += 40; // absurd jump: pose stays valid and in range
  step();
  check(std::hypot(pose.m[0][3], pose.m[1][3], pose.m[2][3]) <= 10.f && shelfPoseUsable(pose), "move cannot leave the 10 m room");
  hands[1].pose.m[0][3] -= 40;
  step();
  hands[1].grab = false;
  ended = step();
  check(ended && !mover.dragging(), "release ends the move so the caller saves the pose");
  auto before2 = pose;
  hands[1].pose.m[0][3] += .3f;
  step();
  check(std::memcmp(&before2, &pose, sizeof pose) == 0, "shelf stays where it was put after release");
  // A squeeze moves it too, unless the switcher claims squeeze.
  ShelfMover grip; double tg = 0; auto gpose = pinnedView.pose; ShelfMover::Hand gh[2];
  gh[0] = {true, false, false, controllerAt(gpose, pillXr, pillY)};
  for (int i = 0; i < 12; ++i) { tg += 16; grip.update(frame, gpose, gh, false, true, tg, .016f); }
  gh[0].squeeze = true; tg += 16; grip.update(frame, gpose, gh, false, true, tg, .016f);
  check(!grip.dragging(), "squeeze does not move the shelf while the switcher uses it");
  gh[0].squeeze = false; tg += 16; grip.update(frame, gpose, gh, true, true, tg, .016f);
  gh[0].squeeze = true; tg += 16; grip.update(frame, gpose, gh, true, true, tg, .016f);
  check(grip.dragging(), "squeeze moves the shelf otherwise");

  // The eyes magnify the shelf but are not a window pick: the gaze consumer keeps focus.
  HostFocus gaze;
  gaze.feedback["one"] = {};
  gaze.activate("one");
  PanelSurface behind{"one", {}, 1};
  behind.placement.width = 2; behind.placement.matrix = pinnedView.pose; behind.placement.matrix.m[2][3] -= 1;
  EyeRay look = rayAt(pinnedView.pose, 0, -shelfRowDrop);
  check(spatialPick(look, fullTiles) >= 0 && bool(pickPanel(look, {behind})),
        "gaze on the shelf is consumed by it even with a window behind");
  for (int i = 0; i < 40; ++i) gaze.update({}, .05f, "eye");
  check(gaze.active == "one" && gaze.feedback["one"].focused, "looking at the shelf leaves the focused window alone");
}
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
  check(std::abs(slim.placement.width - .42f) < 1e-6 && std::abs(slim.height - slim.covered - .004f) < 1e-6 &&
        std::abs(wide.placement.width - .612f) < 1e-6 && std::abs(wide.height - .054f) < 1e-6,
        "menu strip has slim lip and full inset picture at point scale");
  auto halfway = menuStripPlacement(window, metadata, .001f, .5f);
  check(halfway.height > slim.height && halfway.height < wide.height &&
        halfway.placement.width > slim.placement.width && halfway.placement.width < wide.placement.width,
        "menu panel slides and widens together");
  check(std::abs(wide.placement.matrix.m[0][3] - wide.placement.width/2 + .6f) < 1e-6 &&
        wide.placement.matrix.m[2][3] < frameDepth && wide.placement.matrix.m[2][3] > 0,
        "menu panel follows top left behind bezel depth");
  for (const auto &strip : {slim, halfway, wide}) {
    float u = .5f, v = .5f;
    surfaceUV(strip, u, v);
    check(std::abs(u - (strip.placement.width/2 - strip.inset)/strip.pictureWidth) < 1e-6 &&
          std::abs(v - std::max(0.f,(strip.height/2-strip.inset)/strip.pictureHeight)) < 1e-6,
          "visible menu UV maps to cropped source without squeezing titles");
    auto placed = strip; placed.placement.matrix.m[2][3] = -1;
    EyeRay ray{{placed.placement.matrix.m[0][3], placed.placement.matrix.m[1][3]-strip.height/2+.001f,0},{0,0,-1}};
    check(!surfacePick(ray, placed), "covered menu edge cannot take hits");
    ray.origin[1] = placed.placement.matrix.m[1][3] + strip.height/2 - .001f;
    check(bool(surfacePick(ray, placed)), "visible menu lip takes hits");
  }
  // Check the actual overlay arcs, not just their tangent centres. An offset
  // strip can be entirely on one half or cross the window's original seam.
  for (bool vertical : {false, true}) for (bool split : {false,true}) {
    auto bent = window; bent.placement.bendVertical = vertical;
    bent.placement.bendA = .15f; bent.placement.bendB = split ? .23f : .15f;
    bent.placement.matrix.m[2][3] = -2;
    auto message = metadata; message.menusWidth = 1000; message.rect.w = 1000;
    for (float phase : {0.f,.25f,.5f,.75f,1.f}) {
      auto strip = menuStripPlacement(bent, message, .001f, phase);
      auto shape = bendShape(bent.placement,bent.height);
      const auto &region = strip.region;
      const auto expected = multiply(bent.placement.matrix,bendPointXY(shape,region.x,region.y,region.depth));
      for (int r=0;r<3;++r) for (int c=0;c<4;++c)
        check(std::abs(strip.placement.matrix.m[r][c]-expected.m[r][c])<1e-6, "menu centre follows extended top curve");
      const int count = region.segments(strip.placement.width,strip.height);
      check(count == (!vertical && split && phase >= .5f ? 2 : 1), "menu splits only at original window seam");
      for (int i=0;i<count;++i) {
        auto seg = region.segment(i,strip.placement.width,strip.height);
        const float mid = region.shape.vertical ? region.y : region.x;
        const int half = count == 2 ? i : mid < 0 ? 0 : 1;
        const float radius = shape.radius(half)-region.depth;
        check(std::abs(seg.width/(2*float(M_PI)*seg.curvature)-radius)<1e-5,
            "slim wide and animated strip keep concentric layer radius");
        auto world = multiply(strip.placement.matrix,seg.local);
        const float across = vertical ? strip.placement.width : strip.height;
        for (float u : {.01f,.99f}) {
          const float angle = (u-.5f)*seg.width/radius;
          auto sample = spatialIdentity();
          sample.m[0][3] = radius*std::sin(angle);
          sample.m[2][3] = radius*(1-std::cos(angle));
          // Compare the rendered segment to the corresponding window point.
          const float source = seg.bounds.uMin+u*(seg.bounds.uMax-seg.bounds.uMin);
          const auto point = surfacePoint(strip,vertical ? 0 : (source-.5f)*strip.placement.width,
              vertical ? (source-.5f)*strip.height : 0);
          auto rendered = multiply(world,sample);
          for (int r=0;r<3;++r) check(std::abs(rendered.m[r][3]-point.m[r][3])<2e-5,
              "overlay segment samples follow window layer through animation");
        }
        check(across > 0, "strip segment has cross-axis size");
      }
      for (float u : {.02f,.98f}) {
        const float v = .15f;
        const auto point = surfacePoint(strip,(u-.5f)*strip.placement.width,(.5f-v)*strip.height);
        EyeRay ray;
        for (int r=0;r<3;++r) { ray.origin[r]=point.m[r][3]+point.m[r][2]; ray.direction[r]=-point.m[r][2]; }
        const auto hit = surfacePick(ray,strip);
        float mappedU=u,mappedV=v; surfaceUV(strip,mappedU,mappedV);
        check(hit && std::abs(hit.u-mappedU)<2e-4 && std::abs(hit.v-mappedV)<2e-4,
            "curved menu end hits map to original Mac picture coordinates");
      }
      auto drop = message; drop.rect = {40,62,200,120}; drop.anchorRect = message.rect;
      auto menu = surfacePlacement(strip,drop,.001f);
      const float anchorX = -strip.placement.width/2+strip.inset+.03f+.1f;
      const float anchorY = strip.height/2-strip.inset-strip.pictureHeight;
      const auto tangent = surfacePoint(strip,anchorX,anchorY);
      auto top = menu.placement.matrix;
      const float down = .006f+menu.height/2;
      for (int r=0;r<3;++r) {
        for (int c=0;c<3;++c) check(std::abs(top.m[r][c]-tangent.m[r][c])<1e-6,
            "flat dropdown tangent belongs to its strip anchor");
        check(std::abs(top.m[r][3]+down*top.m[r][1]-tangent.m[r][3]-.01f*tangent.m[r][2])<2e-6,
            "dropdown anchor is one centimetre forward along local normal");
      }
      check(menu.placement.bendA==0 && menu.placement.bendB==0 && !menu.region.attached,
          "dropdown remains flat");
    }
  }
  for (float left : {0.f,.15f}) {
    auto bent = window; bent.placement.bendA=left; bent.placement.bendB=left==0 ? .15f : 0;
    auto message=metadata; message.rect.w=message.menusWidth=1700;
    auto strip=menuStripPlacement(bent,message,.001f,1);
    check(strip.region.segments(strip.placement.width,strip.height)==2,"wide menu retains flat and curved halves");
    for (int half=0;half<2;++half) {
      auto segment=strip.region.segment(half,strip.placement.width,strip.height);
      check((segment.curvature==0)==(strip.region.shape.amount[half]==0),"flat half explicitly clears overlay curvature");
    }
    const auto end=surfacePoint(strip,strip.placement.width/2-.01f,0);
    EyeRay ray;
    for (int r=0;r<3;++r) {ray.origin[r]=end.m[r][3]+end.m[r][2];ray.direction[r]=-end.m[r][2];}
    check(bool(surfacePick(ray,strip)),"menu hits beyond window handle margin remain reachable");
    auto flat=menuStripPlacement(window,message,.001f,1);
    check(flat.region.segments(flat.placement.width,flat.height)==1 &&
        flat.region.segment(0,flat.placement.width,flat.height).curvature==0 && !sameSurfaceGeometry(flat,strip),
        "focus change to flat window clears split geometry and curvature");
    auto changed=strip; changed.region.shape.amount[0]+=.01f;
    check(!sameSurfaceGeometry(changed,strip),"held texture tracks bend changes even with unchanged tangent pose");
  }
  for (int changed = 0; changed < 6; ++changed) {
    auto next = slim;
    float *properties[] = {&next.fullHeight,&next.inset,&next.pictureWidth,&next.pictureHeight,&next.covered,&next.menuRise};
    *properties[changed] += .001f;
    check(!sameSurfaceGeometry(slim,next), "held slim surface updates when mapping changes without pose changes");
  }
  check(sameSurfaceGeometry(slim,slim), "unchanged surface can keep held texture");
  HandRoute hiddenPill;
  hiddenPill.consider("pill",-1,1,true); hiddenPill.consider("behind",2,2,false);
  check(hiddenPill.hit.key=="behind" && hiddenPill.reveal.key=="pill", "hidden pill reveals without consuming background hit");
  HandRoute occluded;
  occluded.consider("pill",-1,1,false); occluded.consider("focused",2,2,true);
  check(occluded.hit.key=="focused" && occluded.reveal.key=="focused", "foreground window occludes another pill reveal");
  HandRoute nearest;
  nearest.consider("far",-1,2,false); nearest.consider("near",1,1,false);
  check(nearest.reveal.key=="near", "nearest ordinary surface occludes reveal");
  HandRoute uiBlocked(true);
  uiBlocked.consider("pill",1,1,true);
  check(uiBlocked.hit.key.empty() && uiBlocked.reveal.key.empty(), "auxiliary and shelf hits block reveal");
  HandRoute moving;
  moving.consider("drag",-1,-1,false,true); moving.consider("front",1,1,true);
  check(moving.hit.key=="drag" && moving.reveal.key=="drag", "active move retains hand ownership");
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
  const auto &shelf = shelfTiles.front().surface;
  check(std::abs(shelf.height - (.115f + shelfStatusBand)) < 1e-6 &&
        std::abs(shelf.placement.matrix.m[1][3] + shelf.height/2 - (-.48f+.115f/2)) < 1e-6,
        "shelf extension preserves icon row and top edge");
  check(status.height > 0 && status.placement.width < shelf.placement.width &&
        std::abs(status.placement.matrix.m[0][3] + status.placement.width/2 -
                 (shelf.placement.width/2-shelfInset)) < 1e-6 &&
        status.placement.matrix.m[1][3] + status.height/2 < -.48f-.115f/2,
        "wide status fits inside the lower band and aligns right");
  auto smallMetadata = metadata; smallMetadata.rect.w = 60; smallMetadata.rect.h = 30;
  auto natural = statusStripPlacement(shelfTiles, smallMetadata, .001f);
  check(std::abs(natural.placement.width-.06f)<1e-6 && std::abs(natural.height-.03f)<1e-6,
        "short status retains natural scale without widening shelf");
  auto tallMetadata = smallMetadata; tallMetadata.rect.h = 90;
  auto tallTiles = spatialLayout(shelfState, spatialIdentity(), "", &tallMetadata);
  auto tall = statusStripPlacement(tallTiles, tallMetadata, .001f);
  check(std::abs(tall.height-.09f)<1e-6 && std::abs(tall.placement.width-.06f)<1e-6 &&
        std::abs(tallTiles.front().surface.height-(.115f+.09f+2*shelfInset))<1e-6 &&
        std::abs(tallTiles.front().surface.placement.matrix.m[1][3]+tallTiles.front().surface.height/2-
                 (-.48f+.115f/2))<1e-6,
        "tall narrow status keeps natural scale and extends shelf downward");
  check(statusStripPlacement({}, metadata, .001f).height == 0, "no detached status without shelf");
  metadata.rect = {40, 62, 200, 120}; metadata.anchorRect = {10,20,600,36};
  auto dropdown = surfacePlacement(wide, metadata, .001f);
  check(std::abs(dropdown.placement.matrix.m[0][3] - (wide.placement.matrix.m[0][3] - wide.placement.width/2 + wide.inset + .03f + .1f)) < 1e-6 &&
        std::abs(dropdown.placement.matrix.m[1][3] - (wide.placement.matrix.m[1][3] + wide.height/2 - wide.inset - wide.pictureHeight - .006f - .06f)) < 1e-6 &&
        std::abs(dropdown.placement.matrix.m[2][3] - wide.placement.matrix.m[2][3] - .01f) < 1e-6,
        "surface offset uses global anchor bottom and one centimetre front");
  metadata.anchor = "status";
  auto above = surfacePlacement(status, metadata, .001f);
  check(above.placement.matrix.m[1][3] - above.height/2 > shelf.placement.matrix.m[1][3] + shelf.height/2,
        "status anchored menu opens above shelf");
  check(std::abs(above.placement.width - 200 * status.placement.width/600) < 1e-6 &&
        std::abs(above.placement.matrix.m[0][3] - (status.placement.matrix.m[0][3] - status.placement.width/2 +
                 (30+100)*status.placement.width/600)) < 1e-6,
        "status menu follows fitted item scale and position");
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
  selfTestShelf();
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
