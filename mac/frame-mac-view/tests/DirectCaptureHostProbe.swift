// Disposable 644x484 display + owned pattern window. Never touches Workspace,
// user's windows, the installed companion or the headset. No gaze logs/images.
import AppKit
import ScreenCaptureKit
import CoreVideo
import CoreImage
import ImageIO
import UniformTypeIdentifiers

final class PatternView: NSView {
    static var animation = false, animateCentre = false, phase = 0
    override var isFlipped: Bool { true }
    override func draw(_ dirtyRect: NSRect) {
        for y in Swift.stride(from:0,to:484,by:12) { for x in Swift.stride(from:0,to:644,by:12) {
            let level = Self.level(x,y)
            NSColor(srgbRed:level,green:level,blue:level,alpha:1).setFill()
            NSRect(x:x,y:y,width:12,height:12).fill()
        } }
        if Self.animation {
            let level = Double(32+Self.phase%8*24)/255
            NSColor(srgbRed:level,green:level,blue:level,alpha:1).setFill()
            NSRect(x:0,y:0,width:120,height:484).fill()
            if Self.animateCentre { NSRect(x:274,y:194,width:96,height:96).fill() }
        }
    }
    static func level(_ x:Int,_ y:Int)->Double { let a = (x/12)*37; let b = (y/12)*53; return Double(48+(a+b)%160)/255 }
}
final class PeripheryView: NSView {
    override func draw(_ dirtyRect:NSRect) {
        let level = Double(32+PatternView.phase%8*24)/255
        NSColor(srgbRed:level,green:level,blue:level,alpha:1).setFill()
        bounds.fill()
    }
}
@main enum DirectCaptureHostProbe {
    static var display:CGVirtualDisplay?, window:NSWindow?, peripheryWindow:NSWindow?, source:DirectSCKSource?
    static var lastMovingOutput:Double = 0, maxMovingGap:Double = 0
    static var start:Double = 0, frames = 0, positions = Set<String>(), errors = 0, maxError = 0, centreAfterLoss = 0
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)
        guard CGPreflightScreenCaptureAccess() else { print("FAIL Screen Recording permission unavailable"); exit(2) }
        let descriptor = CGVirtualDisplayDescriptor()
        descriptor.queue = .main; descriptor.name = "Direct Capture Disposable Probe"
        descriptor.maxPixelsWide = 644; descriptor.maxPixelsHigh = 484
        descriptor.sizeInMillimeters = CGSize(width:170,height:128)
        descriptor.vendorID = 0xF0C2; descriptor.productID = 0xD1EC
        descriptor.serialNum = UInt32(ProcessInfo.processInfo.processIdentifier)
        descriptor.terminationHandler = { _,_ in }
        guard let d = CGVirtualDisplay(descriptor:descriptor) else { print("FAIL display create"); exit(2) }
        display = d
        let settings = CGVirtualDisplaySettings(); settings.hiDPI = 0
        settings.modes = [CGVirtualDisplayMode(width:644,height:484,refreshRate:60)!]
        guard d.apply(settings) else { print("FAIL display mode"); exit(2) }
        DispatchQueue.main.asyncAfter(deadline:.now()+1) { setup(d.displayID) }
        app.run()
    }
    static func setup(_ id:CGDirectDisplayID) {
        guard let screen = NSScreen.screens.first(where:{ ($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value == id }) else { finish("FAIL disposable screen missing",2); return }
        let w = NSWindow(contentRect:screen.frame,styleMask:.borderless,backing:.buffered,defer:false,screen:screen)
        w.level = .screenSaver; w.isOpaque = true; w.backgroundColor = .black
        w.setFrame(screen.frame,display:true)
        w.contentView = PatternView(frame:NSRect(x:0,y:0,width:644,height:484)); w.orderFrontRegardless(); window = w
        if CommandLine.arguments.contains("--hold") {
            print("DISPLAY \(id)"); fflush(stdout)
            DispatchQueue.global().async { while readLine() != nil {} ; DispatchQueue.main.async { finish("fixture removed",0) } }
            return
        }
        DispatchQueue.main.asyncAfter(deadline:.now()+0.5) {
            if CommandLine.arguments.contains("--continuity") { runContinuity(id) }
            else if CommandLine.arguments.contains("--snapshot") { runSnapshot(id) }
            else { runCapture(id) }
        }
    }
    static func runCapture(_ id:CGDirectDisplayID) {
        start = ProcessInfo.processInfo.systemUptime
        let s = DirectSCKSource(displayID:id); source = s
        s.selectLayout = { w,h in
            let t = ProcessInfo.processInfo.systemUptime-start
            let eye = t>1 && t<5
            // Continuous60Hz requested movement: immutable crops must still
            // deliver and never be pasted at the latest requested position.
            let u = eye ? 0.5+0.24*sin(t*3) : 0.5, v = eye ? 0.5+0.24*cos(t*2) : 0.5
            return (.makeDirect(w,h,u:u,v:v),eye)
        }
        s.onEnded = { reason in DispatchQueue.main.async { finish("FAIL \(reason)",2) } }
        s.onDirectFrame = { pb,_,_,l,_ in
            if frames == 0, CommandLine.arguments.contains("--image") {
                let ci = CIImage(cvPixelBuffer:pb)
                if let cg = CIContext().createCGImage(ci,from:ci.extent), let dest = CGImageDestinationCreateWithURL(URL(fileURLWithPath:"/tmp/frame-direct-probe.png") as CFURL,UTType.png.identifier as CFString,1,nil) {
                    CGImageDestinationAddImage(dest,cg,nil); CGImageDestinationFinalize(dest)
                }
            }
            guard CVPixelBufferGetWidth(pb) == 644, CVPixelBufferGetHeight(pb) == 484,
                  l.encodedWidth == 644, l.encodedHeight == 484 else { fatalError("direct geometry changed under adaptation") }
            frames += 1; positions.insert("\(l.left):\(l.top)")
            let elapsed = ProcessInfo.processInfo.systemUptime-start
            if elapsed>1.5 && elapsed<4.8 {
                if lastMovingOutput>0 { maxMovingGap = max(maxMovingGap,elapsed-lastMovingOutput) }
                lastMovingOutput = elapsed
            }
            if elapsed>5.0 && l == .makeDirect(644,484,u:0.5,v:0.5) { centreAfterLoss += 1 }
            // Check multiple points in the exact crop against the drawn source.
            // Stay off12px block boundaries and system menu/cursor areas.
            CVPixelBufferLockBaseAddress(pb,.readOnly)
            let bytes = CVPixelBufferGetBaseAddressOfPlane(pb,0)!.assumingMemoryBound(to:UInt8.self)
            let stride = CVPixelBufferGetBytesPerRowOfPlane(pb,0)
            for y in strideFrom(l.top+24,to:l.top+l.coreHeight-12,by:24) {
                for x in strideFrom(l.left+24,to:l.left+l.coreWidth-12,by:24) {
                    let xx = x/12*12+6, yy = y/12*12+6
                    let expected = Int((16+219*PatternView.level(xx,yy)).rounded())
                    let delta = abs(Int(bytes[yy*stride+xx])-expected)
                    maxError = max(maxError,delta)
                    if delta>4 { errors += 1 }
                }
            }
            CVPixelBufferUnlockBaseAddress(pb,.readOnly)
        }
        s.start(maxLong:644,fps:60) { error in if let error { DispatchQueue.main.async { finish("FAIL \(error)",2) } } }
        DispatchQueue.main.asyncAfter(deadline:.now()+2) { s.setMaxLong(320) }
        DispatchQueue.main.asyncAfter(deadline:.now()+3) { s.setMaxLong(4096) }
        DispatchQueue.main.asyncAfter(deadline:.now()+7) {
            let metrics:[String:Any] = ["frames":frames,"distinctCropPositions":positions.count,"centreFramesAfterLoss":centreAfterLoss,
                "maxMovingGazeOutputGapMs":maxMovingGap*1000,"cropPixelErrors":errors,"maximumLumaError":maxError,"capture":s.diagnostics]
            let data = try! JSONSerialization.data(withJSONObject:metrics,options:[.sortedKeys])
            print(String(data:data,encoding:.utf8)!)
            if let i = CommandLine.arguments.firstIndex(of:"--report"), CommandLine.arguments.count>i+1 {
                try! data.write(to:URL(fileURLWithPath:CommandLine.arguments[i+1]))
            }
            finish(frames>10 && positions.count>5 && centreAfterLoss>0 && errors == 0 ? "PASS actual immutable SCK crop provenance/continuous movement/centre loss" : "FAIL host crop verification",errors == 0 && frames>10 && positions.count>5 && centreAfterLoss>0 ? 0 : 1)
        }
    }
    static func saveReport(_ metrics:[String:Any]) {
        let data = try! JSONSerialization.data(withJSONObject:metrics,options:[.sortedKeys])
        print(String(data:data,encoding:.utf8)!); fflush(stdout)
        if let i = CommandLine.arguments.firstIndex(of:"--report"), CommandLine.arguments.count>i+1 {
            try! data.write(to:URL(fileURLWithPath:CommandLine.arguments[i+1]))
        }
    }
    static func runContinuity(_ id:CGDirectDisplayID) {
        let useBaseline = CommandLine.arguments.contains("--baseline")
        let capture:CaptureSource = useBaseline ? SCKSource(.display(id)) : DirectSCKSource(displayID:id)
        let separatePeriphery = CommandLine.arguments.contains("--separate-periphery")
        if separatePeriphery, let background = window {
            let rect = NSRect(x:background.frame.minX,y:background.frame.minY,width:120,height:484)
            let band = NSWindow(contentRect:rect,styleMask:.borderless,backing:.buffered,defer:false)
            band.setFrame(rect,display:true); band.level = .screenSaver; band.isOpaque = true
            band.contentView = PeripheryView(frame:NSRect(x:0,y:0,width:120,height:484))
            band.orderFrontRegardless(); peripheryWindow = band
        }
        let start = nowUs()
        let statsLock = NSLock()
        var counts:[String:Int] = [:], stampAges:[String:[Double]] = [:]
        var output:[(Double,UInt8,UInt8)] = []
        if let direct = capture as? DirectSCKSource {
            source = direct
            direct.selectLayout = { w,h in (.makeDirect(w,h,u:0.5,v:0.5),false) }
            direct.onSampleDiagnostic = { role,status,valid,stamp,pts in
                statsLock.lock(); defer { statsLock.unlock() }
                let elapsed = Double(nowUs()-start)/1_000_000
                let stage = elapsed<1 ? "static" : elapsed<4 ? "periphery" : "both"
                let key = "\(stage):\(role):\(status):valid=\(valid)"
                counts[key,default:0] += 1
                if let stamp { stampAges[key,default:[]].append(Double(nowUs()-stamp)/1000) }
                stampAges[key+":pts",default:[]].append(Double(nowUs()-pts)/1000)
            }
        }
        let received:(CVPixelBuffer,CMTime,Int64)->Void = { pb,_,_ in
            CVPixelBufferLockBaseAddress(pb,.readOnly)
            let bytes = CVPixelBufferGetBaseAddressOfPlane(pb,0)!.assumingMemoryBound(to:UInt8.self)
            let stride = CVPixelBufferGetBytesPerRowOfPlane(pb,0)
            let periphery = bytes[242*stride+48], centre = bytes[242*stride+322]
            CVPixelBufferUnlockBaseAddress(pb,.readOnly)
            statsLock.lock(); output.append((Double(nowUs()-start)/1_000_000,periphery,centre)); statsLock.unlock()
        }
        if let direct = capture as? DirectSCKSource { direct.onDirectFrame = { pb,pts,shown,_,_ in received(pb,pts,shown) } }
        else { capture.onFrame = received }
        capture.onEnded = { reason in DispatchQueue.main.async { finish("FAIL continuity capture: \(reason)",2) } }
        capture.start(maxLong:644,fps:60) { error in if let error { DispatchQueue.main.async { finish("FAIL continuity startup: \(error)",2) } } }
        let animation = Timer.scheduledTimer(withTimeInterval:0.05,repeats:true) { _ in
            let t = Double(nowUs()-start)/1_000_000
            PatternView.animation = t>1
            PatternView.animateCentre = t>4
            PatternView.phase += 1
            if let view = window?.contentView {
                if separatePeriphery { peripheryWindow?.contentView?.needsDisplay = true }
                else { view.setNeedsDisplay(NSRect(x:0,y:0,width:120,height:484)) }
                if PatternView.animateCentre { view.setNeedsDisplay(NSRect(x:274,y:194,width:96,height:96)) }
            }
        }
        DispatchQueue.main.asyncAfter(deadline:.now()+7) {
            animation.invalidate(); capture.stop()
            statsLock.lock(); let frames = output, observations = counts, ages = stampAges; statsLock.unlock()
            func stage(_ lo:Double,_ hi:Double)->[String:Any] {
                let rows = frames.filter { $0.0>=lo && $0.0<hi }
                let gaps = zip(rows,rows.dropFirst()).map { ($1.0-$0.0)*1000 }
                return ["outputs":rows.count,"peripheryValues":Set(rows.map{$0.1}).count,"centreValues":Set(rows.map{$0.2}).count,
                        "maxOutputGapMs":gaps.max() ?? 0,"firstOutputSeconds":rows.first?.0 ?? -1,"lastOutputSeconds":rows.last?.0 ?? -1]
            }
            let staticCentre = stage(1.2,3.8), animatedCentre = stage(4.2,6.8)
            var ageSummary:[String:Any] = [:]
            for (key,values) in ages { ageSummary[key] = ["minMs":values.min() ?? 0,"maxMs":values.max() ?? 0] }
            saveReport(["baseline":useBaseline,"separatePeripheryWindow":separatePeriphery,"staticCentreAnimatedPeriphery":staticCentre,"animatedBoth":animatedCentre,
                        "sampleCounts":observations,"sampleAge":ageSummary,"capture":source?.diagnostics ?? [:]])
            let fixedCentre = UInt8((16+219*PatternView.level(322,242)).rounded())
            let centreExact = frames.filter { $0.0>=1.2 && $0.0<3.8 }.allSatisfy { abs(Int($0.2)-Int(fixedCentre))<=1 }
            // All eight submitted phases must reach the periphery while the
            // focal pixels remain exact and static. Then all eight phases
            // must reach the changing centre too. Late-stage delivery rules
            // out a source that emits only initial frames and then freezes.
            // The production150ms contract bounds the timestamp difference
            // of each emitted base/ROI pair, not OS callback/output cadence.
            // Keep observed output gaps in the report as latency evidence.
            let withinBudget = useBaseline || (source?.diagnostics["maxEmittedSkewMs"] as? Double ?? .infinity)<=150
            let passed = withinBudget && staticCentre["peripheryValues"] as? Int == 8 && staticCentre["centreValues"] as? Int == 1 && centreExact &&
                animatedCentre["peripheryValues"] as? Int == 8 && animatedCentre["centreValues"] as? Int == 8 &&
                (staticCentre["lastOutputSeconds"] as? Double ?? 0)>3.5 && (animatedCentre["lastOutputSeconds"] as? Double ?? 0)>6.5
            finish(passed ? "PASS static centre / animated periphery continuity and animated centre control" : "FAIL capture continuity or centre pixels",passed ? 0 : 1)
        }
    }
    static func runSnapshot(_ id:CGDirectDisplayID) {
        SCShareableContent.getExcludingDesktopWindows(true,onScreenWindowsOnly:false) { content,error in
            guard let display = content?.displays.first(where:{$0.displayID == id}) else {
                return DispatchQueue.main.async { finish("FAIL snapshot display",2) }
            }
            let filter = SCContentFilter(display:display,excludingWindows:[])
            let queue = DispatchQueue(label:"direct-snapshot-probe")
            var latencies:[Double] = [], displayAges:[Double] = [], ptsAges:[Double] = []
            var formats = Set<UInt32>(), dimensions = Set<String>(), errors = 0, maxError = 0, statuses:[String:Int] = [:]
            func next(_ index:Int) {
                let positions = [(0.0,0.0),(1.0,0.0),(0.0,1.0),(1.0,1.0),(0.5,0.5)]
                let uv = positions[index%positions.count], l = FoveationLayout.makeDirect(644,484,u:uv.0,v:uv.1)
                let config = SCStreamConfiguration(); config.width = 322; config.height = 242
                config.sourceRect = CGRect(x:l.left,y:l.top,width:l.coreWidth,height:l.coreHeight)
                config.pixelFormat = kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange
                config.colorMatrix = CGDisplayStream.yCbCrMatrix_ITU_R_709_2; config.colorSpaceName = CGColorSpace.sRGB
                config.showsCursor = false; config.scalesToFit = true; config.preservesAspectRatio = true
                let request = nowUs()
                SCScreenshotManager.captureSampleBuffer(contentFilter:filter,configuration:config) { sample,error in
                    queue.async {
                        guard let sample, let pb = CMSampleBufferGetImageBuffer(sample), error == nil else {
                            return DispatchQueue.main.async { finish("FAIL screenshot: \(String(describing:error))",2) }
                        }
                        let completed = nowUs(); latencies.append(Double(completed-request)/1000)
                        formats.insert(CVPixelBufferGetPixelFormatType(pb))
                        dimensions.insert("\(CVPixelBufferGetWidth(pb))x\(CVPixelBufferGetHeight(pb))")
                        let info = (CMSampleBufferGetSampleAttachmentsArray(sample,createIfNecessary:false) as? [[SCStreamFrameInfo:Any]])?.first
                        statuses[String(describing:info?[.status]),default:0] += 1
                        if let raw = info?[.displayTime] as? UInt64 { displayAges.append(Double(completed-hostUs(raw))/1000) }
                        ptsAges.append(Double(completed-hostUs(CMSampleBufferGetPresentationTimeStamp(sample)))/1000)
                        guard CVPixelBufferGetPixelFormatType(pb) == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
                              CVPixelBufferGetWidth(pb) == 322, CVPixelBufferGetHeight(pb) == 242 else {
                            return DispatchQueue.main.async { finish("FAIL screenshot format/dimensions",2) }
                        }
                        CVPixelBufferLockBaseAddress(pb,.readOnly)
                        let bytes = CVPixelBufferGetBaseAddressOfPlane(pb,0)!.assumingMemoryBound(to:UInt8.self)
                        let stride = CVPixelBufferGetBytesPerRowOfPlane(pb,0)
                        for y in Swift.stride(from:24,to:230,by:24) { for x in Swift.stride(from:24,to:310,by:24) {
                            let gx = (x+l.left)/12*12+6, gy = (y+l.top)/12*12+6
                            let expected = Int((16+219*PatternView.level(gx,gy)).rounded())
                            let delta = abs(Int(bytes[(gy-l.top)*stride+gx-l.left])-expected)
                            maxError = max(maxError,delta); if delta>4 { errors += 1 }
                        } }
                        CVPixelBufferUnlockBaseAddress(pb,.readOnly)
                        if index<59 { return queue.asyncAfter(deadline:.now()+1.0/60) { next(index+1) } }
                        func summary(_ values:[Double])->[String:Double] {
                            let sorted = values.sorted(); guard !sorted.isEmpty else { return [:] }
                            return ["minMs":sorted.first!,"p50Ms":sorted[sorted.count/2],"p95Ms":sorted[min(sorted.count-1,Int(Double(sorted.count)*0.95))],"maxMs":sorted.last!]
                        }
                        DispatchQueue.main.async {
                            saveReport(["requests":latencies.count,"latency":summary(latencies),"displayAge":summary(displayAges),"ptsAge":summary(ptsAges),
                                "formats":formats.sorted(),"dimensions":dimensions.sorted(),"statuses":statuses,"pixelErrors":errors,"maxLumaError":maxError])
                            finish(errors == 0 ? "PASS immutable cropped NV12 screenshot provenance" : "FAIL screenshot pixels",errors == 0 ? 0 : 1)
                        }
                    }
                }
            }
            queue.async { next(0) }
        }
    }
    static func strideFrom(_ from:Int,to:Int,by:Int)->StrideTo<Int> { Swift.stride(from:from,to:to,by:by) }
    static func finish(_ text:String,_ code:Int32) {
        print(text); fflush(stdout); source?.stop(); peripheryWindow?.close(); peripheryWindow = nil; window?.close(); window = nil
        DispatchQueue.main.asyncAfter(deadline:.now()+0.3) { display = nil; exit(code) }
    }
}
