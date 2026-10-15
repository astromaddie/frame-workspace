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

Frame Control never reads your browser's cookies. There are two ways to give
it your token.

**Sign in with Meta** (desktop app only; a plain browser doesn't show it).
Tick **I understand…**, then choose **Sign in with Meta…**. Meta's own login
page (`https://secure.oculus.com`, the address BSManager's "Log in with Meta"
opens) appears in a Frame Control window. Sign in there as usual. When Meta's
site sets its access token cookie (`oc_ac_at`), Frame Control takes it from
that window, connects with it exactly as if you had pasted it (below), and
closes the window. The window:

- has its own storage (the Electron session `persist:meta-signin`), separate
  from Frame Control's main window and from every browser on your computer;
  the cookie is read only from there, and only for `oculus.com`;
- runs Meta's page sandboxed, with no access to Frame Control (no preload, no
  Node, context isolation on, no developer tools) and every permission request
  or check (camera, notifications…) refused;
- only goes to `https://` pages on `oculus.com`, `meta.com`, `facebook.com` and
  `instagram.com` (and their subdomains; Meta's login passes through Instagram).
  A link anywhere else opens in your system browser; a redirect anywhere else
  is stopped, and closing the window then says where it went. Pop-ups on those sites open in the same
  window. Frames inside the page may load any `https://` page (Meta's login
  can embed captcha and CDN frames from other sites, such as Arkose Labs,
  hCaptcha, reCAPTCHA or fbcdn) or an empty frame (`about:blank`,
  `about:srcdoc`); a frame going to any other scheme (`http:`, `data:`,
  `blob:`, `javascript:`, `file:`…) is just stopped. Redirects follow the same
  rule for the frame they happen in. Frames can't reach the
  token: the cookie is read by Frame Control's main process, and the window
  has no IPC;
- sends a cookie value on only if it looks like a user token (`OC` or `FRL`,
  mixed case, no `%`, `|` or `:`), and each value once. If the server refuses
  it, the window stays open; close it to see why;
- gives up after 10 minutes, as BSManager does;
- when it is closed, times out or is signed out of, stops reading cookies
  first, then is destroyed (a page's "leave this page?" can't keep it open),
  and, if it had sent a token, tells the server to drop that sign-in: each
  sign-in has a random attempt id. The cancel is sent only once the connect
  under way has finished (saved, refused or failed), so the server always
  gets it after the connect; with nothing sent, there is nothing to cancel.
  The server lists an attempt only while its
  connect is being checked: cancelled then, it isn't saved; cancelled just
  after its token was saved (even before the connect has finished tidying
  up), that token is deleted (as a disconnect). A cancel
  never removes an account a later sign-in or a pasted token saved, and one
  for an unknown attempt changes nothing.

The page never sees the token: the desktop app hands it straight to the local
server's connect path and tells the page only the answer (connected, and your
Meta display name). It isn't logged anywhere.

The window keeps you signed in to Meta (in `persist:meta-signin` only) until
you **Disconnect**, which also signs out of Meta in Frame Control: it clears
that window's cookies, storage and cache, so the next sign-in starts fresh.
**Sign out of Meta in Frame Control** next to the sign-in button does just
that part, for a sign-in that didn't connect.

**Paste a token.** You copy your own token and paste it in. The two ways
BSManager's guide describes
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
`%`), app tokens (with `|` or `:`), cut-off values (under 20 characters), and
anything with characters other than letters, digits and `+ / = - _` (the
base64 and URL-safe base64 sets), or with no lower-case letter (real tokens are
mixed case). The public tools only describe tokens made
of letters and digits; if a real token needs another character, it has to be
added in one place (`TOKEN_CHARS` in `ui/frame_meta.py`), which also makes
the redaction look for it.

## Using it

1. Tick **I understand…**, then either choose **Sign in with Meta…** (desktop
   app) or paste your token and choose **Connect**. Frame Control asks Meta
   "who am I" with it (a read-only query); only a token Meta accepts is kept.
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
5. Choose **Install** on as many games as you like: they wait in the
   **Install queue** and install one after another, with Quest imports and
   Android store installs. **Remove** takes one out before it starts.
6. **Disconnect** deletes the token from your computer and, in the desktop
   app, signs out of Meta in Frame Control (clears the sign-in window's
   storage). Games already on the Frame stay.

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
`{"action": "disconnect"}`, `{"action": "cancel-signin", "attempt": "..."}`, `{"action": "install", "app": "APP_ID", "test": false,
"allowDowngrade": false}` or `{"action": "test", "package": "..."}`. Install and
test return a background job (`/api/job`); an install waits its turn in the
install queue (`/api/queue`, see [Import from my Quest](quest-import.md#using-it)). Sign in with Meta uses the same
`connect` action: the desktop app's main process (`app/meta-signin.js`) posts
it with the cookie's value and an `"attempt"` id (8 to 64 letters, digits or
`-`) once the page's acknowledgement box is ticked. A connect whose attempt
was cancelled while it was checked answers 409 and saves nothing;
`cancel-signin` for an attempt whose token is the saved account deletes it.

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
the test starts clean; if it can't be closed, nothing is tested. If the Frame
fails while the test reads the game or its log, the test says only that it
couldn't read the log: a failed command's output can be the game's log, so it
is never shown. Signatures look at the first 4 KB of each log line and are
bounded, so a huge or hostile log can't make the test slow. They
are written for Frame Control from the research in [vr-apks.md](vr-apks.md);
they are not copied from other tools.

The result goes through the same path as **Test** and **Report**
(`frame_catalog.add_report`): `via` probe, source `meta-store`, the result
(`runs`, `crashes` or `instance_failed`, the values the database accepts), and
notes with the outcome, the cause, the names of the matched signals and the
version code, all fixed texts. Nothing else: no token, account name, file or
log text. The page may show a short name from the log next to the cause (an
OpenXR extension, a library, an exception class or a signal), taken from the
log line after the token is removed from it, and left out unless it looks like
a real name (64 characters at most, no long mixed letter-and-digit runs);
reports never carry it. It is always kept on this
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
  percent-encoded once or twice (%XX, %25XX) in either case, and any other
  token-shaped value, before it is shortened.
- Disconnect deletes the file and the library Frame Control holds in memory,
  and stops a download under way. A connect still being checked when you
  disconnect is not saved. In the desktop app, before the token is deleted, it
  also stops any sign-in under way, destroys the sign-in window, waits for a
  connect still under way to finish and cancels its attempt (deleting its
  token if it was just saved), and only then clears the `persist:meta-signin` session
  (cookies, storage, cache); no other session is touched. Sign in with Meta
  stays off from the moment you press Disconnect until the token is deleted,
  and the app refuses a sign-in while it is signing out.
- With Sign in with Meta, a second copy of your Meta sign-in lives in that
  session's storage, in Electron's per-user data folder (`Partitions/meta-signin`),
  until you disconnect or sign out of Meta in Frame Control.

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
| Sign in with Meta: open `https://secure.oculus.com` in an app window, and once it reaches `https://secure.oculus.com/my/profile` read the `oc_ac_at` cookie from that window's session (checked on `did-navigate`, `did-navigate-in-page`, `did-stop-loading`); 10-minute timeout | [BSManager `bs-oculus-downloader.service.ts`](https://github.com/Zagrios/bs-manager/blob/00380c79/src/main/services/bs-version-download/bs-oculus-downloader.service.ts) (`getUserTokenFromMetaAuth`) |

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

**Sign in with Meta, verified locally** with fake Electron windows, sessions
and cookies (`app/test/meta-signin.test.js`): the window's options (its own
partition, sandbox, context isolation, no Node, no preload) and Meta's login
address, no developer tools; permission requests and checks refused; the
navigation allow-list (links elsewhere open in the system browser, redirects
elsewhere are stopped, pop-ups never become windows); frames allowed any
HTTPS page but no other scheme, the window itself only Meta's sites; a
page's beforeunload not keeping the window open; the cookie read only from
`persist:meta-signin` and only for `oculus.com`, even when the default
session and another partition hold one; signed-out values ignored; a value
sent once, a refused one keeping the window open; the window closing after a
connect, with the page getting the answer and never the token; nothing
logged, and a server error that echoed the token scrubbed; closing the window
or signing out ending the wait; closing, timing out or signing out during a
connect cancelling that attempt (with its own id) after cookie intake stops
and only after that connect settles (saved, refused or failed), and no cancel
when nothing was sent; sign-out (Disconnect's first step) waiting for a
connect under way, even from a window closed earlier, before it finishes;
sign-out destroying the window before clearing only that partition; no
sign-in starting while a sign-out runs (it is refused: "try again in a
moment"), a second sign-out joining the first, and a sign-out waiting for
every window and connect started before it. The
frame redirects held to the frame rule, the window's to Meta's sites. The
server side (`tests/test_meta.py`): an attempt cancelled while Meta is
answering not saved, even after 300 other cancels; a cancel just after the
save deleting that token; a cancel never removing a later sign-in's or a
pasted token's account; unknown attempts changing nothing; no attempt left
listed after a connect, refused or not; attempt ids checked.

**Differences from BSManager:** BSManager uses its app's default session (and
clears only `.oculus.com` cookies afterwards); Frame Control uses a session of
its own and clears all of it on disconnect. BSManager waits for
`/my/profile`; Frame Control also reacts to the cookie changing, and to any
`oculus.com` page, and lets the server decide whether the value is a
working token.

**Not verified:** Sign in with Meta against Meta's real login page (no real
Meta account was used, and nothing was sent to Meta): whether Meta's login
still goes only through `oculus.com`, `meta.com` and `facebook.com`, whether
it uses pop-ups (they open in the same window here, which would break a
pop-up that reports back to its opener), whether `oc_ac_at` is still set on
`oculus.com` after login, and whether Meta accepts the window (Frame Control
removes Electron's name from the user agent as a precaution). A real Meta token and Meta's real answers (none was
available; nothing was sent to Meta), real downloads and their redirects,
real Quest games installed or tested on a real Frame, and whether the log
signatures match what real games print in Lepton.
