// Exercise the production Workspace rendering and switch handler without a server.
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

const page = readFileSync(new URL("../../ui/index.html", import.meta.url), "utf8");
const source = page.slice(page.indexOf("let mvSeq = 0"), page.indexOf("// ---- pages: #home"));
const elements = new Map(), requests = [], groups = [{}, {}];
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    dataset: {}, parentElement: {}, options: [], value: "", textContent: "", hidden: false,
    setAttribute(name, value) { this[name] = value; }, addEventListener() {}, offsetParent: true,
  });
  return elements.get(id);
}
for (const value of ["5120x1440", "2880x2560"]) element("mvWorkspacePreset").options.push({value});
element("mvQuality").options = [{value: "sharp"}];
const desktop = {available: true, screen: true, accessibility: true, streams: [], windows: [], displays: [],
  workspace: {mode: "desktop", state: "stopped"}};
let current = structuredClone(desktop);
const context = vm.createContext({
  $: element, document: {querySelectorAll: () => groups, hidden: false},
  localStorage: {getItem: () => null, setItem() {}}, setTimeout: () => 1, clearTimeout() {},
  esc: value => String(value), failed: () => { throw Error("Unexpected Request Failure"); },
  api: async (path, body) => {
    if (body) {
      requests.push(body);
      current.workspace.mode = body.action.slice(5);
    }
    return structuredClone(current);
  },
  act: async (label, action) => action(),
});
vm.runInContext(source, context);
function render(state) {
  current = structuredClone(state);
  context.fixture = current;
  vm.runInContext("mvState = fixture; renderMvWorkspace(mvState);", context);
}
render(desktop);
assert.equal(element("mvModeDesktop")["aria-pressed"], "true");
assert.equal(element("mvWorkspacePreset").parentElement.hidden, false);
assert.equal(element("mvWorkspaceStart").textContent, "Start Workspace");

render({...desktop, workspace: {mode: "spatial", state: "running", panelsReady: false},
  streams: [{src: "separate:42"}, {src: "separate:43"}, {src: "test"}]});
assert.equal(element("mvModeSpatial")["aria-pressed"], "true");
assert.equal(element("mvWorkspacePreset").parentElement.hidden, true);
assert.equal(element("mvWorkspaceScale").parentElement.hidden, true);
assert.equal(element("mvSeparate").parentElement.hidden, true);
assert.equal(element("mvPlacementStatus").hidden, true);
assert.equal(element("mvWorkspaceStart").disabled, true);
assert.equal(element("mvWorkspaceStop").hidden, false);
assert.match(element("mvWorkspaceStatus").textContent, /2 Window Panels/);

render({...desktop, workspace: {mode: "spatial", state: "error", removed: false, error: "Removal Pending"}});
assert.equal(element("mvModeDesktop").disabled, true);
assert.equal(element("mvWorkspaceStop").disabled, false);
assert.equal(element("mvWorkspaceStatus").textContent, "Removal Pending");

render({...desktop, workspace: {mode: "spatial", state: "error", removed: true,
  panelCleanupPending: true, error: "Frame Panel Cleanup Pending"}});
assert.equal(element("mvModeDesktop").disabled, true);
assert.equal(element("mvWorkspaceStop").hidden, false);
assert.equal(element("mvWorkspaceStop").disabled, false);

render(desktop);
element("mvModeSpatial").dataset = {mv: "workspace-mode", mode: "spatial"};
await element("macview").onclick({target: {closest: () => element("mvModeSpatial")}});
assert.equal(requests.length, 1);
assert.equal(requests[0].action, "mode_spatial");
assert.equal(element("mvModeSpatial")["aria-pressed"], "true");
assert.equal(element("mvWorkspaceStart").disabled, false);
console.log("Workspace Mode Rendering And Switching Passed");
