// FW-15 spike: does a drag survive jumping straight from a window on one
// display to a window on another (no path across the edge between them), as
// Spatial Mode would do when the laser moves between panels? Two text windows
// of its own; drags a selected word from A to B with posted mouse events.
// Needs Accessibility for the process that runs it; moves the real cursor.
//   swiftc -O mac/frame-mac-view/Diagnostics/SpatialDragProbe.swift -o /tmp/drag && /tmp/drag
import AppKit
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let token = "FW15DRAGTOKEN"
func textWindow(on screen: NSScreen, title: String, text: String) -> (NSWindow, NSTextView) {
  let f = screen.visibleFrame
  let w = NSWindow(contentRect: NSRect(x: f.minX + 80, y: f.minY + 80, width: 520, height: 260),
                   styleMask: [.titled, .closable], backing: .buffered, defer: false)
  w.title = title
  let tv = NSTextView(frame: w.contentView!.bounds)
  tv.font = NSFont.systemFont(ofSize: 28); tv.string = text; tv.isEditable = true
  w.contentView = tv
  w.setFrameOrigin(NSPoint(x: f.minX + 80, y: f.maxY - 340))
  w.makeKeyAndOrderFront(nil)
  w.setFrameOrigin(NSPoint(x: f.minX + 80, y: f.maxY - 340))
  return (w, tv)
}
let main = NSScreen.screens[0]
let other = NSScreen.screens.first { $0.localizedName.contains("Frame Workspace") } ?? main
let (wa, a) = textWindow(on: other, title: "Drag Source", text: "drag \(token) across")
let (wb, b) = textWindow(on: main, title: "Drop Target", text: "drop here: ")
func cg(_ p: NSPoint) -> CGPoint { CGPoint(x: p.x, y: main.frame.maxY - p.y) }  // AppKit -> global CG
func post(_ type: CGEventType, _ p: CGPoint) {
  CGEvent(mouseEventSource: nil, mouseType: type, mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap)
}
DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) {
  app.activate(ignoringOtherApps: true)
  wa.makeKeyAndOrderFront(nil)
  a.layoutManager!.ensureLayout(for: a.textContainer!)
  let range = (a.string as NSString).range(of: token)
  a.setSelectedRange(range)
  let glyphs = a.layoutManager!.glyphRange(forCharacterRange: range, actualCharacterRange: nil)
  var rect = a.layoutManager!.boundingRect(forGlyphRange: glyphs, in: a.textContainer!)
  rect.origin.x += a.textContainerOrigin.x; rect.origin.y += a.textContainerOrigin.y
  let inWindow = a.convert(NSPoint(x: rect.midX, y: rect.midY), to: nil)
  let start = cg(wa.convertPoint(toScreen: inWindow))
  let endView = b.convert(NSPoint(x: b.bounds.maxX - 60, y: 40), to: nil)
  let end = cg(wb.convertPoint(toScreen: endView))
  print("A frame", wa.frame, "B frame", wb.frame, "screens", NSScreen.screens.map { ($0.localizedName, $0.frame) })
  print("from", start, "on", other.localizedName, "to", end, "on", main.localizedName); fflush(stdout)
  post(.mouseMoved, start)
  DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
    post(.leftMouseDown, start)
    var steps: [(Double, CGPoint)] = []
    for i in 1...6 { steps.append((0.4 + Double(i) * 0.03, CGPoint(x: start.x + CGFloat(i * 6), y: start.y))) }  // start the drag
    steps.append((0.75, end))                                      // jump straight to the other display
    for i in 1...4 { steps.append((0.75 + Double(i) * 0.05, CGPoint(x: end.x - CGFloat(i * 4), y: end.y))) }
    for (t, p) in steps { DispatchQueue.main.asyncAfter(deadline: .now() + t) { post(.leftMouseDragged, p) } }
    DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) { post(.leftMouseUp, CGPoint(x: end.x - 16, y: end.y)) }
    DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) {
      let ok = b.string.contains(token)
      print("drop target now:", b.string.debugDescription, "->", ok ? "PASS: the drag crossed displays" : "FAIL")
      print("source now:", a.string.debugDescription); fflush(stdout)
      exit(ok ? 0 : 1)
    }
  }
}
app.run()
