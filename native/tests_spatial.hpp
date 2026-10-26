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
  // The shelf's texture keeps one size through a whole enlarge/shrink animation
  // with the same apps: SteamVR recreates its remote image for every new size.
  {
    ShelfHover hover;
    ShelfRaster first;
    bool haveFirst = false;
    int steps = 0, changes = 0;
    ShelfRaster last;
    float widest = 0;
    // 400 ms enlarging with the pointer sweeping the row, a pause, then shrinking.
    for (int frame = 0; frame < 120; ++frame) {
      const double now = frame * 16.0;
      const bool over = frame < 40;
      const float x = -.5f + frame * .02f;
      hover.update(now, .016f, over, over, over, x);
      auto view = pinnedView;
      view.scale = hover.scale();
      view.magnify = hover.magnify;
      view.pointer = hover.pointer;
      auto tiles = spatialLayout(st, front, "", &statusMessage, view);
      const auto &shelfTile = tiles.front();
      auto raster = shelfRaster(shelfTile);
      if (!haveFirst) { first = raster; haveFirst = true; }
      else {
        check(raster.canvasW == first.canvasW && raster.canvasH == first.canvasH,
              "the shelf texture keeps its dimensions while it magnifies");
        if (raster.w != last.w || raster.h != last.h) ++changes;
      }
      last = raster;
      ++steps;
      widest = std::max(widest, shelfTile.surface.placement.width);
      // The drawn part fits the texture, covers the overlay at the density it
      // had before, and gives the overlay the same aspect as the shelf.
      check(raster.w >= 16 && raster.h >= 16 && raster.w <= raster.canvasW && raster.h <= raster.canvasH &&
            raster.canvasW <= 2048 && std::abs(raster.u * raster.canvasW - raster.w) < 1e-2f &&
            std::abs(raster.v * raster.canvasH - raster.h) < 1e-2f,
            "the drawn shelf is a corner of the texture");
      check(std::abs(raster.w / shelfPixelsPerMetre - shelfTile.surface.placement.width) <= 4 / shelfPixelsPerMetre + 1e-5f &&
            std::abs(raster.h / shelfPixelsPerMetre - shelfTile.surface.height) <= 4 / shelfPixelsPerMetre + 1e-5f,
            "the drawn shelf keeps its pixels per metre at every step");
      check(std::abs(float(raster.w) / raster.h / (shelfTile.surface.placement.width / shelfTile.surface.height) - 1) < .06f,
            "the overlay keeps the shelf's aspect");
      // Picking follows the drawn geometry at this step of the animation.
      for (size_t i = 0; i < tiles.size(); ++i) {
        if (tiles[i].kind != SpatialTile::Dock) continue;
        auto local = multiply(inverseRigid(view.pose), tiles[i].surface.placement.matrix);
        PanelPick pick;
        check(spatialPick(rayAt(view.pose, local.m[0][3], local.m[1][3]), tiles, &pick) == int(i) &&
              std::abs(pick.u - .5f) < 1e-4 && std::abs(pick.v - .5f) < 1e-4,
              "a ray at an icon's centre picks it at every animation step");
      }
    }
    check(steps == 120 && changes > 20, "the animation really changed the drawn size");
    check(widest <= first.canvasW / shelfPixelsPerMetre + 1e-5f, "the texture holds the widest the shelf gets");
    // Different content is a different texture: more apps or a taller status band.
    auto more = st; more.apps.push_back({"f", "f", "", true, false});
    check(shelfRaster(spatialLayout(more, front, "", nullptr, pinnedView).front()).canvasW > first.canvasW,
          "another app resizes the texture");
    SurfaceMessage tall = SurfaceMessage::parse(R"({"t":"surface","v":1,"seq":1,"visible":true,"rect":[0,0,186,120],"anchor":"none","anchorRect":[0,0,186,120]})");
    check(shelfRaster(spatialLayout(st, front, "", &tall, pinnedView).front()).canvasH > first.canvasH,
          "a taller status band resizes the texture");
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
// Launcher A (FW-15): catalogue and state, layout and paging, picking on the
// curved panel and at shelf magnification, folders, the search filter, and
// pick-up/drop reordering.
inline void selfTestLauncher() {
  auto check = [](bool ok, const char *why) {
    if (!ok)
      throw std::runtime_error(why);
  };
  // A catalogue: 30 top-level items (28 apps and two folders), recent apps.
  std::string apps, items;
  for (int i = 0; i < 40; ++i) {
    apps += std::string(i ? "," : "") + "{\"id\":\"com.example.app" + std::to_string(i) + "\",\"name\":\"App " +
            std::to_string(i) + "\",\"v\":\"1\"}";
  }
  for (int i = 0; i < 28; ++i) items += "{\"app\":\"com.example.app" + std::to_string(i) + "\"},";
  items += R"({"folder":"folder-1","name":"Games","apps":["com.example.app28","com.example.app29","com.example.gone","com.example.app0"]},)";
  items += R"({"folder":"folder-2","name":"Tools","apps":["com.example.app30","com.example.app31"]},)";
  items += R"({"folder":"empty","name":"Empty","apps":["com.example.gone"]},{"folder":"../bad","name":"Bad","apps":["com.example.app32"]},)";
  items += R"({"app":"com.example.app1"},{"app":"bad id"},"junk")";
  const std::string raw = "{\"catalog\":7,\"apps\":[" + apps + R"(,{"id":"com.example.app1","name":"Duplicate"},{"id":"../x","name":"X"}],"items":[)" +
                          items + R"(],"recent":["com.example.app5","com.example.app5","com.example.app3","nope"]})";
  LauncherCatalog c;
  check(LauncherCatalog::parse(raw, c), "launcher catalogue parses");
  check(c.catalog == 7 && c.apps.size() == 40 && c.app("com.example.app1") == 1 && c.apps[1].name == "App 1",
        "apps are unique and valid");
  check(c.items.size() == 28 + 2 + 8, "items: apps, two folders, then every app the items left out");
  check(c.items[28].folder && c.items[28].apps == std::vector<int>{28, 29}, "a folder keeps known apps once each");
  check(c.items[30].app == 32 && c.items.back().app == 39, "left-out apps join at the end in catalogue order");
  check(c.recent == std::vector<int>{5, 3}, "recent apps are known and unique");
  for (const char *bad : {"", "[]", "{}", R"({"catalog":"7","apps":[],"items":[]})", R"({"catalog":1,"apps":{},"items":[]})"}) {
    LauncherCatalog none;
    check(!LauncherCatalog::parse(bad, none), "malformed catalogue refused");
  }

  // State from the companion.
  auto stateOf = [](const std::string &text) {
    auto j = json(text);
    return LauncherState::parse(j.get());
  };
  auto st = stateOf(R"({"available":true,"open":true,"seq":4,"catalog":7,"query":"Ap","results":["com.example.app3","../x","com.example.app12"],"selected":1,"pageStep":0,"keyboard":true})");
  check(st.available && st.open && st.keyboard && st.seq == 4 && st.searching() &&
        st.results == std::vector<std::string>{"com.example.app3", "com.example.app12"}, "launcher state parses");
  check(!stateOf(R"({"open":true})").open, "only an available launcher opens");
  check(!stateOf(R"({"available":true,"query":"   "})").searching(), "a blank query is not a search");
  SpatialState spatialState = SpatialState::parse(R"({"mode":"spatial","apps":[],"launcher":{"available":true,"seq":2}})");
  check(spatialState.launcher.available && spatialState.launcher.seq == 2, "spatial state carries the launcher");

  // Layout: eight columns centred like the mockup, three rows a page.
  float cx, top;
  launcherSlot(0, cx, top);
  check(std::fabs(cx - 69) < 1e-3f && std::fabs(top - launcherGridTop) < 1e-3f, "first slot");
  launcherSlot(23, cx, top);
  check(std::fabs(cx - 531) < 1e-3f && std::fabs(top - (launcherGridTop + 120)) < 1e-3f, "last slot of a page");
  check(std::fabs(launcherRecentX(0, 8) + launcherRecentX(7, 8) - launcherUnits) < 1e-3f, "recent row centred");
  check(std::fabs(launcherHeight / launcherWidth - launcherUnitsHigh / launcherUnits) < 1e-5f, "panel aspect");

  LauncherState top_;
  top_.available = top_.open = true;
  auto listing = launcherListing(c, top_, "");
  check(listing.cells.size() == 38 && listing.pages() == 2 && listing.container.empty(), "top level pages");
  auto hit = launcherHit(69, launcherGridTop + 10, listing, 2, 0);
  check(hit.kind == LauncherTarget::Cell && hit.index == 0, "first cell");
  hit = launcherHit(531, launcherGridTop + 130, listing, 2, 1);
  check(hit.kind == LauncherTarget::None || (hit.kind == LauncherTarget::Panel), "past the last item is the panel");
  hit = launcherHit(69 + 66, launcherGridTop + 70, listing, 2, 1);
  check(hit.kind == LauncherTarget::Cell && hit.index == 24 + 9, "a cell on the second page");
  hit = launcherHit(launcherRecentX(1, 2), launcherRecentTop + 10, listing, 2, 0);
  check(hit.kind == LauncherTarget::Recent && hit.index == 1, "recent app");
  hit = launcherHit(launcherDotX(1, 2), launcherDotsY + 1, listing, 2, 0);
  check(hit.kind == LauncherTarget::Dot && hit.index == 1, "page dot");
  check(launcherHit(300, 30, listing, 2, 0).kind == LauncherTarget::Field, "search field");
  check(launcherHit(launcherBackX + 5, launcherBackY + 5, listing, 2, 0).kind != LauncherTarget::Back,
        "no Back at the top level");
  check(launcherHit(-1, 30, listing, 2, 0).kind == LauncherTarget::None &&
        launcherHit(5, 365, listing, 2, 0).kind == LauncherTarget::Panel, "outside and margins");
  check(launcherTargetKey(c, listing, launcherHit(69 + 66 * 4, launcherGridTop + 10, listing, 0, 1)) ==
            "launcher:cell:folder:folder-1", "folder cell key");

  // Paging: thumbstick, the Mac's arrows, a new query, the keyboard selection.
  LauncherStick stick;
  check(stick.update(.7f) == 1 && stick.update(.9f) == 0 && stick.update(.2f) == 0 && stick.update(-.8f) == -1 &&
        stick.update(NAN) == 0, "one page per thumbstick push");
  LauncherView view;
  LauncherState ls;
  ls.available = true;
  auto head = shelfIdentity();
  head.m[1][3] = 1.6f;
  view.request(ls, true);
  check(!view.update(ls, false, head) && !view.open && !ls.open, "opens only in front of a known head");
  view.request(ls, true);
  check(view.update(ls, true, head) && view.open, "a local open is immediate");
  check(std::fabs(view.pose.m[2][3] + launcherDistance) < 1e-4f && std::fabs(view.pose.m[1][3] - 1.6f + launcherDrop) < 1e-4f,
        "the panel opens in front of the head, at eye level");
  LauncherState echo = ls;
  echo.open = false; // a refresh of the same sequence before the companion acknowledged
  view.acknowledge(echo);
  check(echo.open, "a local open holds until acknowledged");
  echo.seq = 1;
  echo.open = true;
  view.acknowledge(echo);
  ls = echo;
  view.update(ls, true, head);
  ls.pageStep = 2;
  view.update(ls, true, head);
  check(view.page == 2, "the Mac's arrows turn pages");
  view.clampPage(listing.pages());
  check(view.page == 1, "pages are clamped");
  ls.query = "App";
  ls.results = {"com.example.app1"};
  view.update(ls, true, head);
  check(view.page == 0, "a new query starts at its first page");
  ls.results.clear();
  for (int i = 0; i < 30; ++i) ls.results.push_back("com.example.app" + std::to_string(i));
  ls.selected = 26;
  view.update(ls, true, head);
  check(view.page == 1, "the keyboard selection keeps its page in view");
  ls.open = false;
  ls.seq = 2;
  view.folder = "folder-1";
  view.carry.hand = 1;
  view.update(ls, true, head);
  check(!view.open && view.folder.empty() && view.page == 0 && !view.carry.active(), "closing resets the view");

  // Folders open in place; the filter shows only matches, in result order.
  auto inFolder = launcherListing(c, top_, "folder-1");
  check(inFolder.inFolder && inFolder.container == "folder-1" && inFolder.folderName == "Games" &&
        inFolder.cells.size() == 2 && inFolder.cells[0].app == 28, "a folder opens in place");
  check(launcherHit(launcherBackX + 5, launcherBackY + 5, inFolder, 2, 0).kind == LauncherTarget::Back, "Back");
  LauncherState search;
  search.available = search.open = true;
  search.query = "app 1";
  search.results = {"com.example.app12", "com.example.unknown", "com.example.app1", "com.example.app30"};
  auto filtered = launcherListing(c, search, "folder-1");
  check(filtered.searching && filtered.cells.size() == 3 && filtered.cells[0].app == 12 && filtered.cells[2].app == 30 &&
        filtered.container.empty(), "the filter overrides the folder and keeps the companion's order");
  LauncherView v2;
  v2.open = true;
  search.selected = 1;
  auto picture = launcherPicture(c, search, v2, {}, false, "/icons");
  check(picture.searching && picture.section == "Matching Applications" && picture.grid.size() == 3 &&
        picture.grid[1].selected && picture.grid[0].icon == "/icons/com.example.app12.png", "filtered picture");
  search.results = {"com.example.none"};
  check(launcherPicture(c, search, v2, {}, false, "").empty, "no matches says so");
  v2.folder = "folder-2";
  auto folderPicture = launcherPicture(c, top_, v2, {LauncherTarget::Back, 0}, true, "");
  check(folderPicture.inFolder && folderPicture.section == "Tools" && folderPicture.backHot && folderPicture.backPressed,
        "folder picture");
  v2.folder.clear();
  auto grid = launcherPicture(c, top_, v2, {LauncherTarget::Cell, 3}, false, "");
  check(grid.grid.size() == 24 && grid.grid[3].hot && grid.grid[4].folder == false && grid.pages == 2 &&
        grid.recent.size() == 2 && grid.signature() != launcherPicture(c, top_, v2, {}, false, "").signature(),
        "the laser lights one icon and redraws");

  // Pick-up and drop: between icons reorders, onto an icon does nothing.
  auto gap = launcherGapAt(69 - 30, launcherGridTop + 20, 0, 38);
  check(gap && gap.gap == 0, "left edge of the first cell is before it");
  gap = launcherGapAt(69 + 30, launcherGridTop + 20, 0, 38);
  check(gap && gap.gap == 1, "right part of a cell is after it");
  gap = launcherGapAt(69, launcherGridTop + 20, 0, 38);
  check(!gap && gap.onto, "the middle of an icon is onto it");
  gap = launcherGapAt(69 + 66 * 5 + 30, launcherGridTop + 140, 1, 38);
  check(gap && gap.gap == 38, "after the last icon is the end");
  check(!launcherGapAt(300, 40, 0, 38), "not over the grid");
  std::vector<std::string> ids;
  for (const auto &cell : listing.cells) ids.push_back(launcherCellID(c, cell));
  std::string before;
  check(!launcherDropBefore(ids, ids[3], 3, before) && !launcherDropBefore(ids, ids[3], 4, before),
        "dropping where it was changes nothing");
  check(launcherDropBefore(ids, ids[5], 0, before) && before == ids[0], "drop before the first");
  check(launcherDropBefore(ids, ids[0], 38, before) && before.empty(), "drop at the end");
  auto moved = c;
  check(launcherMove(moved, "com.example.app5", "com.example.app0", ""), "move in the top level");
  check(moved.items[0].app == 5 && moved.items[1].app == 0 && moved.items.size() == c.items.size(), "moved first");
  check(launcherMove(moved, "folder:folder-2", "", "") && moved.items.back().id == "folder-2", "folders move too");
  check(launcherMove(moved, "com.example.app29", "com.example.app28", "folder-1") &&
        moved.items[std::find_if(moved.items.begin(), moved.items.end(), [](auto &i) { return i.id == "folder-1"; }) -
                    moved.items.begin()].apps == std::vector<int>{29, 28}, "move inside a folder");
  check(!launcherMove(moved, "com.example.app5", "com.example.app5", "") &&
        !launcherMove(moved, "com.example.app5", "com.example.nope", "") &&
        !launcherMove(moved, "com.example.app28", "", "") && !launcherMove(moved, "com.example.app5", "", "folder-1") &&
        !launcherMove(moved, "com.example.app28", "", "nope"), "invalid moves change nothing");
  v2.carry.hand = 1;
  v2.carry.id = ids[6];
  v2.carry.gap = launcherGapAt(69 + 66 * 2 - 30, launcherGridTop + 20, 0, 38);
  auto carried = launcherPicture(c, top_, v2, {LauncherTarget::Cell, 2}, true, "");
  check(carried.grid[6].ghost && !carried.grid[2].hot && std::fabs(carried.markerX - (69 + 66 * 1.5f)) < 1e-3f,
        "the carried icon's place is a ghost and the gap shows a marker");

  // The shelf's Applications button: first slot, magnified and picked like an icon.
  SpatialState shelfState;
  shelfState.spatial = true;
  for (const char *id : {"a", "b", "c"}) shelfState.apps.push_back({id, id, "", true, false});
  ShelfView rest;
  rest.pinned = true;
  rest.pose = shelfFrontOf(shelfIdentity());
  rest.scale = shelfRestScale;
  auto without = spatialLayout(shelfState, rest.pose, "", nullptr, rest);
  shelfState.launcher.available = true;
  auto restTiles = spatialLayout(shelfState, rest.pose, "", nullptr, rest);
  auto find = [](const std::vector<SpatialTile> &tiles, SpatialTile::Kind kind, int index = 0) {
    for (const auto &t : tiles)
      if (t.kind == kind && t.index == index) return t;
    throw std::runtime_error("tile missing");
  };
  check(std::none_of(without.begin(), without.end(), [](auto &t) { return t.kind == SpatialTile::Apps; }),
        "no Applications button without a launcher");
  const auto button = find(restTiles, SpatialTile::Apps);
  check(button.surface.placement.matrix.m[0][3] < find(restTiles, SpatialTile::Dock, 0).surface.placement.matrix.m[0][3],
        "the Applications button is at the left end");
  check(find(restTiles, SpatialTile::Shelf).surface.placement.width > find(without, SpatialTile::Shelf).surface.placement.width,
        "the shelf makes room for it");
  auto rayTo = [](const HmdMatrix34_t &m) {
    EyeRay r;
    for (int i = 0; i < 3; ++i) {
      r.origin[i] = i == 1 ? m.m[1][3] : 0;
      r.direction[i] = m.m[i][3] - r.origin[i];
    }
    const float n = std::hypot(r.direction[0], r.direction[1], r.direction[2]);
    for (auto &d : r.direction) d /= n;
    return r;
  };
  int picked = spatialPick(rayTo(button.surface.placement.matrix), restTiles);
  check(picked >= 0 && restTiles[picked].kind == SpatialTile::Apps, "picked at rest");
  ShelfView big = rest;
  big.scale = 1;
  big.magnify = 1;
  const float step = std::min(.105f, 1.35f / 4);
  big.pointer = -1.5f * step;
  auto bigTiles = spatialLayout(shelfState, rest.pose, "", nullptr, big);
  const auto grown = find(bigTiles, SpatialTile::Apps);
  check(grown.surface.placement.width > step * 1.4f, "magnified under the pointer like the icons");
  picked = spatialPick(rayTo(grown.surface.placement.matrix), bigTiles);
  check(picked >= 0 && bigTiles[picked].kind == SpatialTile::Apps, "picked at full magnification");
  check(!find(bigTiles, SpatialTile::Apps).lit, "unlit while closed");
  shelfState.launcher.open = true;
  check(find(spatialLayout(shelfState, rest.pose, "", nullptr, big), SpatialTile::Apps).lit, "lit while open");

  // The curved panel: a ray at a cell's place on the curve picks that cell.
  LauncherView open;
  open.open = true;
  open.place(head);
  std::vector<SpatialTile> tiles;
  launcherTiles(tiles, open, std::make_shared<LauncherPicture>(grid));
  check(tiles.size() == 2 && tiles[0].kind == SpatialTile::Dim && !tiles[0].pickable &&
        tiles[1].kind == SpatialTile::Launcher && tiles[1].surface.placement.bendA > .1f, "backdrop and curved panel");
  for (int slot : {0, 7, 12, 23}) {
    launcherSlot(slot, cx, top);
    const auto &surface = tiles[1].surface;
    const auto point = surfacePoint(surface, (cx / launcherUnits - .5f) * launcherWidth,
                                    (.5f - (top + 17) / launcherUnitsHigh) * launcherHeight);
    EyeRay r;
    for (int i = 0; i < 3; ++i) {
      r.origin[i] = head.m[i][3];
      r.direction[i] = point.m[i][3] - head.m[i][3];
    }
    PanelPick pick;
    check(spatialPick(r, tiles, &pick) == 1, "the panel is picked through its curve");
    const auto target = launcherHit(pick.u * launcherUnits, pick.v * launcherUnitsHigh, listing, 2, 0);
    check(target.kind == LauncherTarget::Cell && target.index == slot, "the ray's cell is the one drawn there");
  }
  open.carry.hand = 1;
  open.carry.onPanel = true;
  open.carry.x = 300;
  open.carry.y = 200;
  tiles.clear();
  launcherTiles(tiles, open, nullptr, "/icons/a.png");
  check(tiles.size() == 3 && tiles[2].kind == SpatialTile::Carry && !tiles[2].pickable && tiles[2].icon == "/icons/a.png",
        "a carried icon follows the laser");
  check(spatialSortOrder(SpatialTile::Dim) > 54 && spatialSortOrder(SpatialTile::Dim) < spatialSortOrder(SpatialTile::Shelf) &&
        spatialSortOrder(SpatialTile::Shelf) < spatialSortOrder(SpatialTile::Launcher) &&
        spatialSortOrder(SpatialTile::Launcher) < spatialSortOrder(SpatialTile::Carry),
        "the backdrop dims the windows, not the shelf or the launcher");
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
  selfTestLauncher();
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
