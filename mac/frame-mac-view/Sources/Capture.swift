// Where pictures come from: one Mac window or one display (ScreenCaptureKit),
// or a generated test pattern that needs no permissions and shows the input
// the viewer sends, for checking the whole path without touching the Mac.
import AppKit
import CoreMedia
import CoreVideo
import Foundation
import ScreenCaptureKit

enum Source: Equatable {
    case window(CGWindowID)
    case display(CGDirectDisplayID)
    case separate(CGWindowID)  // the window on a display of its own
    case spatial(CGWindowID)
    case menubar, status
    case menu(Int)
    case test

    init?(_ s: String) {
        let parts = s.split(separator: ":", maxSplits: 1).map(String.init)
        switch (parts.first, parts.count > 1 ? UInt32(parts[1]) : nil) {
        case ("window", let id?): self = .window(id)
        case ("display", let id?): self = .display(id)
        case ("separate", let id?): self = .separate(id)
        case ("spatial", let id?): self = .spatial(id)
        case ("menubar", _) where s == "menubar": self = .menubar
        case ("status", _) where s == "status": self = .status
        case ("menu", let slot?) where slot < 4: self = .menu(Int(slot))
        case ("test", _): self = .test
        default: return nil
        }
    }

    var isSpatial: Bool {
        switch self { case .spatial, .menubar, .status, .menu: return true; default: return false }
    }

    var key: String {
        switch self {
        case .window(let id): return "window:\(id)"
        case .display(let id): return "display:\(id)"
        case .separate(let id): return "separate:\(id)"
        case .spatial(let id): return "spatial:\(id)"
        case .menubar: return "menubar"
        case .status: return "status"
        case .menu(let slot): return "menu:\(slot)"
        case .test: return "test"
        }
    }
}

/// Size to capture at: fit the source within its long-edge and pixel-count
/// limits, then use even dimensions for the encoder.
func fitSize(width: Double, height: Double, maxLong: Int, maxPixels: Int = 16_777_216) -> (Int, Int) {
    let sourceWidth = max(1, width), sourceHeight = max(1, height)
    let byLongEdge = min(1, Double(max(16, maxLong)) / max(sourceWidth, sourceHeight))
    let byPixels = min(1, sqrt(Double(max(256, maxPixels)) / (sourceWidth * sourceHeight)))
    let scale = min(byLongEdge, byPixels)
    var w = max(16, Int((sourceWidth * scale / 2).rounded()) * 2)
    var h = max(16, Int((sourceHeight * scale / 2).rounded()) * 2)
    // Rounding to even dimensions may exceed a tight cap by a few pixels.
    while w * h > max(256, maxPixels) {
        if w >= h { w -= 2 } else { h -= 2 }
    }
    return (w, h)
}

/// What CGWindowList says about a window right now (no permission needed for
/// bounds; titles need Screen Recording).
struct WindowInfo {
    let id: CGWindowID
    let pid: pid_t
    let app: String
    let title: String
    let bounds: CGRect
    let layer: Int
    let onScreen: Bool

    static func all(onScreenOnly: Bool = true) -> [WindowInfo] {
        let opts: CGWindowListOption = onScreenOnly ? [.optionOnScreenOnly, .excludeDesktopElements] : [.excludeDesktopElements]
        let list = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[CFString: Any]] ?? []
        return list.compactMap(WindowInfo.init)
    }

    static func find(_ id: CGWindowID) -> WindowInfo? {
        let list = CGWindowListCopyWindowInfo(.optionIncludingWindow, id) as? [[CFString: Any]] ?? []
        return list.compactMap(WindowInfo.init).first { $0.id == id }
    }

    init?(_ d: [CFString: Any]) {
        guard let id = d[kCGWindowNumber] as? CGWindowID, let pid = d[kCGWindowOwnerPID] as? pid_t,
              let b = d[kCGWindowBounds] as? [String: Any], let rect = CGRect(dictionaryRepresentation: b as CFDictionary)
        else { return nil }
        self.id = id
        self.pid = pid
        app = d[kCGWindowOwnerName] as? String ?? ""
        title = d[kCGWindowName] as? String ?? ""
        bounds = rect
        layer = d[kCGWindowLayer] as? Int ?? 0
        onScreen = d[kCGWindowIsOnscreen] as? Bool ?? false
    }
}

protocol CaptureSource: AnyObject {
    /// The picture, its presentation time, and when the Mac composited it (µs, host clock).
    var onFrame: ((CVPixelBuffer, CMTime, Int64) -> Void)? { get set }
    var onEnded: ((String) -> Void)? { get set }
    var onChange: (() -> Void)? { get set }  // title or size changed
    /// True once the window or display is gone for good.
    var gone: Bool { get }
    /// Points on the Mac's global display space that the picture covers.
    var frameRect: CGRect { get }
    var title: String { get }
    var app: String { get }
    var pid: pid_t? { get }
    /// Pixel dimensions the source is configured to capture at. Available even
    /// before the next frame, including after a window resizes while still.
    var pixelSize: (Int, Int)? { get }
    func start(maxLong: Int, fps: Int, completion: @escaping (String?) -> Void)
    /// A smaller or larger picture from now on (the long side, in pixels).
    func setMaxLong(_ maxLong: Int)
    /// A maximum delivery rate from now on. Used for background sessions while
    /// preserving the captured dimensions and the selected session's rate.
    func setMaxFps(_ fps: Int)
    /// Keep capturing at one frame per second with no cursor for dirty checks,
    /// or restore the requested live cadence and cursor. Completion runs after
    /// ScreenCaptureKit has applied the configuration.
    func setStillCaptureMode(_ still: Bool, maxLong: Int, fps: Int, completion: @escaping () -> Void)
    func stop()
    func pointer(x: Double, y: Double, text: String?)  // for the test pattern
}

/// Sources that own a moved window or display can signal only after shutdown
/// has completed, allowing the spatial manager to unmirror after restoration.
protocol CaptureStopAwaitable: AnyObject {
    func stopAndWait(_ completion: @escaping () -> Void)
}

extension CaptureSource {
    var pixelSize: (Int, Int)? { nil }
    func pointer(x: Double, y: Double, text: String?) {}
    func setMaxFps(_ fps: Int) {}
    func setStillCaptureMode(_ still: Bool, maxLong: Int, fps: Int, completion: @escaping () -> Void) {
        if still {
            setMaxFps(1)
        } else {
            setMaxLong(maxLong)
            setMaxFps(fps)
        }
        completion()
    }
}

/// ScreenCaptureKit, for a window or a display.
final class SCKSource: NSObject, CaptureSource, CaptureStopAwaitable, SCStreamOutput, SCStreamDelegate {
    let source: Source
    var onFrame: ((CVPixelBuffer, CMTime, Int64) -> Void)?
    var onEnded: ((String) -> Void)?
    private let geometryLock = NSLock()
    private var configuredFrameRect = CGRect.zero
    var frameRect: CGRect {
        geometryLock.lock(); defer { geometryLock.unlock() }
        return configuredFrameRect
    }
    private(set) var title = ""
    private(set) var app = ""
    private(set) var pid: pid_t?
    private var stream: SCStream?
    private var config = SCStreamConfiguration()
    private let dimensionsLock = NSLock()
    private var configuredPixelSize: (Int, Int) = (0, 0)
    private var maxLong = 1920
    private var maxFps = 60
    private var stillCaptureMode = false
    private var lastShown: Int64 = 0  // display time of the last kept frame
    private var scale = 2.0
    private var poll: DispatchSourceTimer?
    private let queue = DispatchQueue(label: "frame-mac-view.capture", qos: .userInteractive)
    /// Set by stop(), on `queue`; a start still enumerating windows checks it
    /// before it starts capturing, so a viewer that left early leaves nothing on.
    private var cancelled = false
    private var windowIdentity: WindowIdentity?
    private var closeRecovery: WindowCloseRecovery?
    private var windowConfirmationPending = false
    private var windowConfirmationID: UInt64 = 0
    private var stopFinished = false
    private var stopWaiters: [() -> Void] = []
    /// The window or display no longer exists, so retrying can't help.
    private(set) var gone = false
    var onChange: (() -> Void)?  // title or size changed
    /// For a display: capture only this part of it (display points, top-left
    /// origin). Set before start().
    var crop: CGRect?
    var includeChildWindows = false
    var excludedWindowIDs = Set<CGWindowID>()

    func updateCrop(_ rect: CGRect) {
        queue.async {
            self.crop = rect
            let global: CGRect
            if case .display(let id) = self.source { global = self.displayRect(id) }
            else if case .window(let id) = self.source, let frame = WindowInfo.find(id)?.bounds {
                global = rect.offsetBy(dx: frame.minX, dy: frame.minY)
            } else { return }
            self.publishFrameRect(global)
            self.config.sourceRect = rect
            let size = fitSize(width: global.width * self.scale, height: global.height * self.scale, maxLong: self.maxLong)
            self.config.width = size.0; self.config.height = size.1
            self.publishPixelSize()
            self.stream?.updateConfiguration(self.config) { _ in }
            self.onChange?()
        }
    }

    init(_ source: Source) { self.source = source }

    var pixelSize: (Int, Int)? {
        dimensionsLock.lock(); defer { dimensionsLock.unlock() }
        return configuredPixelSize.0 > 0 && configuredPixelSize.1 > 0 ? configuredPixelSize : nil
    }

    private func publishPixelSize() {
        dimensionsLock.lock()
        configuredPixelSize = (config.width, config.height)
        dimensionsLock.unlock()
    }

    private func publishFrameRect(_ rect: CGRect) {
        geometryLock.lock(); configuredFrameRect = rect; geometryLock.unlock()
    }

    /// What's captured, in global points: the display, or the cropped part of it.
    private func displayRect(_ id: CGDirectDisplayID) -> CGRect {
        let b = CGDisplayBounds(id)
        guard let c = crop else { return b }
        return c.offsetBy(dx: b.minX, dy: b.minY).intersection(b)
    }

    func start(maxLong: Int, fps: Int, completion: @escaping (String?) -> Void) {
        queue.async {
            self.maxLong = maxLong
            self.maxFps = max(1, fps)
        }
        SCShareableContent.getExcludingDesktopWindows(true, onScreenWindowsOnly: false) { [self] content, error in
            queue.async { self.begin(content, error, completion: completion) }
        }
    }

    private func begin(_ content: SCShareableContent?, _ error: Error?, completion: @escaping (String?) -> Void) {
        if cancelled { return }
        guard let content else {
            return completion("Screen Recording isn't allowed for Frame Control (\(error?.localizedDescription ?? "no content"))")
        }
        let filter: SCContentFilter
        switch source {
        case .window(let id):
            guard let w = content.windows.first(where: { $0.windowID == id }) else {
                gone = true
                return completion("that window has closed")
            }
            filter = SCContentFilter(desktopIndependentWindow: w)
            let identity = WindowIdentity(windowID: id, processID: w.owningApplication?.processID)
            windowIdentity = identity
            closeRecovery = WindowCloseRecovery(target: identity)
            title = w.title ?? ""
            app = w.owningApplication?.applicationName ?? ""
            pid = w.owningApplication?.processID
            if let crop {
                config.sourceRect = crop
                publishFrameRect(crop.offsetBy(dx: w.frame.minX, dy: w.frame.minY))
            } else { publishFrameRect(w.frame) }
            if #available(macOS 14.2, *) { config.includeChildWindows = includeChildWindows }
        case .display(let id):
            guard let d = content.displays.first(where: { $0.displayID == id }) else {
                gone = true
                return completion("that display isn't connected")
            }
            filter = SCContentFilter(display: d, excludingWindows: content.windows.filter { excludedWindowIDs.contains($0.windowID) })
            title = displayName(id)
            app = "Mac"
            publishFrameRect(displayRect(id))
            if crop != nil { config.sourceRect = frameRect.offsetBy(dx: -CGDisplayBounds(id).minX, dy: -CGDisplayBounds(id).minY) }
        case .test, .separate, .spatial, .menubar, .status, .menu:
            return completion("not a ScreenCaptureKit source")
        }
        scale = Double(filter.pointPixelScale)
        // A display's own mode knows best: ScreenCaptureKit can still say 1
        // for a virtual display that has only just switched to HiDPI.
        if case .display(let id) = source, let mode = CGDisplayCopyDisplayMode(id), mode.width > 0 {
            scale = max(scale, Double(mode.pixelWidth) / Double(mode.width))
        }
        let (w, h) = fitSize(width: frameRect.width * scale, height: frameRect.height * scale, maxLong: maxLong)
        config.width = w
        config.height = h
        publishPixelSize()
        config.minimumFrameInterval = minimumInterval(fps: maxFps)
        config.pixelFormat = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
        config.colorMatrix = CGDisplayStream.yCbCrMatrix_ITU_R_709_2
        config.colorSpaceName = CGColorSpace.sRGB
        config.showsCursor = true
        config.queueDepth = 5
        config.scalesToFit = true
        config.preservesAspectRatio = true
        config.capturesAudio = false
        let stream = SCStream(filter: filter, configuration: config, delegate: self)
        do {
            try stream.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
        } catch {
            return completion("couldn't capture: \(error.localizedDescription)")
        }
        self.stream = stream
        stream.startCapture { error in
            self.queue.async {
                if self.cancelled {
                    stream.stopCapture { _ in }
                    return
                }
                if let error { return completion("couldn't capture: \(error.localizedDescription)") }
                self.startPolling()
                completion(nil)
            }
        }
    }

    func stop() {
        stopAndWait {}
    }

    func stopAndWait(_ completion: @escaping () -> Void) {
        queue.async { self.stopOnQueue(completion: completion) }
    }

    /// Stops capture while already on `queue`; late ScreenCaptureKit
    /// confirmation callbacks observe `cancelled` and cannot end the source.
    private func stopOnQueue() {
        stopOnQueue(completion: nil)
    }

    private func stopOnQueue(completion: (() -> Void)?) {
        if stopFinished { completion?(); return }
        if let completion { stopWaiters.append(completion) }
        guard !cancelled else { return }
        cancelled = true
        closeRecovery?.cancel()
        windowConfirmationPending = false
        windowConfirmationID &+= 1
        poll?.cancel()
        poll = nil
        let finish: () -> Void = { [self] in
            self.queue.async {
                self.stopFinished = true
                let waiters = self.stopWaiters
                self.stopWaiters.removeAll()
                waiters.forEach { $0() }
            }
        }
        if let stream {
            self.stream = nil
            stream.stopCapture { _ in finish() }
        } else {
            finish()
        }
    }

    /// Windows move, resize, retitle and close, and displays change mode;
    /// ScreenCaptureKit doesn't say.
    private func startPolling() {
        if case .display(let id) = source { return pollDisplay(id) }
        guard case .window(let id) = source else { return }
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + 1, repeating: 1)
        t.setEventHandler { [weak self] in
            guard let self else { return }
            guard let info = WindowInfo.find(id),
                  self.windowIdentity?.matches(WindowIdentity(windowID: info.id, processID: info.pid)) == true
            else {
                self.confirmWindow(id)
                return
            }
            _ = self.closeRecovery?.observe(
                cgWindow: WindowIdentity(windowID: info.id, processID: info.pid),
                shareableWindow: nil,
                shareableContentFresh: false,
                at: ProcessInfo.processInfo.systemUptime
            )
            self.applyWindow(title: info.title, bounds: info.bounds)
        }
        t.resume()
        poll = t
    }

    /// CGWindowList can briefly omit a live fullscreen window. Confirm that
    /// omission against fresh ScreenCaptureKit content and a second CG lookup
    /// before treating it as closed. Only one lookup is in flight; its timeout
    /// lets later polls retry if the asynchronous API fails to answer.
    private func confirmWindow(_ id: CGWindowID) {
        guard !cancelled, !gone, !windowConfirmationPending, let target = windowIdentity else { return }
        windowConfirmationPending = true
        windowConfirmationID &+= 1
        let requestID = windowConfirmationID

        SCShareableContent.getExcludingDesktopWindows(true, onScreenWindowsOnly: false) { [weak self] content, error in
            guard let self else { return }
            self.queue.async {
                guard !self.cancelled, !self.gone, self.windowConfirmationPending,
                      self.windowConfirmationID == requestID
                else { return }
                self.windowConfirmationPending = false

                let cgInfo = WindowInfo.find(id)
                let cgWindow = cgInfo.map { WindowIdentity(windowID: $0.id, processID: $0.pid) }
                let shareable = content?.windows.first { $0.windowID == id }
                let shareableWindow = shareable.map {
                    WindowIdentity(windowID: $0.windowID, processID: $0.owningApplication?.processID)
                }
                let decision = self.closeRecovery?.observe(
                    cgWindow: cgWindow,
                    shareableWindow: shareableWindow,
                    shareableContentFresh: content != nil && error == nil,
                    at: ProcessInfo.processInfo.systemUptime
                ) ?? .cancelled

                switch decision {
                case .present:
                    if let cgInfo, target.matches(WindowIdentity(windowID: cgInfo.id, processID: cgInfo.pid)) {
                        self.applyWindow(title: cgInfo.title, bounds: cgInfo.bounds)
                    } else if let shareable,
                              target.matches(WindowIdentity(windowID: shareable.windowID,
                                                           processID: shareable.owningApplication?.processID)) {
                        self.applyWindow(title: shareable.title ?? "", bounds: shareable.frame)
                    }
                case .waiting:
                    break
                case .closed:
                    self.gone = true
                    self.stopOnQueue()
                    self.onEnded?("the window closed")
                case .cancelled:
                    break
                }
            }
        }

        queue.asyncAfter(deadline: .now() + 2) { [weak self] in
            guard let self, !self.cancelled, self.windowConfirmationPending,
                  self.windowConfirmationID == requestID
            else { return }
            self.windowConfirmationPending = false
            self.windowConfirmationID &+= 1
        }
    }

    private func applyWindow(title newTitle: String, bounds: CGRect) {
        var changed = false
        if !newTitle.isEmpty, newTitle != title { title = newTitle; changed = true }
        let old = frameRect
        let captured = crop.map { $0.offsetBy(dx: bounds.minX, dy: bounds.minY) } ?? bounds
        publishFrameRect(captured)
        if abs(old.width - captured.width) > 1 || abs(old.height - captured.height) > 1 {
            let (w, h) = fitSize(width: captured.width * scale, height: captured.height * scale, maxLong: maxLong)
            config.width = w
            config.height = h
            publishPixelSize()
            stream?.updateConfiguration(config) { _ in }
            changed = true
        }
        if changed { onChange?() }
    }

    func setMaxLong(_ n: Int) {
        queue.async {
            self.maxLong = n
            guard self.stream != nil else { return }
            let (w, h) = fitSize(width: self.frameRect.width * self.scale, height: self.frameRect.height * self.scale, maxLong: n)
            guard w != self.config.width || h != self.config.height else { return }
            self.config.width = w
            self.config.height = h
            self.publishPixelSize()
            self.stream?.updateConfiguration(self.config) { _ in }
        }
    }

    /// ScreenCaptureKit's interval gate uses arrival time, and arrival jitter
    /// let it pick frames one 120 Hz refresh apart and then three: only 50-80%
    /// of captures were evenly spaced, which the Frame showed as hiccups. Let
    /// every refresh through here and choose by display time in `keep`.
    private func minimumInterval(fps: Int) -> CMTime {
        let period = 1_000_000 / Int64(max(1, fps))
        return CMTime(value: max(period / 2 - 1_000, 1_000), timescale: 1_000_000)
    }

    /// Keeps one frame per content period by display time. Half a 120 Hz
    /// refresh of slack makes 60 fps take every other refresh and 30/10 fps
    /// caps land on whole refreshes, at 60 or 120 Hz.
    private func keep(shown: Int64) -> Bool {
        let period = 1_000_000 / Int64(max(1, maxFps))
        let gate = max(period - 4_167, period / 2)
        guard lastShown == 0 || shown - lastShown >= gate || shown < lastShown else { return false }
        lastShown = shown
        return true
    }

    func setMaxFps(_ fps: Int) {
        queue.async {
            let bounded = max(1, fps)
            guard !self.stillCaptureMode else { return }
            guard bounded != self.maxFps else { return }
            self.maxFps = bounded
            self.config.minimumFrameInterval = self.minimumInterval(fps: bounded)
            self.stream?.updateConfiguration(self.config) { _ in }
        }
    }

    func setStillCaptureMode(_ still: Bool, maxLong: Int, fps: Int, completion: @escaping () -> Void) {
        queue.async {
            self.stillCaptureMode = still
            self.maxLong = maxLong
            self.maxFps = still ? 1 : max(1, fps)
            self.config.showsCursor = !still
            self.config.minimumFrameInterval = self.minimumInterval(fps: self.maxFps)
            let (w, h) = fitSize(width: self.frameRect.width * self.scale,
                                 height: self.frameRect.height * self.scale, maxLong: self.maxLong)
            self.config.width = w
            self.config.height = h
            self.publishPixelSize()
            guard let stream = self.stream else { completion(); return }
            stream.updateConfiguration(self.config) { _ in
                self.queue.async { completion() }
            }
        }
    }

    private func pollDisplay(_ id: CGDirectDisplayID) {
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + 0.5, repeating: 1)
        t.setEventHandler { [weak self] in
            guard let self, let mode = CGDisplayCopyDisplayMode(id), mode.width > 0 else { return }
            let bounds = self.displayRect(id), scale = Double(mode.pixelWidth) / Double(mode.width)
            let (w, h) = fitSize(width: bounds.width * scale, height: bounds.height * scale, maxLong: self.maxLong)
            self.publishFrameRect(bounds)
            guard w != self.config.width || h != self.config.height else { return }
            self.scale = scale
            self.config.width = w
            self.config.height = h
            self.publishPixelSize()
            self.stream?.updateConfiguration(self.config) { _ in }
            self.onChange?()
        }
        t.resume()
        poll = t
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sample: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, sample.isValid, let pb = CMSampleBufferGetImageBuffer(sample) else { return }
        // Only complete frames carry new pixels; idle and blank ones don't.
        let info = (CMSampleBufferGetSampleAttachmentsArray(sample, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]])?.first
        if let raw = info?[.status] as? Int, let status = SCFrameStatus(rawValue: raw), status != .complete {
            return
        }
        let pts = CMSampleBufferGetPresentationTimeStamp(sample)
        // When WindowServer composited it: the moment it showed on the Mac.
        let shown = (info?[.displayTime] as? UInt64).map(hostUs) ?? hostUs(pts)
        guard keep(shown: shown) else { return }
        onFrame?(pb, pts, shown)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        queue.async {
            guard !self.cancelled else { return }
            self.onEnded?("capture stopped: \(error.localizedDescription)")
        }
    }
}

func displayName(_ id: CGDirectDisplayID) -> String {
    for screen in NSScreen.screens {
        if (screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value == id {
            return screen.localizedName
        }
    }
    return "Display \(id)"
}

/// A moving test card: bars, a clock and a frame counter, plus a dot where the
/// viewer's pointer is and the last key it sent, so input can be checked too.
final class TestSource: CaptureSource {
    var onFrame: ((CVPixelBuffer, CMTime, Int64) -> Void)?
    var onEnded: ((String) -> Void)?
    var onChange: (() -> Void)?
    let gone = false
    let frameRect = CGRect(x: 0, y: 0, width: 1280, height: 720)
    let title = "Test pattern"
    let app = "Frame Control"
    let pid: pid_t? = nil
    private var timer: DispatchSourceTimer?
    private var pool: CVPixelBufferPool?
    private var n = 0
    private var fps = 60
    private var dot: (Double, Double)?
    private var lastText = "Point or type in the headset"
    private let queue = DispatchQueue(label: "frame-mac-view.test")
    private var size = (1280, 720)

    var pixelSize: (Int, Int)? { size }

    func start(maxLong: Int, fps: Int, completion: @escaping (String?) -> Void) {
        self.fps = max(1, fps)
        size = fitSize(width: 1280, height: 720, maxLong: maxLong)
        makePool()
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now(), repeating: 1.0 / Double(self.fps))
        t.setEventHandler { [weak self] in self?.draw() }
        t.resume()
        timer = t
        completion(nil)
    }

    func stop() {
        timer?.cancel()
        timer = nil
    }

    func setMaxLong(_ n: Int) {
        queue.async {
            self.size = fitSize(width: 1280, height: 720, maxLong: n)
            self.makePool()
        }
    }

    func setMaxFps(_ fps: Int) {
        queue.async {
            let bounded = max(1, fps)
            guard bounded != self.fps else { return }
            self.fps = bounded
            self.timer?.schedule(deadline: .now(), repeating: 1.0 / Double(bounded))
        }
    }

    private func makePool() {
        let attrs: [CFString: Any] = [kCVPixelBufferPixelFormatTypeKey: kCVPixelFormatType_32BGRA,
                                      kCVPixelBufferWidthKey: size.0, kCVPixelBufferHeightKey: size.1,
                                      kCVPixelBufferIOSurfacePropertiesKey: [:] as CFDictionary]
        pool = nil
        CVPixelBufferPoolCreate(nil, nil, attrs as CFDictionary, &pool)
    }

    func pointer(x: Double, y: Double, text: String?) {
        queue.async {
            if x >= 0 { self.dot = (x, y) }
            if let text { self.lastText = text }
        }
    }

    private func draw() {
        // Explicit diagnostic only: exercise gaze-driven repacking when the
        // production Session receives no subsequent capture callbacks.
        if n > 0 && ProcessInfo.processInfo.environment["FRAME_MAC_VIEW_TEST_STATIC"] == "1" { return }
        guard let pool else { return }
        var out: CVPixelBuffer?
        CVPixelBufferPoolCreatePixelBuffer(nil, pool, &out)
        guard let pb = out else { return }
        CVPixelBufferLockBaseAddress(pb, [])
        defer { CVPixelBufferUnlockBaseAddress(pb, []) }
        let (w, h) = size
        guard let ctx = CGContext(data: CVPixelBufferGetBaseAddress(pb), width: w, height: h, bitsPerComponent: 8,
                                  bytesPerRow: CVPixelBufferGetBytesPerRow(pb), space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                  bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue)
        else { return }
        let colors: [(CGFloat, CGFloat, CGFloat)] = [(0.75, 0.75, 0.75), (0.75, 0.75, 0), (0, 0.75, 0.75), (0, 0.75, 0),
                                                     (0.75, 0, 0.75), (0.75, 0, 0), (0, 0, 0.75)]
        let bw = CGFloat(w) / CGFloat(colors.count)
        for (i, c) in colors.enumerated() {
            ctx.setFillColor(red: c.0, green: c.1, blue: c.2, alpha: 1)
            ctx.fill(CGRect(x: CGFloat(i) * bw, y: CGFloat(h) * 0.35, width: bw + 1, height: CGFloat(h) * 0.65))
        }
        ctx.setFillColor(red: 0.08, green: 0.09, blue: 0.11, alpha: 1)
        ctx.fill(CGRect(x: 0, y: 0, width: w, height: Int(Double(h) * 0.35)))
        // A bar sweeping once a second shows motion and dropped frames.
        let x = CGFloat(n % 60) / 60 * CGFloat(w)
        ctx.setFillColor(red: 1, green: 1, blue: 1, alpha: 1)
        ctx.fill(CGRect(x: x, y: CGFloat(h) * 0.35, width: max(4, CGFloat(w) / 120), height: CGFloat(h) * 0.65))
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss.SSS"
        let label = "Frame Control test pattern  \(f.string(from: Date()))  frame \(n)\n\(lastText)"
        let text = NSAttributedString(string: label, attributes: [
            .font: NSFont.monospacedSystemFont(ofSize: CGFloat(h) / 24, weight: .medium),
            .foregroundColor: NSColor.white,
        ])
        let ns = NSGraphicsContext(cgContext: ctx, flipped: false)
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = ns
        text.draw(at: CGPoint(x: CGFloat(w) * 0.03, y: CGFloat(h) * 0.08))
        NSGraphicsContext.restoreGraphicsState()
        if let (px, py) = dot {
            let r = CGFloat(h) / 40
            ctx.setFillColor(red: 1, green: 0.2, blue: 0.2, alpha: 1)
            ctx.fillEllipse(in: CGRect(x: CGFloat(px) * CGFloat(w) - r, y: (1 - CGFloat(py)) * CGFloat(h) - r, width: 2 * r, height: 2 * r))
        }
        n += 1
        onFrame?(pb, CMClockGetTime(CMClockGetHostTimeClock()), nowUs())
    }
}
