# Import from my Quest

Move games you own from your own Meta Quest onto the Steam Frame. Plug the
Quest into your computer by USB; Frame Control copies the game off it with
adb, checks it can run in Lepton, and installs it on the Frame like any other
APK, with its own Lepton instance and Steam library entry
([apks.md](apks.md), [vr-apks.md](vr-apks.md)).

There is no licence-check bypass, and there won't be. A game that checks its
Meta store licence is refused, with the reason. See
[What won't run](#what-wont-run-and-why) below.

## Before you start: developer mode on the Quest

adb only reaches a Quest with developer mode on:

1. Make a free Meta developer organisation at
   [developers.meta.com](https://developers.meta.com/horizon/) on a **verified**
   Meta account (Meta asks for a phone number or payment card to verify).
2. In the Meta Horizon phone app, open the headset's settings and switch
   **Developer Mode** on. Restart the Quest.
3. Plug the Quest into the computer that runs Frame Control with a USB cable
   that carries data (some charging cables don't).
4. Put the headset on. Accept **Allow USB debugging**, and tick **Always allow
   from this computer**.

Frame Control uses the adb it already uses for the Frame (an adb you
installed, else the one it bundles). On Linux, adb also needs permission to
open the Quest: add a udev rule for Meta's USB vendor id `2833`, for example
`SUBSYSTEM=="usb", ATTR{idVendor}=="2833", MODE="0660", GROUP="plugdev"`, and
be in that group. Then unplug and replug.

The Frame's own adb connections (Frame Control's SSH tunnels to Lepton,
`127.0.0.1:<port>`) are never touched. Only a device that reports itself as
Oculus or Meta, or a Quest model, counts as a Quest.

## Using it

In the app: **Android → On your Frame → Import from my Quest**. Choose **Look
for my Quest**, then **Import** next to a game. Tick **Also copy each game's
files in Android/data** to bring the game's external files too (some games
keep saves there). Progress shows in the panel; the result and any reason it
stopped appear in the message log.

A game with game data (an OBB folder, or Android/data when ticked) takes two
imports, because Android can only take that data while the game's Lepton
instance is running, and there is no way to start the instance without the
game. Frame Control never starts the game itself:

1. **Import** installs the game and says it has data to add.
2. Start the game from the Steam library and leave it on its first screen.
3. **Import** again. Frame Control sees the game open with the same version,
   copies the data in, and closes the game. Start it again to play.

A game that quits straight away without its data can't take it this way.

From a terminal:

```sh
python3 ui/frame_quest.py devices                      # what adb sees, and which is the Quest
python3 ui/frame_quest.py list                         # apps on the Quest: split or not, OBB and data sizes
python3 ui/frame_quest.py import com.example.game      # copy one to the Frame
python3 ui/frame_quest.py import com.example.game --data --serial 1WMHH000XXXXXX
```

The API is `GET /api/quest` (the Quest, or guidance when there isn't one, and
its apps) and `POST /api/quest` with
`{"action": "import", "package": "...", "serial": "...", "data": false}`,
which returns a background job (`/api/job`) with a stage and percentage.

## What an import does

1. Finds the Quest (`adb devices -l`, then `ro.product.manufacturer` and
   `ro.product.model`). If none is ready it says why: nothing connected, not
   authorised yet (the **Allow USB debugging** prompt), no USB permission
   (Linux), offline, another Android device, or several Quests.
2. Lists third-party apps (`pm list packages -3`), each with `pm path` (one
   line is a single APK, several a split APK) and the size of
   `/sdcard/Android/obb/<package>` and `/sdcard/Android/data/<package>`.
   Labels aren't shown before import: reading them means copying the APK.
3. Checks there is room on the computer (the APKs, a merged and a patched copy
   of them counted uncompressed, the OBB folder and the Android/data folder
   once, since that is streamed to the Frame), then copies the APK and its splits
   into a per-user cache folder (`Frame Control/quest-import` under the
   system's cache folder).
4. **Checks the APK before anything else is copied**, refusing:
   - Meta's Platform SDK licence check (`libovrplatformloader.so`, in the base
     or any split);
   - 32-bit-only builds (native code but no `arm64-v8a`);
   - apps that need an Android newer than Lepton's (API 30);
   - split APKs Frame Control can't merge (below).
   VrApi-only builds (`libvrapi.so`, no OpenXR loader) are flagged, as for any
   APK: they install, but won't run.
5. If the game isn't open on the Frame: installs it through the normal APK
   install (`frame_android.install`: VR detection, the launcher fix, the
   OpenXR compatibility layer, artwork), and says whether it has game data
   to add.
6. If the game is open on the Frame (the second import): refuses if the
   installed version differs from the Quest's (an open game is never
   replaced), otherwise copies the OBB folder (and `Android/data` if ticked)
   from the Quest, waits for Android to have finished booting, copies standard
   `main|patch.<version>.<package>.obb` files with the existing OBB helper
   (checked by SHA-256 inside Android, [sidequest.md](sidequest.md#obb-files)),
   streams the `Android/data` files in as a tar written straight into the SSH
   transfer, then closes the game so its next start reads them. Other files in
   the OBB folder are listed as not copied.
7. Deletes the cache folder, whether the import worked or not. A folder left
   by a crash is removed by a later import after six hours.

Only one import runs at a time. If copying game data fails, the game is
closed and the message says so; start it and import again.

### Split APKs

Lepton runs one APK per instance, so the shared install path now merges a
split APK into one (`frame_android.install(base, splits=[...])`, or
`python3 ui/frame_android.py install base.apk split1.apk ...`). Merging copies
each split's native libraries and assets into the base, drops the base
manifest's `isSplitRequired` flag, and re-signs with Frame Control's key, as
the launcher fix already does. Only splits that hold nothing but
`lib/<abi>/*.so`, `assets/` and their own signature, manifest and an empty
resource table are merged. Refused, with the reason, before anything reaches
the Frame: splits with resources (screen density or language), code
(`classes*.dex`), any other file, a manifest that declares components or
libraries (`<service>`, `<uses-library>`...), feature splits, and splits of
another package. The merged files are held in memory while the APK is
repacked and signed (as for every patched APK), so a merge adds at most
512 MB per file and 1 GB in all, checked from the headers and again while
reading, with each file's checksum. A merged, re-signed APK can't update a
copy signed by the publisher.

## What won't run, and why

- **Games that check their Meta store licence.** Store games call Meta's
  Platform SDK (`libovrplatformloader.so`) to confirm you bought them. That
  needs Meta's services, which only exist on Meta headsets, so on the Frame
  the game quits. Frame Control refuses these and **won't remove or work
  around the check**, and doesn't bundle, call or link to any tool that does.
  Ask the developer for a build without it, or get the game from Steam or
  itch.io. Most Meta Store games have it; most SideQuest, itch.io and
  open-source games don't.
- **32-bit games** (Quest 1 era): the Frame runs 64-bit ARM only.
- **VrApi-only games**: the Frame has OpenXR, not Meta's older VrApi.
- **Anything else that needs Meta**: avatars, Quest-only device checks,
  passthrough and other `XR_FB_*` features SteamVR lacks
  ([vr-apks.md](vr-apks.md#what-steamvrs-android-runtime-supports-verified)).
- **Private app data** (`/data/data/<package>`) can't be read over adb without
  root, so only saves in `Android/data` come across.

What usually works: OpenXR games without Meta's licence check, for example
open-source and itch.io builds you sideloaded onto the Quest.

Only import games you own, for your own use.

## Evidence and limits

**Verified locally** with a fake adb (`tests/fakeadb.py`, `tests/test_quest.py`):
device detection (Quest, the Frame's tunnel, the Frame over USB, other
Android devices, unauthorised, no permissions, offline, none, several),
package listing and split detection, the refusals, the order of copies
(APK checked before the OBB is copied), the two-step install then data copy
(the game is never started by Frame Control, never replaced while open, and
closed after its data is copied), split merging and signing, the split
refusals and size limits (including a forged zip bomb), the space estimate,
and cache clean-up after success and failure.

**Not verified:** a real Quest (none was available), Quest system apps in
`pm list packages -3`, real split APKs from the Meta Store, OBB and
`Android/data` writes into a running Lepton instance on a real Frame
(the OBB helper itself is also unverified there), whether games stay open
without their OBB long enough for the second import, and the Linux udev rule.
Large games are copied into memory while being merged or patched, as every
patched APK already is.
