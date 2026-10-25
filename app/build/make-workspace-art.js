// Render the original Open volume mark and its macOS/Steam exports.
// env -u ELECTRON_RUN_AS_NODE node_modules/.bin/electron build/make-workspace-art.js
const { app, BrowserWindow } = require("electron");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFileSync } = require("child_process");

const output = path.join(__dirname, "workspace");
const scratch = fs.mkdtempSync(path.join(os.tmpdir(), "frame-workspace-art-"));
app.setPath("userData", path.join(scratch, "profile"));
app.setPath("sessionData", path.join(scratch, "session"));
app.dock?.hide();
app.on("window-all-closed", () => {});

const svg = fs.readFileSync(path.join(output, "icon.svg"), "utf8");
const mark = svg.slice(svg.indexOf(">") + 1, svg.lastIndexOf("</svg>"));
const icon = (x, y, size) => `<svg x="${x}" y="${y}" width="${size}" height="${size}" viewBox="0 0 1024 1024">${mark}</svg>`;
const text = (x, y, size, label, anchor = "start", color = "#e8f1ef") =>
  `<text x="${x}" y="${y}" font-family="Arial,sans-serif" font-size="${size}" font-weight="600" text-anchor="${anchor}" fill="${color}">${label}</text>`;
const document = (width, height, content) =>
  `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">${content}</svg>`;
const background = (width, height) => `<defs><linearGradient id="art-bg" x2="0.8" y2="1"><stop stop-color="#25343e"/><stop offset="1" stop-color="#0c1622"/></linearGradient></defs><rect width="${width}" height="${height}" fill="url(#art-bg)"/>`;
const art = {
  capsule: [600, 900, background(600, 900) + icon(102, 82, 396)
    + text(300, 582, 54, "Frame", "middle") + text(300, 650, 54, "Workspace", "middle")
    + text(300, 727, 21, "SPATIAL COMPUTING", "middle", "#9dcac2")],
  header: [920, 430, background(920, 430) + icon(34, 39, 352)
    + text(423, 191, 49, "Frame") + text(423, 253, 49, "Workspace")
    + text(425, 308, 20, "SPATIAL COMPUTING", "start", "#9dcac2")],
  hero: [1920, 620, background(1920, 620) + icon(1250, 32, 556)
    + text(152, 283, 78, "Frame Workspace")
    + text(156, 356, 29, "SPATIAL COMPUTING", "start", "#9dcac2")],
  logo: [1100, 200, text(8, 143, 119, "Frame Workspace")],
};

async function render(source, width, height) {
  const win = new BrowserWindow({ width, height, useContentSize: true, show: false,
    transparent: true, frame: false, webPreferences: { offscreen: true } });
  try {
    await win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(
      `<meta charset="utf-8"><body style="margin:0;background:transparent">${source}</body>`));
    await new Promise(resolve => setTimeout(resolve, 180));
    return (await win.webContents.capturePage()).resize({ width, height });
  } finally { win.destroy(); }
}

app.whenReady().then(async () => {
  let exitCode = 0;
  try {
    const image = await render(svg, 1024, 1024);
    fs.writeFileSync(path.join(output, "icon.png"), image.toPNG());
    const steam = path.join(output, "steam");
    fs.mkdirSync(steam, { recursive: true });
    fs.writeFileSync(path.join(steam, "icon.png"), image.resize({ width: 128, height: 128 }).toPNG());
    for (const [name, [width, height, content]] of Object.entries(art)) {
      const source = document(width, height, content);
      fs.writeFileSync(path.join(steam, name + ".svg"), source);
      fs.writeFileSync(path.join(steam, name + ".png"), (await render(source, width, height)).toPNG());
    }
    const iconset = path.join(scratch, "workspace.iconset");
    fs.mkdirSync(iconset);
    for (const size of [16, 32, 128, 256, 512]) for (const scale of [1, 2]) {
      execFileSync("sips", ["-z", String(size * scale), String(size * scale), path.join(output, "icon.png"),
        "--out", path.join(iconset, `icon_${size}x${size}${scale === 2 ? "@2x" : ""}.png`)], { stdio: "ignore" });
    }
    execFileSync("iconutil", ["-c", "icns", iconset, "-o", path.join(output, "icon.icns")]);
    const files = ["icon.svg", "icon.png", "icon.icns", ...fs.readdirSync(steam).map(name => "steam/" + name)];
    const crypto = require("crypto");
    fs.writeFileSync(path.join(output, "manifest.json"), JSON.stringify({
      design: "Open volume: one centered floating window inside an open room",
      provenance: "Original SVG geometry created for Frame Workspace; no upstream icon reused",
      files: Object.fromEntries(files.map(name => [name,
        crypto.createHash("sha256").update(fs.readFileSync(path.join(output, name))).digest("hex")])),
    }, null, 2) + "\n");
    console.log("Rendered original Workspace icon, macOS icon set, and Steam artwork.");
  } catch (error) {
    console.error(error);
    exitCode = 1;
  } finally {
    // Only this renderer's temporary profile and iconset live here.
    app.exit(exitCode);
  }
});
app.on("quit", () => fs.rmSync(scratch, { recursive: true, force: true }));
