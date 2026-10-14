// "Sign in with Meta" for My Meta library (docs/meta-library.md), the way BSManager's
// "Log in with Meta" works: Meta's own login page in a Frame Control window, then the
// access token cookie (oc_ac_at) that page sets is handed to the local server's
// existing connect path (format check, "who am I" with Meta, 0600 file).
//
// The window has its own persistent session (PARTITION), so the cookie is read only
// from there: never from Frame Control's main window, never from any browser. It has
// no preload and no Node, and it only navigates within Meta's login sites; anything
// else opens in the system browser. Nothing here logs the token, and the page only
// ever gets back the server's answer (connected, display name), never the token.
// Electron is passed in, so app/test/meta-signin.test.js can run it with fakes.
const crypto = require("crypto");

const PARTITION = "persist:meta-signin";
// BSManager's loginUrl (src/main/services/bs-version-download/bs-oculus-downloader.service.ts).
const LOGIN_URL = "https://secure.oculus.com";
const COOKIE = "oc_ac_at";
// Sites the login can go through: the Oculus store web, Meta accounts, Facebook login,
// and Instagram (Meta's login passes through www.instagram.com: seen live, 2026-10-14).
const ALLOWED = ["oculus.com", "meta.com", "facebook.com", "instagram.com"];
const TIMEOUT = 10 * 60 * 1000;  // as BSManager

const underDomain = (host, domains) => {
  host = String(host || "").replace(/^\./, "").toLowerCase();
  return domains.some((d) => host === d || host.endsWith("." + d));
};

// Plain HTTPS on the default port, to one of the login sites.
function allowedUrl(raw) {
  let u;
  try { u = new URL(String(raw)); } catch { return false; }
  if (u.protocol !== "https:" || u.username || u.password || (u.port && u.port !== "443")) return false;
  return underDomain(u.hostname, ALLOWED);
}

// Frames inside the page: any HTTPS address (Meta's login can embed captcha and CDN
// frames from other sites), or an empty frame; never data:, blob:, javascript:, http:,
// file: or any other scheme. Frames can't reach the cookie read or IPC (main process only).
function allowedSubframeUrl(raw) {
  if (/^about:(?:blank|srcdoc)$/.test(String(raw))) return true;
  let u;
  try { u = new URL(String(raw)); } catch { return false; }
  return u.protocol === "https:" && !u.username && !u.password;
}

const onOculus = (raw) => { try { return underDomain(new URL(raw).hostname, ["oculus.com"]); } catch { return false; } };

// A cheap first look, so a signed-out or app value is never sent anywhere; the server's
// check_token (ui/frame_meta.py) has the final word.
function looksLikeUserToken(v) {
  return typeof v === "string" && /^(?:OC|FRL)[A-Za-z0-9+/=_-]{18,4093}$/.test(v)
    && !/^OC\d{15}$/.test(v) && /[a-z]/.test(v);
}

const fingerprint = (v) => crypto.createHash("sha256").update(v).digest("hex");

// Anything token-shaped out of a message, in case a server error ever carried one.
function scrub(text, token) {
  let s = String(text == null ? "" : text);
  if (token) s = s.split(token).join("<redacted>");
  return s.replace(/(?:OC|FRL)[A-Za-z0-9+/=_%|:-]{8,}/g, "<redacted>").slice(0, 600);
}

// deps: { BrowserWindow, session, shell, parent() -> BrowserWindow|null, timeout,
//         connect(token, attempt) -> Promise<{status, data}>  (the server's connect action),
//         cancel(attempt) -> Promise  (the server's cancel-signin: never save that attempt) }
function createMetaSignIn({ BrowserWindow, session, shell, connect, cancel, parent = () => null, timeout = TIMEOUT }) {
  let active = null;  // { win, promise, finish }
  const pending = new Set();  // cancels still to reach the server, from flows already finished
  let signingOut = null;      // the sign-out under way; no sign-in starts while it runs
  const SIGNING_OUT = "Frame Control is signing out of Meta; try again in a moment.";

  const openOutside = (target) => {
    if (/^https?:\/\//i.test(String(target))) shell.openExternal(String(target));
  };

  function signIn() {
    if (signingOut) return Promise.resolve({ ok: false, error: SIGNING_OUT });
    if (active) { active.win.focus(); return active.promise; }
    const ses = session.fromPartition(PARTITION);
    ses.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
    ses.setPermissionCheckHandler(() => false);
    // Electron adds its own name and the app's to the user agent; send a plain Chrome one.
    if (ses.getUserAgent && ses.setUserAgent) {
      ses.setUserAgent(ses.getUserAgent().replace(/\s(?:Electron|frame-control|Frame Control)\/\S+/g, ""));
    }
    const p = parent();
    const win = new BrowserWindow({
      width: 520, height: 760, title: "Sign in with Meta – Frame Control", autoHideMenuBar: true,
      ...(p ? { parent: p } : {}),
      webPreferences: { partition: PARTITION, contextIsolation: true, nodeIntegration: false, sandbox: true,
                        nodeIntegrationInSubFrames: false, webviewTag: false, safeDialogs: true, devTools: false },
    });
    const wc = win.webContents;

    let resolve;
    const promise = new Promise((r) => { resolve = r; });
    let done = false, busy = false, again = false, lastError = null, blocked = null;
    const tried = new Set();  // fingerprints of values already sent, so each goes once
    // The server refuses to save a connect from an attempt this window gave up on.
    const attemptId = crypto.randomUUID();

    const onCookie = (_e, cookie, _cause, removed) => {
      if (!removed && cookie && cookie.name === COOKIE && underDomain(cookie.domain, ["oculus.com"])) attempt();
    };
    const timer = setTimeout(() => finish({ ok: false, error: "Sign-in timed out after 10 minutes. Try again." }), timeout);
    if (timer.unref) timer.unref();

    // Stops cookie intake first, then the window (destroy: a page's beforeunload can't
    // keep it open), and answers the page. Unless it connected, and if it sent a token, it
    // then has the server drop this attempt: only once the connect under way has settled,
    // so the server always sees the cancel after the connect (and deletes the token if
    // that connect saved it). The returned promise settles when that is done.
    let finished = null;
    let inflight = null;  // the last connect sent
    function finish(result) {
      if (done) return finished || Promise.resolve();  // destroy() below re-enters through "closed"
      done = true;
      ses.cookies.removeListener("changed", onCookie);
      clearTimeout(timer);
      if (active && active.win === win) active = null;
      if (!win.isDestroyed()) win.destroy();
      resolve(result);
      if (result.ok || !inflight) return (finished = Promise.resolve());  // nothing sent: nothing to cancel
      finished = Promise.resolve(inflight).catch(() => {}).then(() => cancel(attemptId)).catch(() => {});
      pending.add(finished);
      finished.then(() => pending.delete(finished));
      return finished;
    }

    async function attempt() {
      if (done) return;
      if (busy) { again = true; return; }
      busy = true;
      try {
        do {
          again = false;
          // Only this window's own session, and only Meta's Oculus cookie.
          const cookies = await ses.cookies.get({ name: COOKIE });
          const c = cookies.find((k) => underDomain(k.domain, ["oculus.com"]) && looksLikeUserToken(k.value));
          if (!c || done) continue;
          const key = fingerprint(c.value);
          if (tried.has(key)) continue;
          tried.add(key);
          let r;
          try {
            inflight = connect(c.value, attemptId);
            r = await inflight;
          } catch {
            lastError = "Frame Control's local server didn't answer.";
            continue;
          }
          if (done) return;
          const d = (r && r.data) || {};
          if (r && r.status === 200 && d.connected) {
            finish({ ok: true, connected: true, user: d.user && d.user.name ? { name: scrub(d.user.name, c.value).slice(0, 80) } : null,
                     message: scrub(d.message || "Connected your Meta account (Experimental)", c.value) });
            return;
          }
          lastError = scrub(d.error || `HTTP ${r && r.status}`, c.value);
        } while (again && !done);
      } catch {
        // The session went away (the window closed); nothing to do.
      } finally {
        busy = false;
      }
    }

    // Navigation stays on Meta's login sites. A link elsewhere opens in the system
    // browser; a redirect elsewhere is just stopped (it may carry sign-in data).
    // Every frame: the window itself stays on the list; frames inside it may load any
    // HTTPS page (captchas), and one going anywhere else is just stopped.
    wc.on("will-navigate", (e, target) => { if (!allowedUrl(target)) { e.preventDefault(); openOutside(target); } });
    // A stopped page change in the window itself says where it went (the host only), so
    // closing the stuck window explains why instead of "closed before you signed in".
    const stopped = (to) => {
      let host = "";
      try { host = new URL(String(to)).hostname; } catch { /* not an address */ }
      blocked = `Meta's sign-in went to ${scrub(host || "an address", null).slice(0, 80)}, which Frame Control doesn't allow.`;
    };
    wc.on("will-frame-navigate", (e) => {
      if (e.isMainFrame ? allowedUrl(e.url) : allowedSubframeUrl(e.url)) return;
      e.preventDefault();
      if (e.isMainFrame) stopped(e.url);
    });
    // Redirects fire for frames too: the same rule per frame (details on the event in
    // current Electron, positional arguments in older ones; unknown counts as the window).
    wc.on("will-redirect", (e, target, _inPlace, isMainFrame) => {
      const to = e && e.url !== undefined ? e.url : target;
      const main = e && e.isMainFrame !== undefined ? e.isMainFrame : isMainFrame !== false;
      if (main ? allowedUrl(to) : allowedSubframeUrl(to)) return;
      e.preventDefault();
      if (main) stopped(to);
    });
    // A page's beforeunload never keeps the window open.
    wc.on("will-prevent-unload", (e) => e.preventDefault());
    wc.setWindowOpenHandler(({ url: target }) => {
      if (allowedUrl(target)) wc.loadURL(target).catch(() => {});
      else openOutside(target);
      return { action: "deny" };
    });
    // Signed in: the cookie changes, or the window lands back on Meta's store site.
    ses.cookies.on("changed", onCookie);
    wc.on("did-navigate", (_e, target) => { blocked = null; if (onOculus(target)) attempt(); });  // it recovered
    wc.on("did-navigate-in-page", (_e, target) => { if (onOculus(target)) attempt(); });
    win.on("closed", () => finish({ ok: false, cancelled: true,
      error: lastError ? `The sign-in window closed before Frame Control could connect. ${lastError}`
           : blocked ? `The sign-in window was closed before you signed in. ${blocked}`
                     : "The sign-in window was closed before you signed in." }));

    active = { win, promise, finish };
    wc.loadURL(LOGIN_URL).catch(() => {});
    return promise;
  }

  // Signs out of Meta in Frame Control: stops any sign-in under way (and has the server
// refuse to save it), destroys the window, then clears everything
  // the sign-in session stored (cookies, storage, cache), so the next sign-in starts fresh.
  // One at a time (a second call joins the first), and no sign-in can start meanwhile.
  // It goes round until no window is open and no connect or cancel is left, so nothing
  // started before it can set a token after it (or after the Disconnect that follows).
  function signOut() {
    if (!signingOut) {
      signingOut = (async () => {
        for (;;) {
          if (active) {
            const { win, finish } = active;
            await finish({ ok: false, cancelled: true, error: "Signed out of Meta in Frame Control." });
            // Clear only once nothing can write to the session again.
            if (!win.isDestroyed()) throw new Error("The Sign in with Meta window didn't close; try again.");
          } else if (pending.size) {
            // A connect under way settles and is cancelled before the caller (Disconnect)
            // deletes the account, so it can't be saved afterwards.
            await Promise.all([...pending]);
          } else {
            break;
          }
        }
        const ses = session.fromPartition(PARTITION);
        await ses.clearStorageData();
        await ses.clearCache();
        if (ses.clearAuthCache) await ses.clearAuthCache();
        return true;
      })().finally(() => { signingOut = null; });
    }
    return signingOut;
  }

  return { signIn, signOut, isOpen: () => !!active, isSigningOut: () => !!signingOut };
}

module.exports = { createMetaSignIn, allowedUrl, allowedSubframeUrl, looksLikeUserToken, scrub, PARTITION, LOGIN_URL, COOKIE, ALLOWED };
