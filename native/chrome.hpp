#pragma once
// Palladium window chrome (FW-14), drawn by one GL shader with real lighting.
// Layer 0 is the F2 machined bezel, layer 1 the controls (handles.hpp). The
// metal is silver-lavender, lit from the upper left as in Palladium. The laser
// lights the metal around its tip as in Stargate: a soft pool of light that
// favours faces turned towards it, here in the laser's blue. A dragged piece
// presses in (its bevel flips) and glows from inside, brightest along the
// walls of its recess. Output is premultiplied alpha, top row first.
#include "handles.hpp"
#include "depth.hpp"
#include <SDL.h>
#define GL_GLEXT_PROTOTYPES 1
#include <SDL_opengl.h>
#include <stdexcept>
#include <string>

namespace fw {



// Shared by the live overlay and the offscreen compositor.
inline GLuint createCursorTexture() {
  GLuint texture = 0;
  const int n = 64;
  std::vector<uint8_t> dot(size_t(n) * n * 4, 0);
  for (int y = 0; y < n; y++)
    for (int x = 0; x < n; x++) {
      const float d = std::hypot(x + 0.5f - n / 2.f, y + 0.5f - n / 2.f) / (n / 2.f);
      const float core = std::clamp((0.3f - d) * 20.f + 0.5f, 0.f, 1.f);
      const float ring = std::clamp((0.5f - d) * 20.f + 0.5f, 0.f, 1.f) * 0.55f;
      const float halo = std::exp(-d * d * 9.f) * 0.18f;
      const float blue = std::max(ring, halo) * (1 - core);
      uint8_t *o = &dot[(size_t(y) * n + x) * 4];
      const float a = core + blue;
      o[0] = uint8_t(std::lround(255 * (core + blue * 0.29f)));
      o[1] = uint8_t(std::lround(255 * (core + blue * 0.66f)));
      o[2] = uint8_t(std::lround(255 * (core + blue * 1.0f)));
      o[3] = uint8_t(std::lround(255 * std::min(a, 1.f)));
    }
  glGenTextures(1, &texture);
  glBindTexture(GL_TEXTURE_2D, texture);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
  glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
  glPixelStorei(GL_UNPACK_ALIGNMENT, 1);
  glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, n, n, 0, GL_RGBA, GL_UNSIGNED_BYTE, dot.data());
  return texture;
}

// What the controls show; animated by the caller.
struct ChromeState {
  float reveal[handleCount] = {}; // 0 hidden .. 1 revealed (pill/disc fade, rods slide)
  float wide[handleCount] = {};   // 0 thin .. 1 wide (corners and side grips)
  float held[handleCount] = {};   // 1 while dragged (or pressed, for the disc)
  bool tip = false;               // the laser's tip is on this layer
  float tipX = 0, tipY = 0;       // window coordinates (m)
  // Gaze and focus (FW-15, bezel): the highlight building while the window
  // is looked at, how far a long stare is towards committing focus (0 -> 1),
  // the snap when it takes focus (0 -> 1), and whether it is the active window.
  float focusGlow = 0, focusSnap = 0, focusProgress = 0;
  bool focused = false;
};

// Eyes highlight, an action commits (FW-15, chosen 2026-10-26). Looking at a
// window builds a soft, pale glow along its border within a quarter second:
// that is the highlight, nothing more. Holding the look on it for
// `stareCommitSeconds` commits focus too; from the highlight on, the soft glow
// gathers towards the active window's crisp blue line (`progress`), so the
// user sees the commit coming. Focus itself is only ever set by the host
// (`focused`), which plays the snap.
struct FocusFeedback {
  static constexpr float dwellSeconds = 0.25f, snapSeconds = 0.15f;
  // A long stare commits focus (total uninterrupted look, highlight included).
  static constexpr float stareCommitSeconds = 1.2f;
  float glow = 0, snap = 0, progress = 0;
  bool focused = false;
  // `stare`: seconds of uninterrupted look that may commit (0 while a guard
  // holds it back), `committing`: the commit went out and waits for the Mac.
  void update(float dt, bool lookedAt, float stare = 0, bool committing = false) {
    if (focused) {
      snap = std::min(1.f, snap + dt / snapSeconds);
      glow = 0; progress = 0;
      return;
    }
    if (lookedAt) glow = std::min(1.f, glow + dt / dwellSeconds);
    else glow = std::max(0.f, glow - dt / (dwellSeconds * 0.6f));  // fades a little faster than it builds
    progress = !lookedAt ? 0.f : committing ? 1.f
        : std::clamp((stare - dwellSeconds) / (stareCommitSeconds - dwellSeconds), 0.f, 1.f);
  }
  void lose() { focused = false; snap = 0; glow = 0; progress = 0; }
  void apply(ChromeState &s) const {
    s.focusGlow = glow; s.focusSnap = snap; s.focused = focused; s.focusProgress = progress;
  }
};

// One active window (FW-15): it shows at full brightness and the other
// windows a little dimmer, in Spatial Mode and while something is active. Not
// while the launcher is open: its backdrop already dims everything behind it.
constexpr float spatialInactiveTint = .85f;
inline bool spatialDimmed(bool spatial, const std::string &key, const std::string &active, bool launcherOpen) {
  return spatial && !launcherOpen && !active.empty() && key != active;
}

class ChromeRenderer {
public:
  // The exact material and pressed-control functions are shared by the menu
  // recess and the physical frame walls.
  static std::string materialSource() {
    std::string s=fragmentSource();
    auto a=s.find("// Palladium silver-lavender"), b=s.find("vec4 bezel(");
    return s.substr(a,b-a);
  }
  static constexpr float pixelsPerMeter = 1100;
  static constexpr int maxPixels = 3072;

  // Texture size for a layer `metersWide` x `metersHigh` (window meters);
  // transposed (a quarter turn, for top-to-bottom bends) swaps them.
  static void textureSize(float metersWide, float metersHigh, bool transposed, int &w, int &h) {
    const float ppm = std::min(pixelsPerMeter, maxPixels / std::max(metersWide, metersHigh));
    w = std::max(16, int(std::lround((transposed ? metersHigh : metersWide) * ppm)));
    h = std::max(16, int(std::lround((transposed ? metersWide : metersHigh) * ppm)));
  }

  // Draws `layer` (0 bezel, 1 controls) into `texture`, w x h pixels covering
  // `metersWide` x `metersHigh` of window coordinates, transposed if asked
  // (the texture's u then runs up the window from its bottom edge).
  void render(int layer, const HandleLayout &l, const ChromeState &s, float metersWide, float metersHigh,
              bool transposed, GLuint texture, int w, int h) {
    ensureProgram();
    glBindTexture(GL_TEXTURE_2D, texture);
    if (allocated[layer][0] != w || allocated[layer][1] != h || allocatedTexture[layer] != texture) {
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
      glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
      glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, nullptr);
      allocated[layer][0] = w;
      allocated[layer][1] = h;
      allocatedTexture[layer] = texture;
    }
    GLint oldViewport[4];
    glGetIntegerv(GL_VIEWPORT, oldViewport);
    glBindFramebuffer(GL_FRAMEBUFFER, framebuffer);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, texture, 0);
    if (glCheckFramebufferStatus(GL_FRAMEBUFFER) != GL_FRAMEBUFFER_COMPLETE) {
      glBindFramebuffer(GL_FRAMEBUFFER, 0);
      throw std::runtime_error("chrome framebuffer incomplete");
    }
    glViewport(0, 0, w, h);
    glDisable(GL_BLEND);
    glDisable(GL_DEPTH_TEST);
    glUseProgram(program);
    auto u = [&](const char *name) { return glGetUniformLocation(program, name); };
    glUniform1i(u("uLayer"), layer);
    glUniform2f(u("uSize"), metersWide, metersHigh);
    glUniform1i(u("uTranspose"), transposed ? 1 : 0);
    glUniform1f(u("uPx"), metersWide / float(transposed ? h : w));
    glUniform2f(u("uHalf"), l.width / 2, l.height / 2);
    glUniform1f(u("uBezel"), l.bezel());
    glUniform4f(u("uRod"), l.handleOffset(), l.arm(), l.bend(), l.gripLength());
    glUniform1f(u("uGripAcross"), l.gripLengthAcross());
    glUniform2f(u("uRodWidth"), HandleLayout::rodRest, HandleLayout::rodWide);
    glUniform4f(u("uRow"), l.closeX(), l.pillX(), l.rowY(), l.discDiameter());
    glUniform2f(u("uPill"), l.pillWidth(), l.pillHeight());
    glUniform1fv(u("uReveal"), handleCount, s.reveal);
    glUniform1fv(u("uWideAmt"), handleCount, s.wide);
    glUniform1fv(u("uHeld"), handleCount, s.held);
    glUniform3f(u("uTip"), s.tipX, s.tipY, s.tip ? 1.f : 0.f);
    glUniform4f(u("uFocus"), s.focusGlow, s.focusSnap, s.focused ? 1.f : 0.f, s.focusProgress);
    glBegin(GL_QUADS);
    glTexCoord2f(0, 0); glVertex2f(-1, -1);
    glTexCoord2f(1, 0); glVertex2f(1, -1);
    glTexCoord2f(1, 1); glVertex2f(1, 1);
    glTexCoord2f(0, 1); glVertex2f(-1, 1);
    glEnd();
    glUseProgram(0);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    glViewport(oldViewport[0], oldViewport[1], oldViewport[2], oldViewport[3]);
  }

  // Frees its GL objects; call while the context is still current.
  void release() {
    if (program) glDeleteProgram(program);
    if (framebuffer) glDeleteFramebuffers(1, &framebuffer);
    program = framebuffer = 0;
  }

private:
  GLuint program = 0, framebuffer = 0;
  int allocated[2][2] = {{0, 0}, {0, 0}};
  GLuint allocatedTexture[2] = {0, 0};

  static GLuint compile(GLenum type, const char *source) {
    GLuint shader = glCreateShader(type);
    glShaderSource(shader, 1, &source, nullptr);
    glCompileShader(shader);
    GLint ok = GL_FALSE;
    glGetShaderiv(shader, GL_COMPILE_STATUS, &ok);
    if (ok != GL_TRUE) {
      GLint length = 0;
      glGetShaderiv(shader, GL_INFO_LOG_LENGTH, &length);
      std::string log(size_t(std::max(length, 1)), '\0');
      glGetShaderInfoLog(shader, GLint(log.size()), nullptr, log.data());
      glDeleteShader(shader);
      throw std::runtime_error("chrome shader compile failed: " + log);
    }
    return shader;
  }

  void ensureProgram() {
    if (program) return;
    static const char *vertex =
        "#version 120\n"
        "void main(){ gl_Position=gl_Vertex; gl_TexCoord[0]=gl_MultiTexCoord0; }\n";
    GLuint vs = compile(GL_VERTEX_SHADER, vertex), fs = compile(GL_FRAGMENT_SHADER, fragmentSource());
    program = glCreateProgram();
    glAttachShader(program, vs);
    glAttachShader(program, fs);
    glLinkProgram(program);
    glDeleteShader(vs);
    glDeleteShader(fs);
    GLint linked = GL_FALSE;
    glGetProgramiv(program, GL_LINK_STATUS, &linked);
    if (linked != GL_TRUE) {
      GLint length = 0;
      glGetProgramiv(program, GL_INFO_LOG_LENGTH, &length);
      std::string log(size_t(std::max(length, 1)), '\0');
      glGetProgramInfoLog(program, GLint(log.size()), nullptr, log.data());
      glDeleteProgram(program);
      program = 0;
      throw std::runtime_error("chrome shader link failed: " + log);
    }
    glGenFramebuffers(1, &framebuffer);
  }

  static const char *fragmentSource() {
    return R"GLSL(#version 120
uniform int uLayer;
uniform vec2 uSize;      // the layer, window meters
uniform int uTranspose;  // drawn a quarter turn (top-to-bottom bends)
uniform float uPx;       // meters per pixel, in window coordinates
uniform vec2 uHalf;      // the window's half size
uniform float uBezel;
uniform vec4 uRod;       // handle offset, arm, bend, side grip length
uniform float uGripAcross; // top and bottom grip length
uniform vec2 uRodWidth;  // slid out, under the laser
uniform vec4 uRow;       // close x, pill x, row y, disc diameter
uniform vec2 uPill;      // pill width, height
uniform float uReveal[10];
uniform float uWideAmt[10];
uniform float uHeld[10];
uniform vec3 uTip;       // x, y, on
uniform vec4 uFocus;     // gaze highlight building, snap 0-1, focused, stare commit progress

// Palladium silver-lavender
const vec3 TOP = vec3(.984, .980, .992), FACE = vec3(.890, .875, .918), BOTTOM = vec3(.796, .769, .839);
const vec3 TOP_IN = vec3(.851, .827, .890), FACE_IN = vec3(.804, .776, .851), BOTTOM_IN = vec3(.725, .690, .788);
const vec3 BORDER = vec3(.553, .510, .627);
const vec3 ACTIVE = vec3(.102, .624, 1.);           // #1a9fff, the chrome's blue: the active window
const vec3 HIGHLIGHT = vec3(.66, .84, 1.);          // pale: only looked at
const vec3 LIGHT = vec3(-.4511, .5514, .7018);     // from the upper left, in front
const vec3 HALFWAY = vec3(-.2497, .3052, .9190);   // normalize(LIGHT + view)
const vec2 SHEEN = vec2(.9063, -.4226);            // Palladium's 115 degree gradient
const vec2 SHADOW = vec2(-.5, .866);               // edges facing the light cast down-right

vec3 metal(float t, bool pressed) {
  t = clamp(t, 0., 1.);
  vec3 a = pressed ? TOP_IN : TOP, b = pressed ? FACE_IN : FACE, c = pressed ? BOTTOM_IN : BOTTOM;
  return t < .48 ? mix(a, b, t / .48) : mix(b, c, (t - .48) / .52);
}
vec2 closest(vec2 p, vec2 a, vec2 b) {
  vec2 v = b - a;
  return a + v * clamp(dot(p - a, v) / max(dot(v, v), 1e-12), 0., 1.);
}
vec2 away(vec2 v, vec2 fallback) { float n = length(v); return n > 1e-7 ? v / n : fallback; }

// The laser's tip light (Stargate's tip_light, in the laser's blue): a pool
// fading over ~5 cm, strongest on faces turned towards the tip.
vec3 tipLight(vec3 c, vec2 p, vec3 n) {
  if (uTip.z <= 0.) return c;
  vec3 d = vec3(uTip.xy - p, .008);
  float f = length(d.xy) / .05;
  if (f >= 1.) return c;
  float facing = clamp(dot(n, normalize(d)), 0., 1.);
  vec4 col = f < .22 ? mix(vec4(.82, .92, 1., .95), vec4(.49, .76, 1., .72), f / .22)
           : f < .55 ? mix(vec4(.49, .76, 1., .72), vec4(.23, .62, 1., .3), (f - .22) / .33)
                     : mix(vec4(.23, .62, 1., .3), vec4(.10, .62, 1., 0.), (f - .55) / .45);
  return mix(c, col.rgb, col.a * (.5 + .5 * facing));
}
vec3 lit(vec3 base, vec3 n) {
  float diffuse = dot(n, LIGHT);
  float spec = pow(max(dot(n, HALFWAY), 0.), 40.) * .32;
  return base * (.68 + .42 * diffuse) + vec3(spec);
}
// Held: light inside the recess, brightest where it meets the walls.
vec3 innerGlow(vec3 c, float inside, float width) {
  float walls = exp(-inside / (.2 * width)), rim = exp(-inside / (.06 * width));
  c = mix(c, vec3(.44, .74, 1.), .1);
  c = mix(c, vec3(.18, .62, 1.), .85 * walls);
  return mix(c, vec3(.83, .93, 1.), .9 * rim);
}

float sdBox(vec2 p, vec2 b, float r, out vec2 g) {
  vec2 q = abs(p) - b + r;
  vec2 s = vec2(p.x < 0. ? -1. : 1., p.y < 0. ? -1. : 1.);
  if (q.x > 0. && q.y > 0.) g = normalize(q) * s;
  else g = q.x > q.y ? vec2(s.x, 0.) : vec2(0., s.y);
  return length(max(q, 0.)) + min(max(q.x, q.y), 0.) - r;
}

vec4 bezel(vec2 p) {
  vec2 gO, gI;
  float b = uBezel;
  float dO = sdBox(p, uHalf + b, .7 * b, gO);  // < 0 inside the frame's outline
  float dI = sdBox(p, uHalf, .006, gI);        // > 0 outside the window
  if (dI < 0.) {
    // the bezel's soft shadow on the window's edge (it meets the picture at the front face)
    if (dI < -.04) return vec4(0.);
    float a = .40 * exp(dI / .010) * (.55 + .45 * dot(gI, SHADOW));
    return vec4(0., 0., 0., a);
  }
  // Halo just outside the bezel. Highlight (eyes): soft, wide and pale.
  // Active window: a steady, tight line of chrome blue (#1a9fff), flaring
  // once at the snap. A long stare tightens the highlight towards it.
  float focused = uFocus.z, snap = uFocus.y, progress = uFocus.w;
  float settle = smoothstep(0., 1., snap), flare = focused > .5 && snap < 1. ? .3 * sin(snap * 3.14159) : 0.;
  float haloWidth = focused > .5 ? mix(.007, .0055, settle) : mix(.010, .006, progress);
  float haloAmt = focused > .5 ? mix(1., .85, settle) + flare : uFocus.x * mix(.45, .75, progress);
  vec3 haloColor = focused > .5 ? ACTIVE : mix(HIGHLIGHT, ACTIVE, progress);
  if (dO > uPx) {
    float h = haloAmt * exp(-(dO - uPx) / haloWidth);
    return h > .002 ? vec4(haloColor * h, h) : vec4(0.);
  }
  float s = dI, ich = .26 * b, ch = .16 * b;
  vec3 n = vec3(0., 0., 1.);
  if (s < ich) n.xy = -gI * .75;                // inner chamfer, facing the content
  else if (-dO < ch) n.xy = gO * .75;           // outer chamfer, facing out
  float groove = s - .55 * b;                   // a chiseled line round the face
  if (abs(groove) < .05 * b) n.xy += -sign(groove) * .8 * gI;
  n = normalize(n);
  vec3 c = lit(metal(dot(p / (uHalf + b), SHEEN) * .5 + .5, false), n);
  if (-dO < uPx * 1.2) c = mix(c, BORDER, .6);
  if (s < uPx * 1.2) c = mix(c, BORDER, .5);
  c = tipLight(c, p, n);
  // Highlight: a pale wash builds across the bezel while the window is
  // looked at; a long stare gathers it into the edge line. The active window
  // keeps a crisp, saturated line along its edge (no wash), after one flare.
  c = mix(c, HIGHLIGHT, .45 * uFocus.x * (1. - .6 * progress));
  if (focused < .5 && progress > 0.) {
    float width = mix(.45 * b, .3 * b, progress);
    c = mix(c, mix(HIGHLIGHT, ACTIVE, progress), .9 * uFocus.x * progress * exp(-s / max(width, 1e-4)));
  }
  if (focused > .5) {
    float width = mix(.45 * b, .3 * b, settle);
    c = mix(c, ACTIVE, min(1., .95 + flare) * exp(-s / max(width, 1e-4)));
    c = mix(c, ACTIVE, .18 * (1. - settle));
  }
  float a = clamp(.5 - dO / uPx, 0., 1.) * clamp(.5 + s / uPx, 0., 1.);
  return vec4(c * a, a);
}

// One control: signed distance d to its outline, the outward direction g,
// its half width R, and what its texture needs.
void piece(int i, vec2 p, out float d, out vec2 g, out float R, out vec2 M, out vec2 T, out vec2 centre,
           out float extent) {
  float hx = uHalf.x, hy = uHalf.y;
  float slide = (1. - (i < 2 ? 1. : uReveal[i])) * .012;  // slides out from just inside
  float o = uRod.x - slide;
  vec2 cp;
  if (i == 0) {           // the move pill
    centre = vec2(uRow.y, uRow.z);
    float hl = uPill.x / 2. - uPill.y / 2.;
    cp = closest(p, centre - vec2(hl, 0.), centre + vec2(hl, 0.));
    R = uPill.y / 2.;
    M = centre; T = vec2(1., 0.); extent = uPill.x / 2.;
  } else if (i == 1) {    // the close disc
    centre = vec2(uRow.x, uRow.z);
    cp = centre;
    R = uRow.w / 2.;
    M = centre; T = vec2(1., 0.); extent = R;
  } else if (i < 6) {     // a corner: one L with a rounded bend
    float sx = (i == 3 || i == 5) ? 1. : -1., sy = (i == 2 || i == 3) ? 1. : -1.;
    vec2 B = vec2(sx * (hx + o), sy * (hy + o));
    float r = uRod.z, a = uRod.y;
    vec2 C = B - vec2(sx, sy) * r;
    vec2 c1 = closest(p, vec2(B.x - sx * r, B.y), vec2(B.x - sx * a, B.y));
    vec2 c2 = closest(p, vec2(B.x, B.y - sy * r), vec2(B.x, B.y - sy * a));
    cp = length(p - c1) < length(p - c2) ? c1 : c2;
    vec2 q = p - C;
    if (q.x * sx >= 0. && q.y * sy >= 0.) {
      vec2 c3 = C + away(q, vec2(sx, sy) * .7071) * r;
      if (length(p - c3) < length(p - cp)) cp = c3;
    }
    R = mix(uRodWidth.x, uRodWidth.y, uWideAmt[i]) / 2.;
    M = C + vec2(sx, sy) * r * .7071;
    T = normalize(vec2(sx, -sy));
    centre = B - vec2(sx, sy) * a * .5; extent = a * .7;
  } else if (i < 8) {     // a side grip
    float gx = (i == 6 ? -1. : 1.) * (hx + o);
    cp = closest(p, vec2(gx, -uRod.w / 2.), vec2(gx, uRod.w / 2.));
    R = mix(uRodWidth.x, uRodWidth.y, uWideAmt[i]) / 2.;
    M = vec2(gx, 0.); T = vec2(0., 1.);
    centre = M; extent = uRod.w / 2.;
  } else {                // the top or bottom grip
    float gy = (i == 9 ? -1. : 1.) * (hy + o);
    cp = closest(p, vec2(-uGripAcross / 2., gy), vec2(uGripAcross / 2., gy));
    R = mix(uRodWidth.x, uRodWidth.y, uWideAmt[i]) / 2.;
    M = vec2(0., gy); T = vec2(1., 0.);
    centre = M; extent = uGripAcross / 2.;
  }
  g = away(p - cp, vec2(0., 1.));
  d = length(p - cp) - R;
}

// Three V-cut grooves across a grip at u = -gap, 0, gap.
vec2 grooves(float u, float gap, float w) {
  float best = 1e3;
  for (int k = -1; k <= 1; k++) { float x = u - float(k) * gap; if (abs(x) < abs(best)) best = x; }
  return abs(best) < w ? vec2(-sign(best) * .8, 1.) : vec2(0.);
}

vec4 controls(vec2 p) {
  if (abs(p.x) < uHalf.x && abs(p.y) < uHalf.y) return vec4(0.);
  float bestD = 1e3, R = 0., ext = 1.;
  vec2 g = vec2(0.), M = vec2(0.), T = vec2(1., 0.), cen = vec2(0.);
  int best = -1;
  for (int i = 0; i < 10; i++) {
    if (uReveal[i] <= .001) continue;
    float d, r, e; vec2 gi, m, t, c;
    piece(i, p, d, gi, r, m, t, c, e);
    if (d < bestD) { bestD = d; best = i; g = gi; R = r; M = m; T = t; cen = c; ext = e; }
  }
  if (best < 0 || bestD > .02) return vec4(0.);
  float reveal = uReveal[best];
  float held = uHeld[best];
  bool pressed = held > .5;
  float d = bestD;
  // the soft shadow it casts on the layers behind (not when pressed in)
  float shadow = pressed ? 0. : .28 * exp(-max(d, 0.) / .0035) * (.55 + .45 * dot(g, -SHADOW));
  vec3 n;
  float inside = max(-d, 0.);
  if (best < 2) {
    // flat top with a bevelled edge
    float band = .22 * R;
    n = vec3(inside < band ? g * .85 * (1. - inside / band) : vec2(0.), 1.);
  } else {
    // a rod: round across
    float s = clamp((d + R) / R, 0., 1.);
    n = vec3(g * s * .9, sqrt(max(1. - s * s, .02)));
  }
  // grip texture: grooves across the pill always, and across a rod under the laser
  if (best == 0) {
    if (abs(p.y - M.y) < .21 * uPill.y) n.xy += T * grooves(dot(p - M, T), .14 * uPill.y, .034 * uPill.y).x;
  } else if (best >= 2 && uWideAmt[best] > .05) {
    float u = dot(p - M, T);
    if (length(p - M) < 3. * R) n.xy += T * grooves(u, .55 * R, .15 * R).x * uWideAmt[best];
  }
  if (best == 1) {
    // the engraved X: two V-cuts across the disc's centre
    vec2 q = p - cen;
    float w = .067 * uRow.w, arm = .25 * uRow.w;
    vec2 e1 = vec2(.7071, .7071), e2 = vec2(-.7071, .7071);
    float s1 = dot(q, e2), a1 = dot(q, e1), s2 = dot(q, e1), a2 = dot(q, e2);
    if (abs(a1) < arm && abs(s1) < w) n.xy += -sign(s1) * .9 * e2;
    else if (abs(a2) < arm && abs(s2) < w) n.xy += -sign(s2) * .9 * e1;
  }
  if (pressed) n.xy = -n.xy;   // pressed in: the bevel flips
  n = normalize(n);
  vec3 c = lit(metal(dot(p - cen, SHEEN) / (2. * ext) + .5, pressed), n);
  if (inside < uPx * 1.2) c = mix(c, pressed ? vec3(.36, .56, .77) : BORDER, .55);
  c = tipLight(c, p, n);
  if (pressed) c = innerGlow(c, inside, best < 2 ? .9 * R : 2. * R);
  float a = clamp(.5 - d / uPx, 0., 1.) * reveal;
  shadow *= reveal;
  return vec4(c * a, a + shadow * (1. - a));
}

void main() {
  vec2 uv = gl_TexCoord[0].st;   // t = 0 is the top row
  if (uTranspose == 1) uv = vec2(uv.y, 1. - uv.x);
  vec2 p = vec2((uv.x - .5) * uSize.x, (.5 - uv.y) * uSize.y);
  gl_FragColor = uLayer == 0 ? bezel(p) : controls(p);
}
)GLSL";
  }
};

} // namespace fw
