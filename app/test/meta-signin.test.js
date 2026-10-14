// Run: node --test app/test/
// "Sign in with Meta" (app/meta-signin.js) with fake Electron windows, sessions and cookies.
const nodeTest = require("node:test");
// A broken flow can leave a sign-in waiting forever; fail it instead of hanging.
const test = (name, fn) => nodeTest(name, { timeout: 5000 }, fn);
const assert = require("node:assert");
const { EventEmitter } = require("node:events");
const { createMetaSignIn, allowedUrl, looksLikeUserToken, scrub, PARTITION, LOGIN_URL, COOKIE } = require("../meta-signin");

const TOKEN = "OCAbcdefghijklmnopQRSTUVwxyz0123456789abcdEFGH";
const OTHER = "OCzyxwvutsrqponmlkJIHGFEDcba9876543210zyxwVUTS";

class FakeCookies extends EventEmitter {
  constructor() { super(); this.store = []; this.reads = 0; }
  async get(filter) {
    this.reads++;
    return this.store.filter((c) => !filter.name || c.name === filter.name);
  }
  set(cookie) {  // as the page would: store it, then tell listeners
    this.store = this.store.filter((c) => !(c.name === cookie.name && c.domain === cookie.domain)).concat(cookie);
    this.emit("changed", {}, cookie, "explicit", false);
  }
}

class FakeSession {
  constructor(name, log) { this.name = name; this.log = log; this.cookies = new FakeCookies(); this.cleared = []; this.permission = null; }
  setPermissionRequestHandler(fn) { this.permission = fn; }
  setPermissionCheckHandler(fn) { this.permissionCheck = fn; }
  getUserAgent() { return "Mozilla/5.0 Chrome/140.0 frame-control/0.4.4 Electron/44.4.5 Safari/537.36"; }
  setUserAgent(ua) { this.ua = ua; }
  async clearStorageData(opts) { this.log.push("clear"); this.cleared.push(["storage", opts]); }
  async clearCache() { this.cleared.push(["cache"]); }
  async clearAuthCache() { this.cleared.push(["auth"]); }
}

function fakes({ timeout } = {}) {
  const log = [];  // the order things happened in
  const sessions = new Map();
  const defaultSession = new FakeSession("default", log);
  const session = {
    defaultSession,
    fromPartition(name) {
      if (!sessions.has(name)) sessions.set(name, new FakeSession(name, log));
      return sessions.get(name);
    },
  };
  const windows = [];
  class BrowserWindow extends EventEmitter {
    constructor(opts) {
      super();
      this.opts = opts;
      this.destroyed = false;
      this.veto = false;  // a page whose beforeunload keeps the window open on close()
      this.focused = 0;
      const wc = new EventEmitter();
      wc.loaded = [];
      wc.loadURL = async (u) => { wc.loaded.push(u); };
      wc.setWindowOpenHandler = (fn) => { wc.openHandler = fn; };
      // Deliberately a different session: the code must read the partition's, not guess.
      wc.session = defaultSession;
      this.webContents = wc;
      windows.push(this);
    }
    isDestroyed() { return this.destroyed; }
    focus() { this.focused++; }
    close() { if (this.destroyed || this.veto) return; this.destroy(); }
    destroy() { if (this.destroyed) return; this.destroyed = true; log.push("destroyed"); this.emit("closed"); }
  }
  const opened = [];
  const shell = { openExternal: (u) => { opened.push(u); } };
  const calls = [], attempts = [], cancels = [];
  let answer = () => ({ status: 200, data: { connected: true, user: { name: "Alex" }, message: "Connected your Meta account (Experimental)" } });
  const connect = async (token, attempt) => { calls.push(token); attempts.push(attempt); log.push("connect"); return answer(token); };
  const cancel = async (attempt) => {
    cancels.push(attempt);
    log.push(`cancel (cookie listeners: ${session.fromPartition(PARTITION).cookies.listenerCount("changed")})`);
  };
  const flow = createMetaSignIn({ BrowserWindow, session, shell, connect, cancel, parent: () => null, ...(timeout ? { timeout } : {}) });
  return { flow, session, sessions, windows, opened, calls, attempts, cancels, log, setAnswer: (fn) => { answer = fn; },
           meta: () => session.fromPartition(PARTITION) };
}

const tick = () => new Promise((r) => setImmediate(r));
async function settle() { for (let i = 0; i < 10; i++) await tick(); }

function navigate(wc, event, target) {
  let prevented = false;
  wc.emit(event, { preventDefault: () => { prevented = true; } }, target);
  return prevented;
}

test("only plain HTTPS addresses on Meta's login sites are allowed", () => {
  for (const ok of ["https://secure.oculus.com", "https://secure.oculus.com/my/profile", "https://auth.meta.com/login/",
                    "https://www.meta.com/", "https://www.facebook.com/login.php", "https://oculus.com:443/",
                    "https://www.instagram.com/accounts/login/"]) {
    assert.ok(allowedUrl(ok), ok);
  }
  for (const bad of ["http://secure.oculus.com/", "https://oculus.com.evil.example/", "https://evil-oculus.com/",
                     "https://notmeta.com/", "https://example.com/", "javascript:alert(1)", "file:///etc/passwd",
                     "https://secure.oculus.com:8443/", "https://user:pw@secure.oculus.com/", "https://evil.example/?u=https://www.meta.com",
                     "data:text/html,hi", "not a url"]) {
    assert.ok(!allowedUrl(bad), bad);
  }
});

test("the window is isolated: its own partition, no preload, no Node, sandboxed, on Meta's login page", async () => {
  const f = fakes();
  f.flow.signIn();
  const [w] = f.windows;
  const wp = w.opts.webPreferences;
  assert.strictEqual(wp.partition, PARTITION);
  assert.strictEqual(PARTITION, "persist:meta-signin");
  assert.strictEqual(wp.contextIsolation, true);
  assert.strictEqual(wp.nodeIntegration, false);
  assert.strictEqual(wp.sandbox, true);
  assert.strictEqual(wp.preload, undefined);
  assert.strictEqual(wp.devTools, false);
  assert.deepStrictEqual(w.webContents.loaded, [LOGIN_URL]);
  assert.strictEqual(LOGIN_URL, "https://secure.oculus.com");
  // Permission prompts (camera, notifications...) are refused.
  let granted = null;
  f.meta().permission(null, "media", (v) => { granted = v; });
  assert.strictEqual(granted, false);
  assert.strictEqual(f.meta().permissionCheck(null, "notifications", "https://secure.oculus.com"), false);
  assert.ok(!/Electron|frame-control/.test(f.meta().ua));
  w.close();
});

test("navigation stays on Meta's sites; other links open in the system browser", async () => {
  const f = fakes();
  f.flow.signIn();
  const wc = f.windows[0].webContents;
  assert.strictEqual(navigate(wc, "will-navigate", "https://auth.meta.com/login/"), false);
  assert.strictEqual(navigate(wc, "will-navigate", "https://www.facebook.com/login.php"), false);
  assert.strictEqual(navigate(wc, "will-navigate", "https://example.com/help"), true);
  assert.deepStrictEqual(f.opened, ["https://example.com/help"]);
  // A redirect elsewhere is stopped and not opened anywhere (it may carry sign-in data).
  assert.strictEqual(navigate(wc, "will-redirect", "https://evil.example/cb?x=1"), true);
  assert.strictEqual(navigate(wc, "will-redirect", "https://auth.meta.com/next"), false);
  assert.deepStrictEqual(f.opened, ["https://example.com/help"]);
  // New windows: never created. Meta's own load in this window; others go outside; non-web schemes nowhere.
  assert.deepStrictEqual(wc.openHandler({ url: "https://www.meta.com/legal/terms/" }), { action: "deny" });
  assert.deepStrictEqual(wc.openHandler({ url: "https://example.org/" }), { action: "deny" });
  assert.deepStrictEqual(wc.openHandler({ url: "file:///etc/passwd" }), { action: "deny" });
  await settle();
  assert.deepStrictEqual(wc.loaded, [LOGIN_URL, "https://www.meta.com/legal/terms/"]);
  assert.deepStrictEqual(f.opened, ["https://example.com/help", "https://example.org/"]);
  f.windows[0].close();
});

test("the cookie is read only from the sign-in partition, only for Meta's Oculus domain", async () => {
  const f = fakes();
  // The same cookie elsewhere: the default session and another partition. Never read.
  f.session.defaultSession.cookies.store.push({ name: COOKIE, domain: ".oculus.com", value: OTHER });
  f.session.fromPartition("persist:other").cookies.store.push({ name: COOKIE, domain: ".oculus.com", value: OTHER });
  const done = f.flow.signIn();
  const w = f.windows[0];
  // A cookie with the right name on another site, and a signed-out value: ignored.
  f.meta().cookies.set({ name: COOKIE, domain: ".evil.example", value: OTHER });
  f.meta().cookies.set({ name: COOKIE, domain: ".oculus.com", value: "OC123456789012345" });
  w.webContents.emit("did-navigate", {}, "https://secure.oculus.com/my/profile");
  await settle();
  assert.deepStrictEqual(f.calls, []);
  f.meta().cookies.set({ name: COOKIE, domain: ".oculus.com", value: TOKEN });
  const r = await done;
  assert.deepStrictEqual(f.calls, [TOKEN]);
  assert.strictEqual(r.ok, true);
  assert.strictEqual(f.session.defaultSession.cookies.reads, 0);
  assert.strictEqual(f.session.fromPartition("persist:other").cookies.reads, 0);
  assert.ok(f.sessions.get(PARTITION).cookies.reads > 0);
});

test("landing on the logged-in page picks up a cookie set earlier", async () => {
  const f = fakes();
  const done = f.flow.signIn();
  f.meta().cookies.store.push({ name: COOKIE, domain: "secure.oculus.com", value: TOKEN });  // no change event
  f.windows[0].webContents.emit("did-navigate", {}, "https://auth.meta.com/login/");  // not Meta's store site yet
  await settle();
  assert.deepStrictEqual(f.calls, []);
  f.windows[0].webContents.emit("did-navigate", {}, "https://secure.oculus.com/my/profile");
  assert.strictEqual((await done).ok, true);
  assert.deepStrictEqual(f.calls, [TOKEN]);
});

test("after a connect the window closes, and the page gets the answer, never the token", async () => {
  const f = fakes();
  const done = f.flow.signIn();
  f.meta().cookies.set({ name: COOKIE, domain: ".oculus.com", value: TOKEN });
  const r = await done;
  assert.ok(f.windows[0].isDestroyed());
  assert.ok(!f.flow.isOpen());
  assert.deepStrictEqual(r, { ok: true, connected: true, user: { name: "Alex" }, message: "Connected your Meta account (Experimental)" });
  assert.ok(!JSON.stringify(r).includes(TOKEN));
  // The cookie listener is gone: a later change sends nothing.
  f.meta().cookies.set({ name: COOKIE, domain: ".oculus.com", value: OTHER });
  await settle();
  assert.deepStrictEqual(f.calls, [TOKEN]);
});

test("a token the server refuses keeps the window open, is sent once, and the next one connects", async () => {
  const f = fakes();
  f.setAnswer((t) => t === TOKEN ? { status: 400, data: { error: "Meta didn't accept this token." } }
                                 : { status: 200, data: { connected: true, user: null } });
  const done = f.flow.signIn();
  f.meta().cookies.set({ name: COOKIE, domain: ".oculus.com", value: TOKEN });
  await settle();
  f.windows[0].webContents.emit("did-navigate", {}, "https://secure.oculus.com/my/profile");  // same value again
  await settle();
  assert.deepStrictEqual(f.calls, [TOKEN]);
  assert.ok(!f.windows[0].isDestroyed());
  f.meta().cookies.set({ name: COOKIE, domain: ".oculus.com", value: OTHER });
  const r = await done;
  assert.deepStrictEqual(f.calls, [TOKEN, OTHER]);
  assert.strictEqual(r.ok, true);
  assert.strictEqual(r.user, null);
});

test("the token is never logged, and an error that echoed it is scrubbed", async () => {
  const seen = [];
  const names = ["log", "info", "warn", "error", "debug", "trace"];
  const saved = Object.fromEntries(names.map((n) => [n, console[n]]));
  const writes = [process.stdout.write, process.stderr.write];
  for (const n of names) console[n] = (...a) => { seen.push(a.map(String).join(" ")); };
  process.stdout.write = (c, ...rest) => { seen.push(String(c)); return writes[0].call(process.stdout, c, ...rest); };
  process.stderr.write = (c, ...rest) => { seen.push(String(c)); return writes[1].call(process.stderr, c, ...rest); };
  let r;
  try {
    const f = fakes();
    f.setAnswer((t) => ({ status: 500, data: { error: `RuntimeError: bad ${t} and ${t.slice(0, 12)}` } }));
    const done = f.flow.signIn();
    f.meta().cookies.set({ name: COOKIE, domain: ".oculus.com", value: TOKEN });
    await settle();
    f.windows[0].close();  // the user gives up
    r = await done;
  } finally {
    for (const n of names) console[n] = saved[n];
    [process.stdout.write, process.stderr.write] = writes;
  }
  assert.strictEqual(r.ok, false);
  assert.strictEqual(r.cancelled, true);
  assert.ok(!r.error.includes(TOKEN) && !r.error.includes(TOKEN.slice(0, 12)), r.error);
  assert.match(r.error, /<redacted>/);
  assert.ok(!seen.some((s) => s.includes(TOKEN.slice(0, 12))), "token in output");
  assert.strictEqual(scrub(`x FRL${"a".repeat(30)} y`), "x <redacted> y");
});

test("closing the window before signing in reports it, and a second click reuses the open window", async () => {
  const f = fakes();
  const a = f.flow.signIn();
  const b = f.flow.signIn();
  assert.strictEqual(a, b);
  assert.strictEqual(f.windows.length, 1);
  assert.strictEqual(f.windows[0].focused, 1);
  f.windows[0].close();
  const r = await a;
  assert.deepStrictEqual(r, { ok: false, cancelled: true, error: "The sign-in window was closed before you signed in." });
  assert.deepStrictEqual(f.calls, []);
});

test("disconnect's sign-out closes the window and clears only the sign-in partition", async () => {
  const f = fakes();
  f.session.fromPartition("persist:other");
  const pending = f.flow.signIn();
  await f.flow.signOut();
  const r = await pending;
  assert.strictEqual(r.ok, false);
  assert.ok(f.windows[0].isDestroyed());
  assert.deepStrictEqual(f.meta().cleared, [["storage", undefined], ["cache"], ["auth"]]);
  assert.deepStrictEqual(f.session.defaultSession.cleared, []);
  assert.deepStrictEqual(f.session.fromPartition("persist:other").cleared, []);
  // The next sign-in starts with a new window.
  f.flow.signIn();
  assert.strictEqual(f.windows.length, 2);
  f.windows[1].close();
});

test("the first look at a cookie value matches the server's token rules", () => {
  assert.ok(looksLikeUserToken(TOKEN));
  assert.ok(looksLikeUserToken("FRL" + "aB3".repeat(10)));
  for (const bad of [undefined, "", "OC123456789012345", "OC|123|abc", "OCabc%2Fdef" + "x".repeat(20), "OCshort",
                     "XY" + "a".repeat(30), "OC" + "A".repeat(30)]) {
    assert.ok(!looksLikeUserToken(bad), String(bad));
  }
});

function frameNav(wc, url, isMainFrame) {
  let prevented = false;
  wc.emit("will-frame-navigate", { url, isMainFrame, preventDefault: () => { prevented = true; } });
  return prevented;
}

test("the window stays on Meta's sites; frames inside it may load any HTTPS page, nothing else", async () => {
  const f = fakes();
  f.flow.signIn();
  const wc = f.windows[0].webContents;
  // Frames: captcha and CDN frames from other sites load; other schemes are stopped.
  for (const ok of ["https://client-api.arkoselabs.com/fc/gc/", "https://newassets.hcaptcha.com/captcha/v1/x",
                    "https://www.google.com/recaptcha/api2/anchor", "https://static.xx.fbcdn.net/x",
                    "https://www.facebook.com/plugin", "about:blank", "about:srcdoc"]) {
    assert.strictEqual(frameNav(wc, ok, false), false, ok);
  }
  for (const bad of ["http://www.facebook.com/plugin", "data:text/html,<script>x</script>", "blob:https://evil.example/1",
                     "javascript:alert(1)", "file:///etc/passwd", "ftp://example.com/", "https://u:p@evil.example/",
                     "chrome://settings", "not a url"]) {
    assert.strictEqual(frameNav(wc, bad, false), true, bad);
  }
  // The window itself: Meta's sites only.
  assert.strictEqual(frameNav(wc, "about:blank", true), true);
  assert.strictEqual(frameNav(wc, "https://evil.example/", true), true);
  assert.strictEqual(frameNav(wc, "https://client-api.arkoselabs.com/", true), true);
  assert.strictEqual(frameNav(wc, "https://auth.meta.com/login/", true), false);
  assert.deepStrictEqual(f.opened, []);  // stopped, never opened outside
  f.windows[0].destroy();
});

function redirect(wc, url, isMainFrame, style) {
  let prevented = false;
  const e = { preventDefault: () => { prevented = true; } };
  if (style === "details") wc.emit("will-redirect", Object.assign(e, { url, isMainFrame }));
  else wc.emit("will-redirect", e, url, false, isMainFrame);
  return prevented;
}

test("redirects follow the same rule per frame", async () => {
  const f = fakes();
  f.flow.signIn();
  const wc = f.windows[0].webContents;
  for (const style of ["details", "positional"]) {
    // A captcha frame redirected within HTTPS loads; a frame redirected to data: or http: doesn't.
    assert.strictEqual(redirect(wc, "https://client-api.arkoselabs.com/v2/next", false, style), false, style);
    assert.strictEqual(redirect(wc, "https://newassets.hcaptcha.com/x", false, style), false, style);
    assert.strictEqual(redirect(wc, "data:text/html,<b>x</b>", false, style), true, style);
    assert.strictEqual(redirect(wc, "http://www.facebook.com/x", false, style), true, style);
    assert.strictEqual(redirect(wc, "javascript:alert(1)", false, style), true, style);
    // The window: Meta's sites only.
    assert.strictEqual(redirect(wc, "https://client-api.arkoselabs.com/v2/next", true, style), true, style);
    assert.strictEqual(redirect(wc, "https://auth.meta.com/next", true, style), false, style);
  }
  // Without frame information it is treated as the window.
  assert.strictEqual(redirect(wc, "https://client-api.arkoselabs.com/", undefined, "positional"), true);
  assert.deepStrictEqual(f.opened, []);
  f.windows[0].destroy();
});

test("a redirect the window stops is named when the window is closed", async () => {
  const f = fakes();
  const p = f.flow.signIn();
  const wc = f.windows[0].webContents;
  assert.strictEqual(redirect(wc, "https://www.instagram.com/x?next=1", true, "details"), false);  // Meta's login goes here
  assert.strictEqual(redirect(wc, "https://login.evil.example/cb?code=SECRET", true, "details"), true);
  f.windows[0].close();
  const r = await p;
  assert.strictEqual(r.ok, false);
  assert.match(r.error, /went to login\.evil\.example, which Frame Control doesn't allow/);
  assert.doesNotMatch(r.error, /SECRET|code=/);
});

test("a stopped redirect is forgotten once the window loads a page again", async () => {
  const f = fakes();
  const p = f.flow.signIn();
  const wc = f.windows[0].webContents;
  assert.strictEqual(redirect(wc, "https://login.evil.example/cb", true, "details"), true);
  wc.emit("did-navigate", {}, "https://auth.meta.com/login/");  // the user carried on
  f.windows[0].close();
  assert.deepStrictEqual(await p, { ok: false, cancelled: true, error: "The sign-in window was closed before you signed in." });
});

test("a page's beforeunload can't keep the window open", async () => {
  const f = fakes();
  const done = f.flow.signIn();
  const w = f.windows[0];
  let prevented = false;
  w.webContents.emit("will-prevent-unload", { preventDefault: () => { prevented = true; } });
  assert.ok(prevented);
  // Even if close() would be vetoed, finishing destroys the window.
  w.veto = true;
  f.meta().cookies.set({ name: COOKIE, domain: ".oculus.com", value: TOKEN });
  assert.strictEqual((await done).ok, true);
  assert.ok(w.isDestroyed());
});

test("closing during a connect: the cancel goes to the server only after that connect settles", async () => {
  for (const outcome of ["saved", "refused", "failed"]) {
    const f = fakes();
    let release, fail;
    f.setAnswer(() => new Promise((r, j) => { release = r; fail = j; }));
    const done = f.flow.signIn();
    f.meta().cookies.set({ name: COOKIE, domain: ".oculus.com", value: TOKEN });
    await settle();
    assert.deepStrictEqual(f.calls, [TOKEN]);
    f.windows[0].close();  // the user gives up while the server and Meta are answering
    const r = await done;  // the page hears at once
    assert.strictEqual(r.ok, false);
    await settle();
    assert.deepStrictEqual(f.cancels, [], outcome);  // not before the connect has settled
    if (outcome === "saved") release({ status: 200, data: { connected: true } });
    else if (outcome === "refused") release({ status: 400, data: { error: "no" } });
    else fail(new Error("socket hang up"));
    await settle();
    // The same attempt id the connect carried, after it settled and after cookie intake stopped.
    assert.deepStrictEqual(f.cancels, [f.attempts[0]], outcome);
    assert.match(f.cancels[0], /^[A-Za-z0-9-]{8,64}$/);
    assert.deepStrictEqual(f.log, ["connect", "destroyed", "cancel (cookie listeners: 0)"], outcome);
    assert.deepStrictEqual(f.calls, [TOKEN]);
  }
});

test("closing before any connect needs no cancel", async () => {
  const f = fakes();
  const done = f.flow.signIn();
  f.meta().cookies.set({ name: COOKIE, domain: ".evil.example", value: TOKEN });  // nothing usable
  await settle();
  f.windows[0].close();
  assert.strictEqual((await done).ok, false);
  await f.flow.signOut();
  await settle();
  assert.deepStrictEqual(f.calls, []);
  assert.deepStrictEqual(f.cancels, []);
});

test("a timeout during a connect cancels after it settles", async () => {
  const f = fakes({ timeout: 50 });
  let release;
  f.setAnswer(() => new Promise((r) => { release = r; }));  // Meta is slow
  const done = f.flow.signIn();
  f.meta().cookies.set({ name: COOKIE, domain: ".oculus.com", value: TOKEN });
  const r = await done;
  assert.match(r.error, /timed out/);
  assert.ok(f.windows[0].isDestroyed());
  assert.deepStrictEqual(f.cancels, []);
  release({ status: 400, data: { error: "no" } });
  await settle();
  assert.deepStrictEqual(f.cancels, [f.attempts[0]]);
});

test("disconnect during a connect waits for it, cancels it, then clears, before the page deletes the account", async () => {
  const f = fakes();
  let release;
  f.setAnswer(() => new Promise((r) => { release = r; }));
  const done = f.flow.signIn();
  f.meta().cookies.set({ name: COOKIE, domain: ".oculus.com", value: TOKEN });
  await settle();
  f.log.length = 0;
  let signedOut = false;
  const out = f.flow.signOut().then(() => { signedOut = true; });  // the page's Disconnect, step one
  assert.strictEqual((await done).ok, false);
  await settle();
  assert.strictEqual(signedOut, false);  // still waiting for the connect
  assert.deepStrictEqual(f.cancels, []);
  release({ status: 200, data: { connected: true } });  // it saved, just now
  await out;
  // Only now would the page post its disconnect: the cancel (which deletes what that
  // connect saved) has reached the server, and the session is cleared.
  assert.deepStrictEqual(f.log, ["destroyed", "cancel (cookie listeners: 0)", "clear"]);
  assert.deepStrictEqual(f.cancels, [f.attempts[0]]);
});

test("a later disconnect still waits for a closed window's connect", async () => {
  const f = fakes();
  let release;
  f.setAnswer(() => new Promise((r) => { release = r; }));
  const done = f.flow.signIn();
  f.meta().cookies.set({ name: COOKIE, domain: ".oculus.com", value: TOKEN });
  await settle();
  f.windows[0].close();
  await done;
  let signedOut = false;
  const out = f.flow.signOut().then(() => { signedOut = true; });
  await settle();
  assert.strictEqual(signedOut, false);
  release({ status: 400, data: { error: "no" } });
  await out;
  assert.deepStrictEqual(f.cancels, [f.attempts[0]]);
});

test("nothing sent means nothing to cancel; sign-out destroys the window, cancels, then clears", async () => {
  const f = fakes();
  // Nothing sent: closing cancels nothing.
  const a = f.flow.signIn();
  f.windows[0].close();
  await a;
  assert.deepStrictEqual(f.cancels, []);
  // Something sent, then signed out: the attempt is cancelled before the session is cleared.
  f.setAnswer(() => ({ status: 400, data: { error: "Meta didn't accept this token." } }));
  const b = f.flow.signIn();
  f.meta().cookies.set({ name: COOKIE, domain: ".oculus.com", value: TOKEN });
  await settle();
  f.windows[1].veto = true;  // and its page tries to stay open
  f.log.length = 0;
  await f.flow.signOut();
  assert.strictEqual((await b).ok, false);
  assert.ok(f.windows[1].isDestroyed());
  assert.deepStrictEqual(f.log, ["destroyed", "cancel (cookie listeners: 0)", "clear"]);
  assert.deepStrictEqual(f.cancels, [f.attempts[0]]);
  // After sign-out a new cookie in the partition sends nothing.
  f.meta().cookies.set({ name: COOKIE, domain: ".oculus.com", value: OTHER });
  await settle();
  assert.deepStrictEqual(f.calls, [TOKEN]);
});

test("each sign-in has its own attempt id", async () => {
  const f = fakes();
  f.setAnswer(() => ({ status: 400, data: { error: "no" } }));
  const a = f.flow.signIn();
  f.meta().cookies.set({ name: COOKIE, domain: ".oculus.com", value: TOKEN });
  await settle();
  f.windows[0].close();
  await a;
  const b = f.flow.signIn();
  f.meta().cookies.set({ name: COOKIE, domain: ".oculus.com", value: OTHER });
  await settle();
  f.windows[1].close();
  await b;
  await settle();
  assert.strictEqual(f.attempts.length, 2);
  assert.notStrictEqual(f.attempts[0], f.attempts[1]);
  assert.deepStrictEqual(f.cancels, f.attempts);
});

test("no sign-in can start while a sign-out waits on an earlier connect", async () => {
  const f = fakes();
  let release;
  f.setAnswer(() => new Promise((r) => { release = r; }));
  const first = f.flow.signIn();
  f.meta().cookies.set({ name: COOKIE, domain: ".oculus.com", value: TOKEN });
  await settle();
  const out = f.flow.signOut();  // Disconnect pressed while that connect is under way
  assert.strictEqual((await first).ok, false);
  assert.ok(f.flow.isSigningOut());
  // The page answered, so the user tries again at once: refused, no window, nothing sent.
  const second = await f.flow.signIn();
  assert.deepStrictEqual(second, { ok: false, error: "Frame Control is signing out of Meta; try again in a moment." });
  assert.strictEqual(f.windows.length, 1);
  f.meta().cookies.set({ name: COOKIE, domain: ".oculus.com", value: OTHER });
  await settle();
  assert.deepStrictEqual(f.calls, [TOKEN]);
  // A second sign-out joins the first.
  assert.strictEqual(f.flow.signOut(), out);
  release({ status: 200, data: { connected: true } });
  await out;
  assert.deepStrictEqual(f.cancels, [f.attempts[0]]);
  assert.strictEqual(f.meta().cleared.filter(([k]) => k === "storage").length, 1);
  assert.ok(!f.flow.isSigningOut());
  // Once it is done, signing in works again.
  f.setAnswer(() => ({ status: 200, data: { connected: true } }));
  const third = f.flow.signIn();
  assert.strictEqual(f.windows.length, 2);
  f.meta().cookies.set({ name: COOKIE, domain: ".oculus.com", value: OTHER });
  assert.strictEqual((await third).ok, true);
});

test("sign-out goes round until no window and no connect or cancel is left", async () => {
  const f = fakes();
  let releaseA;
  f.setAnswer(() => new Promise((r) => { releaseA = r; }));
  const a = f.flow.signIn();
  f.meta().cookies.set({ name: COOKIE, domain: ".oculus.com", value: TOKEN });
  await settle();
  f.windows[0].close();  // window A closed; its cancel waits on its connect
  await a;
  // Window B is open, with its own connect under way, when sign-out starts.
  let releaseB;
  f.setAnswer(() => new Promise((r) => { releaseB = r; }));
  const b = f.flow.signIn();
  f.meta().cookies.set({ name: COOKIE, domain: ".oculus.com", value: OTHER });
  await settle();
  let finished = false;
  const out = f.flow.signOut().then(() => { finished = true; });
  await b;
  releaseB({ status: 400, data: { error: "no" } });
  await settle();
  assert.strictEqual(finished, false);  // A's connect is still under way
  releaseA({ status: 200, data: { connected: true } });
  await out;
  assert.deepStrictEqual([...f.cancels].sort(), [...f.attempts].sort());
  assert.strictEqual(f.attempts.length, 2);
  assert.strictEqual(f.log[f.log.length - 1], "clear");
});
