// Update checks and self-update for the desktop app (docs/releasing.md).
//
// The newest version is GitHub's "latest" release of saphid/frame-control. Drafts
// and pre-releases never count, so a build reaches people only when the
// maintainer publishes it after testing (scripts/publish-release.sh). That
// script attaches update.json (version, notes, each asset's SHA-256), read
// through github.com's latest/download link: the REST API allows only 60
// unauthenticated requests an hour per IP address, shared by everyone behind
// the same router, so it's only the fallback.
//
// Every download is checked against the SHA-256 digest GitHub records for the
// asset before anything is replaced. How the update is applied:
//   macOS     the .zip: unpacked next to the running app, swapped in by a small
//             script once the app has quit, then reopened.
//   Windows   the NSIS installer, run silently over the current install; it
//             reopens the app. A copy unpacked from the .zip is updated by hand.
//   Linux     the AppImage replaces itself; .deb installs are updated by hand.
// When the app can't update itself it opens the release page instead.
const { execFile, spawn } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const https = require("https");
const os = require("os");
const path = require("path");

// This fork has no independent installer/update channel yet. Never replace a
// Workspace prototype with an upstream Frame Control build.
const updatesEnabled = require("./package.json").frameWorkspacePrototype !== true;
function requireUpdatesEnabled() {
  if (!updatesEnabled) throw new Error("Frame Workspace prototype updates are disabled until its own release channel is configured");
}

const REPO = "saphid/frame-control";  // renamed from saphid/steam-frame; GitHub redirects the old name
const LATEST = `https://api.github.com/repos/${REPO}/releases/latest`;
const MANIFEST = `https://github.com/${REPO}/releases/latest/download/update.json`;
const RELEASES = `https://github.com/${REPO}/releases`;

// "0.3.1" or "v0.3.1" -> [0, 3, 1]; pre-release suffixes sort before the release.
function parseVersion(v) {
  const m = String(v || "").trim().replace(/^v/i, "").match(/^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$/);
  return m ? { nums: [+m[1], +m[2], +m[3]], pre: m[4] || null } : null;
}

function isNewer(candidate, current) {
  const a = parseVersion(candidate), b = parseVersion(current);
  if (!a || !b) return false;
  for (let i = 0; i < 3; i++) if (a.nums[i] !== b.nums[i]) return a.nums[i] > b.nums[i];
  if (a.pre === b.pre) return false;
  if (!a.pre) return true;   // 1.0.0 is newer than 1.0.0-beta
  if (!b.pre) return false;
  return a.pre > b.pre;
}

// The asset this copy of the app updates from, by the names electron-builder gives them.
function assetName(platform, arch, method) {
  if (method === "mac-zip") return `Frame-Control-mac-${arch}.zip`;
  if (method === "nsis") return `Frame-Control-Setup-${arch}.exe`;
  if (method === "appimage") return `Frame-Control-linux-${arch === "x64" ? "x86_64" : arch}.AppImage`;
  return null;
}

// How this copy can update itself: mac-zip, nsis, appimage, or manual (with why).
function updateMethod({ platform, isPackaged, execPath, env, exists, writable }) {
  if (!isPackaged) return { method: "manual", why: "running from a source checkout" };
  if (platform === "darwin") {
    const bundle = macBundle(execPath);
    if (!bundle) return { method: "manual", why: "can't find the app bundle" };
    if (bundle.includes("/AppTranslocation/") || bundle.startsWith("/Volumes/")) {
      return { method: "manual", why: "move Frame Control to Applications first" };
    }
    if (!writable(path.dirname(bundle))) return { method: "manual", why: `${path.dirname(bundle)} isn't writable` };
    return { method: "mac-zip", bundle };
  }
  if (platform === "win32") {
    // electron-builder's NSIS install puts its uninstaller next to the app.
    const dir = path.dirname(execPath);
    if (exists(path.join(dir, "Uninstall Frame Control.exe"))) return { method: "nsis" };
    return { method: "manual", why: "not installed with the installer" };
  }
  if (platform === "linux" && env.APPIMAGE) {
    if (!writable(path.dirname(env.APPIMAGE))) return { method: "manual", why: "the AppImage's folder isn't writable" };
    return { method: "appimage", appImage: env.APPIMAGE };
  }
  return { method: "manual", why: "installed from a package" };
}

function macBundle(execPath) {
  const i = execPath.indexOf(".app/Contents/MacOS/");
  return i < 0 ? null : execPath.slice(0, i + 4);
}

function get(url, { headers = {}, timeout = 20000, redirects = 5 } = {}) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { headers: { "user-agent": "FrameControl-updater", ...headers }, timeout }, (res) => {
      if ([301, 302, 303, 307, 308].includes(res.statusCode) && res.headers.location && redirects > 0) {
        res.resume();
        const next = new URL(res.headers.location, url);
        if (next.protocol !== "https:") return reject(new Error("refusing a non-HTTPS redirect"));
        return resolve(get(next.href, { headers, timeout, redirects: redirects - 1 }));
      }
      if (res.statusCode !== 200) { res.resume(); return reject(new Error(`HTTP ${res.statusCode} from ${new URL(url).host}`)); }
      resolve(res);
    });
    req.on("timeout", () => req.destroy(new Error("timed out")));
    req.on("error", reject);
  });
}

async function getJson(url, headers) {
  const res = await get(url, { headers });
  let body = "";
  for await (const chunk of res) body += chunk;
  return JSON.parse(body);
}

// The release page the banner links to. update.json for 0.4.0 carried the draft's
// address (releases/tag/untagged-...), which is dead once the release is published,
// and a page from the manifest could also be steered elsewhere (e.g. tag/..\..\other/repo
// normalizes to another repository) before shell.openExternal. So the given page is
// ignored: the link is always this repository's tag page, built from a valid version.
function releasePage(version) {
  const v = parseVersion(version);
  if (!v) return RELEASES;
  return `${RELEASES}/tag/v${v.nums.join(".")}${v.pre ? "-" + v.pre : ""}`;
}

// update.json and the API's release both become { version, notes, page, assets }.
function fromManifest(m) {
  if (!parseVersion(m.version) || !Array.isArray(m.assets)) throw new Error("update.json is malformed");
  const version = String(m.version).replace(/^v/i, "");
  const base = `https://github.com/${REPO}/releases/download/v${version}/`;
  return { version, notes: String(m.notes || "").slice(0, 4000),
           page: releasePage(version),
           // Assets always come from this repository's release, whatever the manifest says.
           assets: m.assets.map((a) => ({ name: String(a.name), url: base + encodeURIComponent(String(a.name)),
                                          size: a.size, digest: a.digest || null })) };
}

function fromApi(r) {
  if (r.draft || r.prerelease) throw new Error("GitHub returned an unpublished release");
  const version = String(r.tag_name || "").replace(/^v/i, "");
  return { version, notes: String(r.body || "").slice(0, 4000),
           page: releasePage(version),
           assets: (r.assets || []).map((a) => ({ name: a.name, url: a.browser_download_url, size: a.size,
                                                 digest: a.digest || null })) };
}

async function latestRelease() {
  requireUpdatesEnabled();
  try {
    return fromManifest(await getJson(MANIFEST));
  } catch (e) {
    if (!/HTTP 404/.test(e.message)) throw e;  // releases before update.json existed
  }
  return fromApi(await getJson(LATEST, { accept: "application/vnd.github+json" }));
}

async function download(asset, dest, onProgress) {
  const m = /^sha256:([0-9a-f]{64})$/.exec(asset.digest || "");
  if (!m) throw new Error(`GitHub has no SHA-256 for ${asset.name}, so it can't be checked`);
  const res = await get(asset.url, { timeout: 60000 });
  const total = +res.headers["content-length"] || asset.size || 0;
  const hash = crypto.createHash("sha256");
  // "wx": a new file only, never through an existing file or symlink at that path.
  const out = fs.createWriteStream(dest, { mode: 0o755, flags: "wx" });
  let done = 0;
  await new Promise((resolve, reject) => {
    res.on("data", (chunk) => { hash.update(chunk); done += chunk.length; onProgress && onProgress(done, total); });
    res.on("error", reject);
    out.on("error", reject);
    out.on("finish", resolve);
    res.pipe(out);
  });
  if (hash.digest("hex") !== m[1]) {
    fs.rmSync(dest, { force: true });
    throw new Error(`${asset.name} didn't match its SHA-256; nothing was changed`);
  }
}

const run = (cmd, args) => new Promise((resolve, reject) =>
  execFile(cmd, args, { timeout: 120000 }, (err, stdout, stderr) => err ? reject(new Error((stderr || err.message).trim())) : resolve(stdout)));

// Waits for this process to exit, swaps the new bundle in (putting the old one
// back if that fails), and reopens the app.
const MAC_SWAP = `set -u
pid="$1"; app="$2"; new="$3"; stage="$4"
while kill -0 "$pid" 2>/dev/null; do sleep 0.2; done
old="$stage/old.app"
if mv "$app" "$old"; then
  if mv "$new" "$app"; then rm -rf "$old"; else mv "$old" "$app"; fi
fi
xattr -dr com.apple.quarantine "$app" 2>/dev/null
rm -rf "$stage"
open "$app"
`;

async function applyMac(release, bundle, onProgress) {
  const asset = release.assets.find((a) => a.name === assetName("darwin", process.arch, "mac-zip"));
  if (!asset) throw new Error(`the release has no ${assetName("darwin", process.arch, "mac-zip")}`);
  // Staged beside the app, so the final move stays on one volume.
  const stage = fs.mkdtempSync(path.join(path.dirname(bundle), ".frame-control-update-"));
  try {
    const zip = path.join(stage, asset.name);
    await download(asset, zip, onProgress);
    await run("/usr/bin/ditto", ["-x", "-k", zip, stage]);
    fs.rmSync(zip, { force: true });
    const name = fs.readdirSync(stage).find((n) => n.endsWith(".app"));
    if (!name) throw new Error("the download has no app in it");
    const fresh = path.join(stage, name);
    const version = (await run("/usr/bin/plutil", ["-extract", "CFBundleShortVersionString", "raw",
                                                   path.join(fresh, "Contents", "Info.plist")])).trim();
    if (version !== release.version) throw new Error(`the download is version ${version}, not ${release.version}`);
    const script = path.join(stage, "swap.sh");
    fs.writeFileSync(script, MAC_SWAP);
    return () => spawn("/bin/sh", [script, String(process.pid), bundle, fresh, stage],
                       { detached: true, stdio: "ignore" }).unref();
  } catch (e) {
    fs.rmSync(stage, { recursive: true, force: true });
    throw e;
  }
}

async function applyNsis(release, onProgress) {
  const asset = release.assets.find((a) => a.name === assetName("win32", process.arch, "nsis"));
  if (!asset) throw new Error(`the release has no ${assetName("win32", process.arch, "nsis")}`);
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "frame-control-update-"));
  const exe = path.join(dir, asset.name);
  await download(asset, exe, onProgress);
  // /S: silent, into the existing install. --force-run: open the app afterwards.
  return () => spawn(exe, ["--updated", "/S", "--force-run"], { detached: true, stdio: "ignore" }).unref();
}

async function applyAppImage(release, appImage, onProgress) {
  const asset = release.assets.find((a) => a.name === assetName("linux", process.arch, "appimage"));
  if (!asset) throw new Error(`the release has no ${assetName("linux", process.arch, "appimage")}`);
  // A private folder beside the AppImage, so the final rename stays on one filesystem.
  const stage = fs.mkdtempSync(path.join(path.dirname(appImage), ".frame-control-update-"));
  try {
    const next = path.join(stage, asset.name);
    await download(asset, next, onProgress);
    fs.chmodSync(next, 0o755);
    fs.renameSync(next, appImage);  // the running copy keeps its open file
  } finally {
    fs.rmSync(stage, { recursive: true, force: true });
  }
  // Without FUSE the AppImage runs extracted (--appimage-extract-and-run, which isn't passed
  // on to the app); a FUSE mount lives under /tmp/.mount_*. Keep the same mode on restart.
  const extracted = process.env.APPIMAGE_EXTRACT_AND_RUN === "1" || !process.execPath.includes("/.mount_");
  const env = { ...process.env, APPIMAGE: appImage, ...(extracted ? { APPIMAGE_EXTRACT_AND_RUN: "1" } : {}) };
  // Started only once this process has exited, or the new copy would lose the single-instance lock.
  return () => spawn("/bin/sh", ["-c", 'while kill -0 "$1" 2>/dev/null; do sleep 0.2; done; exec "$2"',
                                 "sh", String(process.pid), appImage], { detached: true, stdio: "ignore", env }).unref();
}

// Downloads and prepares the update; returns a function that starts the swap,
// to be called just before the app quits.
async function prepare(release, how, onProgress, current) {
  if (!isNewer(release.version, current)) throw new Error(`${release.version} isn't newer than ${current}`);
  requireUpdatesEnabled();
  if (how.method === "mac-zip") return applyMac(release, how.bundle, onProgress);
  if (how.method === "nsis") return applyNsis(release, onProgress);
  if (how.method === "appimage") return applyAppImage(release, how.appImage, onProgress);
  throw new Error(how.why || "this copy can't update itself");
}

function writable(dir) {
  try { fs.accessSync(dir, fs.constants.W_OK); return true; } catch { return false; }
}

module.exports = { REPO, RELEASES, updatesEnabled, parseVersion, isNewer, assetName, updateMethod, macBundle, latestRelease,
                   fromManifest, fromApi,
                   download, prepare, writable };
