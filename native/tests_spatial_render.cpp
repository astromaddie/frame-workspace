// Production GL renderer regression checks. Hidden local GL, no OpenVR or companion.
// Compile with the viewer's dependencies and run this executable directly.
#include "overlay.hpp"
#include "spatial_render.hpp"
#include <iostream>

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
    std::cout << "spatial renderer passed: late icon and live same-texture updates\n";
  } catch (const std::exception &e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
}
