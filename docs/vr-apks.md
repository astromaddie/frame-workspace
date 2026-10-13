# VR APKs and Quest games in Lepton

What it takes to run an immersive (OpenXR) Android app, including Meta Quest
builds, on the Frame. Checked on SteamOS BUILD_ID 20260925.6191901, Lepton
v2.8.14 (rootfs v2.8.11), SteamVR 2.18.1, on 2026-09-28, unless marked
**inferred**.

## How a VR APK reaches SteamVR (verified)

- Lepton ships a standard Khronos system runtime manifest,
  `/vendor/etc/openxr/1/active_runtime.json`, pointing at SteamVR's Android
  client, `/data/steamvr/runtime/bin/androidarm64/vrclient.so`. That is the
  host's `/opt/steamvr/bin/androidarm64/`, bind-mounted in.
- An APK's own Khronos-style `libopenxr_loader.so` tries the runtime brokers
  (`org.khronos.openxr.runtime_broker`, `…system_runtime_broker`), finds
  neither, then falls back to that manifest. Nothing in the APK has to change
  for discovery.
- Install the APK without the flatscreen marker
  (`python3 ui/frame_android.py install app.apk --vr`). The marker only
  controls Lepton's 2D Android surface; the app itself has to start an
  OpenXR session.
- Lepton also loads Valve's `XR_APILAYER_VALVE_fdm_injection` layer from
  `/vendor/etc/openxr/1/api_layers/implicit.d/`. Only layers in Valve's own
  directories are picked up (`liblepton/vulkan_layers.sh`), so a third-party
  layer has to ship inside the APK.

**Open Brush 2.32.29, the Quest APK from its GitHub release (Unity OpenXR,
Vulkan), works unmodified.** Its manifest already has `LAUNCHER` next to
`com.oculus.intent.category.VR`. Unity asked for OpenXR 1.1, got
`XR_ERROR_API_VERSION_UNSUPPORTED`, retried with 1.0 and succeeded. SteamVR
took it as the scene app, created Touch, simple-controller and Frame-controller
bindings, and the session reached `XR_SESSION_STATE_FOCUSED`. A headset capture
(`ui/frame_vrshot.py`, after waking the compositor and closing the dashboard)
showed a dark sky over a mountain horizon; nobody wore the headset to confirm
it was Open Brush's scene or to try drawing.

**Khronos `hello_xr` (Vulkan, 1.1.63 release APK) works unmodified:**
`Instance RuntimeName=SteamVR/OpenXR RuntimeVersion=2.18.1`, 1728×1728
swapchains per eye, session `IDLE → READY → SYNCHRONIZED` (the headset was
not being worn, so it did not reach `FOCUSED`).

## What SteamVR's Android runtime supports (verified, from `vrclient.so`)

- **OpenXR 1.0 only.** An app requesting `XR_API_VERSION_1_0` works; one
  requesting 1.1 (`XR_CURRENT_API_VERSION` in a 1.1 SDK) gets
  `XR_ERROR_API_VERSION_UNSUPPORTED` from the runtime.
- Extensions include `XR_KHR_opengl_es_enable`, `XR_KHR_vulkan_enable{,2}`,
  `XR_KHR_composition_layer_depth`, `XR_KHR_locate_spaces`,
  `XR_EXT_local_floor`, `XR_EXT_uuid`, `XR_EXT_palm_pose`,
  `XR_EXT_hand_tracking`, `XR_EXT_eye_gaze_interaction`, and these Meta ones:
  `XR_FB_display_refresh_rate`, `XR_FB_foveation{,_configuration,_vulkan}`,
  `XR_FB_space_warp`, `XR_FB_swapchain_update_state`,
  `XR_META_foveation_eye_tracked`, `XR_META_recommended_layer_resolution`,
  `XR_META_vulkan_swapchain_create_info`, `XR_META_performance_metrics`.
- Not present: `XR_FB_passthrough`, `XR_FB_hand_tracking_*`,
  `XR_FB_spatial_entity*`, `XR_FB_color_space`,
  `XR_KHR_android_thread_settings`, `XR_OCULUS_*`.
- Interaction profiles include `oculus/touch_controller`, `khr/simple_controller`,
  `valve/frame_controller` and the usual PC controllers. Valve documents Touch
  bindings as a working fallback on the Frame controllers.

## What stops a Quest APK (verified with Wolvic 1.9, `oculusvr` build)

1. **Lepton won't start it.** Lepton's `apk-info-extractor` only accepts an
   activity whose intent filter has `android.intent.action.MAIN` and
   `android.intent.category.LAUNCHER`. Quest apps use
   `com.oculus.intent.category.VR` instead, so Lepton logs `APP_ACTIVITY is
   empty` and exits. There is no override. **Fix:** add the `LAUNCHER`
   category to that intent filter and re-sign. After that, Wolvic started.
2. **OpenXR 1.1.** Wolvic's Quest build then requested OpenXR 1.1 and aborted
   on `XR_ERROR_API_VERSION_UNSUPPORTED`. Unity's OpenXR plugin retries with
   1.0 (Open Brush, above), so this mostly bites native and non-Unity apps. **Fix (inferred):** an API layer
   inside the APK that asks the runtime for 1.0 and maps the 1.1 core
   functions to the extensions the runtime does have (`XR_KHR_locate_spaces`,
   `XR_EXT_local_floor`, `XR_EXT_uuid`, `XR_EXT_palm_pose`).
3. **Lepton's missing clipboard service** still applies to VR apps. The Godot
   XR Tools demo's Quest build (itch.io) dies in `Godot.<init>` casting the
   null clipboard service to `ClipboardManager`, before any OpenXR call. See
   the clipboard table in [apks.md](apks.md).
4. **Not yet reached:** required Meta-only extensions (each app differs),
   swapchain formats (the Lynx Wolvic build needed `GL_SRGB8_ALPHA8`), and
   Meta platform services.

The loader was never the problem: Wolvic's Quest `libopenxr_loader.so` is a
Khronos-style loader and found SteamVR through `/vendor`.

## In the Steam library

Every successful APK install goes through the same mandatory artwork writer:
CLI (including `scripts/install-apk.sh`), upload, catalogue, version finder,
web download and source modules calling `frame_android.install`. Native
Linux/Windows sideloads also use it, preserving their devkit runtime wiring.
A new shortcut is rolled back if artwork fails; failure is never reported as
an installed app with a blank tile.

Artwork preference is **SteamGridDB → source images → generated fallback**.
Set the optional free key in Frame Control's **Library artwork settings**, or
`STEAMGRIDDB_API_KEY` (`FRAME_STEAMGRIDDB_API_KEY` also works). Environment
settings override the saved key. Without a key there are no provider calls or
warnings. Saved keys stay in host app data, mode 0600 on POSIX, and are never
returned by the settings API or copied to the headset. Exact title matches
(including a trailing “VR” variant) use the highest-scored returned static,
non-NSFW image in each slot. Provider failures use the next source.

Sources pass `install(apk_path, artwork={...})`: keys are `grid`, `wide`,
`hero`, `logo`, `icon`, `banner`, `feature_graphic`, `screenshot`, or a list
`screenshots`. Values are PNG/JPEG bytes or HTTP(S) URLs (12 MiB and
4096×4096 pixels maximum; any PNG depth or interlace, since the Frame's
Chromium decodes them). URLs must resolve to public addresses, follow at most
three redirects and share one deadline per install. Any source that fails,
for any reason, becomes a warning and generated art. Banners and feature graphics supply hero/wide art;
screenshots are the next fallback. Source images are cached for refresh.
All images are fitted to 600×900 portrait, 920×430 wide, 3840×1240 hero,
1280×480 logo and 256×256 icon. Explicit logos retain transparency.
Photo-based portrait, wide and hero slots are JPEG: Steam takes at most
12 MiB per slot, and on the Frame (2026-09-28) a noise-heavy 3840×1240 hero
came to more than 12 MiB as PNG, 3.7 MB as JPEG (2.7 s to render); a
landscape photo hero 5.6 MB as PNG, 0.76 MB as JPEG (0.75 s). A render that
still fails is retried once with generated art. Steam keeps a slot's `.png`
and `.jpg` side by side, so each slot is cleared before it is set.

Generated art uses the APK icon, a dominant-colour gradient, a blurred
backdrop and large foreground icon with shadow. Steam's Chromium canvas and
Motiva Sans render real text consistently regardless of the host OS; no
Pillow, host font installation or bitmap font is needed. The hero has no
title; the generated logo is a transparent title. APKs with no usable icon
get a typographic monogram. The desktop package includes the renderer.

Backfill installed Android apps without reinstalling or stopping them:

```sh
python3 ui/frame_android.py refresh-art org.godotengine.open_saber_plus
python3 ui/frame_android.py refresh-art --all
```

Devkit titles installed by Frame Control have the same command,
`python3 ui/frame_titles.py refresh-art ID|--all`. The settings panel's
refresh covers both. The API is `POST /api/android` with
`{"action":"refresh-art","all":true}` (apps and titles) or a `package`, and
`POST /api/titles` with `{"action":"refresh-art","id":…}`; each returns a
background job. Batch results retain per-item errors, and the CLIs exit
nonzero if any failed. Apps and titles without complete artwork show **Add
artwork** and `list` prints the command. Only entries marked `art_pending` at
install (a title Steam registered after an install made while it wasn't
running) are backfilled automatically, when Frame Control lists them with
Steam running (at most every five minutes), and that backfill only fills
slots Steam has no art for: names, icons, flags and any art the user set are
kept. Older installs without the flag are refreshed only on request.

Steam's app overviews carry no `devkit_gameid` (checked 2026-09-28, build
20260925.6191901, on every non-Steam shortcut). A title's shortcut is found by
its saved id, or by an executable or start folder inside
`~/devkit-game/<id>/`, read from `appDetailsStore`; never by display name.
That the devkit shortcut's exe/start folder sit inside the title folder is
inferred from `docs/sideloading.md` (`proton waitforexitandrun
"/home/steamos/devkit-game/<id>/<exe>"`), not yet seen in app details.
Devkit titles keep the VR flag Steam gave them.

**Verified on build 20260925.6191901, SteamVR 2.18.1 (2026-09-28):** both
Open Saber Plus and SuperTux were backfilled. Steam's cached portrait, wide,
hero and logo PNGs have the dimensions above; each shortcut points at its
256×256 icon. This Frame client mishandles custom-art type 4 (documented as
Icon), overwriting the wide capsule; the implementation uses custom types
0–3 and **SetShortcutIcon** separately.

Steam accepts display name, executable/start directory, icon, VR flag and
sort-as name. Android apps join **Android**, immersive apps also **Android
VR**; native sideloads join **Sideloaded**. Existing collection members and
unrelated collections are preserved (both games retained **Played**).
Dynamic/read-only collection conflicts produce warnings. The native notes
API supports a managed **Installation details** note (package, version and
source) while preserving other notes. Notes are keyed by sanitized shortcut
name, so Steam itself cannot distinguish equal-name shortcut notes. No
supported shortcut description/store-page, developer/publisher, release
metadata or custom achievement API was found; these are not fabricated.

The launcher supervises Lepton and handles TERM/INT/HUP and normal exit by
stopping its own container and child process group. A lock refuses duplicate launches;
a container still running while the lock is free was orphaned by a killed
launcher and is stopped before the new launch. Lepton doesn't inherit the
lock. Orphan recovery only stops the app's own, deterministically named
container; a Lepton host process whose launcher was killed before it created
the container may linger briefly. Removing an app or title still deletes its files when Steam isn't
running; tidying Steam's collections and artwork is best effort. Steam Stop uses `TerminateApp` with the exact
64-bit game ID string. Frame Control's Stop additionally has a direct-container
fallback. The stable instance ID and compatdata paths remain unchanged.

Lepton normally forwards the instance `SteamAppId` to Android, causing
SteamVR to associate the scene with a different, artwork-less app. The
launcher uses Lepton's supported `LEPTON_ENV_SteamAppId` passthrough to send
the actual shortcut ID to Android while retaining the stable container ID.
**Verified:** Open Saber was alive 22 seconds after Steam Play, SteamVR
identified `steam.app.3346865537`, and its scene appeared in the headset
capture without the previous blank Resume tile. Steam Stop then removed its
tracked process and stopped the container. An earlier 32-second session was
also tracked until Steam Stop. No global standby or dashboard overrides were
installed; wear detection and other user-opened overlays still apply.

**SuperTux limitation:** Steam launched and tracked it, but SDL crashed during
activity creation because Lepton lacks `ClipboardManager`. Its container
cleaned up on exit after about 17 seconds. Consequently sustained SuperTux
Play/Stop and its VR scene could not be verified. This is an APK/runtime
compatibility failure, separate from library presentation.

Evidence is under `/tmp/vrlib-evidence/` on the development Mac: final artwork
preview and three design passes, `steam-cache-final.log`,
`steam-details-targets.json`, `opensaber-identity-session.log`,
`opensaber-identity-headset.png`, and `supertux-lepton.log`. The preview is
rendered artwork, not a Steam UI screenshot; CDP screenshot capture timed
out. Authenticated SteamGridDB, Windows/Linux packaged builds and the sibling
source-search endpoint remain unverified (the public install seam is tested).

## Out of scope

- **Meta entitlement.** Apps that call the Oculus Platform SDK
  (`libovrplatformloader.so`) to check the Quest store licence need Meta's
  services. Frame Control won't work around that: no patching, no stub
  libraries, no OVRPort or FramePort. Linking the SDK doesn't mean an app
  enforces the check (Meta's VRC.Quest.Security.1 has only been
  *Recommended* since 2021, and a failed check does nothing by itself; the app
  decides), so [Import from my Quest](quest-import.md) and [My Meta
  library](meta-library.md) install these with a warning: they may quit,
  hang, or run without online features.
- **VrApi-era apps** (`libvrapi.so`, before OpenXR) need an API translator,
  not a patch. They install with a warning that they may not run: some
  OpenXR games ship `libvrapi.so` without depending on it.

## Frame Control does this for you

APK uploads and `python3 ui/frame_android.py install app.apk` detect VR
manifest categories, Samsung's `vr_only` flag and the arm64 OpenXR loader.
VR apps default to immersive mode without the flatscreen marker. The upload
selector or CLI `--flat` / `--vr` overrides that choice. Compatibility notes
identify legacy VrApi, Meta platform SDK and OpenXR libraries.

Lepton only starts an `<activity>` whose MAIN intent filter has LAUNCHER; it
ignores `<activity-alias>`, which is where Godot 4 exports put LAUNCHER. When no
real activity qualifies, Frame Control adds LAUNCHER to the VR activity's MAIN
filter, or to the activity the launcher alias targets, then repacks and v2-signs
the APK locally before copying it; `meta.json` records
`"patched": ["launcher"]`. Unchanged ZIP members retain their compressed
bytes; stored libraries are aligned to 16 KiB. The RSA signing identity lives
in Frame Control's per-user app-data directory as `apk-signing-key.json`
(mode 0600). Keep this key to preserve the signer on subsequent patched
updates. A re-signed APK cannot update an installation signed by its original
publisher; Android also treats it as a different signer for signature checks.

VR apps with an arm64 OpenXR loader also get the OpenXR compatibility layer
([frame/openxr-compat](../frame/openxr-compat/README.md)): an implicit API
layer in the APK's `assets/openxr/1/api_layers/implicit.d/`, which the app's
own loader picks up next to Valve's layer. It asks SteamVR for OpenXR 1.0 when
the app wants 1.1 and enables the extensions that became 1.1 core; maps
`xrLocateSpaces` to `xrLocateSpacesKHR` and `grip_surface` to `palm_ext`; drops
1.1 controller profiles SteamVR doesn't know; stubs
`XR_KHR_android_thread_settings` and `XR_OCULUS_android_session_state_enable`;
and keeps the current refresh rate when SteamVR refuses a requested one.
`meta.json` records `"patched": ["openxr-compat"]`. Skip it with
`install … --no-xr-compat`. Its decisions go to logcat under `FrameXrCompat`.
If this copy of Frame Control is missing the layer's library (an incomplete
install, or a file removed after installing), VR apps install without it and
the install message says so; OpenXR 1.0 apps still run. Release builds fail
to package without it (`app/build/check-resources.js`).

Verified on the headset (2026-09-28):

- **Wolvic 1.9, Quest build**, installed as downloaded: Frame Control added
  `LAUNCHER` and the layer. The layer turned OpenXR 1.1.48 into 1.0.63, the
  instance and session were created, and a 144 Hz refresh request that SteamVR
  refused was kept at the current rate. The session reached `SYNCHRONIZED`;
  then Wolvic's Gecko engine crashed (null SIGSEGV on its Gecko thread, the
  same crash its Lynx build has), which is Wolvic's, not OpenXR's.
- **Open Brush, Quest build**, with the layer: 1.1.54 → 1.0.63, the thread
  settings stub in use, `bytedance/pico4_controller` bindings dropped, and the
  session reached `FOCUSED`, the same as without the layer.

Inspect or prepare an APK without contacting the headset:

```sh
python3 ui/frame_android.py info app.apk
python3 ui/frame_android.py patch app.apk patched.apk
python3 ui/frame_android.py patch app.apk patched.apk --add assets/openxr/1/api_layers/implicit.d/X.json=X.json --add lib/arm64-v8a/libX.so=libX.so
```

The patch fixes Lepton's launch-category requirement. It does not supply an
OpenXR 1.1 translation layer, Meta services or a VrApi implementation.
