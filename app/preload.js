// Lets the page read this computer's clipboard through Electron, so sending it
// to the Frame needs no pbpaste, PowerShell, xclip or wl-clipboard. Also tells
// the page where a dropped file or folder lives, so a folder can be sideloaded
// as a title without zipping it (the local server reads it from there).
// It can put a screenshot on the clipboard as an image, open Set Up Connection when
// the headset can't be reached, and keeps the Frame menu's list of headsets up to date.
// It also receives frame-control://install links (docs/web-install.md): only
// what the link asked for, never an install; the page asks the user first.
// And it passes update state both ways: see app/updater.js. It opens the Sign in
// with Meta window and signs out of it (app/meta-signin.js).
const { contextBridge, ipcRenderer, webUtils } = require("electron");

contextBridge.exposeInMainWorld("frameApp", {
  notify: (message, request) => ipcRenderer.invoke("comfort:notify", message, request),
  readClipboard: () => ipcRenderer.invoke("clipboard:read"),
  writeImage: (bytes) => ipcRenderer.invoke("clipboard:writeImage", bytes),
  setUpConnection: () => ipcRenderer.invoke("connection:setup"),
  restartServer: () => ipcRenderer.invoke("server:restart"),  // the "couldn't start" page's Try Again
  // The Frame menu's headset switcher: the page tells it the headsets, and hears picks.
  devicesChanged: (list) => ipcRenderer.send("devices:changed", list),
  onUseDevice: (cb) => {
    ipcRenderer.removeAllListeners("use-device");
    ipcRenderer.on("use-device", (_e, id) => cb(String(id)));
  },
  // While the keyboard-and-trackpad panel holds the keyboard, ⌘W, ⌘R and the rest go to the Frame.
  captureKeys: (on) => ipcRenderer.send("keys:capture", !!on),
  pathForFile: (file) => { try { return webUtils.getPathForFile(file) || ""; } catch { return ""; } },
  // Updates (app/updater.js): the page shows a banner and an Update button.
  update: {
    get: () => ipcRenderer.invoke("update:get"),
    check: () => ipcRenderer.invoke("update:check"),
    install: () => ipcRenderer.invoke("update:install"),
    onState: (cb) => {
      ipcRenderer.removeAllListeners("update:state");
      ipcRenderer.on("update:state", (_e, s) => cb(s));
    },
  },
  // Help → Report a Problem… opens the page's report dialog (ui/frame_report.py).
  onReportProblem: (cb) => {
    ipcRenderer.removeAllListeners("report:open");
    ipcRenderer.on("report:open", () => cb());
  },
  // Sign in with Meta (app/meta-signin.js): Meta's login page in its own window. The
  // answer is the server's (connected, display name), never the token.
  meta: {
    signIn: (acknowledged) => ipcRenderer.invoke("meta:signin", acknowledged === true),
    signOut: () => ipcRenderer.invoke("meta:signout"),
  },
  onInstallLink: (cb) => {
    ipcRenderer.removeAllListeners("install-link");
    ipcRenderer.on("install-link", (_e, req) => cb({ kind: req.kind, target: req.target }));
    ipcRenderer.send("install-link:ready");
  },
});
