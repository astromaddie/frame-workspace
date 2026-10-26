// Turns the viewer's pointer and key events into real Mac input. Needs the
// Accessibility permission ("control your computer"); without it macOS drops
// the events silently, so the agent reports the permission to the viewer.
import AppKit
import ApplicationServices
import Foundation

@_silgen_name("_AXUIElementGetWindow")
private func _AXUIElementGetWindow(_ element: AXUIElement, _ id: UnsafeMutablePointer<CGWindowID>) -> AXError

enum Input {
    /// Tagged, so the Spatial fence can tell these events from the trackpad.
    static let source: CGEventSource? = {
        let source = CGEventSource(stateID: .hidSystemState)
        source?.userData = InjectedEvent.tag
        return source
    }()
    /// Buttons the physical trackpad is holding (from the fence tap). Remote
    /// cleanup never posts an up for one of them. Main thread.
    static var physicallyHeld = Set<Int>()
    /// What each viewer (session id) is holding down, so one panel closing
    /// lets go of its own keys and buttons and nobody else's. Main thread.
    private static var buttonsHeld: [Int: Set<Int>] = [:]
    private static var keysHeld: [Int: Set<CGKeyCode>] = [:]
    private static var sharedButtons = SharedPointerButtons()
    private static let pointerModeLock = NSLock()
    private static var sharedPointer = ProcessInfo.processInfo.environment["FRAME_WINDOW_DISPLAYS"] == "1"

    private static var lastDown: (time: TimeInterval, point: CGPoint, button: Int, count: Int)?

    static var allowed: Bool { AXIsProcessTrusted() }

    /// Every button a viewer holds, shared or per viewer. Main thread.
    static var heldButtons: Set<Int> {
        pointerShared ? sharedButtons.held : buttonsHeld.values.reduce(into: Set<Int>()) { $0.formUnion($1) }
    }

    static func holds(owner: Int) -> Bool {
        (buttonsHeld[owner]?.isEmpty == false) || (pointerShared && sharedButtons.holds(owner: owner))
    }

    static var pointerShared: Bool {
        pointerModeLock.lock(); defer { pointerModeLock.unlock() }
        return sharedPointer
    }

    /// Pointer mode changes and input are serialized by the main queue.
    static func setPointerShared(_ enabled: Bool) {
        pointerModeLock.lock()
        guard sharedPointer != enabled else { pointerModeLock.unlock(); return }
        if enabled {
            for (owner, buttons) in buttonsHeld {
                for button in buttons { _ = sharedButtons.down(button, owner: owner) }
            }
            buttonsHeld.removeAll()
        } else {
            let p = CGEvent(source: nil)?.location ?? .zero
            for button in sharedButtons.releaseAll() where !physicallyHeld.contains(button) {
                let type: CGEventType = button == 2 ? .rightMouseUp : button == 1 ? .otherMouseUp : .leftMouseUp
                CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: p,
                        mouseButton: button == 2 ? .right : button == 1 ? .center : .left)?.post(tap: .cghidEventTap)
            }
        }
        sharedPointer = enabled
        pointerModeLock.unlock()
    }

    /// Brings a window to the front so a click lands on it, not on whatever
    /// covers it, and typing goes to it.
    static func focus(window id: CGWindowID, pid: pid_t) {
        if frontWindow() == id { return }
        let app = AXUIElementCreateApplication(pid)
        var value: CFTypeRef?
        if AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &value) == .success,
           let windows = value as? [AXUIElement] {
            for w in windows {
                var wid: CGWindowID = 0
                if _AXUIElementGetWindow(w, &wid) == .success, wid == id {
                    AXUIElementPerformAction(w, kAXRaiseAction as CFString)
                    AXUIElementSetAttributeValue(w, kAXMainAttribute as CFString, kCFBooleanTrue)
                    break
                }
            }
        }
        AXUIElementSetAttributeValue(app, kAXFrontmostAttribute as CFString, kCFBooleanTrue)
        NSRunningApplication(processIdentifier: pid)?.activate()
    }

    static func frontWindow() -> CGWindowID? {
        WindowInfo.all().first { $0.layer == 0 }?.id
    }

    /// `button` uses the browser's numbering: 0 left, 1 middle, 2 right.
    static func mouse(_ kind: String, button: Int, at p: CGPoint, owner: Int) {
        let button = min(max(button, 0), 2)
        let b: CGMouseButton = button == 2 ? .right : button == 1 ? .center : .left
        let shared = pointerShared
        let type: CGEventType
        switch kind {
        case "down":
            type = b == .left ? .leftMouseDown : b == .right ? .rightMouseDown : .otherMouseDown
            if shared {
                guard sharedButtons.down(button, owner: owner) else { return }
            } else {
                buttonsHeld[owner, default: []].insert(button)
            }
        case "up":
            type = b == .left ? .leftMouseUp : b == .right ? .rightMouseUp : .otherMouseUp
            if shared {
                guard sharedButtons.up(button) else { return }
            } else {
                buttonsHeld[owner]?.remove(button)
                // Another viewer still holding it keeps it down.
                if buttonsHeld.values.contains(where: { $0.contains(button) }) { return }
            }
        default:  // a drag is whatever this viewer is holding
            let mine = shared ? sharedButtons.held : (buttonsHeld[owner] ?? [])
            switch SharedPointerButtons.dragButton(in: mine) {
            case .some(0): type = .leftMouseDragged
            case .some(2): type = .rightMouseDragged
            case .some(1): type = .otherMouseDragged
            default: type = .mouseMoved
            }
        }
        let mine = shared ? sharedButtons.held : (buttonsHeld[owner] ?? [])
        let held: CGMouseButton
        switch SharedPointerButtons.dragButton(in: mine) {
        case .some(2): held = .right
        case .some(1): held = .center
        default: held = .left
        }
        guard let e = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: p,
                              mouseButton: kind == "move" ? held : b) else { return }
        if kind == "down" || kind == "up" {
            let now = ProcessInfo.processInfo.systemUptime
            var count = 1
            if kind == "down" {
                if let l = lastDown, l.button == button, now - l.time < NSEvent.doubleClickInterval,
                   abs(l.point.x - p.x) < 5, abs(l.point.y - p.y) < 5 { count = l.count + 1 }
                lastDown = (now, p, button, count)
            } else if let l = lastDown, l.button == button {
                count = l.count
            }
            e.setIntegerValueField(.mouseEventClickState, value: Int64(count))
        }
        e.post(tap: .cghidEventTap)
    }

    static func scroll(dx: Double, dy: Double, at p: CGPoint, owner: Int) {
        mouse("move", button: 0, at: p, owner: owner)
        // Browsers report pixels with +y meaning "scroll down"; macOS's +y is up.
        guard let e = CGEvent(scrollWheelEvent2Source: source, units: .pixel, wheelCount: 2,
                              wheel1: Int32(-dy.rounded()), wheel2: Int32(-dx.rounded()), wheel3: 0) else { return }
        e.location = p
        e.post(tap: .cghidEventTap)
    }

    /// Lets go of every button and key this viewer is holding down, so a
    /// dropped connection can't leave, say, Shift or ⌘ stuck on.
    static func releaseAll(owner: Int) {
        let p = CGEvent(source: nil)?.location ?? .zero
        var buttons = buttonsHeld.removeValue(forKey: owner) ?? []
        if pointerShared {
            buttons.formUnion(sharedButtons.releaseAll(owner: owner))
        }
        let keys = keysHeld.removeValue(forKey: owner) ?? []
        // Only what no other viewer is still holding.
        for b in buttons where !buttonsHeld.values.contains(where: { $0.contains(b) }) &&
            !(pointerShared && sharedButtons.held.contains(b)) && !physicallyHeld.contains(b) {
            let type: CGEventType = b == 2 ? .rightMouseUp : b == 1 ? .otherMouseUp : .leftMouseUp
            CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: p,
                    mouseButton: b == 2 ? .right : b == 1 ? .center : .left)?.post(tap: .cghidEventTap)
        }
        for k in keys where !keysHeld.values.contains(where: { $0.contains(k) }) {
            CGEvent(keyboardEventSource: source, virtualKey: k, keyDown: false)?.post(tap: .cghidEventTap)
        }
    }

    static func moveCursor(to point: CGPoint) {
        let held = pointerShared ? sharedButtons.held : buttonsHeld.values.reduce(into: Set<Int>()) { $0.formUnion($1) }
        let dragButton = SharedPointerButtons.dragButton(in: held)
        let button: CGMouseButton
        let type: CGEventType
        switch dragButton {
        case .some(0): button = .left; type = .leftMouseDragged
        case .some(2): button = .right; type = .rightMouseDragged
        case .some(1): button = .center; type = .otherMouseDragged
        default: button = .left; type = .mouseMoved
        }
        CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: point,
                mouseButton: button)?.post(tap: .cghidEventTap)
    }

    static var cursorLocation: CGPoint { CGEvent(source: nil)?.location ?? .zero }

    static func key(code: String, key: String, down: Bool, mods: [String], owner: Int) {
        var flags = CGEventFlags()
        if mods.contains("shift") { flags.insert(.maskShift) }
        if mods.contains("ctrl") { flags.insert(.maskControl) }
        if mods.contains("alt") { flags.insert(.maskAlternate) }
        if mods.contains("meta") { flags.insert(.maskCommand) }
        if let vk = keyCodes[code] {
            guard let e = CGEvent(keyboardEventSource: source, virtualKey: vk, keyDown: down) else { return }
            if down {
                keysHeld[owner, default: []].insert(vk)
            } else {
                keysHeld[owner]?.remove(vk)
                if keysHeld.values.contains(where: { $0.contains(vk) }) { return }  // still held elsewhere
            }
            e.flags = flags
            e.post(tap: .cghidEventTap)
        } else if down, !key.isEmpty, key.count <= 4, key.unicodeScalars.allSatisfy({ $0.value >= 0x20 }) {
            // A key the table doesn't know (a non-US layout, the headset's
            // on-screen keyboard): type its character instead.
            text(key)
        }
    }

    static func text(_ s: String) {
        let units = Array(s.utf16)
        for chunk in stride(from: 0, to: units.count, by: 16) {
            let part = Array(units[chunk..<min(chunk + 16, units.count)])
            for down in [true, false] {
                guard let e = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: down) else { continue }
                e.keyboardSetUnicodeString(stringLength: part.count, unicodeString: part)
                e.post(tap: .cghidEventTap)
            }
        }
    }

    /// DOM KeyboardEvent.code -> macOS virtual key code (ANSI positions).
    static let keyCodes: [String: CGKeyCode] = [
        "KeyA": 0x00, "KeyS": 0x01, "KeyD": 0x02, "KeyF": 0x03, "KeyH": 0x04, "KeyG": 0x05, "KeyZ": 0x06, "KeyX": 0x07,
        "KeyC": 0x08, "KeyV": 0x09, "IntlBackslash": 0x0A, "KeyB": 0x0B, "KeyQ": 0x0C, "KeyW": 0x0D, "KeyE": 0x0E,
        "KeyR": 0x0F, "KeyY": 0x10, "KeyT": 0x11, "Digit1": 0x12, "Digit2": 0x13, "Digit3": 0x14, "Digit4": 0x15,
        "Digit6": 0x16, "Digit5": 0x17, "Equal": 0x18, "Digit9": 0x19, "Digit7": 0x1A, "Minus": 0x1B, "Digit8": 0x1C,
        "Digit0": 0x1D, "BracketRight": 0x1E, "KeyO": 0x1F, "KeyU": 0x20, "BracketLeft": 0x21, "KeyI": 0x22,
        "KeyP": 0x23, "Enter": 0x24, "KeyL": 0x25, "KeyJ": 0x26, "Quote": 0x27, "KeyK": 0x28, "Semicolon": 0x29,
        "Backslash": 0x2A, "Comma": 0x2B, "Slash": 0x2C, "KeyN": 0x2D, "KeyM": 0x2E, "Period": 0x2F, "Tab": 0x30,
        "Space": 0x31, "Backquote": 0x32, "Backspace": 0x33, "Escape": 0x35, "MetaRight": 0x36, "MetaLeft": 0x37,
        "ShiftLeft": 0x38, "CapsLock": 0x39, "AltLeft": 0x3A, "ControlLeft": 0x3B, "ShiftRight": 0x3C,
        "AltRight": 0x3D, "ControlRight": 0x3E, "F17": 0x40, "NumpadDecimal": 0x41, "NumpadMultiply": 0x43,
        "NumpadAdd": 0x45, "NumLock": 0x47, "NumpadDivide": 0x4B, "NumpadEnter": 0x4C, "NumpadSubtract": 0x4E,
        "F18": 0x4F, "F19": 0x50, "NumpadEqual": 0x51, "Numpad0": 0x52, "Numpad1": 0x53, "Numpad2": 0x54,
        "Numpad3": 0x55, "Numpad4": 0x56, "Numpad5": 0x57, "Numpad6": 0x58, "Numpad7": 0x59, "F20": 0x5A,
        "Numpad8": 0x5B, "Numpad9": 0x5C, "F5": 0x60, "F6": 0x61, "F7": 0x62, "F3": 0x63, "F8": 0x64, "F9": 0x65,
        "F11": 0x67, "F13": 0x69, "F16": 0x6A, "F14": 0x6B, "F10": 0x6D, "ContextMenu": 0x6E, "F12": 0x6F,
        "F15": 0x71, "Insert": 0x72, "Help": 0x72, "Home": 0x73, "PageUp": 0x74, "Delete": 0x75, "F4": 0x76,
        "End": 0x77, "F2": 0x78, "PageDown": 0x79, "F1": 0x7A, "ArrowLeft": 0x7B, "ArrowRight": 0x7C,
        "ArrowDown": 0x7D, "ArrowUp": 0x7E, "OSLeft": 0x37, "OSRight": 0x36,
    ]
}
