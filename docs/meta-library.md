# My Meta library (Experimental)

Install Quest games your Meta account owns straight from Meta, test whether
they run on the Steam Frame, and add the result to the shared compatibility
reports. It is off until you connect, and it lives in **Android → My Meta
library**.

**Read this first.** My Meta library uses Meta's undocumented internal API
with your own Meta access token, as the open-source tools QAVS, DownQuest and
BSManager do. It may stop working whenever Meta changes things, and using it
may be against Meta's terms. Your account is your responsibility. Frame
Control is unofficial and not made or endorsed by Meta or Valve.

What it will and won't do:

- It lists and downloads only games your account owns (its active
  entitlements). It never downloads anything else.
- It installs games as Meta ships them. There is **no licence-check bypass**:
  no patching, no stub libraries, no OVRPort or FramePort. A game that
  enforces Meta's licence check may quit or hang on the Frame, because that
  check needs Meta's services, which the Frame doesn't have.
- Game files go only to a cache folder on your computer and then to your
  Frame. They are deleted from the cache after each install, and never
  uploaded or shared. Reports carry results only (below), never files or your
  token.

## Your token

Frame Control doesn't sign in to Meta for you and doesn't read your browser's
cookies. You copy your own token and paste it in. The two ways BSManager's
guide describes
([How to obtain your Oculus Token](https://github.com/Zagrios/bs-manager/wiki/How-to-obtain-your-Oculus-Token)):

1. **From the Meta website.** Sign in at `https://secure.oculus.com`. Open the
   browser's developer tools, then **Application** (Chrome, Edge) or
   **Storage** (Firefox) → **Cookies** → `https://secure.oculus.com`. Copy the
   value of `oc_ac_at`. It starts with `OC`.
2. **From the Meta Quest PC app** (Windows). Press **Ctrl+Shift+I**, open
   **Network**, press **Ctrl+R**, type `graph` in the filter, open the first
   request, and copy the access token from its **Payload**. It starts with
   `FRL`.

Treat it like a password: anyone with it can act as your Meta account. Signing
out of the Meta website or app usually ends it; then connect again with a new
one.

Frame Control refuses values that can't be a user token before sending them
anywhere: anything not starting with `OC` or `FRL`, URL-encoded copies (with
`%`), app tokens (with `|` or `:`), and cut-off values.

## Using it

1. Tick **I understand…**, paste your token and choose **Connect**. Frame
   Control asks Meta "who am I" with it (a read-only query); only a token Meta
   accepts is kept.
2. Your Quest games are listed with their version, size, game data, and the
   community verdict from the compatibility database when there is one.
3. **Install** downloads the game and installs it on the Frame, as [Import
   from my Quest](quest-import.md) does. A game with OBB game data then needs
   a second **Install** while you have it open on the Frame: Frame Control
   downloads the data, copies it in and closes the game.
4. **Install and test** does the same, then starts the game itself (twice if
   it has game data to add: once to add it, once for the test), watches it for
   about 30 seconds, and records what happened. **Test again** repeats only
   the test.
5. **Disconnect** deletes the token from your computer. Games already on the
   Frame stay.

From a terminal (the token is read from standard input, never an argument):

```sh
python3 ui/frame_meta.py connect --token-stdin      # paste the token, press Enter
python3 ui/frame_meta.py status
python3 ui/frame_meta.py list [--refresh]           # owned Quest games, with community verdicts
python3 ui/frame_meta.py download APP_ID            # into the local install cache (a later download clears it after 6 hours)
python3 ui/frame_meta.py install APP_ID [--test] [--allow-downgrade]
python3 ui/frame_meta.py test com.example.game [--wait 30] [--no-report]
python3 ui/frame_meta.py disconnect
```

The local API: `GET /api/meta` (connected or not, never the token),
`GET /api/meta?library=1[&refresh=1]` (the games), and `POST /api/meta` with
`{"action": "connect", "token": "...", "acknowledged": true}`,
`{"action": "disconnect"}`, `{"action": "install", "app": "APP_ID", "test": false,
"allowDowngrade": false}` or `{"action": "test", "package": "..."}`. Install and
test return a background job (`/api/job`).

## What an install does

1. Checks the game is in your library (by its Meta app id), and that there's
   room on this computer for the APK, a patched copy of it and the game data.
2. Downloads the APK from Meta's download server, within the size Meta
   declared (and at most 2 GB), checking its SHA-256 if Meta gives one, and
   that it is an APK of the package and version Meta said.
3. Runs Import from my Quest's checks. Refused: 32-bit-only builds (no
   `arm64-v8a`), and apps that need an Android newer than Lepton's (API 30).
   Installed with a warning: games that use Meta's licence check
   (`libovrplatformloader.so`), and VrApi-only games ("may not run").
4. Installs it with `frame_android.install` (VR detection, the launcher fix,
   the OpenXR compatibility layer, artwork, a Steam library entry). An older
   copy never silently replaces a newer one on the Frame unless you tick
   **Allow replacing a newer version**.
5. Game data: the app's OBB (saved as Meta names it when that is a standard
   `main|patch.<version>.<package>.obb` name, otherwise as
   `main.<version code>.<package>.obb`) and any *required* asset files with
   standard OBB names, at most 8 GB each. One install downloads at most 16 GB
   in all, APK included, counted as the bytes arrive (so files whose size Meta
   doesn't give count too); a file that goes past it is stopped and removed.
   The data is copied into the
   running game by the same two-step helper Import from my Quest uses. Other
   asset files (DLC packs, `.pak` files a game fetches through Meta's
   Platform SDK) aren't downloaded; the result lists them.
6. Deletes everything it downloaded, whether the install worked or not.
   Downloads left by a crash are removed after six hours.

## The test, and the report

The test starts the game, waits for its Lepton instance, then looks every 10
seconds for 30 seconds: is the game's process alive, and what does its Android
log (main, system and crash buffers) say. It recognises:

| In the log | Cause given |
|---|---|
| "entitlement check failed", "not entitled", `ovr_Entitlement…` or `IsUserEntitledToApplication` reporting a failure (a call that passes doesn't count) | Meta's licence check failed |
| Platform SDK initialisation errors | Meta's Platform SDK couldn't start |
| `Failed to launch SystemActivities`, "device not supported" | The game checks it is on a Meta Quest |
| `vrapi_… was called before…`, VrApi load errors | Legacy VrApi |
| `XR_ERROR_EXTENSION_NOT_PRESENT` (with the extension's name) | An OpenXR feature the Frame lacks |
| `XR_ERROR_API_VERSION_UNSUPPORTED`, runtime/initialisation failures | OpenXR version or session problems |
| `VK_ERROR_DEVICE_LOST`, `zink`, `libgallium_dri.so`, `GL_INVALID_…`, shader failures | Graphics driver errors |
| `ClipboardManager` | Lepton's missing clipboard service |
| `NoSuchMethodError`, `UnsatisfiedLinkError`, `dlopen failed` | Android or library features Lepton lacks |
| `FATAL EXCEPTION`, `Fatal signal`, `SIGSEGV` | A crash |
| `ANR in …`, "Input dispatching timed out" | Not responding |
| `XR_SESSION_STATE_SYNCHRONIZED/VISIBLE/FOCUSED`, `xrEndFrame` | The VR scene started |

The outcome is one of **runs** or **runs with issues** (its VR scene
started), **quits** (closed by itself), **crashes**, **hangs** (Android
reported it not responding; Frame Control then closes it, and says so if it
couldn't), **didn't start** (its instance never came up), or **no clear
result**: it stayed open but its log never showed its VR scene starting. That
can be a game still loading, or one stuck after a failed check; Frame Control
can't tell, so it doesn't record it. A game already open is closed first, so
the test starts clean; if it can't be closed, nothing is tested. Signatures
are written for Frame Control from the research in [vr-apks.md](vr-apks.md);
they are not copied from other tools.

The result goes through the same path as **Test** and **Report**
(`frame_catalog.add_report`): `via` probe, source `meta-store`, the result
(`runs`, `crashes` or `instance_failed`, the values the database accepts), and
notes with the outcome, the cause, the names of the matched signals and the
version code, all fixed texts. Nothing else: no token, account name, file or
log text. The page may show a short name from the log next to the cause (an
OpenXR extension, a library, an exception class or a signal), cleaned of the
token first; reports never carry it. It is always kept on this
computer; it reaches the shared database only if **Share compatibility
results** is on in Privacy settings, as for every report. The panel offers to
turn that on.

## Your token and privacy

- Kept in `meta-account.json` in Frame Control's per-user data folder
  (`~/Library/Application Support/Frame Control` on macOS,
  `%APPDATA%\Frame Control` on Windows, `~/.local/share/frame-control` on
  Linux), written owner-only (0600) through a temporary file. Never on the
  Frame.
- Sent only to `graph.oculus.com` (in the POST body) and
  `securecdn.oculus.com` (in the download address, which is how that server
  takes it), over HTTPS. A download that redirects elsewhere (Meta's CDN) is
  followed only to a plain HTTPS address on port 443 that doesn't carry the
  token in any form, anywhere (host, path, query or fragment; literal,
  recased, percent-encoded, or 8+ characters of it), nor an `access_token`
  parameter. That is checked before any lookup or connection, and a redirect
  that fails it is refused. API answers that redirect are refused.
- Every connection goes to an address checked to be public (an allow-list:
  no private, local, shared, reserved, multicast, NAT64, 6to4 or Teredo
  addresses), and to exactly that address, with TLS checked for the host
  name, so a second DNS answer can't swap in another. System proxy settings
  aren't used. This is the same design as the itch.io purchases branch.
- Never in logs, error messages, API answers, the page or reports: every
  message is cleaned first, matching the token (or any start of it of 8+
  characters) in any letter case, with each character written literally or
  percent-encoded in either case, and any other token-shaped value, before it
  is shortened.
- Disconnect deletes the file and the library Frame Control holds in memory,
  and stops a download under way. A connect still being checked when you
  disconnect is not saved.

## Where the protocol comes from

Frame Control's code is its own (MIT). These public sources describe the
protocol; Frame Control takes only facts from them (addresses, query ids,
field names), not code. QAVS and BSManager are GPL-3.0; OculusGraphQLApiLib,
DownQuest and Oculus-downgrader state no licence.

| Fact | Source |
|---|---|
| GraphQL at `https://graph.oculus.com/graphql`, POST, form fields `access_token`, `doc_id`, `variables`; `OAuthException` / code 190 for a bad token | [OculusGraphQLApiLib `GraphQLClient.cs`](https://github.com/ComputerElite/OculusGraphQLApiLib/blob/866081e2/OculusGraphQLApiLib/GraphQL/GraphQLClient.cs), [DownQuest `DownQuest.js`](https://github.com/ptrpaws/DownQuest/blob/50f5d558/DownQuest.js) |
| "Who am I": `doc_id` 4149322231793299, `data.viewer.user {id, alias}` | OculusGraphQLApiLib `GraphQLClient.cs` and `Results/User.cs` |
| Owned apps: `doc_id` 4850747515044496, `data.viewer.user.active_entitlements.nodes[].item` | OculusGraphQLApiLib `GraphQLClient.cs`, `Results/Entitlement.cs`, `Results/Application.cs`; used by [QAVS `GameDownloadManager.cs`](https://github.com/ComputerElite/QuestAppVersionSwitcher/blob/36bebd5a/QuestAppVersionSwitcher/GameDownloadManager.cs) |
| One binary: `doc_id` 4734929166632773 with `binaryID`; fields `package_name`, `version`, `version_code`, `size`, `obb_binary`, `asset_files` (`file_name`, `size`, `is_required`) | OculusGraphQLApiLib `GraphQLClient.cs`, `Results/OculusBinary.cs`, `Results/AssetFile.cs` |
| Downloads: `https://securecdn.oculus.com/binaries/download/?id=<id>&access_token=<token>`, the same for the APK, `obb_binary` and asset files; QAVS takes the OBB and required asset files | OculusGraphQLApiLib `Game/GameDownloader.cs`, [QAVS `DownloadManager.cs`](https://github.com/ComputerElite/QuestAppVersionSwitcher/blob/36bebd5a/QuestAppVersionSwitcher/DownloadManager.cs), DownQuest `DownQuest.js` |
| Token checks (`OC`/`FRL`, no `%`, `|`, `:`, not `OC` + 15 digits) | OculusGraphQLApiLib `GraphQL/TokenTools.cs`, [BSManager `oculus.helpers.ts`](https://github.com/Zagrios/bs-manager/blob/00380c79/src/shared/helpers/oculus.helpers.ts) |
| How users get a token (`oc_ac_at` cookie, or the PC app's `FRL` token) | [BSManager wiki](https://github.com/Zagrios/bs-manager/wiki/How-to-obtain-your-Oculus-Token) |

**Assumed, not seen in those sources:** that the entitlement and binary
answers carry the fields above in the shapes the libraries' models describe
(the stored queries are opaque); that the list isn't paginated (none of the
tools page it); that `ANDROID_6DOF` marks Quest apps; where downloads redirect
to (the tools let their HTTP client follow redirects; Frame Control follows
up to five, to any public HTTPS host, and refuses one carrying the token); and
a `sha256`
field (none was seen, so in practice only sizes are checked). The log
signatures for the test are inferred from the compatibility research, not
from a real Frame.

## Evidence and limits

**Verified locally** with a fake Meta and a fake Frame (`tests/test_meta.py`):
token checks; storage owner-only through a temporary file; redaction (the
stored token recased and percent-encoded in either case, starts of it, other
token-shaped values, and redaction before cutting); HTTPS-only URLs; the
public-address allow-list, resolving once and connecting only to a checked
address within one deadline; redirects off Meta's hosts refused when they
carry the token anywhere, before any lookup; the token never sent to the
CDN; GraphQL redirects refused; disconnect beating a connect in flight and
stopping a download; library parsing (Quest apps only, nothing expired or
inactive, details from the binary query); downloads with an OBB; size, length
and checksum limits and the whole-install budget with nothing left behind;
refusing apps not in the
library; the handoff to Import from my Quest's checks, install and data step;
the licence-check warning instead of a refusal; test outcomes from log
fixtures (a passing licence check isn't a failure; no VR scene is no clear
result, not recorded); closing before a test and saying when a close failed;
reports carrying only fixed texts, no token, log text or file names. The
real TLS connection code is the same as the itch.io branch's and is exercised
here only with a fake connection.

**Not verified:** a real Meta token and Meta's real answers (none was
available; nothing was sent to Meta), real downloads and their redirects,
real Quest games installed or tested on a real Frame, and whether the log
signatures match what real games print in Lepton.
