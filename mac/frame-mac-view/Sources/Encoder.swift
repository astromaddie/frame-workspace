// VideoToolbox encoding: H.264 for the normal path (hardware, low-latency rate
// control, no B-frames, Annex B output that WebCodecs takes without a
// description), or JPEG stills for viewers that can't decode H.264.
import CoreMedia
import Foundation
import VideoToolbox

enum Codec: String {
    case h264, jpeg
}

final class Encoder {
    let codec: Codec
    let fps: Int
    let bitsPerPixel: Double
    private(set) var width = 0
    private(set) var height = 0
    private var session: VTCompressionSession?
    private var forceKey = true
    private var lastPts = CMTime.invalid
    private var lastGiven = CMTime.invalid
    /// Bits per second to aim for; nil means from the size, frame rate and
    /// bits per pixel. Changing it takes effect on the next frame.
    private var target: Int?
    private(set) var bitrate = 0
    private let errorLock = NSLock()
    private var reportedFrameError = false
    /// Called on VideoToolbox's thread with one access unit (or JPEG) per
    /// frame, and the sequence number it was submitted with.
    var onFrame: ((Data, Bool, CMTime, UInt32) -> Void)?
    var onError: ((String) -> Void)?
    /// Called once per frame handed to VideoToolbox, however it went.
    var onDone: ((UInt32) -> Void)?
    /// Experiment switches (FRAME_MAC_VIEW_ENCODER, comma-separated), so a
    /// benchmark can compare them without a rebuild.
    static let options = Set((ProcessInfo.processInfo.environment["FRAME_MAC_VIEW_ENCODER"] ?? "")
        .split(separator: ",").map(String.init))

    init(codec: Codec, fps: Int, bitsPerPixel: Double) {
        self.codec = codec
        self.fps = fps
        self.bitsPerPixel = bitsPerPixel
    }

    /// The bitrate the size and quality setting would give.
    func defaultBitrate(width w: Int, height h: Int) -> Int {
        Int(min(max(Double(w * h * fps) * bitsPerPixel, 2_000_000), 60_000_000))
    }

    /// Live, without a new keyframe. Call on the encoding queue.
    func setBitrate(_ bps: Int?) {
        target = bps
        guard codec == .h264, let s = session else { return }
        let b = bps ?? defaultBitrate(width: width, height: height)
        guard b != bitrate else { return }
        bitrate = b
        VTSessionSetProperty(s, key: kVTCompressionPropertyKey_AverageBitRate, value: b as CFTypeRef)
        // A hard ceiling too: no more than 200 ms' worth of bits in any 200 ms,
        // so a keyframe can't hold the link for long.
        if Encoder.options.contains("cap") {
            VTSessionSetProperty(s, key: kVTCompressionPropertyKey_DataRateLimits,
                                 value: [b / 8 / 5, 0.2] as CFArray)
        }
    }

    deinit { invalidate() }

    func requestKeyFrame() { forceKey = true }

    private func reportFrameError(_ status: OSStatus) {
        errorLock.lock()
        let report = !reportedFrameError
        reportedFrameError = true
        errorLock.unlock()
        if report {
            onError?("Video encoding failed (VideoToolbox \(status)). Choose a lower picture quality and press Show again.")
        }
    }

    func invalidate() {
        if let s = session {
            VTCompressionSessionCompleteFrames(s, untilPresentationTimeStamp: .invalid)
            VTCompressionSessionInvalidate(s)
        }
        session = nil
    }

    private func makeSession(width w: Int, height h: Int) -> Bool {
        invalidate()
        errorLock.lock(); reportedFrameError = false; errorLock.unlock()
        var spec: [CFString: Any] = [:]
        if codec == .h264 { spec[kVTVideoEncoderSpecification_EnableLowLatencyRateControl] = true }
        if Encoder.options.contains("hw") { spec[kVTVideoEncoderSpecification_RequireHardwareAcceleratedVideoEncoder] = true }
        var s: VTCompressionSession?
        let type = codec == .h264 ? kCMVideoCodecType_H264 : kCMVideoCodecType_JPEG
        func create(_ spec: [CFString: Any]) -> OSStatus {
            VTCompressionSessionCreate(allocator: nil, width: Int32(w), height: Int32(h), codecType: type,
                                       encoderSpecification: spec as CFDictionary, imageBufferAttributes: nil,
                                       compressedDataAllocator: nil, outputCallback: nil, refcon: nil,
                                       compressionSessionOut: &s)
        }
        var err = create(spec)
        // Low-latency rate control needs Apple's hardware encoder; without it
        // (some VMs), plain real-time encoding still works.
        if err != noErr, !spec.isEmpty { err = create([:]) }
        guard err == noErr, let s else {
            onError?("couldn't start the \(codec.rawValue) encoder (VideoToolbox \(err))")
            return false
        }
        func set(_ key: CFString, _ value: Any) { VTSessionSetProperty(s, key: key, value: value as CFTypeRef) }
        set(kVTCompressionPropertyKey_RealTime, true)
        set(kVTCompressionPropertyKey_ColorPrimaries, kCVImageBufferColorPrimaries_ITU_R_709_2)
        set(kVTCompressionPropertyKey_TransferFunction, kCVImageBufferTransferFunction_ITU_R_709_2)
        set(kVTCompressionPropertyKey_YCbCrMatrix, kCVImageBufferYCbCrMatrix_ITU_R_709_2)
        if codec == .h264 {
            set(kVTCompressionPropertyKey_ProfileLevel, kVTProfileLevel_H264_ConstrainedHigh_AutoLevel)
            set(kVTCompressionPropertyKey_AllowFrameReordering, false)
            set(kVTCompressionPropertyKey_ExpectedFrameRate, fps)
            if Encoder.options.contains("nodelay") { set(kVTCompressionPropertyKey_MaxFrameDelayCount, 0) }
            if Encoder.options.contains("speed") { set(kVTCompressionPropertyKey_PrioritizeEncodingSpeedOverQuality, true) }
            // A keyframe every 10 s at most, so a viewer that lost one recovers
            // even if it never asks. Viewers ask for one when they start.
            set(kVTCompressionPropertyKey_MaxKeyFrameIntervalDuration, 10)
        } else {
            set(kVTCompressionPropertyKey_Quality, 0.8)
        }
        VTCompressionSessionPrepareToEncodeFrames(s)
        session = s
        lastPts = .invalid
        lastGiven = .invalid
        width = w
        height = h
        forceKey = true
        bitrate = 0
        setBitrate(target)
        return true
    }

    /// False if the frame never reached VideoToolbox (then onDone won't come).
    @discardableResult
    func encode(_ pb: CVPixelBuffer, pts given: CMTime, seq: UInt32) -> Bool {
        // The encoder's own timeline: strictly increasing (a resent picture
        // stamped "now" can be followed by a capture stamped a moment earlier),
        // and never more than two frames on from the last one. Rate control
        // budgets bits by elapsed time, so after a pause (the link held frames
        // back, or nothing changed) one frame would otherwise get a quarter
        // second's worth of bits: 200 KB that then hold a slow link for half a second.
        let w = CVPixelBufferGetWidth(pb), h = CVPixelBufferGetHeight(pb)
        if session == nil || w != width || h != height {
            guard makeSession(width: w, height: h) else { return false }  // a new timeline too
        }
        guard let s = session else { return false }
        var pts = given
        if lastPts.isValid, lastGiven.isValid {
            let gap = CMTimeSubtract(given, lastGiven)
            let most = CMTime(value: 2, timescale: CMTimeScale(fps))
            let step = CMTimeCompare(gap, most) > 0 ? most : gap
            pts = CMTimeAdd(lastPts, CMTimeMaximum(step, CMTime(value: 1, timescale: 1_000_000)))
        }
        lastGiven = given
        var props: CFDictionary?
        if forceKey {
            props = [kVTEncodeFrameOptionKey_ForceKeyFrame: true] as CFDictionary
            forceKey = false
        }
        let codec = self.codec
        lastPts = pts
        let status = VTCompressionSessionEncodeFrame(s, imageBuffer: pb, presentationTimeStamp: pts, duration: .invalid,
                                                     frameProperties: props, infoFlagsOut: nil) { [weak self] status, _, sample in
            guard let self else { return }
            defer { self.onDone?(seq) }
            guard status == noErr else { self.reportFrameError(status); return }
            guard let sample else { return }
            if codec == .jpeg {
                if let data = Self.bytes(sample) { self.onFrame?(data, true, pts, seq) }
            } else if let (data, key) = Self.annexB(sample) {
                self.onFrame?(data, key, pts, seq)
            }
        }
        if status != noErr { forceKey = true; reportFrameError(status) }
        return status == noErr
    }

    private static func bytes(_ sample: CMSampleBuffer) -> Data? {
        guard let block = CMSampleBufferGetDataBuffer(sample) else { return nil }
        var length = 0
        var ptr: UnsafeMutablePointer<CChar>?
        guard CMBlockBufferGetDataPointer(block, atOffset: 0, lengthAtOffsetOut: nil, totalLengthOut: &length,
                                          dataPointerOut: &ptr) == noErr, let ptr else { return nil }
        return Data(bytes: ptr, count: length)
    }

    /// AVCC sample -> Annex B access unit, with SPS and PPS before keyframes.
    static func annexB(_ sample: CMSampleBuffer) -> (Data, Bool)? {
        guard let avcc = bytes(sample) else { return nil }
        var key = true
        if let atts = CMSampleBufferGetSampleAttachmentsArray(sample, createIfNecessary: false) as? [[CFString: Any]],
           let first = atts.first, first[kCMSampleAttachmentKey_NotSync] as? Bool == true {
            key = false
        }
        let start: [UInt8] = [0, 0, 0, 1]
        var out = Data()
        if key, let fmt = CMSampleBufferGetFormatDescription(sample) {
            var count = 0
            CMVideoFormatDescriptionGetH264ParameterSetAtIndex(fmt, parameterSetIndex: 0, parameterSetPointerOut: nil,
                                                               parameterSetSizeOut: nil, parameterSetCountOut: &count,
                                                               nalUnitHeaderLengthOut: nil)
            for i in 0..<count {
                var p: UnsafePointer<UInt8>?
                var n = 0
                if CMVideoFormatDescriptionGetH264ParameterSetAtIndex(fmt, parameterSetIndex: i, parameterSetPointerOut: &p,
                                                                      parameterSetSizeOut: &n, parameterSetCountOut: nil,
                                                                      nalUnitHeaderLengthOut: nil) == noErr, let p {
                    out.append(contentsOf: start)
                    out.append(p, count: n)
                }
            }
        }
        let bytes = [UInt8](avcc)
        var i = 0
        while i + 4 <= bytes.count {
            let n = Int(bytes[i]) << 24 | Int(bytes[i + 1]) << 16 | Int(bytes[i + 2]) << 8 | Int(bytes[i + 3])
            i += 4
            guard n > 0, i + n <= bytes.count else { break }
            out.append(contentsOf: start)
            out.append(contentsOf: bytes[i..<(i + n)])
            i += n
        }
        return (out, key)
    }
}
