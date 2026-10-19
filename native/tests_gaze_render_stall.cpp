// Test-only glFinish replacement. Build main.cpp with -DglFinish=fwTestGlFinish
// and link this file. No test switch or timing injection enters production.
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <dlfcn.h>
#include <thread>
static double stamp() {
  return std::chrono::duration<double, std::milli>(
      std::chrono::steady_clock::now().time_since_epoch()).count();
}
extern "C" void fwTestGlFinish() {
  auto realFinish = reinterpret_cast<void (*)()>(dlsym(RTLD_DEFAULT, "glFinish"));
  if (!realFinish) std::abort();
  realFinish();
  const char *path = std::getenv("FW_TEST_STALL_MARKERS");
  if (!path) std::abort();
  const double start = stamp();
  std::this_thread::sleep_for(std::chrono::milliseconds(300));
  FILE *file = std::fopen(path, "a");
  if (!file) std::abort();
  std::fprintf(file, "%.6f %.6f\n", start, stamp());
  std::fclose(file);
}
