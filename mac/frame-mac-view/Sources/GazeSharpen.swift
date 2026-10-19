// Gaze-directed detail: keeps full detail around where the viewer looks and
// softens the picture with distance from that point before encoding, so the
// encoder spends its bits where the eye can resolve them. The picture keeps its
// size and position, so the decoder, texture and placement are unchanged.
//
// This is for constrained links. Worn Wi-Fi carried only ~20 Mbit/s Mac ->
// Frame while video-like motion wanted ~39 Mbit/s (2026-10-19). With the Wi-Fi
// bitrate cap, the encoder's budget then goes to the focal region instead of
// detail in the periphery. Without fresh gaze the picture is sent unchanged.
import CoreVideo
import Foundation
import Metal

final class GazeSharpener {
    /// Distances are in screen widths from the gaze point: full detail inside
    /// `sharpRadius`, fully softened beyond `softRadius`, with a smooth ramp
    /// between. `softenPixels` is the luma blur reach at full softening.
    struct Tuning {
        var sharpRadius: Float = 0.15
        var softRadius: Float = 0.45
        var softenPixels: Float = 2.5
        static func fromEnvironment() -> Tuning {
            var t = Tuning()
            let env = ProcessInfo.processInfo.environment
            func value(_ key: String, _ range: ClosedRange<Float>) -> Float? {
                guard let v = Float(env[key] ?? ""), range.contains(v) else { return nil }
                return v
            }
            if let v = value("FRAME_MAC_VIEW_GAZE_SHARP_RADIUS", 0.02...1) { t.sharpRadius = v }
            if let v = value("FRAME_MAC_VIEW_GAZE_SOFT_RADIUS", 0.03...2) { t.softRadius = v }
            if let v = value("FRAME_MAC_VIEW_GAZE_SOFTEN_PX", 0...8) { t.softenPixels = v }
            t.softRadius = max(t.softRadius, t.sharpRadius + 0.01)
            return t
        }
    }

    /// "auto" softens only while the link is capped (Wi-Fi); "on" always; "off" never.
    /// Starts from FRAME_MAC_VIEW_GAZE_SHARPEN (default off until a worn check
    /// confirms the gaze point lands where the wearer looks); POST
    /// /link?sharpen= changes it live.
    static var mode: String {
        get { modeLock.lock(); defer { modeLock.unlock() }; return modeValue }
        set { guard modes.contains(newValue) else { return }; modeLock.lock(); modeValue = newValue; modeLock.unlock() }
    }
    static let modes = ["auto", "on", "off"]
    private static var modeValue: String = {
        let m = ProcessInfo.processInfo.environment["FRAME_MAC_VIEW_GAZE_SHARPEN"] ?? "off"
        return modes.contains(m) ? m : "off"
    }()
    private static let modeLock = NSLock()
    static var wanted: Bool { mode == "on" || (mode == "auto" && Encoder.linkCap > 0) }

    let tuning = Tuning.fromEnvironment()
    private let device: MTLDevice, queue: MTLCommandQueue, pipeline: MTLComputePipelineState
    private var cache: CVMetalTextureCache!
    private var pool: CVPixelBufferPool?
    private var poolKey = ""

    init() throws {
        guard let d = MTLCreateSystemDefaultDevice(), let q = d.makeCommandQueue() else {
            throw NSError(domain: "GazeSharpen", code: 1)
        }
        device = d; queue = q
        let shader = """
        #include <metal_stdlib>
        using namespace metal;
        struct Params { float2 size; float2 gaze; float aspect; float sharp; float soft; float reach; };
        kernel void soften(texture2d<float,access::sample> src [[texture(0)]],
                           texture2d<float,access::write> dst [[texture(1)]],
                           constant Params &p [[buffer(0)]], uint2 gid [[thread_position_in_grid]]) {
            if(gid.x>=dst.get_width() || gid.y>=dst.get_height()) return;
            constexpr sampler linearSampler(coord::normalized,address::clamp_to_edge,filter::linear);
            float2 uv=(float2(gid)+0.5)/p.size;
            // Distance in screen widths, so the sharp region is round on screen.
            float2 d=float2(uv.x-p.gaze.x,(uv.y-p.gaze.y)/p.aspect);
            float w=smoothstep(p.sharp,p.soft,length(d));
            float4 c=src.read(gid);
            if(w<=0.0) { dst.write(c,gid); return; }
            // Four bilinear taps on the diagonals: a box blur whose reach grows
            // with eccentricity.
            float2 o=(w*p.reach+0.5)/p.size;
            float4 b=(src.sample(linearSampler,uv+o)+src.sample(linearSampler,uv-o)+
                      src.sample(linearSampler,uv+float2(o.x,-o.y))+src.sample(linearSampler,uv+float2(-o.x,o.y)))*0.25;
            dst.write(mix(c,b,w),gid);
        }
        """
        let library = try d.makeLibrary(source: shader, options: nil)
        pipeline = try d.makeComputePipelineState(function: library.makeFunction(name: "soften")!)
        guard CVMetalTextureCacheCreate(nil, nil, d, nil, &cache) == kCVReturnSuccess else {
            throw NSError(domain: "GazeSharpen", code: 2)
        }
    }

    /// A softened copy of an NV12 picture around gaze (u, v in 0...1).
    func apply(_ source: CVPixelBuffer, u: Double, v: Double) throws -> CVPixelBuffer {
        let type = CVPixelBufferGetPixelFormatType(source)
        guard type == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange || type == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange else {
            throw NSError(domain: "GazeSharpen", code: 3)
        }
        let width = CVPixelBufferGetWidth(source), height = CVPixelBufferGetHeight(source)
        let key = "\(width):\(height):\(type)"
        if poolKey != key {
            pool = nil
            let attrs: [CFString: Any] = [kCVPixelBufferWidthKey: width, kCVPixelBufferHeightKey: height,
                kCVPixelBufferPixelFormatTypeKey: type, kCVPixelBufferMetalCompatibilityKey: true,
                kCVPixelBufferIOSurfacePropertiesKey: [:] as CFDictionary]
            guard CVPixelBufferPoolCreate(nil, nil, attrs as CFDictionary, &pool) == kCVReturnSuccess else {
                throw NSError(domain: "GazeSharpen", code: 4)
            }
            poolKey = key
            CVMetalTextureCacheFlush(cache, 0)
        }
        var out: CVPixelBuffer?
        guard let pool, CVPixelBufferPoolCreatePixelBufferWithAuxAttributes(nil, pool,
                [kCVPixelBufferPoolAllocationThresholdKey: 4] as CFDictionary, &out) == kCVReturnSuccess,
              let out, let command = queue.makeCommandBuffer() else { throw NSError(domain: "GazeSharpen", code: 5) }
        CVBufferPropagateAttachments(source, out)
        var retained: [CVMetalTexture] = []
        for plane in 0..<2 {
            let scale = plane == 1 ? 2 : 1
            let w = width / scale, h = height / scale
            func texture(_ pb: CVPixelBuffer) throws -> MTLTexture {
                var ref: CVMetalTexture?
                guard CVMetalTextureCacheCreateTextureFromImage(nil, cache, pb, nil, plane == 0 ? .r8Unorm : .rg8Unorm,
                                                                w, h, plane, &ref) == kCVReturnSuccess,
                      let ref, let tex = CVMetalTextureGetTexture(ref) else { throw NSError(domain: "GazeSharpen", code: 6) }
                retained.append(ref); return tex
            }
            let src = try texture(source), dst = try texture(out)
            guard let encoder = command.makeComputeCommandEncoder() else { throw NSError(domain: "GazeSharpen", code: 7) }
            var params: [Float] = [Float(w), Float(h), Float(u), Float(v), Float(width) / Float(height),
                                   tuning.sharpRadius, tuning.softRadius, tuning.softenPixels / Float(scale)]
            encoder.setComputePipelineState(pipeline)
            encoder.setTexture(src, index: 0); encoder.setTexture(dst, index: 1)
            encoder.setBytes(&params, length: params.count * 4, index: 0)
            encoder.dispatchThreads(MTLSize(width: w, height: h, depth: 1),
                                    threadsPerThreadgroup: MTLSize(width: 16, height: 16, depth: 1))
            encoder.endEncoding()
        }
        command.commit(); command.waitUntilCompleted()
        withExtendedLifetime(retained) {}
        guard command.status == .completed else { throw NSError(domain: "GazeSharpen", code: 8) }
        return out
    }
}
