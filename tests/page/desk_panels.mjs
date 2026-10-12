// The live view's Desktop panel list (#73), run in node against the real functions from
// ui/index.html with a fake server: the headset's reason when it lists no panels, a failed
// listing shown (first time too) rather than "Finding…", and recovery after it.
import { readFileSync } from "fs";
const src = readFileSync(new URL("../../ui/index.html", import.meta.url), "utf8");
const grab = name => {
  const m = src.match(new RegExp(`(?:\\nconst ${name} = [^\\n]*\\n)|((?:async )?function ${name}\\([\\s\\S]*?\\n}\\n)`));
  if (!m) throw new Error("not found: " + name);
  return m[0];
};
// The page's scripts share one global scope, so a second loadPanels would silently replace
// the Desktop view's (what broke #73 until #84): the one grabbed here must be the only one.
for (const name of ["loadPanels", "noPanelWhy", "deskPanel", "ctrlTarget"]) {
  const count = (src.match(new RegExp(`\\n(?:async )?function ${name}\\(`, "g")) || []).length;
  if (count !== 1) { console.log(`FAIL ${name} is defined ${count} times`); process.exit(1); }
}
const code = ["desk", "panelKey", "noPanelWhy", "deskPanel", "loadPanels", "ctrlTarget"].map(grab).join("");
const fail = msg => { console.log("FAIL " + msg); process.exit(1); };

function page(answer) {
  const calls = [], logged = [];
  const env = {
    view: "flat", live: false, ctrlMode: () => "abs", renderPanelPick: () => {}, toggleLive: () => {}, ctrlShow: () => {},
    log: (m) => logged.push(m),
    api: async (path, body) => { calls.push([path, body]); return answer(); },
  };
  const fns = new Function(...Object.keys(env), `${code}
    return { desk, noPanelWhy, loadPanels, ctrlTarget };`)(...Object.values(env));
  return { ...fns, calls, logged };
}

// The Desktop view asks for the headset's panels with a plain GET (no switcher action).
{
  const p = page(() => ({ panels: [], focus: null, why: "The headset's desktop is running, but SteamOS isn't listing it as a panel." }));
  if (p.ctrlTarget().why !== "Finding the headset's panels…") fail("before loading: " + p.ctrlTarget().why);
  await p.loadPanels();
  if (!(p.calls.length === 1 && p.calls[0][0] === "/api/panels" && p.calls[0][1] === undefined))
    fail("request " + JSON.stringify(p.calls));
  if (!p.desk.loaded) fail("not marked loaded");
  if (!p.noPanelWhy().startsWith("The headset's desktop is running")) fail("why for capture: " + p.noPanelWhy());
  if (!p.ctrlTarget().why.startsWith("The headset's desktop is running")) fail("why for control: " + p.ctrlTarget().why);
}
// An older headset script without "why": the generic advice.
{
  const p = page(() => ({ panels: [], focus: null }));
  await p.loadPanels();
  if (!p.noPanelWhy().startsWith("No app panel is open in the headset. Open the desktop")) fail("generic: " + p.noPanelWhy());
}
// The first listing fails: Control and Capture say so (logged once), then recover.
{
  let broken = true;
  const p = page(() => {
    if (broken) throw new Error("Timed out talking to frame");
    return { panels: [{ window: 7, display: ":0", name: "Desktop" }], focus: 7, focus_display: ":0" };
  });
  await p.loadPanels(); await p.loadPanels();
  const why = p.ctrlTarget().why;
  if (why !== "Couldn't list the headset's panels: Timed out talking to frame") fail("initial failure in Control: " + why);
  if (p.noPanelWhy() !== why) fail("initial failure in Capture: " + p.noPanelWhy());
  if (p.logged.length !== 1) fail("logged " + p.logged.length + " times");
  broken = false;
  await p.loadPanels();
  if (p.desk.error) fail("error kept after recovery");
  const t = p.ctrlTarget();
  if (!t.why || t.why.includes("Couldn't list")) fail("after recovery: " + JSON.stringify(t));
  if (p.desk.panels.length !== 1) fail("panels after recovery");
}
// Waiting for the Frame: shown in Control, not logged every poll. An answer about the
// previous headset (stale) is ignored.
{
  let err;
  const p = page(() => { throw err; });
  err = Object.assign(new Error("Can't reach the Frame"), { offline: true });
  await p.loadPanels(); await p.loadPanels();
  if (p.logged.length) fail("offline logged: " + p.logged);
  if (p.ctrlTarget().why !== "Couldn't list the headset's panels: Can't reach the Frame") fail("offline: " + p.ctrlTarget().why);
  const q = page(() => { throw Object.assign(new Error("old headset"), { offline: true, stale: true }); });
  await q.loadPanels();
  if (q.logged.length || q.desk.error) fail("stale answer used: " + q.desk.error);
  if (q.ctrlTarget().why !== "Finding the headset's panels…") fail("stale: " + q.ctrlTarget().why);
}
console.log("ok");
