// The Meta library's rows, run in node against the real functions from ui/index.html.
// Found in a live test with a real account (2026-10-14): sizes were cut off behind the
// package name, a required data file Frame Control can't place was not mentioned, and
// apps and environments were counted as "games".
import { readFileSync } from "fs";
const src = readFileSync(new URL("../../ui/index.html", import.meta.url), "utf8");
const lines = src.split("\n");
const fail = msg => { console.log("FAIL " + msg); process.exit(1); };

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
const els = { metaCount: { textContent: "" }, metaApps: { innerHTML: "" } };
const metaLib = { connected: true, results: new Map(), apps: [
  { id: "1", title: "The Climb", package: "com.crytek.climb", version: "666.0", version_code: 666, size: 30639455,
    data_files: 0, skipped_files: ["main.com.crytek.climb.obb"] },
  { id: "2", title: "Beat Saber", package: "com.beatgames.beatsaber", problem: "Meta's API said HTTP 500", skipped_files: [] },
] };
const render = new Function("$", "metaLib", "androidApps", "installing", "queued",
  grab("esc") + grab("questSize") + "const META_VERDICT = {};\n" + grab("renderMetaApps") + "renderMetaApps();");
render(id => els[id], metaLib, [], new Set(["meta:1"]), new Set(["meta:1"]));
const html = els.metaApps.innerHTML;
if (els.metaCount.textContent !== "2 apps") fail("count: " + els.metaCount.textContent);
const climb = html.split('class="item"')[1];
if (!/<div class="s" title="[^"]*">29 MB · v666\.0 \(666\) · com\.crytek\.climb</.test(climb)) fail("size not first: " + climb);
if (!/Not installed: 1 required data file \(main\.com\.crytek\.climb\.obb\)/.test(climb)) fail("skipped data not shown: " + climb);
const saber = html.split('class="item"')[2];
if (!/<div class="s v-no"[^>]*>Meta&#39;s API said HTTP 500</.test(saber)) fail("problem not on its own line: " + saber);
if (/Not installed/.test(saber)) fail("warning without skipped files");
// Waiting in the install queue: marked, and not offered again.
if (!/<span class="tag">Queued<\/span>/.test(climb) || !/data-meta-install="1" disabled/.test(climb)) fail("queued not shown: " + climb);
if (/Queued|disabled/.test(saber)) fail("queued on the wrong row: " + saber);
metaLib.apps = metaLib.apps.slice(0, 1);
render(id => els[id], metaLib, [], new Set(), new Set());
if (els.metaCount.textContent !== "1 app") fail("singular count: " + els.metaCount.textContent);
console.log("ok");
