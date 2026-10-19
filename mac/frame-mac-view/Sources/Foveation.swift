// Opt-in, per-frame foveated transport. Coordinates are top-left content UVs.
// No gaze coordinates are persisted or included in statistics/logs.
import Foundation
import CoreVideo
import Metal

struct FoveationLayout: Equatable {
    let width: Int, height: Int, encodedWidth: Int, encodedHeight: Int
    let left: Int, top: Int, coreWidth: Int, coreHeight: Int
    var atlas = false
    var direct = false
    var warped: Bool { !direct && coreWidth != 0 }
    static func full(_ w: Int, _ h: Int) -> Self {
        Self(width: w, height: h, encodedWidth: w, encodedHeight: h,
             left: 0, top: 0, coreWidth: 0, coreHeight: 0)
    }
    static func make(_ w: Int, _ h: Int, u: Double, v: Double) -> Self {
        guard w >= 16, h >= 16, w <= 4096, h <= 4096, w % 4 == 0, h % 4 == 0,
              u.isFinite, v.isFinite, (0...1).contains(u), (0...1).contains(v) else { return full(w, h) }
        let cw = (w / 2 / 4) * 4, ch = (h / 2 / 4) * 4
        let x = min(w - cw, max(0, Int(((u * Double(w) - Double(cw) / 2) / 4).rounded()) * 4))
        let y = min(h - ch, max(0, Int(((v * Double(h) - Double(ch) / 2) / 4).rounded()) * 4))
        return Self(width: w, height: h, encodedWidth: (w + cw) / 2, encodedHeight: (h + ch) / 2,
                    left: x, top: y, coreWidth: cw, coreHeight: ch)
    }
    static func makeAtlas(_ w: Int, _ h: Int, u: Double, v: Double) -> Self {
        guard w >= 16, h >= 16, w <= 4096, h <= 4096, w % 4 == 0, h % 4 == 0,
              u.isFinite, v.isFinite, (0...1).contains(u), (0...1).contains(v) else { return full(w, h) }
        let cw = w / 2, ch = h / 2
        let x = min(w - cw, max(0, Int(((u * Double(w) - Double(cw) / 2) / 2).rounded()) * 2))
        let y = min(h - ch, max(0, Int(((v * Double(h) - Double(ch) / 2) / 2).rounded()) * 2))
        return Self(width: w, height: h, encodedWidth: w, encodedHeight: h / 2,
                    left: x, top: y, coreWidth: cw, coreHeight: ch, atlas: true)
    }
    // Direct capture uses ordinary image coordinates and a fixed coded canvas.
    // Dimensions are negotiated once, aligned to four for half-size NV12 inputs.
    static func makeDirect(_ w: Int, _ h: Int, u: Double, v: Double) -> Self {
        precondition(w >= 16 && h >= 16 && w <= 4096 && h <= 4096 && w % 4 == 0 && h % 4 == 0)
        let valid = u.isFinite && v.isFinite && (0...1).contains(u) && (0...1).contains(v)
        let roi = makeAtlas(w, h, u: valid ? u : 0.5, v: valid ? v : 0.5)
        return Self(width: w, height: h, encodedWidth: w, encodedHeight: h,
                    left: roi.left, top: roi.top, coreWidth: roi.coreWidth, coreHeight: roi.coreHeight, direct: true)
    }
    // The extension is 20 bytes: version, mode, reserved u16, then eight BE u16s.
    var wire: Data {
        var out = Data([1, direct ? 3 : (atlas ? 2 : (warped ? 1 : 0)), 0, 0])
        for n in [width, height, encodedWidth, encodedHeight, left, top, coreWidth, coreHeight] {
            var be = UInt16(n).bigEndian
            out.append(Data(bytes: &be, count: 2))
        }
        return out
    }
}

// Serialised by Session.lock. Loss selects centre for direct capture, or
// full quality for legacy packing; reentry requires 500 ms of continuous samples and 2 s since a loss.
struct FoveationGaze {
    private var sample: (u: Double, v: Double, time: Int64, stamp: Int64)?
    private var firstValid: Int64 = 0
    private var lastStamp: Int64 = 0
    private var nativeSampleClock: Bool?
    private var resumeAfter: Int64 = 0
    private(set) var active = false
    // Fixed keys, session memory only. Never retain UVs or sample timestamps in
    // diagnostics; these counters explain full-quality transitions safely.
    private var counts = Dictionary(uniqueKeysWithValues: [
        "accepted", "activations", "fallbacks", "session", "version", "invalid",
        "malformed", "source", "range", "future", "stale", "replay", "sampleGap",
        "expired", "clockFuture"
    ].map { ($0, 0) })
    private var lastReason = "none"
    var diagnostics: [String: Any] { ["active": active, "lastReason": lastReason, "counts": counts] }
    private mutating func count(_ reason: String) {
        if let value = counts[reason], value < Int.max { counts[reason] = value + 1 }
    }
    mutating func invalidate(now: Int64) {
        if active { resumeAfter = now + 2_000_000; count("fallbacks") }
        active = false; sample = nil; firstValid = 0
    }
    private mutating func reject(_ reason: String, now: Int64) {
        count(reason); lastReason = reason; invalidate(now: now)
    }
    mutating func receive(_ m: [String: Any], session: String, now: Int64, allowDiagnostic: Bool) {
        // Reject numeric booleans and strings; JSONSerialization bridges all
        // JSON numbers through NSNumber, so inspect its CF type for booleans.
        func number(_ key: String) -> Double? {
            guard let n = m[key] as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID(),
                  n.doubleValue.isFinite else { return nil }
            return n.doubleValue
        }
        guard m["session"] as? String == session else { reject("session", now: now); return }
        guard number("version") == 1 else { reject("version", now: now); return }
        guard let valid = m["valid"] as? NSNumber, CFGetTypeID(valid) == CFBooleanGetTypeID() else { reject("malformed", now: now); return }
        guard valid.boolValue else { reject("invalid", now: now); return }
        guard m["source"] as? String == "eye" || (allowDiagnostic && m["source"] as? String == "diagnostic") else { reject("source", now: now); return }
        guard let u = number("u"), let v = number("v"), let t = number("time") else { reject("malformed", now: now); return }
        guard (0...1).contains(u), (0...1).contains(v), t > 0 else { reject("range", now: now); return }
        guard t <= Double(now + 50_000) else { reject("future", now: now); return }
        guard t >= Double(now - 150_000) else { reject("stale", now: now); return }
        // Host-clock estimates can step during resync. Freshness still uses
        // host time above; replay/gap ordering uses the sender's monotonic sample
        // clock when supplied. Old native v1 senders retain their prior behavior.
        let usesNativeClock = m["sampleTime"] != nil
        guard nativeSampleClock == nil || nativeSampleClock == usesNativeClock else { reject("malformed", now: now); return }
        let ordering: Double
        if usesNativeClock {
            guard let raw = number("sampleTime"), raw > 0, raw <= 9_007_199_254_740_991,
                  raw.rounded(.towardZero) == raw else { reject("malformed", now: now); return }
            ordering = raw
        } else { ordering = t }
        guard ordering > Double(lastStamp) else { reject("replay", now: now); return }
        let stamp = Int64(ordering)
        if let previous = sample, stamp - previous.stamp > 150_000 { reject("sampleGap", now: now) }
        if firstValid == 0 { firstValid = now }
        nativeSampleClock = usesNativeClock
        lastStamp = stamp; sample = (u, v, Int64(t), stamp); count("accepted")
    }
    /// The current gaze point for gaze-directed detail, or nil. Losing gaze
    /// only means "sharp everywhere" there and costs no decoder reset, so this
    /// applies the freshness checks without the packed layouts' 2 s re-entry
    /// cooldown (a blink would otherwise disable it for seconds).
    func point(now: Int64) -> (u: Double, v: Double)? {
        guard let s = sample, now - s.time <= 150_000, now >= s.time - 50_000 else { return nil }
        return (s.u, s.v)
    }
    mutating func layout(width: Int, height: Int, now: Int64, atlas: Bool = false, direct: Bool = false) -> FoveationLayout {
        let fallback: FoveationLayout = direct ? .makeDirect(width, height, u: 0.5, v: 0.5) : .full(width, height)
        guard let s = sample else { return fallback }
        guard now - s.time <= 150_000 else { reject("expired", now: now); return fallback }
        guard now >= s.time - 50_000 else { reject("clockFuture", now: now); return fallback }
        guard now >= resumeAfter, now - firstValid >= 500_000 else { return fallback }
        let result = direct ? FoveationLayout.makeDirect(width, height, u: s.u, v: s.v)
                           : atlas ? FoveationLayout.makeAtlas(width, height, u: s.u, v: s.v)
                           : FoveationLayout.make(width, height, u: s.u, v: s.v)
        if (result.direct || result.warped) && !active { count("activations") }
        active = result.direct || result.warped
        return result
    }
}

/// GPU sampling of IOSurface-backed NV12 (or the diagnostic BGRA source).
/// Luma and chroma use the same source-pixel edges. Atlas origins are even;
/// legacy warp breaks are four-pixel aligned. Both preserve exact focal samples.
/// Bilinear sampling averages 2x2 source samples for the fixed atlas base.
final class FoveationPacker {
    private let device: MTLDevice, queue: MTLCommandQueue, pipeline: MTLComputePipelineState
    private var cache: CVMetalTextureCache!
    private var pool: CVPixelBufferPool?
    private var poolKey = ""
    init() throws {
        guard let d = MTLCreateSystemDefaultDevice(), let q = d.makeCommandQueue() else {
            throw NSError(domain: "Foveation", code: 1)
        }
        device = d; queue = q
        let shader = """
        #include <metal_stdlib>
        using namespace metal;
        struct Params { float2 sourceSize; float2 origin; float2 core; float scale; float mode; };
        float axis(float x, float a, float c) {
            float b=a*0.5;
            return x<b ? x*2.0 : (x<b+c ? a+x-b : a+c+(x-b-c)*2.0);
        }
        kernel void packPlane(texture2d<float,access::sample> src [[texture(0)]],
                              texture2d<float,access::write> dst [[texture(1)]],
                              constant Params &p [[buffer(0)]], uint2 gid [[thread_position_in_grid]]) {
            if(gid.x>=dst.get_width() || gid.y>=dst.get_height()) return;
            float2 pos=(float2(gid)+0.5)*p.scale;
            float2 mapped;
            if(p.mode==2.0) {
                // Stable whole-desktop base at left, translated 1:1 ROI at right.
                mapped=pos.x<p.core.x ? pos*2.0 : p.origin+float2(pos.x-p.core.x,pos.y);
            } else mapped=float2(axis(pos.x,p.origin.x,p.core.x),axis(pos.y,p.origin.y,p.core.y));
            float2 uv=mapped/p.sourceSize;
            constexpr sampler linearSampler(coord::normalized,address::clamp_to_edge,filter::linear);
            dst.write(src.sample(linearSampler,uv),gid);
        }
        """
        let library = try d.makeLibrary(source: shader, options: nil)
        pipeline = try d.makeComputePipelineState(function: library.makeFunction(name: "packPlane")!)
        guard CVMetalTextureCacheCreate(nil, nil, d, nil, &cache) == kCVReturnSuccess else {
            throw NSError(domain: "Foveation", code: 2)
        }
    }
    func pack(_ source: CVPixelBuffer, layout l: FoveationLayout) throws -> CVPixelBuffer {
        let type = CVPixelBufferGetPixelFormatType(source)
        let nv12 = type == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange || type == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange
        guard nv12 || type == kCVPixelFormatType_32BGRA else { throw NSError(domain: "Foveation", code: 3) }
        let key = "\(l.encodedWidth):\(l.encodedHeight):\(type)"
        if poolKey != key {
            pool = nil
            let attrs: [CFString: Any] = [kCVPixelBufferWidthKey: l.encodedWidth, kCVPixelBufferHeightKey: l.encodedHeight,
                kCVPixelBufferPixelFormatTypeKey: type, kCVPixelBufferMetalCompatibilityKey: true,
                kCVPixelBufferIOSurfacePropertiesKey: [:] as CFDictionary]
            guard CVPixelBufferPoolCreate(nil, nil, attrs as CFDictionary, &pool) == kCVReturnSuccess else {
                throw NSError(domain: "Foveation", code: 4)
            }
            poolKey = key
            CVMetalTextureCacheFlush(cache, 0)
        }
        var out: CVPixelBuffer?
        guard let pool, CVPixelBufferPoolCreatePixelBufferWithAuxAttributes(nil, pool,
                [kCVPixelBufferPoolAllocationThresholdKey: 4] as CFDictionary, &out) == kCVReturnSuccess,
              let out, let command = queue.makeCommandBuffer() else { throw NSError(domain: "Foveation", code: 5) }
        CVBufferPropagateAttachments(source, out)
        var retained: [CVMetalTexture] = []
        for plane in 0..<(nv12 ? 2 : 1) {
            let scale = plane == 1 ? 2 : 1
            let format: MTLPixelFormat = nv12 ? (plane == 0 ? .r8Unorm : .rg8Unorm) : .bgra8Unorm
            func texture(_ pb: CVPixelBuffer, _ w: Int, _ h: Int) throws -> MTLTexture {
                var ref: CVMetalTexture?
                guard CVMetalTextureCacheCreateTextureFromImage(nil, cache, pb, nil, format, w, h, plane, &ref) == kCVReturnSuccess,
                      let ref, let tex = CVMetalTextureGetTexture(ref) else { throw NSError(domain: "Foveation", code: 6) }
                retained.append(ref); return tex
            }
            let src = try texture(source, l.width / scale, l.height / scale)
            let dst = try texture(out, l.encodedWidth / scale, l.encodedHeight / scale)
            guard let encoder = command.makeComputeCommandEncoder() else { throw NSError(domain: "Foveation", code: 7) }
            // Metal struct is 32 bytes; mode occupies the former trailing pad.
            var params: [Float] = [Float(l.width), Float(l.height), Float(l.left), Float(l.top),
                                   Float(l.coreWidth), Float(l.coreHeight), Float(scale), l.atlas ? 2 : 1]
            encoder.setComputePipelineState(pipeline)
            encoder.setTexture(src, index: 0); encoder.setTexture(dst, index: 1)
            encoder.setBytes(&params, length: 32, index: 0)
            encoder.dispatchThreads(MTLSize(width: dst.width, height: dst.height, depth: 1),
                                    threadsPerThreadgroup: MTLSize(width: 16, height: 16, depth: 1))
            encoder.endEncoding()
        }
        command.commit(); command.waitUntilCompleted()
        withExtendedLifetime(retained) {}
        guard command.status == .completed else { throw NSError(domain: "Foveation", code: 8) }
        return out
    }
}
