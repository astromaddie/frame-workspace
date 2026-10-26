// Production GL renderer regression checks. Hidden local GL, no OpenVR or companion.
// Compile with the viewer's dependencies and run this executable directly.
#include "overlay.hpp"
#include "spatial_render.hpp"
#include <iostream>
#include <set>

int main() {
  std::string pattern = (std::filesystem::temp_directory_path() /
                         "fw-spatial-render-XXXXXX").string();
  if (!mkdtemp(pattern.data())) return 1;
  struct Cleanup {
    std::string path;
    ~Cleanup() { std::error_code e; std::filesystem::remove_all(path, e); }
  } cleanup{pattern};
  try {
    auto session = std::make_shared<fw::OverlaySession>(true);
    fw::SpatialRenderer renderer(session);
    fw::SpatialState state;
    state.spatial = true;
    state.apps.push_back({"test.app", "App", "late.png", true, false});
    const auto icon = pattern + "/late.png", capture = pattern + "/capture.ppm";
    auto render = [&] (GLuint texture = 0) {
      glMatrixMode(GL_PROJECTION); glLoadIdentity();
      glMatrixMode(GL_MODELVIEW); glLoadIdentity();
      auto tiles = fw::spatialLayout(state, fw::spatialAnchor(fw::spatialIdentity(), 1.3f, 0));
      renderer.draw(state, tiles, -1, 0, 0, false, pattern,
                    [&](const std::string &) { return fw::SpatialPicture{texture, false}; });
      if (!renderer.dump(capture, tiles)) throw std::runtime_error("readback failed");
      return fw::readFile(capture, 4 * 1024 * 1024);
    };
    const auto missing = render();
    fw::SpatialRaster raster;
    auto surface = raster.surface(0, 128, 128);
    auto context = raster.create(surface);
    raster.rgba(context, .1, .9, .2, 1); raster.paint(context);
    raster.destroy(context);
    void *cairo = nullptr;
    for (auto name : {"libcairo.so.2", "libcairo.2.dylib", "/opt/homebrew/lib/libcairo.2.dylib"})
      if ((cairo = dlopen(name, RTLD_NOW | RTLD_LOCAL))) break;
    auto write = cairo ? reinterpret_cast<int (*)(void *, const char *)>(
                            dlsym(cairo, "cairo_surface_write_to_png")) : nullptr;
    const bool written = write && write(surface, icon.c_str()) == 0;
    raster.surfaceDestroy(surface);
    if (cairo) dlclose(cairo);
    if (!written) throw std::runtime_error("icon fixture unavailable");
    std::this_thread::sleep_for(std::chrono::milliseconds(1100));
    if (missing == render()) throw std::runtime_error("late icon remained cached empty");
    state.open = true;
    state.windows.push_back({"test.window", "Window", "App"});
    GLuint texture = 0;
    glGenTextures(1, &texture);
    glBindTexture(GL_TEXTURE_2D, texture);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    unsigned char pixel[4]{255, 0, 0, 255};
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, 1, 1, 0, GL_RGBA, GL_UNSIGNED_BYTE, pixel);
    const auto red = render(texture);
    glBindTexture(GL_TEXTURE_2D, texture);
    pixel[0] = 0; pixel[2] = 255;
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE, pixel);
    const auto blue = render(texture);
    glDeleteTextures(1, &texture);
    if (red == blue) throw std::runtime_error("same-texture thumbnail did not refresh");
    // The shelf's texture is allocated once and keeps its size through a whole
    // enlarge/shrink animation (SteamVR recreates its remote image per size), and
    // only changing the shelf's content reallocates it.
    {
      fw::SpatialRenderer shelf(session);
      fw::SpatialState apps;
      apps.spatial = true;
      for (const char *id : {"a", "b", "c", "d"}) apps.apps.push_back({id, id, "", true, false});
      fw::ShelfHover hover;
      fw::ShelfView view;
      view.pinned = true;
      auto textureSize = [&](GLuint t) {
        int w = 0, h = 0;
        glBindTexture(GL_TEXTURE_2D, t);
        glGetTexLevelParameteriv(GL_TEXTURE_2D, 0, GL_TEXTURE_WIDTH, &w);
        glGetTexLevelParameteriv(GL_TEXTURE_2D, 0, GL_TEXTURE_HEIGHT, &h);
        return std::make_pair(w, h);
      };
      std::pair<int, int> canvas{0, 0};
      std::set<std::pair<float, float>> extents;
      auto animate = [&](fw::SpatialState &state, int frames, bool over) {
        for (int frame = 0; frame < frames; ++frame) {
          static double now = 0;
          now += 16;
          hover.update(now, .016f, over, over, over, -.2f + frame * .01f);
          view.scale = hover.scale();
          view.magnify = hover.magnify;
          view.pointer = hover.pointer;
          auto tiles = fw::spatialLayout(state, fw::spatialAnchor(fw::spatialIdentity(), 1.3f, 0), "", nullptr, view);
          shelf.draw(state, tiles, -1, 0, 0, false, pattern,
                     [&](const std::string &) { return fw::SpatialPicture{}; });
          const auto size = textureSize(shelf.tileTexture(0));
          if (canvas.first == 0) canvas = size;
          if (size != canvas) throw std::runtime_error("shelf texture changed size while it animated");
          float u, v;
          shelf.tileExtent(0, u, v);
          extents.insert({u, v});
        }
      };
      animate(apps, 60, true);   // enlarging and magnifying
      animate(apps, 120, false); // grace period, then shrinking
      if (extents.size() < 10) throw std::runtime_error("shelf animation did not change what is drawn");
      if (shelf.shelfAllocationCount() != 1)
        throw std::runtime_error("shelf texture was allocated more than once during the animation");
      apps.apps.push_back({"e", "e", "", true, false});
      canvas = {0, 0};
      animate(apps, 1, true);
      if (shelf.shelfAllocationCount() != 2) throw std::runtime_error("shelf content change did not reallocate");
      animate(apps, 60, true);
      if (shelf.shelfAllocationCount() != 2) throw std::runtime_error("shelf texture reallocated again");
    }
    std::cout << "spatial renderer passed: late icon, live same-texture updates and a fixed-size shelf texture\n";
  } catch (const std::exception &e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
}
