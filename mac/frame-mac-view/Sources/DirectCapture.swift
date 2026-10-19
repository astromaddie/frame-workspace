import AppKit
import CoreMedia
import CoreVideo
import ScreenCaptureKit

/// Opt-in display-only capture. All state below runs on queue, including gaze
/// expiry; encoder/network stalls cannot postpone selecting the centre fallback.
final class DirectSCKSource: NSObject, CaptureSource, SCStreamOutput, SCStreamDelegate {
    let displayID: CGDirectDisplayID
    var onFrame: ((CVPixelBuffer, CMTime, Int64) -> Void)?
    var onDirectFrame: ((CVPixelBuffer, CMTime, Int64, FoveationLayout, UInt64) -> Void)?
    // Optional local probe hook; never records image content or gaze coordinates.
    var onSampleDiagnostic: ((String, Int, Bool, Int64?, Int64) -> Void)?
    var selectLayout: ((Int, Int) -> (FoveationLayout, Bool))?
    var onEnded: ((String) -> Void)?
    var onChange: (() -> Void)?
    private(set) var gone = false
    private(set) var frameRect = CGRect.zero
    private(set) var title = ""
    let app = "Mac"
    let pid: pid_t? = nil
    private let queue = DispatchQueue(label: "frame-mac-view.direct-capture", qos: .userInteractive)
    private var filter: SCContentFilter?
    private var compositor: DirectComposite?
    private var baseStream: SCStream?
    private struct Crop {
        let stream: SCStream
        let generation: DirectCropGeneration
        let started: Int64
        var picture: (CVPixelBuffer, Int64)?
    }
    private var current: Crop?, pending: Crop?
    private var policy = DirectCropPolicy()
    private var base: (CVPixelBuffer, Int64)?
    private var timer: DispatchSourceTimer?
    private var completion: ((String?) -> Void)?
    private var ready = false, stopped = false
    private var width = 0, height = 0, fps = 60
    private var lastStart: Int64 = 0, lastOutput: Int64 = 0
    private var outputPending = false
    private var skewHolds = 0
    private var starts = 0, replacements = 0, rejected = 0, outputs = 0
    private var maxStartUs: Int64 = 0, maxGapUs: Int64 = 0, maxEmittedSkewUs: Int64 = 0
    private let diagnosticsLock = NSLock()
    private var summary: [String: Any] = [:]
    private var publishedEpoch: UInt64 = 0
    func acceptsPicture(epoch: UInt64?) -> Bool {
        diagnosticsLock.lock(); defer { diagnosticsLock.unlock() }
        return epoch == publishedEpoch
    }
    var diagnostics: [String: Any] { diagnosticsLock.lock(); defer { diagnosticsLock.unlock() }; return summary }
    init(displayID: CGDirectDisplayID) { self.displayID = displayID }

    func start(maxLong: Int, fps: Int, completion: @escaping (String?) -> Void) {
        queue.async { self.completion = completion; self.fps = max(1,fps) }
        SCShareableContent.getExcludingDesktopWindows(true,onScreenWindowsOnly:false) { [weak self] content,error in
            guard let self else { return }
            self.queue.async {
                guard !self.stopped else { return }
                guard let display = content?.displays.first(where: { $0.displayID == self.displayID }) else {
                    return self.fail("direct capture display unavailable: \(error?.localizedDescription ?? "display not found")")
                }
                self.frameRect = CGDisplayBounds(self.displayID)
                self.title = displayName(self.displayID)
                self.filter = SCContentFilter(display:display,excludingWindows:[])
                let mode = CGDisplayCopyDisplayMode(self.displayID)
                let scale = max(Double(self.filter!.pointPixelScale),Double(mode?.pixelWidth ?? 1)/Double(max(1,mode?.width ?? 1)))
                let size = fitSize(width:self.frameRect.width*scale,height:self.frameRect.height*scale,maxLong:maxLong)
                self.width = max(16,size.0/4*4); self.height = max(16,size.1/4*4)
                do {
                    self.compositor = try DirectComposite()
                    self.baseStream = try self.makeStream(layout:nil)
                    self.baseStream!.startCapture { [weak self] error in
                        if let error { self?.queue.async { self?.fail("direct base capture failed: \(error.localizedDescription)") } }
                    }
                    let timer = DispatchSource.makeTimerSource(queue:self.queue)
                    timer.schedule(deadline:.now(),repeating:1.0/60)
                    timer.setEventHandler { [weak self] in self?.tick() }
                    self.timer = timer; timer.resume()
                } catch { self.fail("direct capture unavailable: \(error.localizedDescription)") }
            }
        }
    }
    private func makeStream(layout: FoveationLayout?) throws -> SCStream {
        let config = SCStreamConfiguration()
        config.width = width/2; config.height = height/2
        config.sourceRect = layout.map { l in CGRect(x:Double(l.left)/Double(width)*frameRect.width,
            y:Double(l.top)/Double(height)*frameRect.height,width:frameRect.width/2,height:frameRect.height/2) }
            ?? CGRect(origin:.zero,size:frameRect.size)
        config.minimumFrameInterval = CMTime(value:1,timescale:CMTimeScale(fps))
        config.pixelFormat = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
        config.colorMatrix = CGDisplayStream.yCbCrMatrix_ITU_R_709_2
        config.colorSpaceName = CGColorSpace.sRGB
        config.showsCursor = true; config.queueDepth = 3
        config.scalesToFit = true; config.preservesAspectRatio = true; config.capturesAudio = false
        let stream = SCStream(filter:filter!,configuration:config,delegate:self)
        try stream.addStreamOutput(self,type:.screen,sampleHandlerQueue:queue)
        return stream
    }
    private func tick() {
        guard !stopped, filter != nil else { return }
        guard CGDisplayIsOnline(displayID) != 0, CGDisplayBounds(displayID).size == frameRect.size else {
            gone = true; return fail("direct capture display disconnected or changed geometry; reconnect required")
        }
        let now = nowUs()
        let selected = selectLayout?(width,height) ?? (.makeDirect(width,height,u:0.5,v:0.5),false)
        policy.observe(eyeActive:selected.1)
        diagnosticsLock.lock(); publishedEpoch = policy.epoch; diagnosticsLock.unlock()
        if let current, !policy.accepts(current.generation) {
            current.stream.stopCapture { _ in }; self.current = nil
        }
        if let pending, !policy.accepts(pending.generation) {
            pending.stream.stopCapture { _ in }; self.pending = nil
        }
        if let pending, now-pending.started > 3_000_000 { return fail("direct ROI capture produced no complete frame within 3 seconds") }
        if !ready, lastStart > 0, now-lastStart > 3_000_000 { return fail("direct capture startup produced no complete base/ROI pair") }
        if outputPending { emit() }
        // Latest target wins at the next bounded start. Do not cancel a valid
        // in-flight crop on every 60 Hz eye sample: that would starve output.
        guard pending == nil, current?.generation.layout != selected.0,
              current == nil || now-lastStart >= 100_000 else { return }
        do {
            let generation = policy.make(selected.0)
            let stream = try makeStream(layout:generation.layout)
            pending = Crop(stream:stream,generation:generation,started:now,picture:nil)
            lastStart = now; starts += 1
            stream.startCapture { [weak self, weak stream] error in
                guard let self, let stream, let error else { return }
                self.queue.async {
                    guard self.pending?.stream === stream else { return }
                    self.fail("direct ROI capture failed: \(error.localizedDescription)")
                }
            }
        } catch { fail("direct ROI capture unavailable: \(error.localizedDescription)") }
    }
    private func emit() {
        guard !stopped, let base, let current, policy.accepts(current.generation), let roi = current.picture else { return }
        // A complete base cannot carry an indefinitely old sharp crop. Idle
        // ROI callbacks also confirm that its pixels remain unchanged as of
        // their own display time. Hold the composite until that confirmation.
        guard base.1-roi.1 <= 150_000 else { skewHolds += 1; return }
        let now = nowUs()
        guard lastOutput == 0 || now-lastOutput >= Int64(900_000/fps) else { return }
        do {
            let output = try compositor!.compose(base:base.0,roi:roi.0,layout:current.generation.layout)
            if !ready { ready = true; let done = completion; completion = nil; done?(nil) }
            if lastOutput > 0 { maxGapUs = max(maxGapUs,now-lastOutput) }
            lastOutput = now; outputs += 1; outputPending = false
            maxEmittedSkewUs = max(maxEmittedSkewUs,base.1-roi.1)
            diagnosticsLock.lock()
            summary = ["path":"direct-display", "width":width,"height":height,"baseWidth":width/2,"baseHeight":height/2,
                "roiWidth":width/2,"roiHeight":height/2,"starts":starts,"replacements":replacements,
                "rejectedCallbacks":rejected,"frames":outputs,"skewHolds":skewHolds,"maxCropStartMs":Double(maxStartUs)/1000,
                "maxOutputGapMs":Double(maxGapUs)/1000,"maxEmittedSkewMs":Double(maxEmittedSkewUs)/1000,"replacementLimitHz":10]
            diagnosticsLock.unlock()
            onDirectFrame?(output,CMClockGetTime(CMClockGetHostTimeClock()),min(base.1,roi.1),current.generation.layout,current.generation.epoch)
        } catch { fail("direct capture composition failed: \(error.localizedDescription)") }
    }
    func stream(_ stream: SCStream, didOutputSampleBuffer sample: CMSampleBuffer, of type: SCStreamOutputType) {
        if let observer = onSampleDiagnostic, type == .screen {
            let diagnostic = (CMSampleBufferGetSampleAttachmentsArray(sample,createIfNecessary:false) as? [[SCStreamFrameInfo:Any]])?.first
            let role = stream === baseStream ? "base" : stream === current?.stream ? "current" : stream === pending?.stream ? "pending" : "retired"
            observer(role,diagnostic?[.status] as? Int ?? -1,sample.isValid,
                     (diagnostic?[.displayTime] as? UInt64).map(hostUs),hostUs(CMSampleBufferGetPresentationTimeStamp(sample)))
        }
        guard !stopped, type == .screen, sample.isValid,
              let info = (CMSampleBufferGetSampleAttachmentsArray(sample,createIfNecessary:false) as? [[SCStreamFrameInfo:Any]])?.first,
              let raw = info[.status] as? Int, let status = SCFrameStatus(rawValue:raw),
              status == .complete || status == .idle else { return }
        let shown = (info[.displayTime] as? UInt64).map(hostUs) ?? hostUs(CMSampleBufferGetPresentationTimeStamp(sample))
        // Re-evaluate expiry BEFORE accepting callbacks, even if timer delivery
        // was delayed. No late non-centre stream may survive an invalidation.
        tick()
        guard !stopped else { return }
        if status == .idle {
            if var crop = current, crop.stream === stream, policy.accepts(crop.generation), let picture = crop.picture {
                crop.picture = (picture.0,max(picture.1,shown)); current = crop
                if outputPending { emit() }
            }
            return
        }
        guard let pb = CMSampleBufferGetImageBuffer(sample),
              CVPixelBufferGetWidth(pb) == width/2, CVPixelBufferGetHeight(pb) == height/2 else {
            return fail("direct capture returned unexpected buffer geometry")
        }
        if stream === baseStream { base = (pb,shown) }
        else if var crop = pending, crop.stream === stream, policy.accepts(crop.generation) {
            crop.picture = (pb,shown)
            maxStartUs = max(maxStartUs,nowUs()-crop.started)
            current?.stream.stopCapture { _ in }
            current = crop; pending = nil; replacements += 1
        } else if var crop = current, crop.stream === stream, policy.accepts(crop.generation) {
            crop.picture = (pb,shown); current = crop
        } else { rejected += 1; return }
        outputPending = true
        emit()
    }
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        queue.async {
            guard !self.stopped, stream === self.baseStream || stream === self.current?.stream || stream === self.pending?.stream else { return }
            self.fail("direct capture stopped: \(error.localizedDescription)")
        }
    }
    // Geometry is a session invariant, including controller adaptation. FPS is
    // applied at composition and to future immutable ROI streams.
    func setMaxLong(_ maxLong: Int) {}
    func setMaxFps(_ fps: Int) { queue.async { self.fps = max(1,fps) } }
    private func fail(_ message: String) {
        guard !stopped else { return }
        let done = completion; completion = nil
        stopOnQueue()
        if let done { done(message) } else { onEnded?(message) }
    }
    func stop() { queue.async { self.stopOnQueue() } }
    private func stopOnQueue() {
        guard !stopped else { return }
        stopped = true; timer?.cancel(); timer = nil
        for stream in [baseStream,current?.stream,pending?.stream].compactMap({ $0 }) { stream.stopCapture { _ in } }
        baseStream = nil; current = nil; pending = nil; base = nil; completion = nil
    }
}
