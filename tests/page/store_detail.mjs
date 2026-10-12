// The Android store's app page, run in node against the real functions from ui/index.html.
// Issue #68: clicking an app opened an empty dialog (a bare horizontal line), because the
// Devices page's renderDetail replaced the store's. The store's is renderAppDetail now.
// (tests/test_server.py PageScripts checks no top-level name is declared twice.)
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
               "updateInstall", "openStoreApp", "loadOfferDetails"];
const code = names.map(grab).join("");

// A fake page: elements by id. The install panel's elements exist only while
// #detailBody holds the real app page, as in the browser.
function page(api = async () => ({})) {
  const els = { appDetail: { open: false, scrollTop: 0, showModal() { this.open = true; } } };
  const inPage = id => ["detailInstall", "detailInstallNote", "detailProgress", "detailOffers"].includes(id);
  const $ = id => inPage(id) && !(els.detailBody?.innerHTML || "").includes(`id="${id}"`) ? null
    : (els[id] ||= { innerHTML: "", textContent: "", hidden: false, value: 0, removeAttribute() {}, querySelectorAll: () => [] });
  const sourceState = { apps: [], detail: null, detailRequest: 0, jobs: new Map() };
  const document = { activeElement: null, querySelector: () => null };
  const console = { error() {} };
  const calls = [];
  const fns = new Function("$", "sourceState", "document", "console", "api", code + "; return { renderAppDetail, openStoreApp };")(
    $, sourceState, document, console, (...a) => { calls.push(a); return api(...a); });
  return { els, sourceState, calls, ...fns };
}

const offer = { source: "fdroid", id: "org.example.brush", source_name: "F-Droid", name: "Open Brush", summary: "Paint in VR",
                description: "Paint in 3D.", version: "2.1", version_code: 21, package: "org.example.brush",
                artwork: { icon: "/source-image/abc", screenshots: [] }, downloadable: true, verdict: { tone: "works", label: "Works" } };

// An app from the catalogue: its page renders, with its name, install and sources.
{
  const p = page();
  p.sourceState.detail = { app: { name: "Open Brush", offers: [{ ...offer }] }, choice: 0 };
  if (p.renderAppDetail() !== true) fail("a good listing reported a render failure");
  const html = p.els.detailBody.innerHTML;
  if (!html.includes('id="detailTitle">Open Brush<')) fail("app page has no title: " + html.slice(0, 200));
  if (!html.includes('id="detailInstall"') || !html.includes("Get it from")) fail("app page has no install panel");
  if (html.includes('role="alert"')) fail("a good listing showed the error state");
}

// A listing that can't render (no offer to show) gets a clear message, not an empty dialog.
{
  const p = page();
  p.sourceState.detail = { app: { name: "Broken <App>", offers: [] }, choice: 0 };
  if (p.renderAppDetail() !== false) fail("a broken listing reported success");
  const html = p.els.detailBody.innerHTML;
  if (!html.includes('role="alert"') || !html.includes("We couldn't show Broken &lt;App&gt;")) fail("no error state: " + html);
  if (!html.includes('data-close="appDetail"')) fail("error state has no way to close");
}

// Opening a listing that can't render: the error stays up, no details request, nothing thrown.
{
  const p = page(async () => { throw new Error("offline"); });
  p.sourceState.apps = [{ name: "Broken", offers: [] }];
  await p.openStoreApp(0);
  if (!p.els.appDetail.open || !p.els.detailBody.innerHTML.includes('role="alert"')) fail("broken listing didn't open with the error");
  if (p.calls.length) fail("asked for details of a listing that couldn't render");
}

// Opening a good listing: the dialog opens with its page, and fresher details fill it in.
{
  const p = page(async () => ({ description: "Fresh description." }));
  p.sourceState.apps = [{ name: "Open Brush", offers: [{ ...offer }] }];
  await p.openStoreApp(0);
  if (!p.els.appDetail.open || !p.els.detailBody.innerHTML.includes("Fresh description.")) fail("details didn't fill in the page");
  if (!String(p.calls[0]?.[0]).startsWith("/api/sources/details?")) fail("no details request: " + JSON.stringify(p.calls));
}

// A good listing whose details request fails keeps its page, with a note.
{
  const p = page(async () => { throw new Error("offline"); });
  p.sourceState.apps = [{ name: "Open Brush", offers: [{ ...offer }] }];
  await p.openStoreApp(0);
  if (!p.els.detailBody.innerHTML.includes('id="detailTitle">Open Brush<')) fail("page lost after a failed details request");
  if (!p.els.detailInstallNote.textContent.includes("couldn't be refreshed")) fail("no note after a failed details request");
}
console.log("ok");
