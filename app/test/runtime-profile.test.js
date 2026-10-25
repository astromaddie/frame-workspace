"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");
const { getConfig } = require("app-builder-lib/out/util/config/config.js");
const { initialPage, isWorkspaceProfile, migrateWorkspaceData, WORKSPACE_DEFAULTS,
        workspaceServerEnvironment } = require("../runtime-profile");

test("Frame Control keeps its existing profile unless the separate profile is selected", () => {
  assert.equal(isWorkspaceProfile({ frameWorkspacePrototype: true }, {}), false);
  assert.equal(isWorkspaceProfile({ frameWorkspaceProfile: true }, {}), true);
  assert.equal(isWorkspaceProfile({}, { FRAME_WORKSPACE_PROFILE: "1" }), true);
  assert.equal(initialPage("http://127.0.0.1:47810/", false), "http://127.0.0.1:47810/");
});

test("Workspace server gets isolated paths, native defaults, and telemetry/update shutoff", () => {
  const env = workspaceServerEnvironment({ FRAME_WORKSPACE_VIEWER: "browser", CUSTOM: "kept" }, {
    dataDir: "/Application Support/Frame Workspace",
    cacheDir: "/Caches/Frame Workspace",
  });
  assert.equal(initialPage("http://127.0.0.1:47810/", true), "http://127.0.0.1:47810/#macview");
  assert.deepEqual(Object.fromEntries(Object.keys(WORKSPACE_DEFAULTS).map((key) => [key, env[key]])),
    WORKSPACE_DEFAULTS);
  assert.equal(env.FRAME_WORKSPACE_VIEWER, "native");
  assert.equal(env.FRAME_CONTROL_DATA_DIR, "/Application Support/Frame Workspace");
  assert.equal(env.FRAME_CONTROL_CACHE_DIR, "/Caches/Frame Workspace");
  assert.equal(env.FRAME_CONTROL_NO_UPDATE_CHECK, "1");
  assert.equal(env.CUSTOM, "kept");
});

test("saved Preview workspace data copies only into missing Workspace destinations", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "frame-workspace-profile-"));
  try {
    const source = path.join(root, "Frame Workspace Preview");
    const target = path.join(root, "Frame Workspace");
    fs.mkdirSync(path.join(source, "workspace"), { recursive: true });
    fs.mkdirSync(path.join(source, "workspace-profiles"), { recursive: true });
    fs.writeFileSync(path.join(source, "workspace", "last-start.json"),
      '{"preset":"2880x2560","scale":1,"quality":"sharp","mode":"spatial"}\n');
    fs.writeFileSync(path.join(source, "workspace-profiles", "profiles.json"), '{"schemaVersion":1,"profiles":[]}\n');
    fs.mkdirSync(path.join(target, "workspace"), { recursive: true });
    fs.writeFileSync(path.join(target, "workspace", "last-start.json"), "existing workspace choice\n");
    const outside = path.join(root, "outside.json");
    fs.writeFileSync(outside, "outside\n");
    fs.symlinkSync(outside, path.join(source, "workspace", "outside.json"));

    assert.deepEqual(migrateWorkspaceData(source, target).sort(), ["workspace-profiles/profiles.json"]);
    assert.equal(fs.readFileSync(path.join(target, "workspace", "last-start.json"), "utf8"), "existing workspace choice\n");
    assert.equal(fs.readFileSync(path.join(target, "workspace-profiles", "profiles.json"), "utf8"),
      '{"schemaVersion":1,"profiles":[]}\n');
    assert.equal(fs.readFileSync(path.join(source, "workspace", "last-start.json"), "utf8"),
      '{"preset":"2880x2560","scale":1,"quality":"sharp","mode":"spatial"}\n');
    assert.equal(fs.readFileSync(outside, "utf8"), "outside\n");
    assert.equal(fs.statSync(path.join(target, "workspace-profiles")).mode & 0o777, 0o700);
    assert.equal(fs.statSync(path.join(target, "workspace-profiles", "profiles.json")).mode & 0o777, 0o600);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test("electron-builder resolves both profiles with the complete base resources and profile identity", async () => {
  const appDir = path.resolve(__dirname, "..");
  const packageJson = require("../package.json");
  const checker = require("../build/check-resources.js");
  const base = await getConfig(appDir, null, null);
  const workspace = await getConfig(appDir, "build/workspace.js", null);
  const frameControl = await getConfig(appDir, "build/frame-control.js", null);

  for (const resolved of [workspace, frameControl]) {
    assert.equal(resolved.afterPack, packageJson.build.afterPack);
    assert.deepEqual(resolved.files, base.files);
    assert.deepEqual(resolved.extraResources, base.extraResources);
    assert.deepEqual(resolved.mac.extraResources, base.mac.extraResources);
    assert.deepEqual(resolved.mac.target, base.mac.target);
    assert.deepEqual(resolved.linux, base.linux);
    assert.deepEqual(resolved.win, base.win);
    assert.ok(resolved.files.some((entry) => entry.filter?.includes("runtime-profile.js")));
    for (const [from, to] of [["../ui", "ui"], ["../scripts", "scripts"],
                              ["build/deps/${os}-${arch}/python", "python"],
                              ["build/deps/${os}-${arch}/tools", "tools"]])
      assert.ok(resolved.extraResources.some((entry) => entry.from === from && entry.to === to), `${from} -> ${to}`);
    assert.ok(resolved.mac.extraResources.some((entry) => entry.from === "../mac/bin"
      && entry.to === "mac/bin" && entry.filter.includes("frame-mac-view")));
  }

  assert.equal(workspace.appId, "com.astromaddie.frame-workspace");
  assert.equal(workspace.productName, "Frame Workspace");
  assert.equal(workspace.artifactName, "Frame-Workspace-mac-${arch}.${ext}");
  assert.equal(workspace.mac.artifactName, "Frame-Workspace-mac-${arch}.${ext}");
  assert.equal(workspace.dmg.title, "Frame Workspace ${version}");
  assert.equal(workspace.extraMetadata.frameWorkspaceProfile, true);
  assert.equal(workspace.extraMetadata.productName, "Frame Workspace");
  assert.equal(workspace.protocols, undefined);
  assert.equal(frameControl.appId, "com.saphid.frame-control");
  assert.equal(frameControl.productName, "Frame Control");
  assert.deepEqual(frameControl.dmg, base.dmg);
  assert.equal(frameControl.protocols[0].name, "Frame Control install link");
  assert.deepEqual(frameControl.protocols[0].schemes, ["frame-control"]);
  assert.equal(packageJson.build.appId, "com.saphid.frame-control");
  assert.equal(packageJson.build.productName, "Frame Control");
  assert.equal(packageJson.build.protocols, undefined);
  assert.ok(packageJson.scripts.dist.includes("build/frame-control.js"));
  assert.ok(packageJson.scripts["dist:workspace"].includes("build/workspace.js"));
  for (const file of ["scripts/workspace-dash-on-frame.sh", "scripts/workspace-launch-on-frame.py",
                      "scripts/workspace-native-on-frame.sh", "scripts/workspace-spatial-on-frame.py",
                      "scripts/workspace-viewer-on-frame.sh",
                      "mac/bin/frame-mac-view"])
    assert.ok(checker.WORKSPACE_REQUIRED.includes(file));
});
