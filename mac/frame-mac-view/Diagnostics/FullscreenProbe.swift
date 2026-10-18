// FW-06 feasibility probe. This is a separate diagnostic executable: it
// observes native full-screen and Space changes, and exercises the existing
// SCKSource(.window) path without moving or resizing any target window.
import AppKit
import ApplicationServices
import CoreGraphics
import CoreMedia
import Foundation
import ScreenCaptureKit

private struct ProbeOptions {
    var fixture = true
    var bundleID: String?
    var pid: pid_t?
    var requestedWindowID: CGWindowID?
    var sampleContent = false
    var seconds = 300.0

    var contentSamplingRequested: Bool { fixture || sampleContent }

    init(_ args: [String]) throws {
        var i = 0
        while i < args.count {
            let key = args[i]
            switch key {
            case "--fixture":
                fixture = true
                i += 1
            case "--sample-content":
                sampleContent = true
                i += 1
            case "--bundle-id", "--pid", "--window-id", "--seconds":
                guard i + 1 < args.count else { throw ProbeError("\(key) needs a value") }
                let value = args[i + 1]
                switch key {
                case "--bundle-id": bundleID = value; fixture = false
                case "--pid":
                    guard let n = Int32(value), n > 0 else { throw ProbeError("--pid must be positive") }
                    pid = n; fixture = false
                case "--window-id":
                    guard let n = UInt32(value), n > 0 else { throw ProbeError("--window-id must be positive") }
                    requestedWindowID = n
                default:
                    guard let n = Double(value), n.isFinite, (10...1800).contains(n) else {
                        throw ProbeError("--seconds must be between 10 and 1800")
                    }
                    seconds = n
                }
                i += 2
            case "--help", "-h":
                print("usage: fullscreen-probe [--fixture | --bundle-id ID | --pid PID] [--window-id CGWINDOWID] [--sample-content] [--seconds 300]")
                print("external --sample-content requires an explicit --window-id and emits only change/count fields")
                exit(0)
            default:
                throw ProbeError("unknown option \(key)")
            }
        }
        if fixture && (bundleID != nil || pid != nil) { throw ProbeError("choose one target mode") }
        if bundleID != nil && pid != nil { throw ProbeError("choose --bundle-id or --pid") }
        if sampleContent && !fixture && requestedWindowID == nil {
            throw ProbeError("--sample-content requires an explicit --window-id for external targets")
        }
    }
}

private struct ProbeError: Error, CustomStringConvertible {
    let description: String
    init(_ description: String) { self.description = description }
}

private final class FixtureView: NSView {
    var tick = 0 { didSet { needsDisplay = true } }

    override func draw(_ dirtyRect: NSRect) {
        NSColor(calibratedRed: 0.055, green: 0.075, blue: 0.11, alpha: 1).setFill()
        bounds.fill()
        let colors: [NSColor] = [.systemRed, .systemGreen, .systemBlue, .systemYellow]
        let block = NSRect(x: 32, y: 86, width: max(0, bounds.width - 64), height: max(0, bounds.height - 190))
        for (index, color) in colors.enumerated() {
            color.withAlphaComponent(0.72).setFill()
            NSRect(x: block.minX + CGFloat(index) * block.width / 4, y: block.minY,
                   width: block.width / 4, height: block.height).fill()
        }
        let pulseColors: [NSColor] = [.white, .black, .magenta, .cyan]
        pulseColors[tick % pulseColors.count].setFill()
        NSRect(x: bounds.midX - 120, y: bounds.midY - 120, width: 240, height: 240).fill()
        let heading = "FW-06 owned fixture · tick \(tick)"
        let instruction = "Use the standard green titlebar button. This window belongs to the probe."
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 23, weight: .semibold), .foregroundColor: NSColor.white
        ]
        (heading as NSString).draw(at: NSPoint(x: 32, y: bounds.height - 48), withAttributes: attrs)
        (instruction as NSString).draw(at: NSPoint(x: 32, y: 34), withAttributes: [
            .font: NSFont.systemFont(ofSize: 14), .foregroundColor: NSColor.white
        ])
    }
}

@main
private final class FullscreenProbe: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private let options: ProbeOptions
    private var application: NSApplication!
    private var window: NSWindow?
    private var fixtureView: FixtureView?
    private var targetPID: pid_t?
    private var targetAX: AXUIElement?
    private var axObserver: AXObserver?
    private var timer: Timer?
    private var spaceObserver: NSObjectProtocol?
    private var capture: SCKSource?
    private var selectedWindowID: CGWindowID?
    private var capturePermission = false
    private var accessibilityPermission = false
    private var contentSamplingReady = false
    private var captureStarted = false
    private var captureFrameCount = 0
    private var framesAtSpaceChange = 0
    private let contentSampleLock = NSLock()
    private var lastContentSampleAt: TimeInterval = 0
    private var previousContentHash: UInt64?
    private var distinctContentHashes = Set<UInt64>()
    private var contentSampleCount = 0
    private var lastSpaceChange: TimeInterval?
    private var lastSummaryAt: TimeInterval = 0
    private var lastAXStates: [String: String] = [:]
    private var observerInstalled = false
    private var axSubscriptionAttempts = 0
    private var axSubscriptionsAccepted = 0
    private var startedAt = ProcessInfo.processInfo.systemUptime
    private let dateFormatter = ISO8601DateFormatter()

    init(options: ProbeOptions) { self.options = options }

    static func main() {
        do {
            let probe = FullscreenProbe(options: try ProbeOptions(Array(CommandLine.arguments.dropFirst())))
            let app = NSApplication.shared
            probe.application = app
            app.setActivationPolicy(probe.options.fixture ? .regular : .accessory)
            app.delegate = probe
            app.run()
        } catch {
            FileHandle.standardError.write(Data("fullscreen-probe: \(error)\n".utf8))
            exit(2)
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        capturePermission = CGPreflightScreenCaptureAccess()
        accessibilityPermission = AXIsProcessTrusted()
        if options.fixture {
            targetPID = ProcessInfo.processInfo.processIdentifier
            makeFixture()
            if let window { selectedWindowID = UInt32(exactly: window.windowNumber) }
            contentSamplingReady = selectedWindowID != nil
        } else {
            targetPID = options.pid ?? NSWorkspace.shared.runningApplications.first(where: {
                $0.bundleIdentifier == options.bundleID
            })?.processIdentifier
            if let requested = options.requestedWindowID { selectedWindowID = requested }
            if options.sampleContent, let pid = targetPID, let id = selectedWindowID {
                if let owner = WindowInfo.find(id), owner.pid == pid {
                    contentSamplingReady = true
                    emit("content_sampling_enabled", ["windowID": id, "targetPID": pid,
                                                       "ownerTitle": owner.title,
                                                       "reason": "explicit window ID belongs to requested process"])
                } else {
                    emit("content_sampling_blocked", ["windowID": id, "targetPID": pid,
                                                       "reason": "explicit window ID is not listed for requested process"])
                }
            }
        }
        emit("started", [
            "targetPID": targetPID as Any? ?? NSNull(),
            "bundleID": options.bundleID as Any? ?? NSNull(),
            "fixture": options.fixture,
            "screenCapturePreflight": capturePermission,
            "accessibilityPreflight": accessibilityPermission,
            "capturePolicy": "preflight only; no TCC prompt is requested",
            "sampleContentRequested": options.contentSamplingRequested,
            "sampleContentReady": contentSamplingReady,
            "windowID": selectedWindowID as Any? ?? NSNull()
        ])
        guard targetPID != nil else {
            emit("target_unavailable", ["reason": "target application is not running"])
            application.terminate(nil)
            return
        }
        installSpaceObserver()
        installAXObserverIfAllowed()
        startCaptureIfReady()
        timer = Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            self?.sample()
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + options.seconds) { [weak self] in
            self?.emit("duration_elapsed", ["seconds": self?.options.seconds ?? 0])
            self?.application.terminate(nil)
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func windowDidEnterFullScreen(_ notification: Notification) {
        emitWindowEvent("native_fullscreen_enter")
        sample(force: true)
        inspectShareableContent()
    }

    func windowDidExitFullScreen(_ notification: Notification) {
        emitWindowEvent("native_fullscreen_exit")
        sample(force: true)
        inspectShareableContent()
    }

    func windowDidBecomeKey(_ notification: Notification) { emitWindowEvent("window_became_key") }
    func windowDidResignKey(_ notification: Notification) { emitWindowEvent("window_resigned_key") }
    func windowDidMiniaturize(_ notification: Notification) { emitWindowEvent("window_minimized") }
    func windowDidDeminiaturize(_ notification: Notification) { emitWindowEvent("window_restored") }
    func windowDidResize(_ notification: Notification) { emitWindowEvent("window_resized") }
    func windowDidMove(_ notification: Notification) { emitWindowEvent("window_moved") }

    func windowWillClose(_ notification: Notification) {
        emitWindowEvent("window_closed")
        if let capture { capture.stop() }
        application.terminate(nil)
    }

    private func makeFixture() {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 920, height: 620),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable],
                         backing: .buffered, defer: false)
        w.title = "FW-06 Native Fullscreen Fixture"
        w.collectionBehavior = [.fullScreenPrimary]
        w.delegate = self
        w.isReleasedWhenClosed = false
        let view = FixtureView(frame: w.contentView?.bounds ?? .zero)
        view.autoresizingMask = [.width, .height]
        w.contentView = view
        w.center()
        w.makeKeyAndOrderFront(nil)
        window = w
        fixtureView = view
        emit("fixture_created", ["windowID": UInt32(exactly: w.windowNumber) as Any? ?? NSNull(),
                                  "nativeGreenButton": true, "movedOrResizedByProbe": false])
        Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak view] _ in
            guard let view else { return }
            view.tick += 1
        }
    }

    private func installSpaceObserver() {
        spaceObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.activeSpaceDidChangeNotification, object: nil, queue: .main
        ) { [weak self] _ in
            guard let self else { return }
            let now = ProcessInfo.processInfo.systemUptime
            self.lastSpaceChange = now
            self.framesAtSpaceChange = self.captureFrameCount
            self.emit("activeSpaceDidChange", [
                "elapsedSeconds": now - self.startedAt,
                "windowID": self.selectedWindowID as Any? ?? NSNull(),
                "captureFrames": self.captureFrameCount,
                "windowInfoPresent": self.selectedWindowID.map { WindowInfo.find($0) != nil } as Any? ?? NSNull()
            ])
            self.inspectShareableContent()
            self.sample(force: true)
        }
    }

    private func installAXObserverIfAllowed() {
        guard accessibilityPermission, let pid = targetPID else {
            emit("accessibility_observer_unavailable", ["reason": "Accessibility preflight denied; no prompt requested"])
            return
        }
        let appElement = AXUIElementCreateApplication(pid)
        var observer: AXObserver?
        let result = AXObserverCreate(pid, { _, element, notification, context in
            guard let context else { return }
            let probe = Unmanaged<FullscreenProbe>.fromOpaque(context).takeUnretainedValue()
            probe.emit("ax_notification", ["name": notification as String,
                                           "elementHash": String(CFHash(element))])
            DispatchQueue.main.async { probe.sample(force: true) }
        }, &observer)
        guard result == .success, let observer else {
            emit("accessibility_observer_unavailable", ["reason": "AXObserverCreate returned \(result.rawValue)"])
            return
        }
        let context = Unmanaged.passUnretained(self).toOpaque()
        for name in [kAXWindowCreatedNotification, kAXFocusedWindowChangedNotification,
                     kAXMainWindowChangedNotification] {
            recordAXSubscription(observer, element: appElement, name: name, scope: "application", context: context)
        }
        var rawWindows: CFTypeRef?
        let windowsResult = AXUIElementCopyAttributeValue(appElement, kAXWindowsAttribute as CFString, &rawWindows)
        if windowsResult == .success, let windows = rawWindows as? [AXUIElement] {
            for element in windows {
                for name in [kAXMovedNotification, kAXResizedNotification, kAXUIElementDestroyedNotification,
                             kAXWindowMiniaturizedNotification, kAXWindowDeminiaturizedNotification] {
                    recordAXSubscription(observer, element: element, name: name, scope: "window", context: context)
                }
            }
        } else {
            emit("ax_window_enumeration_failed", ["resultCode": windowsResult.rawValue])
        }
        CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .defaultMode)
        targetAX = appElement
        axObserver = observer
        observerInstalled = true
        emit("accessibility_observer_ready", ["pid": pid, "fullScreenAttribute": "AXFullScreen (dynamic string; queried, not set)",
                                               "subscriptionAttempts": axSubscriptionAttempts,
                                               "subscriptionsAccepted": axSubscriptionsAccepted])
    }

    private func startCaptureIfReady() {
        guard !captureStarted, let id = selectedWindowID else { return }
        guard capturePermission else {
            captureStarted = true
            emit("capture_blocked", ["reason": "CGPreflightScreenCaptureAccess denied; no prompt requested", "windowID": id])
            return
        }
        captureStarted = true
        let source = SCKSource(.window(id))
        source.onFrame = { [weak self] pixelBuffer, pts, hostTime in
            let contentSample: [String: Any]?
            if let self, self.contentSamplingReady {
                contentSample = self.sampleFixtureContent(pixelBuffer, windowID: id)
            } else {
                contentSample = nil
            }
            DispatchQueue.main.async {
                guard let self else { return }
                self.captureFrameCount += 1
                if self.captureFrameCount == 1 || self.captureFrameCount % 30 == 0 {
                    self.emit("capture_frame", ["windowID": id, "frameCount": self.captureFrameCount,
                                                 "ptsSeconds": CMTimeGetSeconds(pts), "hostTimeMicros": hostTime])
                }
                if let contentSample { self.emit("capture_content_sample", contentSample) }
            }
        }
        source.onEnded = { [weak self, weak source] reason in
            // Mirror the production stream consumer: finish this source after
            // its first terminal callback, so its poll cannot emit a second end.
            source?.stop()
            DispatchQueue.main.async { self?.emit("capture_ended", ["windowID": id, "reason": reason]) }
        }
        source.onChange = { [weak self, weak source] in
            DispatchQueue.main.async {
                guard let self, let source else { return }
                self.emit("capture_source_changed", ["windowID": id, "frameRect": self.rectJSON(source.frameRect),
                                                     "title": source.title, "app": source.app])
            }
        }
        capture = source
        selectedWindowID = id
        source.start(maxLong: 1920, fps: 15) { [weak self] error in
            DispatchQueue.main.async {
                guard let self else { return }
                if let error {
                    self.emit("capture_start_failed", ["windowID": id, "error": error])
                } else {
                    self.emit("capture_started", ["windowID": id, "app": source.app, "title": source.title,
                                                   "frameRect": self.rectJSON(source.frameRect), "capturePath": "production SCKSource(.window)"])
                }
            }
        }
    }

    private func recordAXSubscription(_ observer: AXObserver, element: AXUIElement,
                                      name: String, scope: String, context: UnsafeMutableRawPointer) {
        let result = AXObserverAddNotification(observer, element, name as CFString, context)
        axSubscriptionAttempts += 1
        if result == .success { axSubscriptionsAccepted += 1 }
        emit("ax_notification_subscription", ["targetPID": targetPID as Any? ?? NSNull(),
                                                "scope": scope, "name": name,
                                                "resultCode": result.rawValue,
                                                "accepted": result == .success])
    }

    /// Hash a sparse luma grid only for the built-in fixture or an explicitly
    /// selected external window. No bytes or hash values are emitted in reports.
    private func sampleFixtureContent(_ buffer: CVPixelBuffer, windowID: CGWindowID) -> [String: Any]? {
        contentSampleLock.lock()
        defer { contentSampleLock.unlock() }
        let now = ProcessInfo.processInfo.systemUptime
        guard now - lastContentSampleAt >= 1 else { return nil }
        lastContentSampleAt = now
        guard CVPixelBufferLockBaseAddress(buffer, .readOnly) == kCVReturnSuccess else { return nil }
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }

        let planar = CVPixelBufferGetPlaneCount(buffer) > 0
        let width = planar ? CVPixelBufferGetWidthOfPlane(buffer, 0) : CVPixelBufferGetWidth(buffer)
        let height = planar ? CVPixelBufferGetHeightOfPlane(buffer, 0) : CVPixelBufferGetHeight(buffer)
        let rowBytes = planar ? CVPixelBufferGetBytesPerRowOfPlane(buffer, 0) : CVPixelBufferGetBytesPerRow(buffer)
        let base = planar ? CVPixelBufferGetBaseAddressOfPlane(buffer, 0) : CVPixelBufferGetBaseAddress(buffer)
        guard width >= 16, height >= 16, let base else { return nil }

        let bytes = base.assumingMemoryBound(to: UInt8.self)
        var hash: UInt64 = 14_695_981_039_346_656_037
        for gy in 0..<16 {
            let y = gy * (height - 1) / 15
            let row = bytes.advanced(by: y * rowBytes)
            for gx in 0..<16 {
                let x = gx * (width - 1) / 15
                hash = (hash ^ UInt64(row[x])) &* 1_099_511_628_211
            }
        }

        let changed = previousContentHash.map { $0 != hash } ?? false
        previousContentHash = hash
        contentSampleCount += 1
        if distinctContentHashes.count < 4096 { distinctContentHashes.insert(hash) }
        return ["windowID": windowID,
                "sampleNumber": contentSampleCount,
                "sampleChanged": changed,
                "distinctHashCount": distinctContentHashes.count,
                "distinctHashCountCapped": distinctContentHashes.count == 4096,
                "sampledLumaPixelCount": 256]
    }

    private func sample(force: Bool = false) {
        guard let pid = targetPID else { return }
        let now = ProcessInfo.processInfo.systemUptime
        if options.fixture, let window {
            let id = UInt32(exactly: window.windowNumber)
            selectedWindowID = id ?? selectedWindowID
            startCaptureIfReady()
        } else if options.requestedWindowID == nil {
            let candidates = axWindowSamples(pid: pid)
            if let candidate = candidates.first(where: { $0.fullScreen == true && $0.windowID != nil })
                ?? candidates.first(where: { $0.windowID != nil }) {
                if selectedWindowID != candidate.windowID {
                    selectedWindowID = candidate.windowID
                    captureStarted = false
                    capture?.stop()
                    capture = nil
                    startCaptureIfReady()
                }
            }
        }
        guard force || now - lastSummaryAt >= 1 else { return }
        lastSummaryAt = now
        let axRows = axWindowSamples(pid: pid)
        let sample: [String: Any] = [
            "elapsedSeconds": now - startedAt,
            "targetPID": pid,
            "windows": axRows.map(\.json),
            "windowID": selectedWindowID as Any? ?? NSNull(),
            "captureFrames": captureFrameCount,
            "windowInfoPresent": selectedWindowID.map { WindowInfo.find($0) != nil } as Any? ?? NSNull(),
            "appKitFullScreen": window.map { $0.styleMask.contains(.fullScreen) } as Any? ?? NSNull(),
            "appKitMiniaturized": window?.isMiniaturized as Any? ?? NSNull(),
            "framesSinceSpaceChange": lastSpaceChange.map { _ in captureFrameCount - framesAtSpaceChange } as Any? ?? NSNull(),
            "secondsSinceSpaceChange": lastSpaceChange.map { now - $0 } as Any? ?? NSNull(),
            "observerInstalled": observerInstalled
        ]
        emit("sample", sample)
    }

    private struct AXWindowSample {
        var title: String
        var fullScreen: Bool?
        var frame: CGRect?
        var windowID: CGWindowID?
        var mapping: String

        var json: [String: Any] {
            ["title": title, "axFullScreen": fullScreen as Any? ?? NSNull(),
             "frame": frame.map { ["x": $0.minX, "y": $0.minY, "width": $0.width, "height": $0.height] } as Any? ?? NSNull(),
             "windowID": windowID as Any? ?? NSNull(), "mapping": mapping]
        }
    }

    private func axWindowSamples(pid: pid_t) -> [AXWindowSample] {
        guard accessibilityPermission else { return [] }
        let app = targetAX ?? AXUIElementCreateApplication(pid)
        var raw: CFTypeRef?
        guard AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &raw) == .success,
              let elements = raw as? [AXUIElement] else { return [] }
        let cgRows = WindowInfo.all(onScreenOnly: false).filter { $0.pid == pid && $0.layer == 0 }
        return elements.map { element in
            var titleValue: CFTypeRef?, fullValue: CFTypeRef?
            _ = AXUIElementCopyAttributeValue(element, kAXTitleAttribute as CFString, &titleValue)
            let fullResult = AXUIElementCopyAttributeValue(element, "AXFullScreen" as CFString, &fullValue)
            let fullscreen: Bool? = fullResult == .success ? (fullValue as? NSNumber)?.boolValue : nil
            let title = titleValue as? String ?? ""
            let rect = axFrame(element)
            let candidates = rect.map { frame in cgRows.filter { sameRect($0.bounds, frame) } } ?? []
            let id = candidates.count == 1 ? candidates[0].id : nil
            let mapping = candidates.count == 1 ? "unique PID+frame match" : (candidates.isEmpty ? "unmapped" : "ambiguous")
            let sample = AXWindowSample(title: title, fullScreen: fullscreen, frame: rect, windowID: id, mapping: mapping)
            let state = "\(fullscreen.map(String.init) ?? "unknown")|\(id.map(String.init) ?? "none")|\(rect.map(rectKey) ?? "none")"
            let key = title.isEmpty ? String(CFHash(element)) : title
            if lastAXStates[key] != state {
                lastAXStates[key] = state
                emit("ax_fullscreen_state", ["title": title, "axFullScreen": fullscreen as Any? ?? NSNull(),
                                             "query": fullResult == .success ? "success" : "AXError \(fullResult.rawValue)",
                                             "windowID": id as Any? ?? NSNull(), "mapping": mapping,
                                             "frame": rect.map { ["x": $0.minX, "y": $0.minY, "width": $0.width, "height": $0.height] } as Any? ?? NSNull()])
            }
            return sample
        }
    }

    private func inspectShareableContent() {
        guard let id = selectedWindowID, capturePermission else { return }
        SCShareableContent.getExcludingDesktopWindows(true, onScreenWindowsOnly: false) { [weak self] content, error in
            DispatchQueue.main.async {
                guard let self else { return }
                let match = content?.windows.first(where: { $0.windowID == id })
                self.emit("shareable_content_probe", ["windowID": id, "windowIDPresent": match != nil,
                                                       "title": match?.title as Any? ?? NSNull(),
                                                       "frame": match.map { ["x": $0.frame.minX, "y": $0.frame.minY,
                                                                             "width": $0.frame.width, "height": $0.frame.height] } as Any? ?? NSNull(),
                                                       "error": error?.localizedDescription as Any? ?? NSNull()])
            }
        }
    }

    private func axFrame(_ element: AXUIElement) -> CGRect? {
        var positionValue: CFTypeRef?, sizeValue: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXPositionAttribute as CFString, &positionValue) == .success,
              AXUIElementCopyAttributeValue(element, kAXSizeAttribute as CFString, &sizeValue) == .success,
              let positionValue, let sizeValue,
              CFGetTypeID(positionValue) == AXValueGetTypeID(),
              CFGetTypeID(sizeValue) == AXValueGetTypeID() else { return nil }
        let position = unsafeBitCast(positionValue, to: AXValue.self)
        let size = unsafeBitCast(sizeValue, to: AXValue.self)
        var point = CGPoint.zero, dimensions = CGSize.zero
        guard AXValueGetValue(position, .cgPoint, &point), AXValueGetValue(size, .cgSize, &dimensions) else { return nil }
        return CGRect(origin: point, size: dimensions)
    }

    private func sameRect(_ a: CGRect, _ b: CGRect) -> Bool {
        abs(a.minX - b.minX) < 2 && abs(a.minY - b.minY) < 2 &&
        abs(a.width - b.width) < 2 && abs(a.height - b.height) < 2
    }

    private func rectKey(_ rect: CGRect) -> String {
        "\(Int(rect.minX)),\(Int(rect.minY)),\(Int(rect.width)),\(Int(rect.height))"
    }

    private func emitWindowEvent(_ event: String) {
        emit(event, ["windowID": window.flatMap { UInt32(exactly: $0.windowNumber) } as Any? ?? NSNull(),
                     "appKitFullScreen": window.map { $0.styleMask.contains(.fullScreen) } as Any? ?? NSNull(),
                     "miniaturized": window?.isMiniaturized as Any? ?? NSNull()])
    }

    private func rectJSON(_ rect: CGRect) -> [String: Double] {
        ["x": rect.minX, "y": rect.minY, "width": rect.width, "height": rect.height]
    }

    private func emit(_ event: String, _ fields: [String: Any]) {
        var row = fields
        row["event"] = event
        row["timestamp"] = dateFormatter.string(from: Date())
        if let data = try? JSONSerialization.data(withJSONObject: row, options: [.sortedKeys]) {
            FileHandle.standardOutput.write(data + Data("\n".utf8))
        }
    }
}
