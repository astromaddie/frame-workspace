// FW-15 option B spike 2: fixture-only shared-display experiment, no mirroring or TCC prompts.
// From repo root (builds, snapshots and captures stay in the capsule's scratch directory):
//   work=/private/tmp/claude-501/-Users-mads-code-frame-workspace/d6cfb87f-3bf9-45b7-bf70-3ae621c211ed/scratchpad/b1
//   mkdir -p "$work"
//   xcrun swiftc -O -swift-version 5 -module-cache-path "$work/modules" \
//     mac/frame-mac-view/Diagnostics/SharedDisplayFixture.swift -o "$work/shared-display-fixture"
//   xcrun swiftc -O -swift-version 5 -module-cache-path "$work/modules" \
//     -import-objc-header mac/frame-mac-view/Sources/CGVirtualDisplay.h \
//     mac/frame-mac-view/Diagnostics/SharedDisplayProbe.swift -o "$work/shared-display-probe"
//   "$work/shared-display-probe" "$work" --preflight
//   "$work/shared-display-probe" "$work" > "$work/results.jsonl" 2> "$work/stderr.txt"
// Later targeted runs: --known-mode-only skips rejected sizes; --menus-only skips capture/load/input.
// Requires existing Screen Recording, Accessibility and event-posting grants; stops without prompting.
// Checks swap before each heavy step, restores fixture frames/cursor, removes its display/process,
// compares every other CGWindowList frame and display layout. SIGINT/SIGTERM use the same cleanup.
import AppKit
import ApplicationServices
import ScreenCaptureKit

let nsApp = NSApplication.shared // Required before CGVirtualDisplay/CGS calls.
nsApp.setActivationPolicy(.accessory)
let scratch = CommandLine.arguments[1]
let ownPID = ProcessInfo.processInfo.processIdentifier
let fixture = Process(), fixtureInput = Pipe()
var display: CGVirtualDisplay?
var fixturePID: pid_t = -1
var initialFixture: [[String: Any]] = []
var fixtureAX: [Int: AXUIElement] = [:]
var activeStreams: [SCStream] = []
var cleaning = false
var beforeWindows: [String: [String: Any]] = [:]
var beforeDisplays: [[String: Any]] = []
let originalCursor = CGEvent(source: nil)!.location
let start = ProcessInfo.processInfo.systemUptime
func now() -> Double { ProcessInfo.processInfo.systemUptime }
func emit(_ kind: String, _ fields: [String: Any] = [:]) {
    var obj = fields; obj["kind"] = kind; obj["elapsed"] = now() - start
    if let data = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys]),
       let line = String(data: data, encoding: .utf8) { print(line); fflush(stdout) }
}
func run(_ path: String, _ args: [String]) -> String {
    let p = Process(), pipe = Pipe(); p.executableURL = URL(fileURLWithPath: path)
    p.arguments = args; p.standardOutput = pipe; p.standardError = FileHandle.nullDevice
    do { try p.run(); let data = pipe.fileHandleForReading.readDataToEndOfFile(); p.waitUntilExit()
        return String(data: data, encoding: .utf8) ?? "" } catch { return "" }
}
func checkSwap(_ step: String) throws {
    let output = run("/usr/sbin/sysctl", ["vm.swapusage"])
    emit("swap", ["step": step, "value": output.trimmingCharacters(in: .whitespacesAndNewlines)])
    let parts = output.components(separatedBy: "used = ")
    guard parts.count == 2, let mb = Double(parts[1].prefix { $0.isNumber || $0 == "." }) else { throw Failure("cannot read swap") }
    if mb > 10 * 1024 { throw Failure("swap exceeds 10 GB; stopped") }
}
struct Failure: Error, CustomStringConvertible { let description: String; init(_ s: String) { description = s } }
func pause(_ seconds: Double) async { try? await Task.sleep(nanoseconds: UInt64(seconds * 1e9)) }
func rect(_ r: CGRect) -> [Double] { [r.minX, r.minY, r.width, r.height] }
func displayList() -> [[String: Any]] {
    var ids = [CGDirectDisplayID](repeating: 0, count: 32), n: UInt32 = 0
    CGGetOnlineDisplayList(32, &ids, &n)
    return ids.prefix(Int(n)).sorted().map { id in
        let mode = CGDisplayCopyDisplayMode(id)
        return ["id": id, "bounds": rect(CGDisplayBounds(id)), "main": CGDisplayIsMain(id),
            "mirror": CGDisplayMirrorsDisplay(id), "mode": [mode?.width ?? 0, mode?.height ?? 0,
            mode?.pixelWidth ?? 0, mode?.pixelHeight ?? 0], "hz": mode?.refreshRate ?? 0]
    }
}
func windowList() -> [String: [String: Any]] {
    let list = CGWindowListCopyWindowInfo(.optionAll, kCGNullWindowID) as? [[String: Any]] ?? []
    var result: [String: [String: Any]] = [:]
    for w in list {
        let pid = (w[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value ?? -1
        guard pid != ownPID && pid != fixturePID, let id = w[kCGWindowNumber as String] as? NSNumber else { continue }
        result[id.stringValue] = ["pid": pid, "owner": w[kCGWindowOwnerName as String] ?? "",
            "bounds": w[kCGWindowBounds as String] ?? [:], "layer": w[kCGWindowLayer as String] ?? 0]
    }
    return result
}
func jsonEqual(_ a: Any, _ b: Any) -> Bool {
    (try? JSONSerialization.data(withJSONObject: a, options: [.sortedKeys])) ==
    (try? JSONSerialization.data(withJSONObject: b, options: [.sortedKeys]))
}
func writeJSON(_ value: Any, _ name: String) {
    try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .prettyPrinted])
        .write(to: URL(fileURLWithPath: scratch + "/" + name), options: .atomic)
}
func state() -> [String: Any] {
    guard let data = try? Data(contentsOf: URL(fileURLWithPath: scratch + "/fixture.json")) else { return [:] }
    return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
}
func command(_ name: String, _ value: Int = 0) async {
    let seq = state()["sequence"] as? Int ?? -1
    if let data = try? JSONSerialization.data(withJSONObject: ["command": name, "value": value]) {
        fixtureInput.fileHandleForWriting.write(data + Data([10]))
    }
    for _ in 0..<30 { if (state()["sequence"] as? Int ?? -1) > seq { break }; await pause(0.05) }
}
func attr(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
    var value: CFTypeRef?; AXUIElementCopyAttributeValue(element, name as CFString, &value); return value
}
func axPoint(_ element: AXUIElement, _ key: String) -> CGPoint? {
    guard let value = attr(element, key), CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero; AXValueGetValue(value as! AXValue, .cgPoint, &point); return point
}
func setFrame(_ element: AXUIElement, _ frame: CGRect) -> [Int32] {
    var p = frame.origin, s = frame.size
    let resize = AXUIElementSetAttributeValue(element, kAXSizeAttribute as CFString, AXValueCreate(.cgSize, &s)!)
    let move = AXUIElementSetAttributeValue(element, kAXPositionAttribute as CFString, AXValueCreate(.cgPoint, &p)!)
    return [resize.rawValue, move.rawValue]
}
func cgFrame(_ w: [String: Any]) -> CGRect {
    let f = w["frameAppKit"] as! [Double]
    return CGRect(x: f[0], y: CGDisplayBounds(CGMainDisplayID()).height - f[1] - f[3], width: f[2], height: f[3])
}
func fixtureWindows() -> [[String: Any]] { state()["windows"] as? [[String: Any]] ?? [] }
func config(_ width: Int, _ height: Int) -> SCStreamConfiguration {
    let c = SCStreamConfiguration(); c.width = width; c.height = height
    c.pixelFormat = kCVPixelFormatType_32BGRA; c.minimumFrameInterval = CMTime(value: 1, timescale: 30)
    c.showsCursor = false; c.queueDepth = 3; c.includeChildWindows = true; c.ignoreShadowsSingleWindow = true
    return c
}
final class Frames: NSObject, SCStreamOutput, SCStreamDelegate {
    let lock = NSLock(), index: Int
    var count = 0, matches = 0, tested = 0, first = 0.0, last = 0.0
    var dimensions = [Int](), sampleColors = [[Int]](), contentRect = "", error = ""
    var image: CGImage?
    init(_ index: Int) { self.index = index }
    func stream(_ stream: SCStream, didStopWithError error: Error) {
        lock.lock(); self.error = error.localizedDescription; lock.unlock()
    }
    func stream(_ stream: SCStream, didOutputSampleBuffer sample: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .screen, sample.isValid,
            let info = (CMSampleBufferGetSampleAttachmentsArray(sample, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]])?.first,
            (info[.status] as? Int) == SCFrameStatus.complete.rawValue,
            let b = CMSampleBufferGetImageBuffer(sample) else { return }
        lock.lock(); defer { lock.unlock() }
        count += 1; last = now(); if first == 0 { first = last }
        guard dimensions.isEmpty else { return }
        let w = CVPixelBufferGetWidth(b), h = CVPixelBufferGetHeight(b)
        dimensions = [w, h]; contentRect = String(describing: info[.contentRect])
        CVPixelBufferLockBaseAddress(b, .readOnly); defer { CVPixelBufferUnlockBaseAddress(b, .readOnly) }
        guard let base = CVPixelBufferGetBaseAddress(b) else { return }
        let bytes = base.assumingMemoryBound(to: UInt8.self), row = CVPixelBufferGetBytesPerRow(b)
        for (x, y) in [(w/10, h/4), (9*w/10, h/4), (w/10, 3*h/4), (9*w/10, 3*h/4)] {
            let p = bytes.advanced(by: y*row+x*4); let rgb = [Int(p[2]), Int(p[1]), Int(p[0])]
            sampleColors.append(rgb)
            if index >= 0 {
                let expected = [40 + index*37%170, 50 + index*61%160, 60 + index*83%150]
                tested += 1; if zip(rgb, expected).allSatisfy({ abs($0-$1) <= 4 }) { matches += 1 }
            }
        }
        if index == 0 || index == -1 {
            let data = Data(bytes: base, count: row*h)
            if let provider = CGDataProvider(data: data as CFData) {
                image = CGImage(width: w, height: h, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: row,
                    space: CGColorSpace(name: CGColorSpace.sRGB)!, bitmapInfo: CGBitmapInfo(rawValue:
                        CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue),
                    provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent)
            }
        }
    }
    func report() -> [String: Any] {
        lock.lock(); defer { lock.unlock() }
        return ["index": index, "completeFrames": count, "fps": count > 1 ? Double(count-1)/(last-first) : 0,
            "dimensions": dimensions, "matchingIdentityPixels": matches, "testedIdentityPixels": tested,
            "rgb": sampleColors, "contentRect": contentRect, "error": error]
    }
}
func saveImage(_ img: CGImage?, _ name: String) {
    guard let img else { return }
    try? NSBitmapImageRep(cgImage: img).representation(using: .png, properties: [:])?
        .write(to: URL(fileURLWithPath: scratch + "/" + name))
}
func cpu(_ pid: pid_t) -> (Double, Int) {
    let text = run("/bin/ps", ["-p", String(pid), "-o", "time=,rss="]).split(whereSeparator: { $0.isWhitespace })
    guard text.count == 2 else { return (0,0) }
    let parts = text[0].split(separator: ":").compactMap { Double($0) }
    return (parts.reversed().enumerated().reduce(0) { $0 + $1.element * pow(60, Double($1.offset)) }, Int(text[1]) ?? 0)
}
func captures(_ n: Int, _ label: String) async throws {
    try checkSwap(label)
    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
    let ids = Set(fixtureWindows().prefix(n).compactMap { $0["id"] as? Int })
    let wins = content.windows.filter { $0.owningApplication?.processID == fixturePID && ids.contains(Int($0.windowID)) }
    guard wins.count == n else { throw Failure("only \(wins.count)/\(n) fixture windows shareable") }
    var outputs: [Frames] = []
    for w in wins {
        let index = fixtureWindows().first { ($0["id"] as? Int) == Int(w.windowID) }!["index"] as! Int
        let out = Frames(index), filter = SCContentFilter(desktopIndependentWindow: w)
        let s = SCStream(filter: filter, configuration: config(Int(w.frame.width*2), Int(w.frame.height*2)), delegate: out)
        try s.addStreamOutput(out, type: .screen, sampleHandlerQueue: DispatchQueue(label: "frame.\(index)"))
        activeStreams.append(s); outputs.append(out); try await s.startCapture()
        emit("window_capture", ["label": label, "index": index, "frame": rect(w.frame), "filterRect": rect(filter.contentRect)])
    }
    await pause(0.5)
    let wsPID = Int32(run("/usr/bin/pgrep", ["-x", "WindowServer"]).trimmingCharacters(in: .whitespacesAndNewlines)) ?? -1
    let pids = [ownPID, fixturePID, wsPID], baseline = pids.map(cpu), t = now()
    await pause(4)
    let elapsed = now()-t, final = pids.map(cpu)
    emit("resources", ["label": label, "seconds": elapsed, "processes": pids.enumerated().map { i, pid -> [String: Any] in
        ["pid": pid, "role": ["probe", "fixture", "WindowServer"][i],
         "cpuPercent": (final[i].0-baseline[i].0)/elapsed*100, "rssMiB": Double(final[i].1)/1024]
    }])
    for s in activeStreams { try await s.stopCapture() }; activeStreams.removeAll()
    emit("captures", ["label": label, "windows": outputs.map { $0.report() }])
    for o in outputs where o.index == 0 { saveImage(o.image, label + ".png") }
}
func post(_ type: CGEventType, _ p: CGPoint) { CGEvent(mouseEventSource: nil, mouseType: type,
    mouseCursorPosition: p, mouseButton: .left)?.post(tap: .cghidEventTap) }
func key(_ code: CGKeyCode, _ down: Bool) { CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: down)?.post(tap: .cghidEventTap) }
func click(_ p: CGPoint) async { CGWarpMouseCursorPosition(p); post(.mouseMoved, p); await pause(0.08)
    post(.leftMouseDown, p); await pause(0.06); post(.leftMouseUp, p); await pause(0.25) }
func captureStrip(_ id: CGDirectDisplayID, _ name: String) async throws {
    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
    guard let d = content.displays.first(where: { $0.displayID == id }) else { throw Failure("display not shareable") }
    let cfg = config(Int(CGDisplayBounds(id).width*2), 72); cfg.sourceRect = CGRect(x: 0, y: 0, width: CGDisplayBounds(id).width, height: 36)
    let img = try await SCScreenshotManager.captureImage(contentFilter: SCContentFilter(display: d, excludingWindows: []), configuration: cfg)
    saveImage(img, name + ".png"); emit("strip", ["name": name, "size": [img.width, img.height], "sourceRect": rect(cfg.sourceRect)])
}
func menuWindows(_ label: String, _ previous: Set<CGWindowID>, _ hidden: CGRect) async throws {
    try checkSwap(label)
    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
    let fixtureIDs = Set(fixtureWindows().compactMap { $0["id"] as? Int })
    // Menu/Control Center surfaces can reuse an already-listed SCWindow ID. Do not require a new ID.
    let candidates = content.windows.filter { w in w.isOnScreen && w.frame.intersects(hidden) &&
        (!previous.contains(w.windowID) || ((w.owningApplication?.processID == fixturePID ||
            w.owningApplication?.bundleIdentifier == "com.apple.controlcenter") && !fixtureIDs.contains(Int(w.windowID)) && w.frame.height > 30)) }
    emit("menu_windows", ["label": label, "candidates": candidates.map { w -> [String: Any] in
        ["id": w.windowID, "owner": w.owningApplication?.applicationName ?? "", "pid": w.owningApplication?.processID ?? -1,
         "layer": w.windowLayer, "frame": rect(w.frame), "title": w.title ?? ""]
    }])
    for w in candidates where (w.owningApplication?.processID == fixturePID || w.owningApplication?.bundleIdentifier == "com.apple.controlcenter") && w.frame.width > 30 && w.frame.height > 20 {
        // Display crop is a fallback when a transient menu is listed but cannot start its own stream.
        if let d = content.displays.first(where: { CGDisplayBounds($0.displayID) == hidden }) {
            let crop = w.frame.insetBy(dx:-8,dy:-8).intersection(hidden)
            let c = config(Int(crop.width*2),Int(crop.height*2))
            c.sourceRect = crop.offsetBy(dx:-hidden.minX,dy:-hidden.minY)
            let img = try await SCScreenshotManager.captureImage(contentFilter:SCContentFilter(display:d,excludingWindows:[]),configuration:c)
            saveImage(img,label+"-display-crop.png")
            emit("menu_crop",["label":label,"size":[img.width,img.height],"sourceRect":rect(c.sourceRect)])
        }
        let out = Frames(-1), s = SCStream(filter: SCContentFilter(desktopIndependentWindow: w),
            configuration: config(Int(w.frame.width*2), Int(w.frame.height*2)), delegate: out)
        try s.addStreamOutput(out, type: .screen, sampleHandlerQueue: DispatchQueue(label: "menu"))
        activeStreams.append(s)
        do { try await s.startCapture(); await pause(0.6); try await s.stopCapture()
            emit("menu_stream", ["label": label, "id": w.windowID, "result": out.report()]); saveImage(out.image, label + "-\(w.windowID).png")
        } catch { emit("menu_stream_error", ["label": label, "error": String(describing: error)]) }
        activeStreams.removeAll()
    }
}
func cleanup() async {
    guard !cleaning else { return }; cleaning = true
    for s in activeStreams { try? await s.stopCapture() }; activeStreams.removeAll()
    if fixture.isRunning {
        await command("count",20); await pause(0.15)
        // AX elements for ordered-out windows become invalid; rediscover after ordering them back in.
        let axApp = AXUIElementCreateApplication(fixturePID)
        for ax in attr(axApp,kAXWindowsAttribute) as? [AXUIElement] ?? [] {
            if let title = attr(ax,kAXTitleAttribute) as? String,
                let index = Int(title.replacingOccurrences(of:"Shared Display Fixture ",with:"")) { fixtureAX[index] = ax }
        }
        let results = initialFixture.map { w -> [Int32] in
            guard let i = w["index"] as? Int, let ax = fixtureAX[i] else { return [-1,-1] }
            return setFrame(ax,cgFrame(w))
        }
        await pause(0.25)
        let frames = fixtureWindows().map { $0["frameAppKit"] ?? [] }, originals = initialFixture.map { $0["frameAppKit"] ?? [] }
        emit("fixture_restored", ["results":results,"exact":jsonEqual(frames,originals),"frames":frames,"originals":originals])
        await command("quit"); await pause(0.3)
        if fixture.isRunning { fixture.terminate(); await pause(0.2) }
    }
    CGWarpMouseCursorPosition(originalCursor)
    let displayID = display?.displayID; display = nil
    for _ in 0..<50 { if displayID == nil || !displayList().contains(where: { ($0["id"] as? UInt32) == displayID }) { break }; await pause(0.1) }
    await pause(1)
    let after = windowList(), displays = displayList()
    var changed: [[String: Any]] = []
    for (id, before) in beforeWindows {
        if let item = after[id], !jsonEqual(before["bounds"]!, item["bounds"]!) {
            changed.append(["id": id, "owner": before["owner"] ?? "", "before": before["bounds"]!, "after": item["bounds"]!])
        }
    }
    emit("teardown", ["displayLayoutExact": jsonEqual(beforeDisplays, displays), "beforeDisplays": beforeDisplays,
        "afterDisplays": displays, "beforeWindows": beforeWindows.count, "commonWindows": beforeWindows.keys.filter { after[$0] != nil }.count,
        "changedFrames": changed, "missingWindows": beforeWindows.keys.filter { after[$0] == nil }.sorted().map { ["id":$0,"window":beforeWindows[$0]!] },
        "addedWindowIDs": after.keys.filter { beforeWindows[$0] == nil }.sorted(), "fixtureStillRunning": fixture.isRunning,
        "cursor": rect(CGRect(origin: CGEvent(source: nil)!.location, size: .zero))])
    writeJSON(after, "after-windows.json"); writeJSON(displays, "after-displays.json")
}
func experiment() async throws {
    emit("preflight", ["screenRecording": CGPreflightScreenCaptureAccess(), "accessibility": AXIsProcessTrusted(),
        "postEvents": CGPreflightPostEventAccess()])
    guard CGPreflightScreenCaptureAccess(), AXIsProcessTrusted(), CGPreflightPostEventAccess() else {
        throw Failure("permission preflight failed; no prompt requested; dependent steps stopped")
    }
    if CommandLine.arguments.contains("--preflight") { return }
    try checkSwap("display creation")
    beforeWindows = windowList(); beforeDisplays = displayList()
    writeJSON(beforeWindows, "before-windows.json"); writeJSON(beforeDisplays, "before-displays.json")
    emit("before", ["displays": beforeDisplays, "windows": beforeWindows.count,
        "separateSpacesPreference": run("/usr/bin/defaults", ["read", "com.apple.spaces", "spans-displays"])])
    var requested = CGSize.zero
    let candidates = CommandLine.arguments.contains("--known-mode-only") ? [CGSize(width: 3600, height: 2250)] :
        [CGSize(width: 3840, height: 2400), CGSize(width: 3840, height: 2304), CGSize(width: 3840, height: 2250),
         CGSize(width: 3840, height: 2160), CGSize(width: 3600, height: 2250), CGSize(width: 3200, height: 2000), CGSize(width: 2560, height: 1600)]
    for size in candidates {
        try checkSwap("display \(size)")
        let t = now(), descriptor = CGVirtualDisplayDescriptor()
        descriptor.queue = .main; descriptor.name = "Shared Display Spike"; descriptor.maxPixelsWide = UInt32(size.width*2)
        descriptor.maxPixelsHigh = UInt32(size.height*2); descriptor.sizeInMillimeters = CGSize(width: size.width*0.2646, height: size.height*0.2646)
        descriptor.vendorID = 0xF0C2; descriptor.productID = 0xB115; descriptor.serialNum = UInt32(ownPID)
        descriptor.terminationHandler = { _, _ in }
        display = CGVirtualDisplay(descriptor: descriptor)
        guard let d = display else { emit("display_rejected", ["size": [size.width,size.height]]); continue }
        let settings = CGVirtualDisplaySettings(); settings.hiDPI = 1
        settings.modes = [CGVirtualDisplayMode(width: UInt32(size.width*2), height: UInt32(size.height*2), refreshRate: 60)!,
                          CGVirtualDisplayMode(width: UInt32(size.width), height: UInt32(size.height), refreshRate: 60)!]
        let applied = d.apply(settings)
        emit("display_created", ["id": d.displayID, "requestedPoints": [size.width,size.height], "applied": applied, "seconds": now()-t])
        for attempt in 0..<40 {
            let m = CGDisplayCopyDisplayMode(d.displayID)
            if [0,4,14,24,39].contains(attempt) {
                let modes = CGDisplayCopyAllDisplayModes(d.displayID, [kCGDisplayShowDuplicateLowResolutionModes: true] as CFDictionary) as? [CGDisplayMode] ?? []
                emit("mode_observed", ["attempt": attempt, "current": [m?.width ?? 0,m?.height ?? 0,m?.pixelWidth ?? 0,m?.pixelHeight ?? 0],
                    "available": modes.map { [$0.width,$0.height,$0.pixelWidth,$0.pixelHeight] }, "displays": displayList(),
                    "screens": NSScreen.screens.map { ["name":$0.localizedName,"frame":rect($0.frame)] }])
            }
            if m?.width == Int(size.width), m?.height == Int(size.height), m?.pixelWidth == Int(size.width*2),
                m?.pixelHeight == Int(size.height*2), NSScreen.screens.contains(where: { ($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? UInt32) == d.displayID }) {
                requested = size; emit("display_ready", ["seconds": now()-t, "mode": [m!.width,m!.height,m!.pixelWidth,m!.pixelHeight], "hz": m!.refreshRate]); break
            }
            if [4,14,24].contains(attempt), let modes = CGDisplayCopyAllDisplayModes(d.displayID, [kCGDisplayShowDuplicateLowResolutionModes: true] as CFDictionary) as? [CGDisplayMode],
                let mode = modes.first(where: { $0.width == Int(size.width) && $0.height == Int(size.height) && $0.pixelWidth == Int(size.width*2) }) {
                var c: CGDisplayConfigRef?; if CGBeginDisplayConfiguration(&c) == .success, let c {
                    let a = CGConfigureDisplayWithDisplayMode(c, d.displayID, mode, nil), b = CGCompleteDisplayConfiguration(c, .forAppOnly)
                    emit("mode_retry", ["attempt": attempt, "configure": a.rawValue, "complete": b.rawValue, "seconds": now()-t])
                }
            }
            await pause(0.1)
        }
        if requested != .zero { break }; display = nil; await pause(0.5)
    }
    guard let id = display?.displayID, requested != .zero else { throw Failure("no requested HiDPI display became ready") }
    // Share a 128-point edge near the bottom-right; macOS snapped a one-point edge to a corner.
    let anchor = beforeDisplays.max { a,b in (a["bounds"] as! [Double])[1] + (a["bounds"] as! [Double])[3] < (b["bounds"] as! [Double])[1] + (b["bounds"] as! [Double])[3] }!["bounds"] as! [Double]
    var c: CGDisplayConfigRef?; guard CGBeginDisplayConfiguration(&c) == .success, let c else { throw Failure("begin placement failed") }
    let origin = CGPoint(x: anchor[0]+anchor[2]-128, y: anchor[1]+anchor[3])
    let placed = CGConfigureDisplayOrigin(c, id, Int32(origin.x), Int32(origin.y)), completed = CGCompleteDisplayConfiguration(c, .forAppOnly)
    await pause(0.3)
    let hidden = CGDisplayBounds(id)
    emit("placement", ["configure": placed.rawValue, "complete": completed.rawValue, "bounds": rect(hidden), "displays": displayList()])
    guard hidden.origin == origin, CGDisplayMirrorsDisplay(id) == kCGNullDirectDisplay else { throw Failure("hidden display placement/mirror mismatch") }
    try checkSwap("fixture launch")
    fixture.executableURL = URL(fileURLWithPath: scratch + "/shared-display-fixture"); fixture.arguments = [scratch + "/fixture.json"]
    try? FileManager.default.removeItem(atPath:scratch+"/fixture.json")
    fixture.standardInput = fixtureInput; fixture.standardOutput = FileHandle.nullDevice
    FileManager.default.createFile(atPath: scratch + "/fixture-stderr.txt", contents: nil)
    fixture.standardError = try FileHandle(forWritingTo: URL(fileURLWithPath: scratch + "/fixture-stderr.txt"))
    try fixture.run(); fixturePID = fixture.processIdentifier
    for _ in 0..<60 { if (state()["pid"] as? Int32) == fixturePID && fixtureWindows().count == 20 { break }; await pause(0.05) }
    initialFixture = fixtureWindows(); guard initialFixture.count == 20 else { throw Failure("fixture did not start") }
    let axApp = AXUIElementCreateApplication(fixturePID)
    let axes = attr(axApp, kAXWindowsAttribute) as? [AXUIElement] ?? []
    for ax in axes { if let title = attr(ax, kAXTitleAttribute) as? String, let index = Int(title.replacingOccurrences(of: "Shared Display Fixture ", with: "")) { fixtureAX[index] = ax } }
    // Ordered-out windows are not exposed by AX; order all briefly to discover them, then move only our fixtures.
    await command("count", 20)
    _ = NSRunningApplication(processIdentifier: fixturePID)?.activate(options: [])
    AXUIElementSetMessagingTimeout(axApp, 1)
    for attempt in 0..<30 {
        var value: CFTypeRef?
        let result = AXUIElementCopyAttributeValue(axApp, kAXWindowsAttribute as CFString, &value)
        let elements = value as? [AXUIElement] ?? []
        for ax in elements {
            if let title = attr(ax, kAXTitleAttribute) as? String, let index = Int(title.replacingOccurrences(of: "Shared Display Fixture ", with: "")) { fixtureAX[index] = ax }
        }
        if attempt == 0 || attempt == 29 { emit("ax_discovery", ["code":result.rawValue,"elements":elements.count,"titles":elements.map { attr($0,kAXTitleAttribute) as? String ?? "" }]) }
        if fixtureAX.count == 20 { break }; await pause(0.1)
    }
    guard fixtureAX.count == 20 else { throw Failure("only \(fixtureAX.count)/20 AX fixture windows") }
    let normal = CGRect(x: hidden.minX+80, y: hidden.minY+90, width: 640, height: 422)
    let moves = (0..<20).map { setFrame(fixtureAX[$0]!, normal) }
    emit("ax_moves", ["results": moves])
    guard moves.flatMap({ $0 }).allSatisfy({ $0 == 0 }) else { throw Failure("fixture AX move failed") }
    if !CommandLine.arguments.contains("--menus-only") {
      for n in [1,5,20] {
        await command("count", n); await pause(0.3)
        emit("fixture_frames", ["label": "\(n)-windows", "windows": fixtureWindows().prefix(n).map { ["index": $0["index"]!, "frame": rect(cgFrame($0))] }])
        try await captures(n, "\(n)-windows")
      }
      await command("count", 1)
      for size in [CGSize(width: 2400,height: 1500), CGSize(width: 3600,height: 2200)] {
        try checkSwap("large \(size)")
        let result = setFrame(fixtureAX[0]!, CGRect(origin: CGPoint(x:hidden.minX,y:hidden.minY+40), size: size)); await pause(0.3)
        emit("large_resize", ["requested": [size.width,size.height], "ax": result, "window": fixtureWindows()[0]])
        try await captures(1, "large-\(Int(size.width))")
      }
    }
    for i in 0..<20 { _ = setFrame(fixtureAX[i]!, normal) }
    await command("count", 5); await pause(0.3)
    try checkSwap("raise and input")
    let t = now(), raise = AXUIElementPerformAction(fixtureAX[0]!, kAXRaiseAction as CFString)
    let activated = NSRunningApplication(processIdentifier: fixturePID)?.activate(options: []) ?? false
    var ready = false
    for _ in 0..<100 { if (attr(axApp, kAXFocusedWindowAttribute).map { CFEqual($0,fixtureAX[0]!) } ?? false),
        NSWorkspace.shared.frontmostApplication?.processIdentifier == fixturePID { ready = true; break }; await pause(0.005) }
    let readyMs = (now()-t)*1000
    let p = CGPoint(x: normal.minX+200,y: normal.minY+200)
    if !CommandLine.arguments.contains("--menus-only") {
    await click(p)
    for _ in 0..<50 { if (state()["events"] as? [[String: Any]] ?? []).contains(where: { ($0["kind"] as? String) == "down" }) { break }; await pause(0.01) }
    let down = (state()["events"] as? [[String: Any]] ?? []).first { ($0["kind"] as? String) == "down" }
    post(.leftMouseDown, p); await pause(0.05)
    for step in 1...5 { post(.leftMouseDragged, CGPoint(x: p.x+Double(step*10),y: p.y+Double(step*5))); await pause(0.04) }
    post(.leftMouseUp, CGPoint(x:p.x+50,y:p.y+25)); key(0, true); key(0,false); key(11,true); key(11,false); await pause(0.3)
    emit("input", ["raiseCode": raise.rawValue, "activated": activated, "ready": ready, "raiseToAXReadyMs": readyMs,
        "raiseToFirstDownMs": ((down?["uptime"] as? Double ?? t)-t)*1000, "cursor": [CGEvent(source:nil)!.location.x,CGEvent(source:nil)!.location.y],
        "events": state()["events"] ?? []])
    } else { await click(p) }
    try checkSwap("menu capture")
    try await captureStrip(id, "menubar")
    let bar = attr(axApp, kAXMenuBarAttribute) as! AXUIElement
    let items = attr(bar, kAXChildrenAttribute) as? [AXUIElement] ?? []
    emit("menu_ax", ["items": items.map { ["title": attr($0,kAXTitleAttribute) as? String ?? "", "position": axPoint($0,kAXPositionAttribute).map { [$0.x,$0.y] } ?? []] }])
    let prior = Set(try await SCShareableContent.excludingDesktopWindows(false,onScreenWindowsOnly:false).windows.map(\.windowID))
    if let item = items.first(where: { (attr($0,kAXTitleAttribute) as? String) == "Probe" }), let point = axPoint(item,kAXPositionAttribute) {
        let x = point.x >= hidden.minX ? point.x-hidden.minX : point.x-CGDisplayBounds(CGMainDisplayID()).minX
        await click(CGPoint(x:hidden.minX+x+15,y:hidden.minY+12))
        try await captureStrip(id,"menubar-open"); try await menuWindows("app-menu",prior,hidden)
        key(53,true); key(53,false); await pause(0.2)
    } else { emit("menu_blocked",["reason":"fixture Probe menu has no AX position"]) }
    // Own status item exercises the same NSStatusBar path without opening any user's app window.
    // Its AX extras menu positions may be on the active display, so log them before choosing.
    if let extras = attr(axApp, "AXExtrasMenuBar") {
        let children = attr(extras as! AXUIElement,kAXChildrenAttribute) as? [AXUIElement] ?? []
        emit("status_ax",["items": children.map { ["title":attr($0,kAXTitleAttribute) as? String ?? "", "description":attr($0,kAXDescriptionAttribute) as? String ?? "", "position":axPoint($0,kAXPositionAttribute).map { [$0.x,$0.y] } ?? []] }])
        if let item = children.first, let point = axPoint(item,kAXPositionAttribute) {
            let main = CGDisplayBounds(CGMainDisplayID())
            let x = point.x >= hidden.minX ? point.x-hidden.minX : hidden.width-(main.maxX-point.x)
            let previous = Set(try await SCShareableContent.excludingDesktopWindows(false,onScreenWindowsOnly:false).windows.map(\.windowID))
            await click(CGPoint(x:hidden.minX+x+10,y:hidden.minY+12))
            try await captureStrip(id,"statusbar-open"); try await menuWindows("status-menu",previous,hidden)
            key(53,true); key(53,false); await pause(0.2)
        }
    } else { emit("status_blocked",["reason":"no AXExtrasMenuBar for owned status item"]) }
    // Also try the system Control Center item, which the capsule explicitly authorizes opening.
    if let cc = NSRunningApplication.runningApplications(withBundleIdentifier:"com.apple.controlcenter").first {
        let ccAX = AXUIElementCreateApplication(cc.processIdentifier)
        if let extras = attr(ccAX,"AXExtrasMenuBar") {
            let items = attr(extras as! AXUIElement,kAXChildrenAttribute) as? [AXUIElement] ?? []
            emit("system_status_ax",["items":items.map { ["title":attr($0,kAXTitleAttribute) as? String ?? "", "description":attr($0,kAXDescriptionAttribute) as? String ?? "", "identifier":attr($0,"AXIdentifier") as? String ?? "", "position":axPoint($0,kAXPositionAttribute).map { [$0.x,$0.y] } ?? []] }])
            if let item = items.first(where: { (attr($0,"AXIdentifier") as? String ?? "").localizedCaseInsensitiveContains("controlcenter") || (attr($0,kAXDescriptionAttribute) as? String ?? "").localizedCaseInsensitiveContains("control center") }), let point = axPoint(item,kAXPositionAttribute) {
                let main = CGDisplayBounds(CGMainDisplayID())
                let x = point.x >= hidden.minX && point.y >= hidden.minY ? point.x-hidden.minX : hidden.width-(main.maxX-point.x)
                let previous = Set(try await SCShareableContent.excludingDesktopWindows(false,onScreenWindowsOnly:false).windows.map(\.windowID))
                await click(CGPoint(x:hidden.minX+x+10,y:hidden.minY+12))
                try await menuWindows("control-center",previous,hidden)
                key(53,true); key(53,false); await pause(0.2)
            }
        }
    }
    writeJSON(state(),"fixture-final.json")
}
signal(SIGINT,SIG_IGN); signal(SIGTERM,SIG_IGN)
let signalSources = [SIGINT,SIGTERM].map { sig -> DispatchSourceSignal in
    let s = DispatchSource.makeSignalSource(signal:sig,queue:.main)
    s.setEventHandler { Task { await cleanup(); exit(130) } }; s.resume(); return s
}
Task { @MainActor in
    var failed = false
    do { try await experiment() } catch { failed = true; emit("failure",["error":String(describing:error)]) }
    if !beforeDisplays.isEmpty { await cleanup() }; exit(failed ? 1 : 0)
}
nsApp.run()
