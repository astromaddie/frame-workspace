// A window of its own that opens, in turn: a popover (1.5 s), a sheet (4 s), a context menu (6.5 s).
import AppKit
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let w = NSWindow(contentRect: NSRect(x: 200, y: 200, width: 640, height: 420), styleMask: [.titled, .closable], backing: .buffered, defer: false)
w.title = "Child Window Probe"
let v = NSView(frame: w.contentView!.bounds); v.wantsLayer = true; v.layer?.backgroundColor = NSColor.systemTeal.cgColor
w.contentView = v; w.makeKeyAndOrderFront(nil)
print(w.windowNumber); fflush(stdout)
app.activate(ignoringOtherApps: true)
let pop = NSPopover(); let vc = NSViewController(); let pv = NSView(frame: NSRect(x: 0, y: 0, width: 260, height: 140))
pv.wantsLayer = true; pv.layer?.backgroundColor = NSColor.systemPink.cgColor; vc.view = pv; pop.contentViewController = vc; pop.behavior = .applicationDefined
DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { pop.show(relativeTo: NSRect(x: 600, y: 380, width: 10, height: 10), of: v, preferredEdge: .maxX) }
DispatchQueue.main.asyncAfter(deadline: .now() + 3.5) { pop.close() }
DispatchQueue.main.asyncAfter(deadline: .now() + 4.0) {
  let sheet = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 360, height: 200), styleMask: [.titled], backing: .buffered, defer: false)
  let sv = NSView(frame: sheet.contentView!.bounds); sv.wantsLayer = true; sv.layer?.backgroundColor = NSColor.systemOrange.cgColor; sheet.contentView = sv
  w.beginSheet(sheet) { _ in }
  DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { w.endSheet(sheet) }
}
DispatchQueue.main.asyncAfter(deadline: .now() + 6.5) {
  let m = NSMenu(); for t in ["Copy", "Paste", "Inspect Element", "Look Up"] { m.addItem(withTitle: t, action: nil, keyEquivalent: "") }
  DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { m.cancelTracking() }
  m.popUp(positioning: nil, at: NSPoint(x: 560, y: 120), in: v)
}
DispatchQueue.main.asyncAfter(deadline: .now() + 10) { exit(0) }
app.run()
