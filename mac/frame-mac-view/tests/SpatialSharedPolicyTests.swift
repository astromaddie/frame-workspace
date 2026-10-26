import Foundation

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else { fputs("SpatialSharedPolicyTests: \(message)\n", stderr); exit(1) }
}

@main
private enum SpatialSharedPolicyTests {
    static func main() {
        let display = CGRect(x: 1672, y: 1169, width: 3600, height: 2250)
        let bar = CGRect(x: 1672, y: 1169, width: 3600, height: 36)
        expect(SpatialSharedPolicy.sharedFrame(size: CGSize(width: 640, height: 422), display: display, menuBar: bar) ==
            CGRect(x: 1672, y: 1205, width: 640, height: 422), "windows overlap immediately below the menu bar")
        expect(SpatialSharedPolicy.sharedFrame(size: CGSize(width: 4000, height: 3000), display: display, menuBar: bar) ==
            CGRect(x: 1672, y: 1205, width: 3600, height: 2214), "oversized windows clamp to usable bounds")
        let physicalBar = CGRect(x: 0, y: 0, width: 1800, height: 36)
        expect(SpatialSharedPolicy.sharedFrame(size: CGSize(width: 640, height: 422), display: display, menuBar: physicalBar).minY == display.minY,
            "a built-in strip does not reserve space on another display")
        expect(SpatialSharedPolicy.menuBarDisplay(shared: 99, builtIn: 1, separateSpaces: false) == 1,
            "with separate Spaces off the built-in supplies menus")
        expect(SpatialSharedPolicy.menuBarDisplay(shared: 99, builtIn: 1, separateSpaces: true) == 99,
            "with separate Spaces on the shared display supplies menus")
        expect(SpatialSharedPolicy.menuBarSplit(statusItemFrames: []) == nil, "an absent status item has no invented split")
        expect(SpatialSharedPolicy.menuBarSplit(statusItemFrames: [CGRect(x: 3000, y: 1169, width: 40, height: 36),
            CGRect(x: 2800, y: 1169, width: 40, height: 36)]) == 2800, "the leftmost status item defines the split")
        expect(SpatialSharedPolicy.isMenuBarSurface(frame: CGRect(x: 1800, y: 1200, width: 150, height: 34), layer: 101,
            owner: "Fixture", menuBarRect: bar), "menu tops tolerate the observed five-point overlap")
        expect(!SpatialSharedPolicy.isMenuBarSurface(frame: CGRect(x: 1800, y: 1500, width: 150, height: 34), layer: 101,
            owner: "Fixture", menuBarRect: bar), "context menus are already children, not menu-bar surfaces")
        expect(SpatialSharedPolicy.isMenuBarSurface(frame: CGRect(x: 4058, y: 1169, width: 1958, height: 1556), layer: 22,
            owner: "Control Center", menuBarRect: bar), "Control Center's padded surface is recognized")
        expect(!SpatialSharedPolicy.isMenuBarSurface(frame: bar, layer: 22, owner: "Other App", menuBarRect: bar),
            "ordinary layer-22 panels are excluded")
        var pixels = [UInt8](repeating: 0, count: 4 * 6 * 4)
        for y in 1...2 { for x in 2...4 { pixels[(y * 6 + x) * 4 + 3] = 255 } }
        expect(SpatialSharedPolicy.alphaBounds(bgra: pixels, width: 6, height: 4) == CGRect(x: 2, y: 1, width: 3, height: 2),
            "transparent Control Center padding is cropped away")
        expect(SpatialSharedPolicy.alphaBounds(bgra: [], width: 6, height: 4) == nil, "truncated buffers are rejected")
        expect(SpatialSharedPolicy.alphaBounds(bgra: [UInt8](repeating: 0, count: 16), width: 2, height: 2) == nil,
            "fully transparent surfaces have no opaque bounds")
        let p = SpatialSharedPolicy.panelToGlobal(u: 0.25, v: 0.5, captureRect: CGRect(x: 100, y: 200, width: 800, height: 600))
        expect(p == CGPoint(x: 300, y: 500), "panel input maps through the current global capture rectangle")
        expect(SpatialSharedPolicy.displaySize(nil) == CGSize(width: 3600, height: 2250), "the verified shared mode is the default")
        expect(SpatialSharedPolicy.displaySize("1920x1200") == CGSize(width: 1920, height: 1200), "experimental sizes parse")
        expect(SpatialSharedPolicy.displaySize("1x2") == CGSize(width: 3600, height: 2250), "invalid sizes fall back safely")
        let items = [SpatialSharedPolicy.MenuItem(frame: CGRect(x: 100, y: 0, width: 40, height: 36), selected: false),
                     SpatialSharedPolicy.MenuItem(frame: CGRect(x: 140, y: 0, width: 45, height: 36), selected: true)]
        let dropdown = CGRect(x: 100, y: 34, width: 200, height: 300)
        expect(SpatialSharedPolicy.selectedMenuItem(items: items, dropdowns: [dropdown], menuBar: physicalBar) == items[1].frame,
            "AXSelected takes precedence over dropdown alignment")
        let unselected = items.map { SpatialSharedPolicy.MenuItem(frame: $0.frame, selected: false) }
        expect(SpatialSharedPolicy.selectedMenuItem(items: unselected, dropdowns: [dropdown], menuBar: physicalBar) == items[0].frame,
            "dropdown origin selects the item when AXSelected is absent")
        expect(SpatialSharedPolicy.selectedMenuItem(items: items, dropdowns: [], menuBar: physicalBar) == nil,
            "closed dropdown clears stale AX selection")
        expect(SpatialSharedPolicy.selectedMenuItem(items: items, dropdowns: [CGRect(x: 900, y: 100, width: 80, height: 80)], menuBar: physicalBar) == nil,
            "context and status panels do not press app menu titles")
        expect(SpatialSharedPolicy.selectedMenuItem(items: unselected, dropdowns: [CGRect(x: 800, y: 36, width: 80, height: 80)], menuBar: physicalBar) == nil,
            "unmatched dropdown has no invented title")
        print("SpatialSharedPolicy: 24 geometry, menu, alpha, mapping and separate-Spaces checks passed")
        // One cursor: only the active window's capture draws it; strips and menus while it is on them.
        let windows = ["spatial:1", "spatial:2", "spatial:3"]
        func drawing(focused: String?, onSurface: Bool = false) -> [String] {
            windows.filter { SpatialCursorPolicy.window($0, focused: focused, cursorOnSurface: onSurface) }
        }
        expect(drawing(focused: "spatial:1") == ["spatial:1"], "only the active window draws the cursor")
        expect(drawing(focused: "spatial:2") == ["spatial:2"], "it moves with focus")
        expect(drawing(focused: nil).isEmpty, "nothing active, no window cursor")
        expect(drawing(focused: "spatial:2", onSurface: true).isEmpty, "on a strip or menu, the window does not")
        let strip = CGRect(x: 0, y: 0, width: 3600, height: 36), menu = CGRect(x: 100, y: 36, width: 200, height: 300)
        expect(SpatialCursorPolicy.onSurface(cursor: CGPoint(x: 150, y: 100), menuBar: strip, surfaces: [menu]), "in a menu")
        expect(SpatialCursorPolicy.onSurface(cursor: CGPoint(x: 900, y: 10), menuBar: strip, surfaces: []), "on the bar")
        expect(!SpatialCursorPolicy.onSurface(cursor: CGPoint(x: 900, y: 400), menuBar: strip, surfaces: [menu]), "in a window")
        expect(SpatialCursorPolicy.surface(rect: menu, visible: true, cursor: CGPoint(x: 150, y: 100)), "the menu draws it")
        expect(!SpatialCursorPolicy.surface(rect: menu, visible: false, cursor: CGPoint(x: 150, y: 100)), "a hidden one does not")
        expect(!SpatialCursorPolicy.surface(rect: menu, visible: true, cursor: CGPoint(x: 900, y: 400)), "nor off it")
        print("SpatialSharedPolicy: 10 cursor visibility checks passed")
    }
}
