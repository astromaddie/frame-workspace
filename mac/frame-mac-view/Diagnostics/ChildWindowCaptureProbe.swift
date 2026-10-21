// FW-15 option B spike: captures one window with ScreenCaptureKit, child windows on and off, saving
// PNGs (popover, sheet and context menu from ChildWindowFixture.swift).
//   swiftc -O ChildWindowFixture.swift -o /tmp/f && swiftc -O ChildWindowCaptureProbe.swift -o /tmp/p
//   /tmp/f > /tmp/f.out & sleep 0.5; /tmp/p $(head -1 /tmp/f.out) /tmp/shot
import AppKit
import ScreenCaptureKit
let nsApp = NSApplication.shared
let wid = CGWindowID(CommandLine.arguments[1])!, outPrefix = CommandLine.arguments[2]
let times: [Double] = CommandLine.arguments.count > 3 ? [1.5] : [1.0, 2.5, 5.0, 7.5]
func save(_ img: CGImage, _ path: String) {
  let rep = NSBitmapImageRep(cgImage: img); try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
}
Task {
  let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
  guard let win = content.windows.first(where: { $0.windowID == wid }) else { print("window not found"); exit(1) }
  let start = Date()
  for (i, t) in times.enumerated() {
    let wait = t - Date().timeIntervalSince(start); if wait > 0 { try await Task.sleep(nanoseconds: UInt64(wait * 1e9)) }
    for child in [false, true] {
      let filter = SCContentFilter(desktopIndependentWindow: win)
      let cfg = SCStreamConfiguration()
      cfg.width = 1600; cfg.height = 1100; cfg.showsCursor = false
      if #available(macOS 14.2, *) { cfg.includeChildWindows = child }
      do {
        let img = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: cfg)
        save(img, "\(outPrefix)-\(i)-\(child ? "children" : "alone").png")
      } catch { print("capture error", error) }
    }
    print("captured t=\(t)"); fflush(stdout)
  }
  exit(0)
}
RunLoop.main.run()
