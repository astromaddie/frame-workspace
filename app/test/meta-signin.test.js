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
  constructor(name) { this.name = name; this.cookies = new FakeCookies(); this.cleared = []; this.permission = null; }
  setPermissionRequestHandler(fn) { this.permission = fn; }
  getUserAgent() { return "Mozilla/5.0 Chrome/140.0 frame-control/0.4.4 Electron/44.4.5 Safari/537.36"; }
  setUserAgent(ua) { this.ua = ua; }
  async clearStorageData(opts) { this.cleared.push(["storage", opts]); }
  async clearCache() { this.cleared.push(["cache"]); }
  async clearAuthCache() { this.cleared.push(["auth"]); }
}

function fakes() {
  const sessions = new Map();
  const defaultSession = new FakeSession("default");
  const session = {
    defaultSession,
    fromPartition(name) {
      if (!sessions.has(name)) sessions.set(name, new FakeSession(name));
      return sessions.get(name);
    },
  };
  const windows = [];
  class BrowserWindow extends EventEmitter {
    constructor(opts) {
      super();
      this.opts = opts;
      this.destroyed = false;
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
    close() { if (this.destroyed) return; this.destroyed = true; this.emit("closed"); }
  }
  const opened = [];
  const shell = { openExternal: (u) => { opened.push(u); } };
  const calls = [];
  let answer = () => ({ status: 200, data: { connected: true, user: { name: "Alex" }, message: "Connected your Meta account (Experimental)" } });
  const connect = async (token) => { calls.push(token); return answer(token); };
  const flow = createMetaSignIn({ BrowserWindow, session, shell, connect, parent: () => null });
  return { flow, session, sessions, windows, opened, calls, setAnswer: (fn) => { answer = fn; },
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
                    "https://www.meta.com/", "https://www.facebook.com/login.php", "https://oculus.com:443/"]) {
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
  assert.deepStrictEqual(w.webContents.loaded, [LOGIN_URL]);
  assert.strictEqual(LOGIN_URL, "https://secure.oculus.com");
  // Permission prompts (camera, notifications...) are refused.
  let granted = null;
  f.meta().permission(null, "media", (v) => { granted = v; });
  assert.strictEqual(granted, false);
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
