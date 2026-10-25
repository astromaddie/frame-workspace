"use strict";

// --config replaces package.json's build object, so begin with the complete
// base configuration and apply only Workspace identity/profile overrides.
const build = JSON.parse(JSON.stringify(require("../package.json").build));
const PRODUCT_NAME = "Frame Workspace";

module.exports = {
  ...build,
  appId: "com.astromaddie.frame-workspace",
  productName: PRODUCT_NAME,
  artifactName: "Frame-Workspace-mac-${arch}.${ext}",
  extraMetadata: {
    ...build.extraMetadata,
    frameWorkspaceProfile: true,
    productName: PRODUCT_NAME,
  },
  files: build.files.map(file => file === "build/icon.png" ? "build/workspace/icon.png" : file),
  mac: {
    ...build.mac,
    icon: "build/workspace/icon.icns",
    artifactName: "Frame-Workspace-mac-${arch}.${ext}",
    extendInfo: {
      ...build.mac.extendInfo,
      NSAppleEventsUsageDescription: `${PRODUCT_NAME} opens Terminal for SSH sessions and for power actions that need the Developer Mode password.`,
      NSLocalNetworkUsageDescription: `${PRODUCT_NAME} connects to your Steam Frame over SSH on the local network.`,
      NSScreenCaptureUsageDescription: `${PRODUCT_NAME} shows your Mac's windows and screens inside the Steam Frame when you ask it to.`,
    },
  },
  dmg: {
    ...build.dmg,
    title: "Frame Workspace ${version}",
  },
};
