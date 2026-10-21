import AppKit
import ApplicationServices
import CoreMedia
import CoreVideo
import ScreenCaptureKit

private func spatialDiagnostic(_ message: String) {
    if ProcessInfo.processInfo.environment["FRAME_SPATIAL_DIAGNOSTICS"] == "1" {
        FileHandle.standardError.write(Data("frame-mac-view: spatial \(message)\n".utf8))
    }
}

private let spatialFocusChanged = Notification.Name("FrameSpatialFocusChanged")

final class SpatialFocus: NSObject {
    private let memory = FocusCursorMemory()
    let fence = SpatialCursorFence()
    var focusedSource: String? { memory.focusedSource }
    private var windowRect = CGRect.zero
    private var menuRect = CGRect.zero
    private var menuRects: [CGRect] = []

    @discardableResult
    func focus(windowID: CGWindowID, u: Double = 0.5, v: Double = 0.5) -> Bool {
        guard let first = NormalizedPoint(u: u, v: v), let info = WindowInfo.find(windowID),
              SpatialCoordinator.allowedPID(info.pid) else { return false }
        let key = "spatial:\(windowID)"
        var leaving: NormalizedPoint?
        if let old = memory.focusedSource, case .spatial(let oldID)? = Source(old), let frame = WindowInfo.find(oldID)?.bounds {
            let point = Input.cursorLocation
            if frame.contains(point) {
                leaving = NormalizedPoint(u: Double((point.x - frame.minX) / frame.width),
                                          v: Double((point.y - frame.minY) / frame.height))
            }
        }
        let selection = memory.focus(key, firstVisit: first, leavingPoint: leaving)
        Input.focus(window: windowID, pid: info.pid)
        windowRect = WindowInfo.find(windowID)?.bounds ?? info.bounds
        updateFence()
        if selection.shouldMoveCursor {
            Input.moveCursor(to: SpatialSharedPolicy.panelToGlobal(u: selection.point.u, v: selection.point.v,
                                                                   captureRect: windowRect))
        }
        NotificationCenter.default.post(name: spatialFocusChanged, object: self)
        return true
    }

    func ensureFrontmost(windowID: CGWindowID) {
        if focusedSource != "spatial:\(windowID)" || Input.frontWindow() != windowID { _ = focus(windowID: windowID) }
    }

    func removeWindow(_ windowID: CGWindowID) {
        let wasFocused = focusedSource == "spatial:\(windowID)"
        memory.remove("spatial:\(windowID)")
        if wasFocused { windowRect = .zero; updateFence() }
    }

    func updateMenus(rect: CGRect, surfaces: [CGRect]) {
        menuRect = rect; menuRects = surfaces
        if let key = focusedSource, case .spatial(let id)? = Source(key), let frame = WindowInfo.find(id)?.bounds { windowRect = frame }
        updateFence()
    }

    private func updateFence() { fence.update([windowRect, menuRect] + menuRects) }
    func clear() { memory.clear(); fence.stop(); windowRect = .zero; menuRect = .zero; menuRects.removeAll() }
}

/// A window stays on the shared display across viewer disconnects. Its capture
/// owns no window movement or display lifetime.
final class SharedWindowSource: CaptureSource, CaptureStopAwaitable {
    private let inner: SCKSource
    private let coordinator: SpatialCoordinator
    let windowID: CGWindowID
    private var focusObserver: NSObjectProtocol?
    private var requestedFps = 60
    private var still = false
    var onFrame: ((CVPixelBuffer, CMTime, Int64) -> Void)? {
        get { inner.onFrame } set { inner.onFrame = newValue }
    }
    var onEnded: ((String) -> Void)? { get { inner.onEnded } set { inner.onEnded = newValue } }
    var onChange: (() -> Void)? { get { inner.onChange } set { inner.onChange = newValue } }
    var gone: Bool { inner.gone }
    var frameRect: CGRect { WindowInfo.find(windowID)?.bounds ?? inner.frameRect }
    var title: String { inner.title }
    var app: String { inner.app }
    var pid: pid_t? { inner.pid }
    var pixelSize: (Int, Int)? { inner.pixelSize }
    var focusCap: Int { coordinator.focus.focusedSource == "spatial:\(windowID)" ? 60 : 30 }

    init(windowID: CGWindowID, coordinator: SpatialCoordinator) {
        self.windowID = windowID; self.coordinator = coordinator
        inner = SCKSource(.window(windowID))
        inner.includeChildWindows = true
    }

    func start(maxLong: Int, fps: Int, completion: @escaping (String?) -> Void) {
        DispatchQueue.main.async {
            guard self.coordinator.isRunning, self.coordinator.contains(windowID: self.windowID) else {
                return completion("That Spatial Window Is Not Available")
            }
            self.requestedFps = fps
            self.focusObserver = NotificationCenter.default.addObserver(forName: spatialFocusChanged,
                object: self.coordinator.focus, queue: .main) { [weak self] _ in
                guard let self, !self.still else { return }
                spatialDiagnostic("window \(self.windowID) focus fps=\(min(self.requestedFps, self.focusCap))")
                self.inner.setMaxFps(min(self.requestedFps, self.focusCap))
            }
            self.inner.start(maxLong: maxLong, fps: min(fps, self.focusCap), completion: completion)
        }
    }
    func setMaxLong(_ n: Int) { inner.setMaxLong(n) }
    func setMaxFps(_ fps: Int) {
        DispatchQueue.main.async { self.requestedFps = fps; self.inner.setMaxFps(min(fps, self.focusCap)) }
    }
    func setStillCaptureMode(_ still: Bool, maxLong: Int, fps: Int, completion: @escaping () -> Void) {
        DispatchQueue.main.async {
            self.still = still; self.requestedFps = fps
            self.inner.setStillCaptureMode(still, maxLong: maxLong, fps: min(fps, self.focusCap), completion: completion)
        }
    }
    func stop() { stopAndWait {} }
    func stopAndWait(_ completion: @escaping () -> Void) {
        DispatchQueue.main.async {
            if let observer = self.focusObserver { NotificationCenter.default.removeObserver(observer) }
            self.focusObserver = nil
            self.inner.stopAndWait(completion)
        }
    }
}

struct SpatialSurface: Equatable {
    var id: CGWindowID? = nil
    var owner = ""
    var rect = CGRect.zero
    var windowRect = CGRect.zero
    var anchor = "none"
    var anchorRect = CGRect.zero
    var menusWidth: CGFloat = 0
    var selectedRect: CGRect? = nil
    var hot = false
    var visible = false
    var displayID: CGDirectDisplayID = 0
    var seq: UInt64 = 0

    var json: [String: Any] {
        var value: [String: Any] = ["t": "surface", "v": 1, "seq": seq, "visible": visible,
         "rect": SpatialSharedPolicy.rectJSON(rect), "anchor": anchor,
         "anchorRect": SpatialSharedPolicy.rectJSON(anchorRect), "menusWidth": menusWidth, "hot": hot]
        if let selectedRect { value["selectedRect"] = SpatialSharedPolicy.rectJSON(selectedRect) }
        return value
    }
}

/// Polls visibility and geometry, since menu window IDs can be reused. All
/// callbacks run on main; no application window is changed by this watcher.
final class MenuSurfaceWatcher {
    private weak var coordinator: SpatialCoordinator?
    private var timer: DispatchSourceTimer?
    private var listeners: [UUID: (Source, (SpatialSurface) -> Void)] = [:]
    private var values: [String: SpatialSurface] = [:]
    private var busy = false
    private var generation = 0
    private var alphaRects: [CGWindowID: (CGRect, CGRect)] = [:]

    init(coordinator: SpatialCoordinator) { self.coordinator = coordinator }
    var menusJSON: [[String: Any]] {
        (0..<4).compactMap { slot in
            guard let surface = values["menu:\(slot)"], surface.visible, let id = surface.id else { return nil }
            return ["slot": slot, "id": id, "owner": surface.owner,
                    "rect": SpatialSharedPolicy.rectJSON(surface.rect), "anchor": surface.anchor]
        }
    }

    func start() {
        stop()
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now(), repeating: 0.1)
        timer.setEventHandler { [weak self] in self?.refresh() }
        timer.resume(); self.timer = timer
    }
    func stop() {
        generation += 1; timer?.cancel(); timer = nil; busy = false; alphaRects.removeAll()
        for key in Array(values.keys) {
            var value = values[key] ?? SpatialSurface(); value.visible = false; value.selectedRect = nil
            publish(key, value)
        }
    }
    func subscribe(_ source: Source, callback: @escaping (SpatialSurface) -> Void) -> UUID {
        let id = UUID(); listeners[id] = (source, callback)
        spatialDiagnostic("subscribe \(source.key) visible=\(values[source.key]?.visible ?? false)")
        callback(values[source.key] ?? SpatialSurface())
        refresh()
        return id
    }
    func unsubscribe(_ id: UUID) { listeners[id] = nil }

    private func publish(_ key: String, _ next: SpatialSurface) {
        var value = next
        let old = values[key]
        value.seq = old?.seq ?? 0
        guard value != old else { return }
        value.seq &+= 1
        values[key] = value
        for (source, callback) in Array(listeners.values) where source.key == key { callback(value) }
    }

    private func menuItems() -> [SpatialSharedPolicy.MenuItem] {
        guard AXIsProcessTrusted(), let pid = NSWorkspace.shared.frontmostApplication?.processIdentifier else { return [] }
        func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
            var value: CFTypeRef?
            return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
        }
        let app = AXUIElementCreateApplication(pid)
        guard let raw = attribute(app, kAXMenuBarAttribute), CFGetTypeID(raw) == AXUIElementGetTypeID() else { return [] }
        let bar = unsafeBitCast(raw, to: AXUIElement.self)
        guard let children = attribute(bar, kAXChildrenAttribute) as? [AXUIElement] else { return [] }
        return children.compactMap { item in
            guard attribute(item, kAXRoleAttribute) as? String == kAXMenuBarItemRole,
                  let p = attribute(item, kAXPositionAttribute), let s = attribute(item, kAXSizeAttribute),
                  CFGetTypeID(p) == AXValueGetTypeID(), CFGetTypeID(s) == AXValueGetTypeID() else { return nil }
            var point = CGPoint.zero, size = CGSize.zero
            guard AXValueGetValue(unsafeBitCast(p, to: AXValue.self), .cgPoint, &point),
                  AXValueGetValue(unsafeBitCast(s, to: AXValue.self), .cgSize, &size) else { return nil }
            return SpatialSharedPolicy.MenuItem(frame: CGRect(origin: point, size: size),
                selected: attribute(item, kAXSelectedAttribute) as? Bool == true)
        }
    }

    private func refresh() {
        guard !busy, let coordinator, coordinator.isRunning, let displayID = coordinator.menuBarDisplayID else { return }
        let bar = coordinator.menuBarRect
        let infos = WindowInfo.all()
        let statusFrames = infos.filter { $0.layer == 25 && $0.bounds.intersects(bar) }.map(\.bounds)
        let split = min(bar.maxX, max(bar.minX, SpatialSharedPolicy.menuBarSplit(statusItemFrames: statusFrames) ?? bar.maxX))
        let menus = CGRect(x: bar.minX, y: bar.minY, width: max(1, split - bar.minX), height: bar.height)
        let status = CGRect(x: split, y: bar.minY, width: max(1, bar.maxX - split), height: bar.height)
        let hot = bar.contains(Input.cursorLocation)
        let dropdowns = infos.filter { $0.layer == 101 && $0.bounds.minX < split - 8 &&
            SpatialSharedPolicy.isMenuBarSurface(frame: $0.bounds, layer: $0.layer, owner: $0.app, menuBarRect: bar) }.map(\.bounds)
        let selected = SpatialSharedPolicy.selectedMenuItem(items: dropdowns.isEmpty ? [] : menuItems(),
            dropdowns: dropdowns, menuBar: menus)
        publish("menubar", SpatialSurface(rect: menus, anchor: "menubar", anchorRect: menus,
            menusWidth: menus.width, selectedRect: selected, hot: hot, visible: true, displayID: displayID))
        publish("status", SpatialSurface(rect: status, anchor: "status", anchorRect: status,
            menusWidth: menus.width, hot: hot, visible: true, displayID: displayID))
        let candidates = infos.filter {
            SpatialSharedPolicy.isMenuBarSurface(frame: $0.bounds, layer: $0.layer, owner: $0.app, menuBarRect: bar)
        }
        // Keep existing visible slots stable while reusing empty ones.
        var ordered = [WindowInfo]()
        for slot in 0..<4 {
            if let id = values["menu:\(slot)"]?.id, let info = candidates.first(where: { $0.id == id }) { ordered.append(info) }
        }
        ordered += candidates.filter { info in !ordered.contains { $0.id == info.id } }
        var used = Set<CGWindowID>()
        for slot in 0..<4 {
            let prior = values["menu:\(slot)"]?.id
            let info = ordered.first { $0.id == prior && !used.contains($0.id) } ?? ordered.first { !used.contains($0.id) }
            guard let info else { publish("menu:\(slot)", SpatialSurface()); continue }
            used.insert(info.id)
            let rect = alphaRects[info.id].flatMap { $0.0 == info.bounds ? $0.1 : nil } ?? info.bounds
            let anchor = info.layer == 22 || info.bounds.minX >= split - 8 ? "status" : "menubar"
            publish("menu:\(slot)", SpatialSurface(id: info.id, owner: info.app, rect: rect,
                windowRect: info.bounds, anchor: anchor, anchorRect: anchor == "status" ? status : menus,
                menusWidth: menus.width, hot: hot, visible: true, displayID: displayID))
        }
        coordinator.focus.updateMenus(rect: bar, surfaces: (0..<4).compactMap { values["menu:\($0)"].flatMap { $0.visible ? $0.rect : nil } })
        alphaRects = alphaRects.filter { used.contains($0.key) }
        guard let padded = candidates.first(where: { $0.layer == 22 && alphaRects[$0.id]?.0 != $0.bounds }) else { return }
        busy = true
        let epoch = generation
        SCShareableContent.getExcludingDesktopWindows(true, onScreenWindowsOnly: true) { [weak self] content, _ in
            guard let self, let window = content?.windows.first(where: { $0.windowID == padded.id }) else {
                DispatchQueue.main.async { self?.busy = false }; return
            }
            let config = SCStreamConfiguration()
            config.width = Int(window.frame.width); config.height = Int(window.frame.height)
            config.pixelFormat = kCVPixelFormatType_32BGRA
            config.showsCursor = false
            SCScreenshotManager.captureSampleBuffer(contentFilter: SCContentFilter(desktopIndependentWindow: window),
                                                    configuration: config) { sample, _ in
                var opaque: CGRect?
                if let sample, let pb = CMSampleBufferGetImageBuffer(sample) {
                    CVPixelBufferLockBaseAddress(pb, .readOnly)
                    if let base = CVPixelBufferGetBaseAddress(pb) {
                        let stride = CVPixelBufferGetBytesPerRow(pb), h = CVPixelBufferGetHeight(pb)
                        opaque = SpatialSharedPolicy.alphaBounds(bgra: Array(UnsafeBufferPointer(
                            start: base.assumingMemoryBound(to: UInt8.self), count: stride * h)),
                            width: CVPixelBufferGetWidth(pb), height: h, bytesPerRow: stride)
                    }
                    CVPixelBufferUnlockBaseAddress(pb, .readOnly)
                }
                let result = opaque
                DispatchQueue.main.async {
                    guard self.generation == epoch else { return }
                    self.busy = false
                    if let result { self.alphaRects[padded.id] = (padded.bounds, result.offsetBy(dx: padded.bounds.minX, dy: padded.bounds.minY)) }
                }
            }
        }
    }
}

/// An auxiliary session can exist while its menu slot is idle. Retargeting
/// stops the old capture before publishing a visible surface and new frames.
final class SpatialSurfaceSource: CaptureSource, CaptureStopAwaitable {
    private let source: Source
    private let coordinator: SpatialCoordinator
    private var subscription: UUID?
    private let lock = NSRecursiveLock()
    private var surface = SpatialSurface()
    private var inner: SCKSource?
    private var innerUsesDisplay = false
    private let retiring = DispatchGroup()
    private var generation: UInt64 = 0
    private var stopped = false
    private var maxLong = 1920
    private var fps = 30
    private var startCompletion: ((String?) -> Void)?
    var onSurface: (([String: Any]) -> Void)?
    var onFrame: ((CVPixelBuffer, CMTime, Int64) -> Void)?
    var onEnded: ((String) -> Void)?
    var onChange: (() -> Void)?
    var gone: Bool { false }
    var frameRect: CGRect { lock.lock(); defer { lock.unlock() }; return surface.rect }
    var title: String { source.key }
    var app: String { "Mac" }
    var pid: pid_t? { nil }
    var pixelSize: (Int, Int)? { lock.lock(); defer { lock.unlock() }; return inner?.pixelSize }

    init(_ source: Source, coordinator: SpatialCoordinator) { self.source = source; self.coordinator = coordinator }
    func start(maxLong: Int, fps: Int, completion: @escaping (String?) -> Void) {
        DispatchQueue.main.async {
            guard self.coordinator.isRunning else { return completion("Spatial Mode Is Not Running") }
            self.maxLong = maxLong; self.fps = min(fps, 30); self.startCompletion = completion
            self.subscription = self.coordinator.surfaces.subscribe(self.source) { [weak self] in self?.apply($0) }
            // An idle slot starts successfully without manufacturing a picture.
            self.finishStart(nil)
        }
    }
    private func finishStart(_ error: String?) { let callback = startCompletion; startCompletion = nil; callback?(error) }
    private func apply(_ value: SpatialSurface) {
        lock.lock()
        guard !stopped else { lock.unlock(); return }
        let previous = surface
        surface = value
        let changed = previous.id != value.id || previous.visible != value.visible || previous.displayID != value.displayID
        if !changed, let inner, value.visible {
            let usesDisplay = innerUsesDisplay
            lock.unlock()
            onSurface?(value.json)
            if previous.rect != value.rect {
                let origin = usesDisplay ? CGDisplayBounds(value.displayID).origin : value.windowRect.origin
                inner.updateCrop(value.rect.offsetBy(dx: -origin.x, dy: -origin.y))
            }
            return
        }
        generation &+= 1
        let epoch = generation
        let old = inner; inner = nil
        lock.unlock()
        let begin: () -> Void = { [weak self] in
            DispatchQueue.main.async {
                guard let self else { return }
                self.lock.lock()
                let current = !self.stopped && self.generation == epoch
                self.lock.unlock()
                guard current else { return }
                self.onSurface?(value.json)
                guard value.visible else { return }
                self.beginCapture(value, epoch: epoch, fallback: false)
            }
        }
        if let old {
            retiring.enter()
            old.stopAndWait { self.retiring.leave(); begin() }
        } else { begin() }
    }
    private func beginCapture(_ value: SpatialSurface, epoch: UInt64, fallback: Bool) {
        spatialDiagnostic("capture \(source.key) rect=\(value.rect) fallback=\(fallback)")
        let window = value.id != nil && !fallback
        let capture = SCKSource(window ? .window(value.id!) : .display(value.displayID))
        let origin = window ? value.windowRect.origin : CGDisplayBounds(value.displayID).origin
        capture.crop = value.rect.offsetBy(dx: -origin.x, dy: -origin.y)
        if !window, let cover = coordinator.coverWindowID { capture.excludedWindowIDs = [cover] }
        lock.lock(); inner = capture; innerUsesDisplay = !window; lock.unlock()
        var receivedPicture = false
        var retargeted = false
        let useDisplayCrop: () -> Void = { [weak self, weak capture] in
            guard let self, let capture, !retargeted else { return }
            self.lock.lock()
            let current = !self.stopped && self.generation == epoch && self.inner === capture
            self.lock.unlock()
            guard current else { return }
            retargeted = true
            self.retiring.enter()
            capture.stopAndWait {
                self.retiring.leave()
                DispatchQueue.main.async {
                    self.lock.lock()
                    let current = !self.stopped && self.generation == epoch
                    self.lock.unlock()
                    if current { self.beginCapture(value, epoch: epoch, fallback: true) }
                }
            }
        }
        if window {
            DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
                if !receivedPicture { useDisplayCrop() }
            }
        }
        capture.onFrame = { [weak self] pb, pts, shown in
            guard let self else { return }
            DispatchQueue.main.async {
                self.lock.lock()
                let current = !self.stopped && self.generation == epoch
                self.lock.unlock()
                guard current, !retargeted else { return }
                receivedPicture = true
                self.onFrame?(pb, pts, shown)
            }
        }
        capture.onChange = { [weak self] in self?.onChange?() }
        capture.onEnded = { [weak self] reason in
            DispatchQueue.main.async {
                guard let self else { return }
                self.lock.lock()
                let current = !self.stopped && self.generation == epoch
                self.lock.unlock()
                if current && !retargeted { self.onEnded?(reason) }
            }
        }
        capture.start(maxLong: maxLong, fps: fps) { [weak self] error in
            DispatchQueue.main.async {
                guard let self else { return }
                self.lock.lock()
                let current = !self.stopped && self.generation == epoch
                self.lock.unlock()
                guard current else { capture.stop(); return }
                spatialDiagnostic("capture \(self.source.key) started error=\(error ?? "none")")
                if error != nil, window {
                    useDisplayCrop()
                } else if let error { self.onEnded?(error) }
            }
        }
    }
    func setMaxLong(_ n: Int) { DispatchQueue.main.async { self.lock.lock(); defer { self.lock.unlock() }; self.maxLong = n; self.inner?.setMaxLong(n) } }
    func setMaxFps(_ fps: Int) { DispatchQueue.main.async { self.lock.lock(); defer { self.lock.unlock() }; self.fps = min(fps, 30); self.inner?.setMaxFps(self.fps) } }
    func stop() { stopAndWait {} }
    func stopAndWait(_ completion: @escaping () -> Void) {
        DispatchQueue.main.async {
            self.lock.lock()
            self.stopped = true; self.generation &+= 1
            let inner = self.inner; self.inner = nil
            if let id = self.subscription { self.coordinator.surfaces.unsubscribe(id) }; self.subscription = nil
            self.lock.unlock()
            if let inner {
                self.retiring.enter()
                inner.stopAndWait { self.retiring.leave() }
            }
            self.retiring.notify(queue: .main, execute: completion)
        }
    }
}
