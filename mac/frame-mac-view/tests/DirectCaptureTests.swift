import Foundation
import CoreVideo
import CoreMedia

@main enum DirectCaptureTests {
    static func check(_ ok: @autoclosure () -> Bool, _ name: String) { if !ok() { fatalError(name) } }
    static func main() throws {
        var gaze = FoveationGaze()
        let start: Int64 = 10_000_000
        let centre = FoveationLayout.makeDirect(644,484,u:0.5,v:0.5)
        check(gaze.layout(width:644,height:484,now:start,direct:true) == centre,"no gaze centre")
        for i in 0...30 {
            let t = start+Int64(i)*20_000
            gaze.receive(["version":1,"session":"a","valid":true,"source":"eye","u":1.0,"v":0.0,"time":t,"sampleTime":t],session:"a",now:t,allowDiagnostic:false)
        }
        check(gaze.layout(width:644,height:484,now:start+600_000,direct:true).left == 322,"fresh eye ROI")
        check(gaze.layout(width:644,height:484,now:start+750_001,direct:true) == centre,"stale centre")
        for bad in [["session":"wrong"], ["session":"a","version":1,"valid":false],
                    ["session":"a","version":1,"valid":true,"source":"eye","u":0.0,"v":0.0,"time":start+2_000_000]] as [[String:Any]] {
            gaze.receive(bad,session:"a",now:start+800_000,allowDiagnostic:false)
            check(gaze.layout(width:644,height:484,now:start+800_000,direct:true) == centre,"invalid centre")
        }
        for i in 0...1000 {
            let l = FoveationLayout.makeDirect(644,484,u:Double(i)/1000,v:1-Double(i)/1000)
            check(l.direct && !l.warped && !l.atlas && l.wire[1] == 3 && l.encodedWidth == 644 && l.encodedHeight == 484,"fixed direct geometry/wire")
            check(l.left%2 == 0 && l.top%2 == 0,"chroma alignment")
        }
        check(FoveationLayout.makeDirect(644,484,u:.nan,v:0) == centre,"bad UV centre")
        var policy = DirectCropPolicy()
        let initial = policy.make(centre)
        policy.observe(eyeActive:true)
        let eye = policy.make(.makeDirect(644,484,u:1,v:1))
        let successor = policy.make(.makeDirect(644,484,u:0,v:0))
        check(!policy.accepts(initial) && policy.accepts(eye) && policy.accepts(successor),"valid starts keep immutable placement")
        policy.observe(eyeActive:false)
        let fallback = policy.make(centre)
        check(!policy.accepts(eye) && !policy.accepts(successor) && policy.accepts(fallback),"late superseded eye callbacks rejected after loss")
        policy.observe(eyeActive:true)
        check(!policy.accepts(eye),"reacquisition cannot revive old epoch")
        for fullRange in [false,true] {
            let compositor = try DirectComposite()
            let base = try buffer(322,242,fullRange:fullRange,roi:false)
            let roi = try buffer(322,242,fullRange:fullRange,roi:true)
            for (u,v) in [(0.0,0.0),(1.0,0.0),(0.0,1.0),(1.0,1.0),(0.5,0.5)] {
                let l = FoveationLayout.makeDirect(644,484,u:u,v:v)
                let out = try compositor.compose(base:base,roi:roi,layout:l)
                check(CVPixelBufferGetWidth(out) == 644 && CVPixelBufferGetHeight(out) == 484,"actual fixed buffer")
                compare(base:base,roi:roi,out:out,layout:l)
            }
            let changedFormatBase = try buffer(322,242,fullRange:!fullRange,roi:false)
            let changedFormatROI = try buffer(322,242,fullRange:!fullRange,roi:true)
            do {
                _ = try compositor.compose(base:changedFormatBase,roi:changedFormatROI,layout:centre)
                fatalError("pool pixel format change should fail")
            } catch {}
            do {
                _ = try compositor.compose(base:base,roi:roi,layout:.makeDirect(640,480,u:0.5,v:0.5))
                fatalError("dimension adaptation should fail")
            } catch {}
        }
        print("PASS direct: fixed mode3 canvas; no/stale/invalid gaze centre; immutable generation loss/reentry; all NV12 luma/chroma samples at centre/corners in both ranges; dimension/format mismatch rejected")
    }
    static func buffer(_ w:Int,_ h:Int,fullRange:Bool,roi:Bool) throws -> CVPixelBuffer {
        var pb:CVPixelBuffer?
        check(CVPixelBufferCreate(nil,w,h,fullRange ? kCVPixelFormatType_420YpCbCr8BiPlanarFullRange : kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
            [kCVPixelBufferIOSurfacePropertiesKey:[:] as CFDictionary,kCVPixelBufferMetalCompatibilityKey:true] as CFDictionary,&pb) == kCVReturnSuccess,"buffer create")
        CVPixelBufferLockBaseAddress(pb!,[])
        for plane in 0...1 {
            let p = CVPixelBufferGetBaseAddressOfPlane(pb!,plane)!.assumingMemoryBound(to:UInt8.self), stride = CVPixelBufferGetBytesPerRowOfPlane(pb!,plane)
            for y in 0..<(h/(plane+1)) { for x in 0..<w {
                p[y*stride+x] = roi ? UInt8(32+(x*71+y*43)%192) : UInt8(40+(x*3+y*7)%160)
            } }
        }
        CVPixelBufferUnlockBaseAddress(pb!,[])
        return pb!
    }
    static func compare(base:CVPixelBuffer,roi:CVPixelBuffer,out:CVPixelBuffer,layout l:FoveationLayout) {
        for pb in [base,roi,out] { CVPixelBufferLockBaseAddress(pb,.readOnly) }
        defer { for pb in [base,roi,out] { CVPixelBufferUnlockBaseAddress(pb,.readOnly) } }
        for plane in 0...1 {
            let scale = plane+1, channels = plane+1
            let b = CVPixelBufferGetBaseAddressOfPlane(base,plane)!.assumingMemoryBound(to:UInt8.self)
            let r = CVPixelBufferGetBaseAddressOfPlane(roi,plane)!.assumingMemoryBound(to:UInt8.self)
            let o = CVPixelBufferGetBaseAddressOfPlane(out,plane)!.assumingMemoryBound(to:UInt8.self)
            let bs = CVPixelBufferGetBytesPerRowOfPlane(base,plane), rs = CVPixelBufferGetBytesPerRowOfPlane(roi,plane), os = CVPixelBufferGetBytesPerRowOfPlane(out,plane)
            let rw = l.coreWidth/scale, rh = l.coreHeight/scale, left = l.left/scale, top = l.top/scale
            for y in 0..<(l.height/scale) { for x in 0..<(l.width/scale) { for c in 0..<channels {
                if x>=left && x<left+rw && y>=top && y<top+rh {
                    check(o[y*os+x*channels+c] == r[(y-top)*rs+(x-left)*channels+c],"exact ROI luma/chroma placement including boundary")
                } else {
                    // Independent clamped bilinear upsampling oracle.
                    let fx = Double(x)/2-0.25, fy = Double(y)/2-0.25
                    let x0 = Int(floor(fx)), y0 = Int(floor(fy)), tx = fx-floor(fx), ty = fy-floor(fy)
                    func at(_ xx:Int,_ yy:Int)->Double { Double(b[max(0,min(rh-1,yy))*bs+max(0,min(rw-1,xx))*channels+c]) }
                    let expected = (at(x0,y0)*(1-tx)+at(x0+1,y0)*tx)*(1-ty)+(at(x0,y0+1)*(1-tx)+at(x0+1,y0+1)*tx)*ty
                    check(abs(Double(o[y*os+x*channels+c])-expected)<=1.01,"base ordinary-coordinate bilinear oracle")
                }
            } } }
        }
    }
}
