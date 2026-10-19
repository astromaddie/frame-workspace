import Foundation
import CoreVideo
import CoreMedia

@main enum FoveationTests {
    static func check(_ value: @autoclosure () -> Bool, _ name: String) {
        if !value() { fatalError(name) }
    }
    static func main() throws {
        gazePolicy()
        atlasPolicy()
        let packer = try FoveationPacker()
        try atlasPlaneEdges(packer)
        let folder = CommandLine.arguments.count > 1 ? URL(fileURLWithPath: CommandLine.arguments[1]) : nil
        if let folder { try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true) }
        let hardwareSized = CommandLine.arguments.contains("--hardware-sized")
        let atlas = CommandLine.arguments.contains("--atlas")
        let productionSized = CommandLine.arguments.contains("--production-sized")
        let fallbackChain = CommandLine.arguments.contains("--fallback-chain")
        let pictureCount = fallbackChain ? 180 : 40
        func layout(_ w: Int, _ h: Int, _ u: Double, _ v: Double) -> FoveationLayout {
            atlas ? .makeAtlas(w, h, u: u, v: v) : .make(w, h, u: u, v: v)
        }
        var records: [[String: Any]] = []
        var maximumPlaneError = 0
        for (index, item) in [(0.5, 0.5), (0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0), (0.25, 0.75), (0.75, 0.25)].enumerated() {
            for fullRange in [false, true] {
                if productionSized && (index != 0 || !fullRange) { continue }
                let w = productionSized ? 2880 : index == 6 ? (hardwareSized ? 768 : 512) : atlas && index == 5 ? 644 : 640
                let h = productionSized ? 2560 : index == 6 ? (hardwareSized ? 576 : 384) : atlas && index == 5 ? 484 : 480
                let source = try buffer(w, h, fullRange: fullRange, atlasPattern: atlas)
                let layout = layout(w, h, item.0, item.1)
                check(layout.warped && layout.encodedWidth * layout.encodedHeight < w * h, "pixel reduction")
                let compact = try packer.pack(source, layout: layout)
                let error = compare(source, compact, layout)
                maximumPlaneError = max(maximumPlaneError, error)
                check(error <= 1, "Metal packed samples differ from independent box/copy oracle")
                if let folder {
                    let id = records.count
                    let name = "frame-\(id)"
                    let encoder = Encoder(codec: .h264, fps: 30, bitsPerPixel: 0.5)
                    let done = DispatchSemaphore(value: 0)
                    var sequence = Data()
                    let callbackLock = NSLock()
                    encoder.onDone = { _ in done.signal() }
                    var expectedLayouts: [UInt32: FoveationLayout] = [:]
                    encoder.onFrame = { data, key, _, seq, ownLayout in
                        check(seq >= 1 && seq <= pictureCount && ownLayout == expectedLayouts[seq], "encoder callback layout association")
                        var packet = Data([key ? 0x81 : 0x80])
                        packet.append(Data(repeating: 0, count: 8))
                        var number = seq.bigEndian; packet.append(Data(bytes: &number, count: 4))
                        packet.append(Data(repeating: 0, count: 4)); packet.append(ownLayout!.wire); packet.append(data)
                        var size = UInt32(packet.count).bigEndian
                        callbackLock.lock()
                        sequence.append(Data(bytes: &size, count: 4)); sequence.append(packet)
                        if seq == 1 { check(key, "sequence starts at IDR") }
                        callbackLock.unlock()
                    }
                    // Production transitions: actual size change recreates VT at
                    // picture4; gaze movement at7 retains that reference chain.
                    // Never splice independently initialized same-size encoders:
                    // QCOM treats their repeated idr_pic_id as an invalid chain.
                    let initialW = w == 640 ? (hardwareSized ? 768 : 512) : 640, initialH = h == 480 ? (hardwareSized ? 576 : 384) : 480
                    let initialSource = try buffer(initialW, initialH, fullRange: fullRange, atlasPattern: atlas)
                    let otherUV = index == 1 ? 1.0 : 0.0
                    let initialLayout = atlas ? FoveationLayout.makeAtlas(initialW, initialH, u: otherUV, v: otherUV) : FoveationLayout.make(initialW, initialH, u: otherUV, v: otherUV)
                    let initialCompact = try packer.pack(initialSource, layout: initialLayout)
                    let movedLayout = atlas ? FoveationLayout.makeAtlas(w, h, u: otherUV, v: otherUV) : FoveationLayout.make(w, h, u: otherUV, v: otherUV)
                    let movedCompact = try packer.pack(source, layout: movedLayout)
                    for frame in 1...pictureCount {
                        let ownLayout: FoveationLayout
                        let ownCompact: CVPixelBuffer
                        if fallbackChain {
                            // Two settled seconds per full/atlas/full stage;
                            // ROI moves inside the same middle VT reference chain.
                            if frame <= 60 || frame > 120 { ownLayout = .full(w,h); ownCompact = source }
                            else if frame <= 90 { ownLayout = movedLayout; ownCompact = movedCompact }
                            else { ownLayout = layout; ownCompact = compact }
                        } else {
                            ownLayout = frame <= 3 ? initialLayout : frame <= 6 ? movedLayout : layout
                            ownCompact = frame <= 3 ? initialCompact : frame <= 6 ? movedCompact : compact
                        }
                        expectedLayouts[UInt32(frame)] = ownLayout
                        if frame == 21 { encoder.requestKeyFrame() }
                        check(encoder.encode(ownCompact, pts: CMTime(value: Int64(frame), timescale: 30), seq: UInt32(frame), layout: ownLayout), "VT submission")
                        check(done.wait(timeout: .now() + 5) == .success, "VT callback timeout")
                    }
                    encoder.invalidate()
                    callbackLock.lock(); let chain = sequence; callbackLock.unlock()
                    check(!chain.isEmpty, "no VT output")
                    try chain.write(to: folder.appendingPathComponent(name + ".sequence"))
                    records.append(["name": name, "width": w, "height": h, "encodedWidth": layout.encodedWidth,
                        "encodedHeight": layout.encodedHeight, "left": layout.left, "top": layout.top,
                        "fallbackChain": fallbackChain, "mode": atlas ? 2 : 1, "coreWidth": layout.coreWidth, "coreHeight": layout.coreHeight, "fullRange": fullRange])
                }
            }
        }
        if let folder { try JSONSerialization.data(withJSONObject: records, options: [.prettyPrinted, .sortedKeys]).write(to: folder.appendingPathComponent("fixtures.json")) }
        print("PASS Metal NV12 packing (atlas=\(atlas)): gaze positions/sizes and color ranges; maximum plane error \(maximumPlaneError)/255; focal luma and chroma exact")
        if folder != nil { print("PASS VideoToolbox H264 callback association: \(records.count) encoded fixtures") }
    }
    static func gazePolicy() {
        var g = FoveationGaze()
        let start: Int64 = 10_000_000
        func message(_ t: Int64, _ u: Double = 0.5) -> [String: Any] {
            ["version": 1, "session": "test", "valid": true, "source": "eye", "u": u, "v": 0.5, "time": t]
        }
        check(!g.layout(width: 640, height: 480, now: start).warped, "no gaze full quality")
        for step in 0...6 { g.receive(message(start + Int64(step) * 100_000), session: "test", now: start + Int64(step) * 100_000, allowDiagnostic: false) }
        check(g.layout(width: 640, height: 480, now: start + 600_000).warped, "stable valid gaze enables warp")
        check(!g.layout(width: 640, height: 480, now: start + 800_001).warped, "stale fallback")
        for step in 9...27 { g.receive(message(start + Int64(step) * 100_000), session: "test", now: start + Int64(step) * 100_000, allowDiagnostic: false) }
        check(!g.layout(width: 640, height: 480, now: start + 2_700_000).warped, "loss cooldown")
        g.receive(message(start + 2_900_000), session: "test", now: start + 2_900_000, allowDiagnostic: false)
        check(!g.layout(width: 640, height: 480, now: start + 2_900_000).warped, "sample gap restarts acquisition")
        for step in 30...35 { g.receive(message(start + Int64(step) * 100_000), session: "test", now: start + Int64(step) * 100_000, allowDiagnostic: false) }
        check(g.layout(width: 640, height: 480, now: start + 3_500_000).warped, "reacquisition")
        for invalid in [message(start + 3_600_000, .nan), message(start + 3_600_000, 1.1), message(start), message(start + 10_000_000), ["valid": false], ["session": "old"]] {
            g.receive(invalid, session: "test", now: start + 3_600_000, allowDiagnostic: false)
            check(!g.layout(width: 640, height: 480, now: start + 3_600_000).warped, "invalid sample fallback")
        }
        var synthetic = message(start + 3_700_000); synthetic["source"] = "diagnostic"
        g.receive(synthetic, session: "test", now: start + 3_700_000, allowDiagnostic: false)
        check(!g.layout(width: 640, height: 480, now: start + 3_700_000).warped, "no implicit diagnostic gaze")
        check(!FoveationLayout.make(638, 480, u: 0.5, v: 0.5).warped, "unaligned source fallback")
        var ordered = FoveationGaze()
        for step in 0...100 {
            let now = start + Int64(step) * 20_000
            var m = message(now + (step < 60 ? 20_000 : -20_000))
            m["sampleTime"] = 1_000_000 + step * 20_000
            ordered.receive(m, session: "test", now: now, allowDiagnostic: false)
            let l = ordered.layout(width: 640, height: 480, now: now)
            if step >= 25 { check(l.warped, "clock correction must not invalidate monotonically ordered gaze") }
        }
        var d = ordered.diagnostics["counts"] as! [String: Int]
        check(d["replay"] == 0 && d["fallbacks"] == 0 && d["activations"] == 1, "clock correction counters")
        var replay = message(start + 2_020_000); replay["sampleTime"] = 3_000_000
        ordered.receive(replay, session: "test", now: start + 2_020_000, allowDiagnostic: false)
        d = ordered.diagnostics["counts"] as! [String: Int]
        check(d["replay"] == 1 && d["fallbacks"] == 1, "native sample clock replay defense")
        var stale = message(start + 2_040_000 - 150_001); stale["sampleTime"] = 3_040_000
        ordered.receive(stale, session: "test", now: start + 2_040_000, allowDiagnostic: false)
        var future = message(start + 2_060_000 + 50_001); future["sampleTime"] = 3_060_000
        ordered.receive(future, session: "test", now: start + 2_060_000, allowDiagnostic: false)
        d = ordered.diagnostics["counts"] as! [String: Int]
        check(d["stale"] == 1 && d["future"] == 1, "unchanged host freshness bounds with native ordering")
        print("PASS gaze freshness, validity, session, cooldown and explicit diagnostic policy")
    }
    static func atlasPlaneEdges(_ packer: FoveationPacker) throws {
        for fullRange in [false,true] {
            let source = try buffer(644,484,fullRange:fullRange,atlasPattern:true)
            var base: Data?
            for (u,v) in [(0.0,0.0),(1.0,0.0),(0.0,1.0),(1.0,1.0),(0.5,0.5)] {
                let layout = FoveationLayout.makeAtlas(644,484,u:u,v:v)
                let packed = try packer.pack(source,layout:layout)
                check(compare(source,packed,layout) <= 1, "div4-not8 atlas seam/corner plane oracle")
                CVPixelBufferLockBaseAddress(packed,.readOnly)
                var current = Data()
                for plane in 0...1 {
                    let bytes = CVPixelBufferGetBaseAddressOfPlane(packed,plane)!
                    let stride = CVPixelBufferGetBytesPerRowOfPlane(packed,plane)
                    for row in 0..<(plane == 0 ? 242 : 121) {
                        current.append(Data(bytes:bytes.advanced(by:row*stride),count:322))
                    }
                }
                CVPixelBufferUnlockBaseAddress(packed,.readOnly)
                if let base { check(current == base, "base tile bytes must remain invariant under ROI movement") }
                else { base = current }
            }
        }
        print("PASS 644x484 atlas plane seams/corners: invariant base, byte-exact focal Y/UV, both ranges")
    }
    static func atlasPolicy() {
        for (w, h) in [(640,480), (644,484), (2880,2560)] {
            for step in 0...1000 {
                let u = Double(step) / 1000, v = 1-u
                let l = FoveationLayout.makeAtlas(w, h, u:u, v:v)
                check(l.atlas && l.encodedWidth == w && l.encodedHeight == h/2, "atlas geometry fixed during sweep")
                check(l.coreWidth == w/2 && l.coreHeight == h/2 && l.left%2 == 0 && l.top%2 == 0, "exact half core with NV12 alignment")
                check(u*Double(w) >= Double(l.left) && u*Double(w) <= Double(l.left+l.coreWidth) &&
                      v*Double(h) >= Double(l.top) && v*Double(h) <= Double(l.top+l.coreHeight), "sweep gaze remains in exact 1:1 ROI")
            }
            for (u,v) in [(0.0,0.0),(1.0,0.0),(0.0,1.0),(1.0,1.0)] {
                let l = FoveationLayout.makeAtlas(w,h,u:u,v:v)
                check(l.left == (u == 0 ? 0 : w/2) && l.top == (v == 0 ? 0 : h/2), "inclusive aligned atlas corners")
            }
        }
        check(!FoveationLayout.makeAtlas(638,480,u:0.5,v:0.5).warped, "unsupported atlas size falls back")
        var g = FoveationGaze()
        for step in 0...30 {
            let now:Int64 = 10_000_000 + Int64(step)*20_000
            g.receive(["version":1,"session":"a","valid":true,"source":"eye","u":0.75,"v":0.25,"time":now],session:"a",now:now,allowDiagnostic:false)
        }
        check(g.layout(width:644,height:484,now:10_600_000,atlas:true).atlas, "negotiated gaze selects atlas")
        check(!g.layout(width:644,height:484,now:10_750_001,atlas:true).warped, "atlas stale full fallback")
        print("PASS atlas sweep/corners/div4-not8 bounds, exact half geometry, mode selection and expiry")
    }
    static func buffer(_ w: Int, _ h: Int, fullRange: Bool, atlasPattern: Bool = false) throws -> CVPixelBuffer {
        var result: CVPixelBuffer?
        let attrs: [CFString: Any] = [kCVPixelBufferIOSurfacePropertiesKey: [:] as CFDictionary, kCVPixelBufferMetalCompatibilityKey: true]
        check(CVPixelBufferCreate(nil, w, h, fullRange ? kCVPixelFormatType_420YpCbCr8BiPlanarFullRange : kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
                                 attrs as CFDictionary, &result) == kCVReturnSuccess, "fixture buffer")
        let pb = result!
        CVBufferSetAttachment(pb, kCVImageBufferYCbCrMatrixKey, kCVImageBufferYCbCrMatrix_ITU_R_709_2, .shouldPropagate)
        CVPixelBufferLockBaseAddress(pb, [])
        for plane in 0...1 {
            let bytes = CVPixelBufferGetBaseAddressOfPlane(pb, plane)!.assumingMemoryBound(to: UInt8.self)
            let stride = CVPixelBufferGetBytesPerRowOfPlane(pb, plane)
            for y in 0..<(plane == 0 ? h : h / 2) {
                for x in 0..<w {
                    // Fine luma lines expose any focal resampling; colored blocks
                    // expose chroma phase, clipping and matrix/range mistakes.
                    bytes[y * stride + x] = plane == 0 ? UInt8(32 + (x * 71 + y * 43) % 192) : UInt8(64 + ((x / 48 + y / 32 + x % 2 * 3) % 5) * 32)
                    if atlasPattern && plane == 1 {
                        // Strongly opposed UV halves expose accidental filtering
                        // across atlas tile edges (base right versus ROI left).
                        bytes[y * stride + x] = UInt8(((x < w/2) == (x%2 == 0)) ? 32 : 224)
                    }
                }
            }
        }
        CVPixelBufferUnlockBaseAddress(pb, [])
        return pb
    }
    static func compare(_ source: CVPixelBuffer, _ compact: CVPixelBuffer, _ l: FoveationLayout) -> Int {
        CVPixelBufferLockBaseAddress(source, .readOnly); CVPixelBufferLockBaseAddress(compact, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(source, .readOnly); CVPixelBufferUnlockBaseAddress(compact, .readOnly) }
        var maximum = 0
        for plane in 0...1 {
            let scale = plane == 0 ? 1 : 2, channels = plane == 0 ? 1 : 2
            let src = CVPixelBufferGetBaseAddressOfPlane(source, plane)!.assumingMemoryBound(to: UInt8.self)
            let dst = CVPixelBufferGetBaseAddressOfPlane(compact, plane)!.assumingMemoryBound(to: UInt8.self)
            let ss = CVPixelBufferGetBytesPerRowOfPlane(source, plane), ds = CVPixelBufferGetBytesPerRowOfPlane(compact, plane)
            let a = l.left / scale, b = l.top / scale, cw = l.coreWidth / scale, ch = l.coreHeight / scale
            for y in 0..<(l.encodedHeight / scale) {
                let axisY = y < b / 2 ? y * 2 : (y < b / 2 + ch ? b + y - b / 2 : b + ch + (y - b / 2 - ch) * 2)
                let axisNY = y >= b / 2 && y < b / 2 + ch ? 1 : 2
                for x in 0..<(l.encodedWidth / scale) {
                    let axisX = x < a / 2 ? x * 2 : (x < a / 2 + cw ? a + x - a / 2 : a + cw + (x - a / 2 - cw) * 2)
                    let axisNX = x >= a / 2 && x < a / 2 + cw ? 1 : 2
                    let focal = x >= cw
                    let xx = l.atlas ? (focal ? a+x-cw : x*2) : axisX
                    let yy = l.atlas ? (focal ? b+y : y*2) : axisY
                    let nx = l.atlas ? (focal ? 1 : 2) : axisNX
                    let ny = l.atlas ? (focal ? 1 : 2) : axisNY
                    for c in 0..<channels {
                        var sum = 0
                        for dy in 0..<ny { for dx in 0..<nx { sum += Int(src[(yy + dy) * ss + (xx + dx) * channels + c]) } }
                        let expected = Int((Double(sum) / Double(nx * ny)).rounded())
                        let error = abs(expected - Int(dst[y * ds + x * channels + c]))
                        if nx == 1 && ny == 1 { check(error == 0, "1:1 focal plane must be byte exact") }
                        maximum = max(maximum, error)
                    }
                }
            }
        }
        return maximum
    }
}
