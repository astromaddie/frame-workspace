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
    case test

    init?(_ s: String) {
        let parts = s.split(separator: ":", maxSplits: 1).map(String.init)
        switch (parts.first, parts.count > 1 ? UInt32(parts[1]) : nil) {
        case ("window", let id?): self = .window(id)
        case ("display", let id?): self = .display(id)
        case ("separate", let id?): self = .separate(id)
        case ("test", _): self = .test
        default: return nil
        }
    }

    var key: String {
        switch self {
        case .window(let id): return "window:\(id)"
        case .display(let id): return "display:\(id)"
        case .separate(let id): return "separate:\(id)"
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
    func start(maxLong: Int, fps: Int, completion: @escaping (String?) -> Void)
    /// A smaller or larger picture from now on (the long side, in pixels).
    func setMaxLong(_ maxLong: Int)
    func stop()
    func pointer(x: Double, y: Double, text: String?)  // for the test pattern
}

extension CaptureSource {
    func pointer(x: Double, y: Double, text: String?) {}
}

/// ScreenCaptureKit, for a window or a display.
final class SCKSource: NSObject, CaptureSource, SCStreamOutput, SCStreamDelegate {
    let source: Source
    var onFrame: ((CVPixelBuffer, CMTime, Int64) -> Void)?
    var onEnded: ((String) -> Void)?
    private(set) var frameRect = CGRect.zero
    private(set) var title = ""
    private(set) var app = ""
    private(set) var pid: pid_t?
    private var stream: SCStream?
    private var config = SCStreamConfiguration()
    private var maxLong = 1920
    private var scale = 2.0
    private var poll: DispatchSourceTimer?
    private let queue = DispatchQueue(label: "frame-mac-view.capture", qos: .userInteractive)
    /// Set by stop(), on `queue`; a start still enumerating windows checks it
    /// before it starts capturing, so a viewer that left early leaves nothing on.
    private var cancelled = false
    /// The window or display no longer exists, so retrying can't help.
    private(set) var gone = false
    var onChange: (() -> Void)?  // title or size changed
    /// For a display: capture only this part of it (display points, top-left
    /// origin). Set before start().
    var crop: CGRect?

    init(_ source: Source) { self.source = source }

    /// What's captured, in global points: the display, or the cropped part of it.
    private func displayRect(_ id: CGDirectDisplayID) -> CGRect {
        let b = CGDisplayBounds(id)
        guard let c = crop else { return b }
        return c.offsetBy(dx: b.minX, dy: b.minY).intersection(b)
    }

    func start(maxLong: Int, fps: Int, completion: @escaping (String?) -> Void) {
        self.maxLong = maxLong
        SCShareableContent.getExcludingDesktopWindows(true, onScreenWindowsOnly: false) { [self] content, error in
            queue.async { self.begin(content, error, fps: fps, completion: completion) }
        }
    }

    private func begin(_ content: SCShareableContent?, _ error: Error?, fps: Int, completion: @escaping (String?) -> Void) {
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
            title = w.title ?? ""
            app = w.owningApplication?.applicationName ?? ""
            pid = w.owningApplication?.processID
            frameRect = w.frame
        case .display(let id):
            guard let d = content.displays.first(where: { $0.displayID == id }) else {
                gone = true
                return completion("that display isn't connected")
            }
            filter = SCContentFilter(display: d, excludingWindows: [])
            title = displayName(id)
            app = "Mac"
            frameRect = displayRect(id)
            if crop != nil { config.sourceRect = frameRect.offsetBy(dx: -CGDisplayBounds(id).minX, dy: -CGDisplayBounds(id).minY) }
        case .test, .separate:
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
        config.minimumFrameInterval = CMTime(value: 1, timescale: CMTimeScale(fps))
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
        queue.async {
            self.cancelled = true
            self.poll?.cancel()
            self.poll = nil
            self.stream?.stopCapture { _ in }
            self.stream = nil
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
            guard let info = WindowInfo.find(id) else {
                self.stop()
                self.onEnded?("the window closed")
                return
            }
            var changed = false
            if !info.title.isEmpty, info.title != self.title { self.title = info.title; changed = true }
            let old = self.frameRect
            self.frameRect = info.bounds
            if abs(old.width - info.bounds.width) > 1 || abs(old.height - info.bounds.height) > 1 {
                let (w, h) = fitSize(width: info.bounds.width * self.scale, height: info.bounds.height * self.scale, maxLong: self.maxLong)
                self.config.width = w
                self.config.height = h
                self.stream?.updateConfiguration(self.config) { _ in }
                changed = true
            }
            if changed { self.onChange?() }
        }
        t.resume()
        poll = t
    }

    func setMaxLong(_ n: Int) {
        queue.async {
            self.maxLong = n
            guard self.stream != nil else { return }
            let (w, h) = fitSize(width: self.frameRect.width * self.scale, height: self.frameRect.height * self.scale, maxLong: n)
            guard w != self.config.width || h != self.config.height else { return }
            self.config.width = w
            self.config.height = h
            self.stream?.updateConfiguration(self.config) { _ in }
        }
    }

    private func pollDisplay(_ id: CGDirectDisplayID) {
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + 0.5, repeating: 1)
        t.setEventHandler { [weak self] in
            guard let self, let mode = CGDisplayCopyDisplayMode(id), mode.width > 0 else { return }
            let bounds = self.displayRect(id), scale = Double(mode.pixelWidth) / Double(mode.width)
            let (w, h) = fitSize(width: bounds.width * scale, height: bounds.height * scale, maxLong: self.maxLong)
            self.frameRect = bounds
            guard w != self.config.width || h != self.config.height else { return }
            self.scale = scale
            self.config.width = w
            self.config.height = h
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
        onFrame?(pb, pts, shown)
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        onEnded?("capture stopped: \(error.localizedDescription)")
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
    private var dot: (Double, Double)?
    private var lastText = "Point or type in the headset"
    private let queue = DispatchQueue(label: "frame-mac-view.test")
    private var size = (1280, 720)

    func start(maxLong: Int, fps: Int, completion: @escaping (String?) -> Void) {
        size = fitSize(width: 1280, height: 720, maxLong: maxLong)
        makePool()
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now(), repeating: 1.0 / Double(fps))
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
