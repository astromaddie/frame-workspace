# Shelf And Emerging Menu Validation — 2026-10-21

Branch `fw15b/shelf-menubar`; source base `a9f1f0e`. Native-only revision:
status joins the D1 shelf, the menu slides behind the bezel, and pills reveal
only below their own unobscured window. No Mac/UI source, headset, displays,
or system settings were changed.

## Source Checks

`scripts/build-workspace-native.sh` passed with the macOS GL adapter documented
in `spatial-scene-validation.md`, without warnings. Built-in and explicit
`--self-test` passed; `git diff --check` and scene-generator AST checks passed.
Build free-memory checks were 31–39%, all above the required 15%.
Final binary SHA-256:
`e3ada19d91c082e976218c3654607fb6fab078969315ac2c2f771e8a91414824`.
The scene pair and menu-transition frames were rendered on
`ddfae24ff5df0894f7349e287ffcb4098932ddc0c9f76d822deae450ff5c0cda`;
the only subsequent rendering change joined absolute icon paths correctly.
The affected wall and shelf-close-up scenes were rerendered on the final binary.
The scene pair uses relative icons; menu close-ups contain no icons.

Self-tests cover shelf extension/top-edge preservation, natural and fitted status
scale/right alignment, tall narrow status preserving natural scale with downward band growth, fitted status-menu origin and clearance above the shelf;
menu lip/full/intermediate crop, point mapping, covered-edge exclusion, depth,
bent tangents, animation/grace and metadata-only held-picture changes; pill
zones, hidden hit exclusion, reveal/fade/grace, hover and drag retention.
Host hand routing tests distinguish non-consuming hidden-pill reveal from hit
ownership and check nearest, focused-window, auxiliary/shelf occlusion and drag
ownership. Wall placement remains unchanged. The scratch-based fixtures exposed an absolute-icon
path join defect in the renderer; path joining now preserves absolute fixture paths,
while production inventory parsing continues to require bare icon filenames.

## Native Integration Checks

All checks used the final binary above and the local CGL bootstrap below.
The 37 C/C++ source/header hashes in its build metadata matched the final tree.

- `python3 native/tests_scene.py native/build/frame-workspace-native` passed
  in 161.45 s: flat/even/split/vertical texture orientation, camera changes,
  foreground ordering, premultiplied alpha and invalid inputs.
- The unchanged host suite hit its 8 s first-panel-readiness deadline.
  A scratch-only copy with 30 s readiness waits passed the main host scenario:
  two streams, still/live, controls/pose, lifecycle/failure isolation, spatial
  thumbnails, switcher sequence handling, Desktop isolation and an empty host.
- The auxiliary-surface scenario passed separately in 47.53 s with 90 s local
  readiness waits and its host lifetime extended from 60 to 240 s. It covered
  four roles across five streams, show/hide/equal/newer/older/re-show/hot gating,
  focus coordinates/requests, pose/control exclusion and Desktop isolation.
  Both the native host and driver exited 0. Earlier shorter local attempts were
  inconclusive; the surface phase's earlier exit reason was not captured.

Only scratch copies changed readiness deadlines and host lifetime. Repository
tests, assertions, stream sequences and fences stayed unchanged. No native test
process remained after verification. These checks establish local host behavior,
not tracked interaction or headset acceptance.

## Render Reproduction

Generate task-local inputs (absolute picture paths keep scratch self-contained):

```sh
python3 native/scene/generate-wall-scenes.py "$scratch/wall"
python3 native/scene/generate-chrome-scenes.py "$scratch/chrome"
native/build/frame-workspace-native --render-scene native/scene/example.json native/evidence/spatial-scene.png
native/build/frame-workspace-native --render-scene native/scene/example-slim.json native/evidence/spatial-scene-slim.png
native/build/frame-workspace-native --render-scene "$scratch/wall/wall-6.json" native/evidence/spatial-wall-6.png
native/build/frame-workspace-native --render-scene "$scratch/wall/wall-9.json" native/evidence/spatial-wall-9.png --fov 125
native/build/frame-workspace-native --render-scene "$scratch/chrome/shelf-status.json" native/evidence/shelf-status.png --eye 0,-.506,0 --fov 28 --size 1600x700
for phase in slim halfway hot; do
  native/build/frame-workspace-native --render-scene "$scratch/chrome/menubar-$phase.json" "$scratch/chrome/menubar-$phase.png" --eye -.12,.44,0 --fov 50 --size 900x380
done
python3 native/scene/generate-chrome-scenes.py "$scratch/chrome" native/evidence/menubar-emerging.png
```

### Local Graphics Bootstrap

Normal SDL startup and a process-local `SDL_MAC_BACKGROUND_APP=1` retry stalled
inside AppKit menu registration / SkyLight display queries before GL. Only the
task's render processes were terminated. A temporary interpose library replaced
SDL window/context bootstrap with an accelerated offscreen CGL context. The first
successful context still spent about 52 s in `CGLChoosePixelFormat` / SkyLight;
it then reported **Apple / Apple M4** and ran the actual scene compositor and
production shaders. This replaces no material, placement or pixel rendering code.
It creates no window/virtual display and changes no system configuration.

To reproduce only when macOS SDL startup is blocked, put this source in task
scratch as `cgl-bootstrap.c` (do not ship the resulting library):

```c
#include <OpenGL/OpenGL.h>
#include <SDL.h>
#include <stdlib.h>
static int init(Uint32 flags) { (void)flags; return 0; }
static int attr(SDL_GLattr a,int v) { (void)a;(void)v;return 0; }
static SDL_Window *window(const char *t,int x,int y,int w,int h,Uint32 f) {
  (void)t;(void)x;(void)y;(void)w;(void)h;(void)f;return (SDL_Window *)1;
}
static SDL_GLContext context(SDL_Window *w) {
  (void)w; CGLPixelFormatObj pf; GLint n; CGLContextObj ctx;
  CGLPixelFormatAttribute attrs[]={kCGLPFAAccelerated,kCGLPFAAllowOfflineRenderers,0};
  if(CGLChoosePixelFormat(attrs,&pf,&n)!=kCGLNoError)return NULL;
  CGLError err=CGLCreateContext(pf,NULL,&ctx); CGLDestroyPixelFormat(pf);
  if(err!=kCGLNoError)return NULL;
  CGLSetCurrentContext(ctx);return ctx;
}
static void deleteContext(SDL_GLContext c) { CGLSetCurrentContext(NULL);CGLDestroyContext(c); }
static void destroyWindow(SDL_Window *w) { (void)w; }
static void quit(void) {}
#define INTERPOSE(fn,orig) __attribute__((used)) static struct {const void *replacement,*original;} interpose_##fn __attribute__((section("__DATA,__interpose")))={(const void *)fn,(const void *)orig};
INTERPOSE(init,SDL_Init)
INTERPOSE(attr,SDL_GL_SetAttribute)
INTERPOSE(window,SDL_CreateWindow)
INTERPOSE(context,SDL_GL_CreateContext)
INTERPOSE(deleteContext,SDL_GL_DeleteContext)
INTERPOSE(destroyWindow,SDL_DestroyWindow)
INTERPOSE(quit,SDL_Quit)
```

```sh
memory_pressure | tail -1 # Wait/retry below 15% before compiling.
cc -dynamiclib -DGL_SILENCE_DEPRECATION -I/opt/homebrew/include/SDL2 "$scratch/cgl-bootstrap.c" -framework OpenGL -L/opt/homebrew/lib -lSDL2 -o "$scratch/cgl-bootstrap.dylib"
export DYLD_INSERT_LIBRARIES="$scratch/cgl-bootstrap.dylib"
# Run the same commands above; unset DYLD_INSERT_LIBRARIES afterwards.
```

These are synthetic, monoscopic accelerated GL scenes. They do not establish
SteamVR compositor behavior, real controller interaction, optical readability,
or worn acceptance. Existing source-scale limitation remains: the native host
estimates window metres per point from an uncapped 2x capture; capped video needs
the companion's exact point scale to be conveyed separately.

## Companion Checks

The command `(cd tests && python3 -m unittest -v test_native_pose_launcher
test_macview)`: 124 tests in 146.60 s, one expected Accessibility skip; two
real-helper HTTP timeouts (bad-frame status recovery and Workspace preflight)
and one missing timing echo within 10 s. The targeted three-test retry used a
45 s local HTTP timeout without repository changes: Workspace parser-only
validation passed; bad-frame status still timed out and timing echo still failed.
Display-creation and termination lifecycle test flags were both explicitly zero.
Launcher and pure companion checks passed. No further companion retry was made.

## Final Evidence And Cleanup

The scene pair and side-by-side menu transition were opened and inspected.
Text remains upright across horizontal and vertical bends. The single laser-targeted
window shows its pill/close disc; other windows do not. Slim menu titles are hidden,
halfway titles are clipped by the bezel, and hot titles are fully visible inside
the panel's distinct top/side bevel. The bezel covers the lower edge in all states.
The status picture sits inside one taller shelf, below the icon row.

Both wall scenes were compared with `docs/frame-workspace/evidence/spatial-layout-l2.png`.
They retain the two-row, centred/left/right arrangement and shelf clearance;
portrait inputs preserve aspect. The nine-window scene uses the documented
125-degree diagnostic projection, which enlarges the outer rectilinear edges.
No demonstrable wall geometry defect required correction.

Generated binaries/adapters and temporary scene inputs were retained only until
the final integration checks completed, then removed as described below.

All six retained PNGs were opened after their applicable final render. The corrected
wall/shelf rerenders restore all four icons; the shelf close-up includes its full
height. Final retained evidence:

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `spatial-scene.png` | 247253 | `8aa218e80cdf235594ea0fc1862112f97b17cb8586396c9a2f3ac05d57fd5d35` |
| `spatial-scene-slim.png` | 240148 | `bcd412e121d4908904d434eb9e7150739b33f5e2c20f22dc4e4c42b7e179d5b3` |
| `shelf-status.png` | 100329 | `331ec707ac5a02d79fc0d57a425b6c06c21ef00aa386171202b4ff5a6d3b512d` |
| `menubar-emerging.png` | 77475 | `54e90cb0629198de245ca9fa02ce153363870c9faad22856e0129b3fd6dd6e2a` |
| `spatial-wall-6.png` | 221241 | `80fe5917fe0cf9bb0cc0396660bd9a0e630b6a980014b58f675e4a5d02dd1356` |
| `spatial-wall-9.png` | 277462 | `17220ac1d7678f2cda4e1c9b0cf705edd5c2d1c98dbb6b8f890952a072a00243` |

Cleanup removed `native/build` and the task's `scratchpad/chrome2`: 35 generated
files, 7207069 logical bytes and 7335936 allocated file bytes (7.00 MiB). These
were binaries/debug symbols/build metadata, GL adapters, temporary manifests,
test copies and logs. An earlier cleanup removed 199278 logical bytes of obsolete
process samples after preserving the diagnostic stacks and reproduction above.
Source, the six current evidence PNGs and their reproducible inputs/generators
remain. No active work or user-owned data was removed.
