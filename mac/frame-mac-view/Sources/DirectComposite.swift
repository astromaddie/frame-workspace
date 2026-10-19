// Two independently captured half-size NV12 images, composited at ordinary
// screen positions. No full-resolution capture, packing, or viewer remapping.
import Foundation
import CoreVideo
import Metal

final class DirectComposite {
    private let device: MTLDevice, queue: MTLCommandQueue, pipeline: MTLComputePipelineState
    private var cache: CVMetalTextureCache!
    private var pool: CVPixelBufferPool?
    private var dimensions: (Int, Int, OSType)?
    init() throws {
        guard let d = MTLCreateSystemDefaultDevice(), let q = d.makeCommandQueue() else { throw Self.error() }
        device = d; queue = q
        let library = try d.makeLibrary(source: """
        #include <metal_stdlib>
        using namespace metal;
        kernel void composite(texture2d<float,access::sample> base [[texture(0)]],
                              texture2d<float,access::read> roi [[texture(1)]],
                              texture2d<float,access::write> out [[texture(2)]],
                              constant uint2 &origin [[buffer(0)]], uint2 gid [[thread_position_in_grid]]) {
            if(gid.x>=out.get_width() || gid.y>=out.get_height()) return;
            if(all(gid>=origin) && all(gid-origin<uint2(roi.get_width(),roi.get_height()))) {
                out.write(roi.read(gid-origin),gid);
            } else {
                constexpr sampler linearSampler(coord::normalized,address::clamp_to_edge,filter::linear);
                out.write(base.sample(linearSampler,(float2(gid)+0.5)/float2(out.get_width(),out.get_height())),gid);
            }
        }
        """, options: nil)
        pipeline = try d.makeComputePipelineState(function: library.makeFunction(name: "composite")!)
        guard CVMetalTextureCacheCreate(nil, nil, d, nil, &cache) == kCVReturnSuccess else { throw Self.error() }
    }
    private static func error() -> NSError { NSError(domain: "DirectCapture", code: 1, userInfo: [NSLocalizedDescriptionKey: "direct NV12 composition failed"]) }
    func compose(base: CVPixelBuffer, roi: CVPixelBuffer, layout l: FoveationLayout) throws -> CVPixelBuffer {
        let type = CVPixelBufferGetPixelFormatType(base)
        guard l.direct, l.encodedWidth == l.width, l.encodedHeight == l.height,
              l.left % 2 == 0, l.top % 2 == 0,
              type == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange || type == kCVPixelFormatType_420YpCbCr8BiPlanarFullRange,
              CVPixelBufferGetPixelFormatType(roi) == type,
              CVPixelBufferGetWidth(base) == l.width/2, CVPixelBufferGetHeight(base) == l.height/2,
              CVPixelBufferGetWidth(roi) == l.coreWidth, CVPixelBufferGetHeight(roi) == l.coreHeight else { throw Self.error() }
        if let dimensions {
            guard dimensions.0 == l.width && dimensions.1 == l.height && dimensions.2 == type else { throw Self.error() }
        } else {
            let attrs: [CFString: Any] = [kCVPixelBufferWidthKey: l.width, kCVPixelBufferHeightKey: l.height,
                kCVPixelBufferPixelFormatTypeKey: type, kCVPixelBufferMetalCompatibilityKey: true,
                kCVPixelBufferIOSurfacePropertiesKey: [:] as CFDictionary]
            guard CVPixelBufferPoolCreate(nil,nil,attrs as CFDictionary,&pool) == kCVReturnSuccess else { throw Self.error() }
            dimensions = (l.width,l.height,type)
        }
        var output: CVPixelBuffer?
        guard let pool, CVPixelBufferPoolCreatePixelBufferWithAuxAttributes(nil,pool,
            [kCVPixelBufferPoolAllocationThresholdKey: 6] as CFDictionary,&output) == kCVReturnSuccess,
            let output, let command = queue.makeCommandBuffer() else { throw Self.error() }
        CVBufferPropagateAttachments(base, output)
        var refs: [CVMetalTexture] = []
        for plane in 0...1 {
            let scale = plane == 0 ? 1 : 2
            func texture(_ pb: CVPixelBuffer) throws -> MTLTexture {
                var ref: CVMetalTexture?
                guard CVMetalTextureCacheCreateTextureFromImage(nil,cache,pb,nil,plane == 0 ? .r8Unorm : .rg8Unorm,
                    CVPixelBufferGetWidthOfPlane(pb,plane),CVPixelBufferGetHeightOfPlane(pb,plane),plane,&ref) == kCVReturnSuccess,
                    let ref, let tex = CVMetalTextureGetTexture(ref) else { throw Self.error() }
                refs.append(ref); return tex
            }
            let b = try texture(base), r = try texture(roi), out = try texture(output)
            guard let encoder = command.makeComputeCommandEncoder() else { throw Self.error() }
            var origin: [UInt32] = [UInt32(l.left/scale),UInt32(l.top/scale)]
            encoder.setComputePipelineState(pipeline)
            encoder.setTexture(b,index:0); encoder.setTexture(r,index:1); encoder.setTexture(out,index:2)
            encoder.setBytes(&origin,length:8,index:0)
            encoder.dispatchThreads(MTLSize(width:out.width,height:out.height,depth:1),threadsPerThreadgroup:MTLSize(width:16,height:16,depth:1))
            encoder.endEncoding()
        }
        command.commit(); command.waitUntilCompleted()
        withExtendedLifetime(refs) {}
        guard command.status == .completed else { throw Self.error() }
        return output
    }
}

// Immutable stream identity is the source-location proof. updateConfiguration
// completion is NOT a queue-drain barrier, and SCK's surface rectangles are not
// sourceRect echoes. A loss changes epoch; late old callbacks cannot resurrect it.
struct DirectCropGeneration: Equatable {
    let id: UInt64
    let epoch: UInt64
    let layout: FoveationLayout
}
struct DirectCropPolicy {
    private(set) var epoch: UInt64 = 0
    private var nextID: UInt64 = 0
    private var centred = true
    mutating func observe(eyeActive: Bool) {
        if centred == eyeActive { epoch &+= 1 }
        centred = !eyeActive
    }
    mutating func make(_ layout: FoveationLayout) -> DirectCropGeneration {
        nextID &+= 1
        return DirectCropGeneration(id: nextID, epoch: epoch, layout: layout)
    }
    func accepts(_ generation: DirectCropGeneration) -> Bool { generation.epoch == epoch }
}
