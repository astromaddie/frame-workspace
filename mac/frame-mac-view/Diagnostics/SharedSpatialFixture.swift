// Owned fixture for SharedSpatialFixtureRun.sh; supports 30/60 Hz redraw.
// No user window or system setting is changed.
import AppKit
import ApplicationServices

let app = NSApplication.shared
app.setActivationPolicy(.regular)
func inventory() -> [String: Any] {
    var ids = [CGDirectDisplayID](repeating: 0, count: 32), count: UInt32 = 0
    CGGetOnlineDisplayList(32, &ids, &count)
    let displays = ids.prefix(Int(count)).sorted().map { id -> [String: Any] in
        let b = CGDisplayBounds(id), mode = CGDisplayCopyDisplayMode(id)
        return ["id": id, "bounds": [b.minX, b.minY, b.width, b.height],
                "mode": [mode?.width ?? 0, mode?.height ?? 0, mode?.pixelWidth ?? 0, mode?.pixelHeight ?? 0],
                "hz": mode?.refreshRate ?? 0, "mirror": CGDisplayMirrorsDisplay(id), "main": CGDisplayIsMain(id)]
    }
    let windows = (CGWindowListCopyWindowInfo([.excludeDesktopElements], kCGNullWindowID) as? [[CFString: Any]] ?? []).compactMap { value -> [String: Any]? in
        guard (value[kCGWindowOwnerPID] as? Int32) != ProcessInfo.processInfo.processIdentifier,
              (value[kCGWindowLayer] as? Int) == 0, let id = value[kCGWindowNumber], let pid = value[kCGWindowOwnerPID],
              let frame = value[kCGWindowBounds] else { return nil }
        return ["id": id, "pid": pid, "app": value[kCGWindowOwnerName] as? String ?? "", "frame": frame]
    }
    let point = CGEvent(source: nil)?.location ?? .zero
    return ["displays": displays, "windows": windows, "cursor": [point.x, point.y]]
}
if CommandLine.arguments.contains("--inventory") {
    let data = try JSONSerialization.data(withJSONObject: inventory(), options: [.sortedKeys])
    print(String(data: data, encoding: .utf8)!); exit(0)
}
let statePath = CommandLine.arguments[1]
let redrawHz = CommandLine.arguments.contains("--60hz") ? 60.0 : 30.0
let started = ProcessInfo.processInfo.systemUptime
var windows: [NSWindow] = []
var events: [[String: Any]] = []
var sequence = 0
func rect(_ r: CGRect) -> [Double] { [r.minX, r.minY, r.width, r.height] }
func saveState() {
    let value: [String: Any] = ["redrawHz": redrawHz, "cursor": inventory()["cursor"] ?? [], "pid": ProcessInfo.processInfo.processIdentifier, "sequence": sequence,
        "windows": windows.enumerated().map { i, w -> [String: Any] in
            ["index": i, "id": w.windowNumber, "title": w.title, "frameAppKit": rect(w.frame),
             "visible": w.isVisible, "key": w.isKeyWindow, "scale": w.backingScaleFactor,
             "screenVisibleFrame": w.screen.map { rect($0.visibleFrame) } ?? [],
             "maximumSize": [w.maxSize.width,w.maxSize.height]]
        }, "events": events]
    if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) {
        try? data.write(to: URL(fileURLWithPath: statePath), options: .atomic)
    }
}
func record(_ kind: String, _ index: Int, _ event: NSEvent? = nil) {
    var value: [String: Any] = ["kind": kind, "index": index,
        "uptime": ProcessInfo.processInfo.systemUptime, "elapsed": ProcessInfo.processInfo.systemUptime - started]
    if let event { value["location"] = [event.locationInWindow.x, event.locationInWindow.y]
        if event.type == .keyDown { value["characters"] = event.characters ?? "" } }
    events.append(value); saveState()
}
final class FixtureView: NSView {
    let index: Int
    var tick = 0
    init(index: Int) { self.index = index; super.init(frame: .zero) }
    required init?(coder: NSCoder) { fatalError("not a nib") }
    override var acceptsFirstResponder: Bool { true }
    override func draw(_ dirtyRect: NSRect) {
        // Distinct sRGB solid colors identify every covered window at four distant sample points.
        NSColor(srgbRed: CGFloat(40 + index * 37 % 170) / 255,
                green: CGFloat(50 + index * 61 % 160) / 255,
                blue: CGFloat(60 + index * 83 % 150) / 255, alpha: 1).setFill()
        bounds.fill()
        (tick % 2 == 0 ? NSColor.white : NSColor.black).setFill()
        NSRect(x: bounds.midX - 20, y: bounds.midY - 20, width: 40, height: 40).fill()
    }
    override func mouseDown(with event: NSEvent) { window?.makeFirstResponder(self); record("down", index, event) }
    override func mouseDragged(with event: NSEvent) { record("drag", index, event) }
    override func mouseUp(with event: NSEvent) { record("up", index, event) }
    override func keyDown(with event: NSEvent) { record("key", index, event) }
}
final class MenuTarget: NSObject {
    @objc func menuAction(_ sender: Any?) { record("menuAction", -1) }
    @objc func statusAction(_ sender: Any?) { record("statusAction", -1) }
}
let target = MenuTarget()
let menu = NSMenu()
let appItem = NSMenuItem(title: "Fixture", action: nil, keyEquivalent: "")
let appMenu = NSMenu(title: "Fixture"); appMenu.addItem(withTitle: "Owned fixture", action: nil, keyEquivalent: "")
appItem.submenu = appMenu; menu.addItem(appItem)
let probeItem = NSMenuItem(title: "Probe", action: nil, keyEquivalent: "")
let probeMenu = NSMenu(title: "Probe")
let actionItem = NSMenuItem(title: "Record fixture action", action: #selector(MenuTarget.menuAction(_:)), keyEquivalent: "")
actionItem.target = target; probeMenu.addItem(actionItem); probeItem.submenu = probeMenu; menu.addItem(probeItem)
app.mainMenu = menu
let status = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
status.button?.title = "FW15"
let statusMenu = NSMenu(title: "FW15")
let statusAction = NSMenuItem(title: "Owned status menu", action: #selector(MenuTarget.statusAction(_:)), keyEquivalent: "")
statusAction.target = target; statusMenu.addItem(statusAction); status.menu = statusMenu
for i in 0..<20 {
    let w = NSWindow(contentRect: NSRect(x: 100, y: 200, width: 640, height: 400),
        styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
    w.title = "Shared Display Fixture \(i)"; w.isReleasedWhenClosed = false
    let v = FixtureView(index: i); w.contentView = v; w.makeFirstResponder(v)
    // Keep all windows out of the desktop until the controller explicitly orders them.
    windows.append(w)
}
var input = Data()
let frameObservers = [NSWindow.didMoveNotification, NSWindow.didResizeNotification].map { name in
    NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { _ in saveState() }
}
FileHandle.standardInput.readabilityHandler = { handle in
    let data = handle.availableData
    DispatchQueue.main.async {
        if data.isEmpty { NSStatusBar.system.removeStatusItem(status); app.terminate(nil); return }
        input.append(data)
        while let end = input.firstIndex(of: 10) {
            let line = input.prefix(upTo: end); input.removeSubrange(...end)
            guard let cmd = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any],
                  let command = cmd["command"] as? String else { continue }
            if command == "count" {
                let n = cmd["value"] as? Int ?? 0
                for (i, w) in windows.enumerated() { if i < n { w.orderFront(nil) } else { w.orderOut(nil) } }
            } else if command == "cursor", let coordinates = cmd["point"] as? [Double], coordinates.count == 2 {
                let point = CGPoint(x: coordinates[0], y: coordinates[1])
                CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) { saveState() }
            } else if command == "menu-info" {
                func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
                    var value: CFTypeRef?
                    AXUIElementCopyAttributeValue(element, name as CFString, &value)
                    return value
                }
                let axApp = AXUIElementCreateApplication(ProcessInfo.processInfo.processIdentifier)
                for name in [kAXMenuBarAttribute as String, "AXExtrasMenuBar"] {
                    guard let value = attribute(axApp, name) else { continue }
                    let bar = value as! AXUIElement
                    for item in attribute(bar, kAXChildrenAttribute as String) as? [AXUIElement] ?? [] {
                        guard let value = attribute(item, kAXPositionAttribute as String) else { continue }
                        var point = CGPoint.zero
                        AXValueGetValue(value as! AXValue, .cgPoint, &point)
                        events.append(["kind": "menu-info", "bar": name,
                            "title": attribute(item, kAXTitleAttribute as String) as? String ?? "",
                            "point": [point.x, point.y]])
                    }
                }
            } else if command == "quit" {
                NSStatusBar.system.removeStatusItem(status); app.terminate(nil)
            }
            sequence += 1; saveState()
        }
    }
}
Timer.scheduledTimer(withTimeInterval: 1.0 / redrawHz, repeats: true) { _ in
    for w in windows where w.isVisible { let v = w.contentView as! FixtureView; v.tick += 1; v.needsDisplay = true }
}
saveState()
app.run()
