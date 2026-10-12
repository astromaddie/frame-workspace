// The Android store's app page, run in node against the real functions from ui/index.html.
// Issue #68: clicking an app opened an empty dialog (a bare horizontal line), because the
// Devices page's renderDetail replaced the store's. The store's is renderAppDetail now.
import { readFileSync } from "fs";
const src = readFileSync(new URL("../../ui/index.html", import.meta.url), "utf8");
const lines = src.split("\n");
const fail = msg => { console.log("FAIL " + msg); process.exit(1); };

// A top-level declaration, from its first line to where its braces balance.
function grab(name) {
  const start = lines.findIndex(l => new RegExp(`^(?:(?:async )?function ${name}\\(|const ${name} = )`).test(l));
  if (start < 0) fail(`no top-level ${name} in index.html`);
  let depth = 0;
  for (let i = start; i < lines.length; i++) {
    for (const c of lines[i]) depth += c === "{" ? 1 : c === "}" ? -1 : 0;
    if (depth <= 0) return lines.slice(start, i + 1).join("\n") + "\n";
  }
  fail(`unbalanced ${name}`);
}
const names = ["esc", "storeName", "storeHue", "storeInitial", "storeText", "storeOpenSource", "sourcePage", "storeImage",
               "storeArt", "storeIcon", "sourceMark", "currentOffer", "detailBadges", "renderAppDetail", "renderAppDetailBody",
               "updateInstall"];
const code = names.map(grab).join("");

function page() {
  const els = {};
  const $ = id => id === "detailInstall" ? null : (els[id] ||= { innerHTML: "", textContent: "" });
  const sourceState = { detail: null, jobs: new Map() };
  const document = { activeElement: null, querySelector: () => null };
  const console = { error() {} };
  const fns = new Function("$", "sourceState", "document", "console", code + "; return { renderAppDetail };")($, sourceState, document, console);
  return { els, sourceState, ...fns };
}

const offer = { source: "fdroid", id: "org.example.brush", source_name: "F-Droid", name: "Open Brush", summary: "Paint in VR",
                description: "Paint in 3D.", version: "2.1", version_code: 21, package: "org.example.brush",
                artwork: { icon: "/source-image/abc", screenshots: [] }, downloadable: true, verdict: { tone: "works", label: "Works" } };

// An app from the catalogue: its page renders, with its name, install and sources.
{
  const p = page();
  p.sourceState.detail = { app: { name: "Open Brush", offers: [{ ...offer }] }, choice: 0 };
  p.renderAppDetail();
  const html = p.els.detailBody.innerHTML;
  if (!html.includes('id="detailTitle">Open Brush<')) fail("app page has no title: " + html.slice(0, 200));
  if (!html.includes('id="detailInstall"') || !html.includes("Get it from")) fail("app page has no install panel");
  if (html.includes('role="alert"')) fail("a good listing showed the error state");
}

// A listing that can't render (no offer to show) gets a clear message, not an empty dialog.
{
  const p = page();
  p.sourceState.detail = { app: { name: "Broken <App>", offers: [] }, choice: 0 };
  p.renderAppDetail();
  const html = p.els.detailBody.innerHTML;
  if (!html.includes('role="alert"') || !html.includes("We couldn't show Broken &lt;App&gt;")) fail("no error state: " + html);
  if (!html.includes('data-close="appDetail"')) fail("error state has no way to close");
}

// No other top-level function in the page's scripts may take a store function's name:
// a later function declaration silently replaces an earlier one.
{
  const scripts = [...src.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join("\n");
  const seen = new Map();
  for (const m of scripts.matchAll(/^(?:async )?function\*? ?([\w$]+)\s*\(/gm)) seen.set(m[1], (seen.get(m[1]) || 0) + 1);
  const twice = [...seen].filter(([, n]) => n > 1).map(([k]) => k);
  if (twice.length) fail("functions declared twice in index.html: " + twice.join(", "));
}
console.log("ok");
