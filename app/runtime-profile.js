"use strict";

const fs = require("fs");
const path = require("path");

const WORKSPACE_DEFAULTS = Object.freeze({
  FRAME_WORKSPACE_VIEWER: "native",
  FRAME_WINDOW_VIEWER: "native",
  FRAME_WORKSPACE_DECODER: "v4l2",
  FRAME_WORKSPACE_CONVERSION: "cpu",
  FRAME_WORKSPACE_FPS: "30",
  FRAME_WORKSPACE_MOUSE: "1",
  FRAME_MAC_VIEW_ADAPT: "0",
  FRAME_PRIVATE_SSH: "1",
  FRAME_CONTROL_TELEMETRY: "0",
  DO_NOT_TRACK: "1",
});

function isWorkspaceProfile(packageJson, env = process.env) {
  return packageJson.frameWorkspaceProfile === true || env.FRAME_WORKSPACE_PROFILE === "1";
}

function workspaceServerEnvironment(env, { dataDir, cacheDir }) {
  return {
    ...env,
    ...WORKSPACE_DEFAULTS,
    FRAME_CONTROL_DATA_DIR: dataDir,
    FRAME_CONTROL_CACHE_DIR: cacheDir,
    FRAME_CONTROL_NO_UPDATE_CHECK: "1",
  };
}

function initialPage(origin, workspace) {
  return new URL(workspace ? "#macview" : ".", origin).href;
}

// Copy only Workspace Preview's workspace-owned state. Keep the source intact
// and never replace an existing Frame Workspace setting or saved profile.
function migrateWorkspaceData(sourceRoot, targetRoot, io = fs) {
  const copied = [];
  for (const name of ["workspace", "workspace-profiles"]) {
    const sourceDir = path.join(sourceRoot, name);
    let entries;
    try { entries = io.readdirSync(sourceDir, { withFileTypes: true }); }
    catch (error) { if (error.code === "ENOENT") continue; throw error; }

    const targetDir = path.join(targetRoot, name);
    io.mkdirSync(targetDir, { recursive: true, mode: 0o700 });
    try { io.chmodSync(targetDir, 0o700); } catch {}
    for (const entry of entries) {
      // These directories contain small JSON settings. Do not follow symlinks
      // or recursively pull arbitrary files from the previous app profile.
      if (!entry.isFile()) continue;
      const source = path.join(sourceDir, entry.name);
      const target = path.join(targetDir, entry.name);
      try {
        io.lstatSync(target);
        continue;
      } catch (error) { if (error.code !== "ENOENT") throw error; }

      const temporary = path.join(targetDir, `.${entry.name}.migrate-${process.pid}-${Date.now()}`);
      try {
        io.copyFileSync(source, temporary, io.constants.COPYFILE_EXCL);
        io.chmodSync(temporary, 0o600);
        // A hard link is an atomic, no-overwrite install on the same volume.
        try { io.linkSync(temporary, target); copied.push(path.join(name, entry.name)); }
        catch (error) { if (error.code !== "EEXIST") throw error; }
      } finally {
        try { io.unlinkSync(temporary); } catch (error) { if (error.code !== "ENOENT") throw error; }
      }
    }
  }
  return copied;
}

module.exports = { WORKSPACE_DEFAULTS, initialPage, isWorkspaceProfile, migrateWorkspaceData, workspaceServerEnvironment };
