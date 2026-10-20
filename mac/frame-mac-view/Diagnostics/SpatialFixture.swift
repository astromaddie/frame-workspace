// FW-15 spike fixture: N windows of its own that keep changing (scrolling
// lines of text and a moving bar), as a worst case for many window streams,
// or stay still. They open on the Frame Workspace display if there is one.
//   swiftc -O mac/frame-mac-view/Diagnostics/SpatialFixture.swift -o /tmp/spatial-fixture
//   /tmp/spatial-fixture N [static] [small]   (1280x800, or 640x400 with small)
import AppKit
final class Busy: NSView {
  var tick = 0, moving = true
  override var isFlipped: Bool { true }
  override func draw(_ r: NSRect) {
    NSColor(calibratedRed: 0.11, green: 0.12, blue: 0.15, alpha: 1).setFill(); bounds.fill()
    let font = NSFont(name: "Menlo", size: 14) ?? NSFont.systemFont(ofSize: 14)
    let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: NSColor.white]
    for i in 0..<48 {
      let n = tick + i
      ("\(n)  let value\(n % 97) = compute(\(n * 7 % 1000), \"spatial\")  // line \(n)" as NSString)
        .draw(at: NSPoint(x: 16, y: CGFloat(i) * 18 + 8), withAttributes: attrs)
    }
    NSColor.systemBlue.setFill()
    NSRect(x: CGFloat(tick * 6 % Int(max(bounds.width - 80, 1))), y: bounds.height - 24, width: 80, height: 12).fill()
  }
}
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let n = Int(CommandLine.arguments.dropFirst().first ?? "4") ?? 4
let moving = !CommandLine.arguments.contains("static")
let side = CommandLine.arguments.contains("small") ? (w: 640.0, h: 400.0) : (w: 1280.0, h: 800.0)
// Open on the Frame Workspace display if there is one, not the laptop screen.
let host = NSScreen.screens.first { $0.localizedName.contains("Frame Workspace") } ?? NSScreen.main!
var windows: [NSWindow] = []
var views: [Busy] = []
for i in 0..<n {
  let w = NSWindow(contentRect: NSRect(x: host.frame.minX + CGFloat(40 + i * 30), y: host.frame.minY + CGFloat(40 + i * 30),
                                       width: side.w, height: side.h),
                   styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
  w.title = "Spatial Spike \(i + 1)"
  let v = Busy(frame: w.contentView!.bounds); v.autoresizingMask = [.width, .height]
  w.contentView = v; w.orderFront(nil)
  windows.append(w); views.append(v)
}
if moving {
  Timer.scheduledTimer(withTimeInterval: 1.0 / 60, repeats: true) { _ in
    for v in views { v.tick += 1; v.needsDisplay = true }
  }
}
print("fixture pid \(ProcessInfo.processInfo.processIdentifier) windows \(n)"); fflush(stdout)
app.run()
