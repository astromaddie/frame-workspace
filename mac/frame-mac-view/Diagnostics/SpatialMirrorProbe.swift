// FW-15 spike: can the laptop screen mirror the Frame Workspace display, so
// macOS has one desktop (as with the lid closed)? Applied for this process
// only (.forAppOnly), so macOS undoes it when the process exits.
import AppKit
func displays() -> [CGDirectDisplayID] {
  var n: UInt32 = 0; CGGetOnlineDisplayList(0, nil, &n)
  var ids = [CGDirectDisplayID](repeating: 0, count: Int(n)); CGGetOnlineDisplayList(n, &ids, &n); return ids
}
func name(_ id: CGDirectDisplayID) -> String {
  NSScreen.screens.first { ($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value == id }?.localizedName ?? "(no NSScreen)"
}
func report(_ label: String) {
  print("--", label)
  for id in displays() {
    let b = CGDisplayBounds(id), m = CGDisplayCopyDisplayMode(id)
    print(String(format: "  %u %@ main=%d builtin=%d mirrors=%u inMirrorSet=%d bounds=%.0fx%.0f@%.0f,%.0f mode=%dx%d px=%dx%d active=%d",
                 id, name(id), CGDisplayIsMain(id), CGDisplayIsBuiltin(id), CGDisplayMirrorsDisplay(id), CGDisplayIsInMirrorSet(id),
                 b.width, b.height, b.minX, b.minY, m?.width ?? 0, m?.height ?? 0, m?.pixelWidth ?? 0, m?.pixelHeight ?? 0, CGDisplayIsActive(id)))
  }
  print("  NSScreens:", NSScreen.screens.map { $0.localizedName }, "menu bar on:", NSScreen.screens.first?.localizedName ?? "?")
}
let app = NSApplication.shared
app.setActivationPolicy(.prohibited)
DispatchQueue.main.async {
  report("before")
  guard let builtin = displays().first(where: { CGDisplayIsBuiltin($0) != 0 }),
        let workspace = displays().first(where: { name($0).contains("Frame Workspace") }) else { print("displays not found"); exit(2) }
  var cfg: CGDisplayConfigRef?
  CGBeginDisplayConfiguration(&cfg)
  let e1 = CGConfigureDisplayMirrorOfDisplay(cfg, builtin, workspace)
  let e2 = CGCompleteDisplayConfiguration(cfg, .forAppOnly)
  print("mirror request:", e1.rawValue, e2.rawValue)
  DispatchQueue.main.asyncAfter(deadline: .now() + 6) {
    report("laptop screen mirroring the Workspace")
    var cfg2: CGDisplayConfigRef?
    CGBeginDisplayConfiguration(&cfg2)
    let r1 = CGConfigureDisplayMirrorOfDisplay(cfg2, builtin, kCGNullDirectDisplay)
    let r2 = CGCompleteDisplayConfiguration(cfg2, .forAppOnly)
    print("undo request:", r1.rawValue, r2.rawValue)
    DispatchQueue.main.asyncAfter(deadline: .now() + 4) { report("after undo"); exit(0) }
  }
}
app.run()
