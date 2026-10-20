#pragma once
// Diagnostic: renders both chrome layers for one state into a PPM, flat, over
// a dark striped stand-in for the window. Needs a current GL context.
//   scene: rest | all | hover | held | close | close-held | focus-glow | focus-snap | focused
#include "chrome.hpp"
#include <cstdio>
#include <string>
#include <vector>

namespace fw {
inline bool renderChromePreview(const std::string &scene, float windowWidth, const std::string &outPath) {
  HandleLayout l;
  l.width = windowWidth;
  l.height = windowWidth * 0.75f;
  ChromeState s, frame;
  s.reveal[handleIndex(Handle::Pill)] = s.reveal[handleIndex(Handle::Close)] = 1;
  auto out = [&](Handle h) { s.reveal[handleIndex(h)] = 1; };
  if (scene == "all") {
    for (int i = 0; i < handleCount; i++) s.reveal[i] = 1;
  } else if (scene == "hover" || scene == "held") {
    for (Handle h : {Handle::CornerTR, Handle::CornerBR, Handle::BarRight, Handle::BarTop}) out(h);
    s.tip = true;
    if (scene == "hover") {
      s.wide[handleIndex(Handle::BarRight)] = 1;
      s.tipX = l.width / 2 + l.handleOffset();
      s.tipY = 0.01f;
    } else {
      s.wide[handleIndex(Handle::CornerBR)] = s.held[handleIndex(Handle::CornerBR)] = 1;
      s.tipX = l.width / 2 + l.handleOffset() - 0.01f;
      s.tipY = -(l.height / 2 + l.handleOffset()) + 0.01f;
    }
  } else if (scene == "close" || scene == "close-held") {
    s.tip = true;
    s.tipX = l.closeX() + 0.004f;
    s.tipY = l.rowY() + 0.006f;
    if (scene == "close-held") s.held[handleIndex(Handle::Close)] = 1;
  } else if (scene == "focus-glow" || scene == "focus-snap" || scene == "focused") {
    frame.focusGlow = scene == "focus-glow" ? 0.7f : 0.f;
    frame.focused = scene != "focus-glow";
    frame.focusSnap = scene == "focus-snap" ? 0.4f : 1.f;
  } else if (scene != "rest") {
    return false;
  }
  frame.tip = s.tip;
  frame.tipX = s.tipX;
  frame.tipY = s.tipY;
  ChromeRenderer r;
  GLuint tex[2];
  glGenTextures(2, tex);
  int fw_ = 0, fh = 0, cw = 0, ch = 0;
  ChromeRenderer::textureSize(l.frameWidth(), l.frameHeight(), false, fw_, fh);
  ChromeRenderer::textureSize(l.chromeWidth(), l.chromeHeight(), false, cw, ch);
  r.render(0, l, frame, l.frameWidth(), l.frameHeight(), false, tex[0], fw_, fh);
  r.render(1, l, s, l.chromeWidth(), l.chromeHeight(), false, tex[1], cw, ch);
  glFinish();
  auto read = [](GLuint t, int w, int h) {
    std::vector<unsigned char> px(size_t(w) * h * 4);
    GLuint fb = 0;
    glGenFramebuffers(1, &fb);
    glBindFramebuffer(GL_FRAMEBUFFER, fb);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, t, 0);
    glReadPixels(0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, px.data());
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    glDeleteFramebuffers(1, &fb);
    return px; // row 0 is the top of the layer
  };
  const auto F = read(tex[0], fw_, fh), C = read(tex[1], cw, ch);
  glDeleteTextures(2, tex);
  r.release();
  const float mpp = l.chromeWidth() / cw;
  FILE *f = fopen(outPath.c_str(), "wb");
  if (!f) return false;
  fprintf(f, "P6\n%d %d\n255\n", cw, ch);
  for (int y = 0; y < ch; y++)
    for (int x = 0; x < cw; x++) {
      const float px = (x + .5f) * mpp - l.chromeWidth() / 2, py = l.chromeHeight() / 2 - (y + .5f) * mpp;
      float c[3] = {.09f, .085f, .105f};
      if (std::fabs(px) < l.width / 2 && std::fabs(py) < l.height / 2) {
        const bool line = int((py + 10) / .03f) % 2 == 0 && std::fmod(px + 10, .4f) < .25f;
        c[0] = line ? .42f : .114f; c[1] = line ? .48f : .122f; c[2] = line ? .59f : .153f;
      }
      const int fx = int((px + l.frameWidth() / 2) / l.frameWidth() * fw_);
      const int fy = int((l.frameHeight() / 2 - py) / l.frameHeight() * fh);
      if (fx >= 0 && fy >= 0 && fx < fw_ && fy < fh) {
        const unsigned char *q = &F[(size_t(fy) * fw_ + fx) * 4];
        for (int k = 0; k < 3; k++) c[k] = q[k] / 255.f + c[k] * (1 - q[3] / 255.f);
      }
      const unsigned char *q = &C[(size_t(y) * cw + x) * 4];
      for (int k = 0; k < 3; k++) {
        c[k] = q[k] / 255.f + c[k] * (1 - q[3] / 255.f);
        fputc(int(std::lround(std::clamp(c[k], 0.f, 1.f) * 255)), f);
      }
    }
  fclose(f);
  return true;
}
} // namespace fw
