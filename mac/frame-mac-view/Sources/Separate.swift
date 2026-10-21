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
    /// The shared spatial display is internal to the helper.
    static let spatialVendor: UInt32 = 0xF0C2
    /// How often macOS composites our displays (FRAME_MAC_VIEW_VD_HZ to experiment).
    static let refreshRate = Double(ProcessInfo.processInfo.environment["FRAME_MAC_VIEW_VD_HZ"] ?? "") ?? 60
    private var display: CGVirtualDisplay?
    let id: CGDirectDisplayID
    private let purpose: Purpose
    private(set) var lastModeConfigurationError: String?
    /// Removal is deferred while the Mac's screen sleeps, and an identity
    /// can't be reused until it's gone, so each display gets a fresh serial.
    private static var serial = (UInt32(truncatingIfNeeded: ProcessInfo.processInfo.processIdentifier) & 0xFFFF) << 12

    enum Purpose { case separate, workspace, spatialShared }

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
            d.vendorID = purpose == .workspace ? VirtualDisplay.workspaceVendor :
                (purpose == .spatialShared) ? VirtualDisplay.spatialVendor : VirtualDisplay.vendor
            d.productID = purpose == .workspace ? 0x5E9B :
                (purpose == .spatialShared) ? 0x5E9D : 0x5E9A
            d.serialNum = VirtualDisplay.serial
            d.terminationHandler = { _, _ in }
            guard let vd = CGVirtualDisplay(descriptor: d) else { continue }
            let s = CGVirtualDisplaySettings()
            s.hiDPI = pixelScale == 2 ? 1 : 0
            let hz = purpose == .spatialShared ? 60 : VirtualDisplay.refreshRate
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
        self.purpose = purpose
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
                if CGBeginDisplayConfiguration(&cfg) == .success, let cfg,
                   CGConfigureDisplayWithDisplayMode(cfg, id, hidpi, nil) == .success {
                    _ = CGCompleteDisplayConfiguration(cfg, .forSession)
                } else if let cfg {
                    CGCancelDisplayConfiguration(cfg)
                }
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
        // Spatial mode changes have the lifetime of the owning helper:
        // macOS rolls them back when the owning helper exits.
        let completed = CGCompleteDisplayConfiguration(config,
            purpose == .spatialShared ? .forAppOnly : .forSession)
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

    static func isSpatial(_ id: CGDirectDisplayID) -> Bool { CGDisplayVendorNumber(id) == spatialVendor }
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

    var role: String? { stringAttribute(kAXRoleAttribute) }
    var subrole: String? { stringAttribute(kAXSubroleAttribute) }
    var isStandardWindow: Bool { role == (kAXWindowRole as String) && subrole == (kAXStandardWindowSubrole as String) }

    var isMinimized: Bool {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXMinimizedAttribute as CFString, &value) == .success else {
            return false
        }
        return (value as? Bool) == true
    }

    private func stringAttribute(_ attribute: String) -> String? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success else { return nil }
        return value as? String
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
final class SeparateSource: CaptureSource, CaptureStopAwaitable {
    let windowID: CGWindowID
    private let startedAt = Date()
    /// Each Spatial window's startup steps, in the helper's log.
    private func log(_ step: String) {
        FileHandle.standardError.write(Data(String(format: "frame-mac-view: window %u +%.2fs %@\n", windowID,
                                                   Date().timeIntervalSince(startedAt), step).utf8))
    }
    var onFrame: ((CVPixelBuffer, CMTime, Int64) -> Void)?
    var onEnded: ((String) -> Void)?
    var onChange: (() -> Void)?
    var onStopComplete: (() -> Void)?
    private(set) var title = ""
    private(set) var app = ""
    private(set) var pid: pid_t?
    private(set) var gone = false
    private var display: VirtualDisplay?
    private let innerLock = NSLock()
    private var inner: SCKSource?
    private var window: AXWindow?
    private var original: CGRect?
    private var crop: CGRect?  // display points, when only part of the display is captured
    private var maxFps = 60
    private var poll: DispatchSourceTimer?
    private var stopped = false
    private var stopComplete = false
    private var captureStopFinished = false
    private var restorationInProgress = false
    private var stopCallbacks: [() -> Void] = []
    private let queue = DispatchQueue(label: "frame-mac-view.separate")

    var hasCompletedStop: Bool { stopComplete }

    init(windowID: CGWindowID) {
        self.windowID = windowID
    }

    private var activeDisplay: VirtualDisplay? { display }

    private var currentInner: SCKSource? {
        innerLock.lock(); defer { innerLock.unlock() }
        return inner
    }
    var frameRect: CGRect { currentInner?.frameRect ?? .zero }
    var displayID: CGDirectDisplayID? { activeDisplay?.id }
    var pixelSize: (Int, Int)? { currentInner?.pixelSize }

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
            guard let w = AXWindow(windowID: windowID, pid: info.pid), let currentFrame = w.frame else {
                return completion("couldn't reach that window through Accessibility")
            }
            if w.isFullScreen { return completion("take the window out of full screen first") }
            let frame = currentFrame
            // A display the window's size, plus room for the menu bar, in
            // points; within what a panel can show sharply.
            let width = min(max(Int(frame.width.rounded()), 640), 2560) / 2 * 2
            let height = min(max(Int(frame.height.rounded()) + 40, 480), 1600) / 2 * 2
            window = w
            original = frame
            guard let vd = VirtualDisplay(name: app.isEmpty ? "Frame Control" : "\(app) (Frame)", width: width, height: height) else {
                return completion("macOS wouldn't create a display for this window")
            }
            display = vd
            placeWindow(attempt: 0, maxLong: maxLong, completion: completion)
        }
    }

    private func placeWindow(attempt: Int, maxLong: Int, completion: @escaping (String?) -> Void) {
        guard !stopped, let vd = activeDisplay, let w = window else { return }
        // Wait for the screen to appear, and for its HiDPI mode, so the
        // first frames are captured sharp.
        if vd.screen == nil || !vd.isHiDPI {
            if attempt >= 30 {
                log("display not ready: screen \(vd.screen != nil), HiDPI \(vd.isHiDPI)")
                return stopAndWait { completion("the window display did not become ready") }
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                self.placeWindow(attempt: attempt + 1, maxLong: maxLong, completion: completion)
            }
            return
        }
        let target = vd.usableRect
        w.setFrame(target)
        // An app can take a moment to answer Accessibility right after a move.
        var moved = w.frame
        for _ in 0..<10 where moved == nil {
            Thread.sleep(forTimeInterval: 0.05)
            moved = w.frame
        }
        log("moved to \(target), now \(String(describing: moved))")
        guard let now = moved, vd.bounds.intersects(now) else {
            return stopAndWait { completion("macOS didn't let the window move to its own display (Stage Manager can prevent this)") }
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
        innerLock.lock(); inner = sck; innerLock.unlock()
        sck.start(maxLong: maxLong, fps: maxFps) { [weak self] error in
            // Back on main, where stop() runs too, so a viewer that left during
            // startup can't leave a capture running.
            DispatchQueue.main.async {
                guard let self, !self.stopped else { return sck.stop() }
                self.log(error.map { "capture failed: \($0)" } ?? "capture started")
                if let error {
                    return self.stopAndWait { completion(error) }
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
        guard !stopped, let vd = activeDisplay, let w = window else { return }
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
    func stop() { stopAndWait {} }

    func stopAndWait(_ completion: @escaping () -> Void) {
        DispatchQueue.main.async { [self] in
            if stopComplete { completion(); return }
            stopCallbacks.append(completion)
            if stopped {
                if captureStopFinished && !restorationInProgress { restoreForStop(attempt: 0) }
                return
            }
            stopped = true
            poll?.cancel()
            poll = nil
            let finishCapture: () -> Void = { [weak self] in
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.captureStopFinished = true
                    self.restoreForStop(attempt: 0)
                }
            }
            if let awaitable = currentInner {
                awaitable.stopAndWait(finishCapture)
            } else {
                currentInner?.stop()
                finishCapture()
            }
        }
    }

    /// Restore the frame captured when this separate stream began.
    private func restoreOrParkWindow() -> (safe: Bool, closed: Bool) {
        guard let window else { return (true, true) }
        guard let current = WindowInfo.find(windowID), current.pid == pid else {
            self.window = nil
            original = nil
            return (true, true)
        }
        let safe: Bool
        if let frame = original {
            window.setFrame(frame)
            safe = window.frame.map { Self.closeEnough($0, frame) } ?? false
        } else {
            safe = true
        }
        if safe {
            self.window = nil
            original = nil
        }
        return (safe, false)
    }

    private func restoreForStop(attempt: Int) {
        guard captureStopFinished, !stopComplete else { return }
        if !restorationInProgress { restorationInProgress = true }
        let result = restoreOrParkWindow()
        guard result.safe else {
            guard attempt < 10 else {
                restorationInProgress = false
                let callbacks = stopCallbacks
                stopCallbacks.removeAll()
                callbacks.forEach { $0() }
                return
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in
                self?.restoreForStop(attempt: attempt + 1)
            }
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in
            guard let self else { return }
            self.display?.release()
            self.display = nil
            self.finishStop()
        }
    }

    private static func closeEnough(_ lhs: CGRect, _ rhs: CGRect) -> Bool {
        abs(lhs.minX - rhs.minX) <= 2 && abs(lhs.minY - rhs.minY) <= 2 &&
            abs(lhs.width - rhs.width) <= 2 && abs(lhs.height - rhs.height) <= 2
    }

    private func finishStop() {
        guard !stopComplete else { return }
        stopComplete = true
        let callbacks = stopCallbacks
        stopCallbacks.removeAll()
        callbacks.forEach { $0() }
        onStopComplete?()
        onStopComplete = nil
    }

    func setMaxLong(_ n: Int) { DispatchQueue.main.async { self.currentInner?.setMaxLong(n) } }
    func setMaxFps(_ fps: Int) {
        DispatchQueue.main.async {
            self.maxFps = max(1, fps)
            self.currentInner?.setMaxFps(self.maxFps)
        }
    }

    func setStillCaptureMode(_ still: Bool, maxLong: Int, fps: Int, completion: @escaping () -> Void) {
        DispatchQueue.main.async {
            self.maxFps = still ? 1 : max(1, fps)
            guard let inner = self.currentInner else { completion(); return }
            inner.setStillCaptureMode(still, maxLong: maxLong, fps: self.maxFps, completion: completion)
        }
    }

    func pointer(x: Double, y: Double, text: String?) {}
}
