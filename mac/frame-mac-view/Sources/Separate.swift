// "Separate" mode: a streamed window gets a virtual display of its own. The
// window is moved onto it and sized to fill it, and the whole display is
// captured, so its menus, sheets, popovers and tooltips come along, nothing
// can cover it, and clicks land on it without raising anything. On stop the
// window goes back where it was. See docs/mac-window-separation.md.
import AppKit
import ApplicationServices
import CoreMedia
import Foundation

@_silgen_name("_AXUIElementGetWindow")
private func axWindowID(_ element: AXUIElement, _ id: UnsafeMutablePointer<CGWindowID>) -> AXError

/// One of our virtual displays (per-window sources default to HiDPI). Main thread only.
final class VirtualDisplay {
    static let vendor: UInt32 = 0xF0C0
    /// Workspace displays remain ordinary entries in the display picker. Only
    /// per-window displays use `vendor`, which `displaysJSON()` intentionally hides.
    static let workspaceVendor: UInt32 = 0xF0C1
    /// How often macOS composites our displays (FRAME_MAC_VIEW_VD_HZ to experiment).
    static let refreshRate = Double(ProcessInfo.processInfo.environment["FRAME_MAC_VIEW_VD_HZ"] ?? "") ?? 60
    private var display: CGVirtualDisplay?
    let id: CGDirectDisplayID
    private(set) var lastModeConfigurationError: String?
    /// Removal is deferred while the Mac's screen sleeps, and an identity
    /// can't be reused until it's gone, so each display gets a fresh serial.
    private static var serial = (UInt32(truncatingIfNeeded: ProcessInfo.processInfo.processIdentifier) & 0xFFFF) << 12

    enum Purpose { case separate, workspace }

    init?(name: String, width: Int, height: Int, pixelScale: Int = 2, purpose: Purpose = .separate) {
        var made: CGVirtualDisplay?
        for _ in 0..<8 where made == nil {
            VirtualDisplay.serial &+= 1
            let d = CGVirtualDisplayDescriptor()
            d.queue = DispatchQueue.main
            d.name = name
            d.maxPixelsWide = UInt32(pixelScale * width)
            d.maxPixelsHigh = UInt32(pixelScale * height)
            d.sizeInMillimeters = CGSize(width: Double(width) * 0.2646, height: Double(height) * 0.2646)
            d.vendorID = purpose == .workspace ? VirtualDisplay.workspaceVendor : VirtualDisplay.vendor
            d.productID = purpose == .workspace ? 0x5E9B : 0x5E9A
            d.serialNum = VirtualDisplay.serial
            d.terminationHandler = { _, _ in }
            guard let vd = CGVirtualDisplay(descriptor: d) else { continue }
            let s = CGVirtualDisplaySettings()
            s.hiDPI = pixelScale == 2 ? 1 : 0
            let hz = VirtualDisplay.refreshRate
            if pixelScale == 2 {
                s.modes = [CGVirtualDisplayMode(width: UInt32(2 * width), height: UInt32(2 * height), refreshRate: hz)!,
                           CGVirtualDisplayMode(width: UInt32(width), height: UInt32(height), refreshRate: hz)!]
            } else {
                s.modes = [CGVirtualDisplayMode(width: UInt32(width), height: UInt32(height), refreshRate: hz)!]
            }
            if vd.apply(s), vd.displayID != 0 { made = vd }
        }
        guard let made else { return nil }
        display = made
        id = made.displayID
        // Preserve the per-window mode selection exactly; Workspace also
        // supports a real 1x logical-to-pixel mode.
        let modes = (CGDisplayCopyAllDisplayModes(id, [kCGDisplayShowDuplicateLowResolutionModes: true] as CFDictionary)
            as? [CGDisplayMode]) ?? []
        let requestedMode: CGDisplayMode?
        if purpose == .separate {
            requestedMode = modes.first(where: {
                $0.width == width && $0.height == height && $0.pixelWidth == 2 * width && $0.refreshRate == VirtualDisplay.refreshRate
            }) ?? modes.first(where: { $0.width == width && $0.height == height && $0.pixelWidth == 2 * width })
        } else {
            requestedMode = modes.first(where: {
                $0.width == width && $0.height == height && $0.pixelWidth == pixelScale * width &&
                    $0.pixelHeight == pixelScale * height && $0.refreshRate == VirtualDisplay.refreshRate
            }) ?? modes.first(where: {
                $0.width == width && $0.height == height && $0.pixelWidth == pixelScale * width && $0.pixelHeight == pixelScale * height
            })
        }
        if let hidpi = requestedMode {
            if purpose == .separate {
                var cfg: CGDisplayConfigRef?
                CGBeginDisplayConfiguration(&cfg)
                CGConfigureDisplayWithDisplayMode(cfg, id, hidpi, nil)
                CGCompleteDisplayConfiguration(cfg, .forSession)
            } else {
                _ = applyWorkspaceMode(hidpi)
            }
        }
    }

    var bounds: CGRect { CGDisplayBounds(id) }

    /// Reapply once the newly added display has appeared in WindowServer.
    /// Some large modes are enumerable before CGVirtualDisplay is configurable.
    @discardableResult
    func applyWorkspaceMode(width: Int, height: Int, pixelScale: Int) -> Bool {
        let modes = (CGDisplayCopyAllDisplayModes(id, [kCGDisplayShowDuplicateLowResolutionModes: true] as CFDictionary)
            as? [CGDisplayMode]) ?? []
        guard let mode = modes.first(where: {
            $0.width == width && $0.height == height && $0.pixelWidth == pixelScale * width &&
                $0.pixelHeight == pixelScale * height && $0.refreshRate == VirtualDisplay.refreshRate
        }) ?? modes.first(where: {
            $0.width == width && $0.height == height && $0.pixelWidth == pixelScale * width && $0.pixelHeight == pixelScale * height
        }) else {
            lastModeConfigurationError = "the requested mode is not in the display mode list"
            return false
        }
        return applyWorkspaceMode(mode)
    }

    private func applyWorkspaceMode(_ mode: CGDisplayMode) -> Bool {
        var config: CGDisplayConfigRef?
        let begin = CGBeginDisplayConfiguration(&config)
        guard begin == .success, let config else {
            lastModeConfigurationError = "CGBeginDisplayConfiguration returned \(begin.rawValue)"
            return false
        }
        let configured = CGConfigureDisplayWithDisplayMode(config, id, mode, nil)
        guard configured == .success else {
            CGCancelDisplayConfiguration(config)
            lastModeConfigurationError = "CGConfigureDisplayWithDisplayMode returned \(configured.rawValue)"
            return false
        }
        let completed = CGCompleteDisplayConfiguration(config, .forSession)
        guard completed == .success else {
            lastModeConfigurationError = "CGCompleteDisplayConfiguration returned \(completed.rawValue)"
            return false
        }
        lastModeConfigurationError = nil
        return true
    }

    var isHiDPI: Bool { CGDisplayCopyDisplayMode(id).map { $0.pixelWidth > $0.width } ?? false }

    var screen: NSScreen? {
        NSScreen.screens.first {
            ($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value == id
        }
    }

    /// Where a window may go, in global (top-left origin) points: the display
    /// minus its menu bar and anything else macOS reserves there.
    var usableRect: CGRect {
        let b = bounds
        guard let s = screen else { return b }
        let top = s.frame.maxY - s.visibleFrame.maxY, bottom = s.visibleFrame.minY - s.frame.minY
        let left = s.visibleFrame.minX - s.frame.minX, right = s.frame.maxX - s.visibleFrame.maxX
        return CGRect(x: b.minX + left, y: b.minY + top, width: b.width - left - right, height: b.height - top - bottom)
    }

    func release() { display = nil }

    static func isOurs(_ id: CGDirectDisplayID) -> Bool { CGDisplayVendorNumber(id) == vendor }

    static func isWorkspace(_ id: CGDirectDisplayID) -> Bool { CGDisplayVendorNumber(id) == workspaceVendor }
}

/// A window through the Accessibility API.
struct AXWindow {
    let element: AXUIElement

    init?(windowID: CGWindowID, pid: pid_t) {
        let app = AXUIElementCreateApplication(pid)
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &value) == .success,
              let windows = value as? [AXUIElement] else { return nil }
        guard let match = windows.first(where: { w in
            var id: CGWindowID = 0
            return axWindowID(w, &id) == .success && id == windowID
        }) else { return nil }
        element = match
    }

    var frame: CGRect? {
        var p: CFTypeRef?, s: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXPositionAttribute as CFString, &p) == .success,
              AXUIElementCopyAttributeValue(element, kAXSizeAttribute as CFString, &s) == .success else { return nil }
        var point = CGPoint.zero, size = CGSize.zero
        AXValueGetValue(p as! AXValue, .cgPoint, &point)
        AXValueGetValue(s as! AXValue, .cgSize, &size)
        return CGRect(origin: point, size: size)
    }

    var isFullScreen: Bool {
        var v: CFTypeRef?
        return AXUIElementCopyAttributeValue(element, "AXFullScreen" as CFString, &v) == .success && (v as? Bool) == true
    }

    /// Moving to another display: position first (so the size fits there),
    /// then size, then position again in case the size change moved it.
    func setFrame(_ r: CGRect) {
        var origin = r.origin, size = r.size
        let pos = AXValueCreate(.cgPoint, &origin)!, sz = AXValueCreate(.cgSize, &size)!
        AXUIElementSetAttributeValue(element, kAXPositionAttribute as CFString, pos)
        AXUIElementSetAttributeValue(element, kAXSizeAttribute as CFString, sz)
        AXUIElementSetAttributeValue(element, kAXPositionAttribute as CFString, pos)
    }
}

/// A window on a display of its own, captured as that display.
final class SeparateSource: CaptureSource {
    let windowID: CGWindowID
    var onFrame: ((CVPixelBuffer, CMTime, Int64) -> Void)?
    var onEnded: ((String) -> Void)?
    var onChange: (() -> Void)?
    private(set) var title = ""
    private(set) var app = ""
    private(set) var pid: pid_t?
    private(set) var gone = false
    private var display: VirtualDisplay?
    private var inner: SCKSource?
    private var window: AXWindow?
    private var original: CGRect?
    private var crop: CGRect?  // display points, when only part of the display is captured
    private var maxFps = 60
    private var poll: DispatchSourceTimer?
    private var stopped = false
    private let queue = DispatchQueue(label: "frame-mac-view.separate")

    init(windowID: CGWindowID) { self.windowID = windowID }

    var frameRect: CGRect { inner?.frameRect ?? .zero }
    var displayID: CGDirectDisplayID? { display?.id }

    func start(maxLong: Int, fps: Int, completion: @escaping (String?) -> Void) {
        guard AXIsProcessTrusted() else {
            return completion("Separate windows need the Accessibility permission (to move the window)")
        }
        guard let info = WindowInfo.find(windowID) else {
            gone = true
            return completion("that window has closed")
        }
        title = info.title
        app = info.app
        pid = info.pid
        DispatchQueue.main.async { [self] in
            guard !stopped else { return }
            maxFps = max(1, fps)
            guard let w = AXWindow(windowID: windowID, pid: info.pid), let frame = w.frame else {
                return completion("couldn't reach that window through Accessibility")
            }
            if w.isFullScreen { return completion("take the window out of full screen first") }
            // A display the window's size, plus room for the menu bar, in
            // points; within what a panel can show sharply.
            let width = min(max(Int(frame.width.rounded()), 640), 2560) / 2 * 2
            let height = min(max(Int(frame.height.rounded()) + 40, 480), 1600) / 2 * 2
            guard let vd = VirtualDisplay(name: app.isEmpty ? "Frame Control" : "\(app) (Frame)", width: width, height: height) else {
                return completion("macOS wouldn't create a display for this window")
            }
            display = vd
            window = w
            original = frame
            // The new screen shows up in NSScreen a moment later; its menu bar
            // decides where the window can go.
            placeWindow(attempt: 0, maxLong: maxLong, completion: completion)
        }
    }

    private func placeWindow(attempt: Int, maxLong: Int, completion: @escaping (String?) -> Void) {
        guard !stopped, let vd = display, let w = window else { return }
        // Wait for the screen to appear, and for its HiDPI mode, so the
        // first frames are captured sharp.
        if vd.screen == nil || !vd.isHiDPI, attempt < 30 {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                self.placeWindow(attempt: attempt + 1, maxLong: maxLong, completion: completion)
            }
            return
        }
        let target = vd.usableRect
        w.setFrame(target)
        guard let now = w.frame, vd.bounds.intersects(now) else {
            restore()
            return completion("macOS didn't let the window move to its own display (Stage Manager can prevent this)")
        }
        let sck = SCKSource(.display(vd.id))
        // A window that keeps its own size (Calculator, some dialogs) sits in
        // the display's top-left corner: send only that corner, menu bar
        // included, with room around it for menus, not a panel of wallpaper.
        let b = vd.bounds
        if now.width < target.width - 8 || now.height < target.height - 8 {
            let w = min(b.width, max(now.maxX - b.minX, 480)), h = min(b.height, max(now.maxY - b.minY, 360))
            crop = CGRect(x: 0, y: 0, width: (w / 2).rounded(.up) * 2, height: (h / 2).rounded(.up) * 2)
            sck.crop = crop
        }
        sck.onFrame = { [weak self] pb, pts, shown in self?.onFrame?(pb, pts, shown) }
        sck.onEnded = { [weak self] reason in self?.onEnded?(reason) }
        inner = sck
        sck.start(maxLong: maxLong, fps: maxFps) { [weak self] error in
            // Back on main, where stop() runs too, so a viewer that left during
            // startup can't leave a capture running.
            DispatchQueue.main.async {
                guard let self, !self.stopped else { return sck.stop() }
                if let error {
                    self.restore()
                    return completion(error)
                }
                self.startPolling()
                completion(nil)
            }
        }
    }

    /// The window can close or be retitled; ScreenCaptureKit won't say.
    private func startPolling() {
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + 1, repeating: 1)
        t.setEventHandler { [weak self] in
            guard let self else { return }
            guard let info = WindowInfo.find(self.windowID) else {
                self.gone = true
                self.onEnded?("the window closed")
                return
            }
            if !info.title.isEmpty, info.title != self.title {
                self.title = info.title
                self.onChange?()
            }
            // Displays added at the same moment can rearrange each other, and
            // someone may drag the window away: put it back on its display.
            if !info.bounds.isEmpty {
                DispatchQueue.main.async { self.keepOnDisplay(info.bounds) }
            }
        }
        t.resume()
        poll = t
    }

    private func keepOnDisplay(_ now: CGRect) {
        guard !stopped, let vd = display, let w = window else { return }
        let b = vd.bounds
        if let c = crop {
            // Only part of the display is sent: the window must stay inside it.
            guard !c.offsetBy(dx: b.minX, dy: b.minY).insetBy(dx: -2, dy: -2).contains(now) else { return }
        } else {
            guard !b.contains(CGPoint(x: now.midX, y: now.minY + 10)) else { return }
        }
        let target = vd.usableRect
        w.setFrame(CGRect(origin: target.origin, size: now.size.width < target.width - 8 ? now.size : target.size))
    }

    /// Lifecycle state (stopped, inner, poll, window, display) is only touched on main.
    func stop() {
        DispatchQueue.main.async { [self] in
            stopped = true
            poll?.cancel()
            poll = nil
            inner?.stop()
            restore()
        }
    }

    /// Puts the window back where it was, then removes the display. In that
    /// order: removing a display first would let macOS pick where it goes.
    private func restore() {
        if let w = window, let r = original, WindowInfo.find(windowID) != nil { w.setFrame(r) }
        window = nil
        original = nil
        let vd = display
        display = nil
        // Give WindowServer a moment to move the window before the display goes.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { vd?.release() }
    }

    func setMaxLong(_ n: Int) { DispatchQueue.main.async { self.inner?.setMaxLong(n) } }
    func setMaxFps(_ fps: Int) {
        DispatchQueue.main.async {
            self.maxFps = max(1, fps)
            self.inner?.setMaxFps(self.maxFps)
        }
    }

    func pointer(x: Double, y: Double, text: String?) {}
}
