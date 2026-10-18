// A foreground owner for one extended Frame Workspace display. This reuses
// the existing CGVirtualDisplay wrapper while keeping workspace displays
// visible to the normal display picker.
import AppKit
import ApplicationServices
import CoreGraphics
import Darwin
import Foundation
import IOKit.pwr_mgt
import ScreenCaptureKit

struct WorkspaceOptions {
    let width: Int
    let height: Int
    let scale: Int
    let duration: TimeInterval?
    let exitOnStdinEOF: Bool
    let captureProbe: Bool
    let recoverOrphan: Bool

    static let defaultWidth = 5120
    static let defaultHeight = 1440
    static let name = "Frame Workspace"
    static let usage = "usage: frame-mac-view workspace [--width 5120] [--height 1440] [--scale 1|2] [--duration seconds] [--exit-on-stdin-eof] [--capture-probe] [--recover-orphan]"
    private static let maxBufferPixels = 67_108_864

    init(arguments: [String]) throws {
        var width = Self.defaultWidth
        var height = Self.defaultHeight
        var scale = 1
        var duration: TimeInterval?
        var exitOnStdinEOF = false
        var captureProbe = false
        var recoverOrphan = false
        var seen = Set<String>()
        var i = 0
        while i < arguments.count {
            let flag = arguments[i]
            guard seen.insert(flag).inserted else { throw WorkspaceOptionsError("duplicate option \(flag)") }
            switch flag {
            case "--width", "--height", "--scale", "--duration":
                guard i + 1 < arguments.count, !arguments[i + 1].hasPrefix("--") else {
                    throw WorkspaceOptionsError("\(flag) needs a value")
                }
                let raw = arguments[i + 1]
                switch flag {
                case "--width":
                    guard let value = Int(raw) else { throw WorkspaceOptionsError("width must be an integer") }
                    width = value
                case "--height":
                    guard let value = Int(raw) else { throw WorkspaceOptionsError("height must be an integer") }
                    height = value
                case "--scale":
                    guard let value = Int(raw) else { throw WorkspaceOptionsError("scale must be 1 or 2") }
                    scale = value
                default:
                    guard let value = Double(raw), value.isFinite, value >= 0.1, value <= 86_400 else {
                        throw WorkspaceOptionsError("duration must be between 0.1 and 86400 seconds")
                    }
                    duration = value
                }
                i += 2
            case "--exit-on-stdin-eof":
                exitOnStdinEOF = true
                i += 1
            case "--capture-probe":
                captureProbe = true
                i += 1
            case "--recover-orphan":
                recoverOrphan = true
                i += 1
            default:
                throw WorkspaceOptionsError("unknown workspace option \(flag)")
            }
        }
        if recoverOrphan {
            guard seen.isSubset(of: ["--recover-orphan", "--exit-on-stdin-eof"]) else {
                throw WorkspaceOptionsError("--recover-orphan cannot be combined with display options")
            }
        } else {
            guard (1280...7680).contains(width) else { throw WorkspaceOptionsError("width must be between 1280 and 7680 logical points") }
            guard (720...4320).contains(height) else { throw WorkspaceOptionsError("height must be between 720 and 4320 logical points") }
            guard scale == 1 || scale == 2 else { throw WorkspaceOptionsError("scale must be 1 or 2") }
            guard width * height * scale * scale <= Self.maxBufferPixels else {
                throw WorkspaceOptionsError("requested buffer exceeds the 67,108,864-pixel safety limit")
            }
        }
        self.width = width
        self.height = height
        self.scale = scale
        self.duration = duration
        self.exitOnStdinEOF = exitOnStdinEOF
        self.captureProbe = captureProbe
        self.recoverOrphan = recoverOrphan
    }
}

struct WorkspaceOptionsError: Error, CustomStringConvertible {
    let description: String
    init(_ description: String) { self.description = description }
}

private struct DisplayLayout: Equatable {
    let id: CGDirectDisplayID
    let bounds: CGRect
    let mode: (width: Int, height: Int, pixelWidth: Int, pixelHeight: Int)?
    let mirroredDisplay: CGDirectDisplayID
    let main: Bool

    static func == (lhs: Self, rhs: Self) -> Bool {
        lhs.id == rhs.id && lhs.bounds == rhs.bounds && lhs.mode?.width == rhs.mode?.width &&
            lhs.mode?.height == rhs.mode?.height && lhs.mode?.pixelWidth == rhs.mode?.pixelWidth &&
            lhs.mode?.pixelHeight == rhs.mode?.pixelHeight && lhs.mirroredDisplay == rhs.mirroredDisplay && lhs.main == rhs.main
    }
}

private struct WorkspaceWindowSnapshot {
    let id: CGWindowID
    let pid: pid_t
    let frame: CGRect
}

private func onlineDisplayIDs() -> [CGDirectDisplayID] {
    var ids = [CGDirectDisplayID](repeating: 0, count: 32)
    var count: UInt32 = 0
    CGGetOnlineDisplayList(UInt32(ids.count), &ids, &count)
    return Array(ids.prefix(Int(count)))
}

private func displayLayout(_ id: CGDirectDisplayID) -> DisplayLayout {
    let mode = CGDisplayCopyDisplayMode(id)
    return DisplayLayout(
        id: id,
        bounds: CGDisplayBounds(id),
        mode: mode.map { (Int($0.width), Int($0.height), $0.pixelWidth, $0.pixelHeight) },
        mirroredDisplay: CGDisplayMirrorsDisplay(id),
        main: CGDisplayIsMain(id) != 0
    )
}

private func currentLayout(excludingWorkspace: Bool = true) -> [DisplayLayout] {
    onlineDisplayIDs().filter { !excludingWorkspace || !VirtualDisplay.isWorkspace($0) }
        .map(displayLayout).sorted { $0.id < $1.id }
}

private func rectJSON(_ rect: CGRect) -> [String: Double] {
    ["x": Double(rect.minX), "y": Double(rect.minY), "width": Double(rect.width), "height": Double(rect.height)]
}

/// Owns a single virtual display and releases only the instance it created.
/// All display and AppKit operations run on the main queue.
final class WorkspaceCommand {
    private let options: WorkspaceOptions
    private let app: NSApplication
    private var display: VirtualDisplay?
    private var displayID: CGDirectDisplayID?
    private var displaySleepAssertion: IOPMAssertionID = 0
    private var wakeProcess: Process?
    private var originalLayout: [DisplayLayout] = []
    private var originalWindows: [WorkspaceWindowSnapshot] = []
    private var probe: SCKSource?
    private var probeTimer: DispatchSourceTimer?
    private var stopTimer: DispatchSourceTimer?
    private var stopRequested = false
    private var readyEmitted = false
    private var probeGotFrame = false
    private var retriedModeAfterRegistration = false
    private var workspaceLease: Int32 = -1
    private var recoveryAttempt = 0
    private var recoveredOrphan = false
    private var sawPriorWorkspace = false
    private var restoredWindowCount = 0
    private(set) var exitStatus: Int32 = 0

    init(options: WorkspaceOptions, app: NSApplication) {
        self.options = options
        self.app = app
    }

    func start() {
        guard acquireWorkspaceLease() else {
            return fail("another live Frame Workspace owner holds the per-user lease; its display was left untouched")
        }
        sawPriorWorkspace = onlineDisplayIDs().contains(where: VirtualDisplay.isWorkspace)
        guard IOPMAssertionCreateWithName(kIOPMAssertionTypePreventUserIdleDisplaySleep as CFString,
                                          IOPMAssertionLevel(kIOPMAssertionLevelOn),
                                          "Frame Workspace virtual display is active" as CFString,
                                          &displaySleepAssertion) == kIOReturnSuccess else {
            return fail("macOS would not prevent idle display sleep while Workspace is active")
        }
        // CGVirtualDisplay removal can be deferred while the built-in screen
        // sleeps. Briefly request user activity at startup, then retain only
        // PreventUserIdleDisplaySleep for the duration of ownership.
        let wake = Process()
        wake.executableURL = URL(fileURLWithPath: "/usr/bin/caffeinate")
        wake.arguments = ["-u", "-t", "1"]
        wake.standardOutput = FileHandle.nullDevice
        wake.standardError = FileHandle.nullDevice
        do {
            try wake.run()
            wakeProcess = wake
        } catch {
            return fail("macOS would not wake displays for Workspace startup")
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
            guard let self else { return }
            self.wakeProcess = nil
            guard !self.stopRequested else { return }
            self.recoverOrWaitForExistingWorkspace()
        }
    }

    /// A held lease proves no other command using this implementation owns an
    /// online workspace. A leftover display may be waiting for WindowServer to
    /// wake; request a brief wake and wait for macOS itself to unregister it.
    /// Never search for or release another process's CGVirtualDisplay object.
    private func recoverOrWaitForExistingWorkspace() {
        let existing = onlineDisplayIDs().contains(where: VirtualDisplay.isWorkspace)
        recoveredOrphan = sawPriorWorkspace || existing
        guard existing else {
            if options.recoverOrphan { return finishRecovery(recovered: recoveredOrphan) }
            return createDisplay()
        }
        recoveryAttempt = 0
        waitForExistingWorkspaceToDisappear()
    }

    private func waitForExistingWorkspaceToDisappear() {
        guard !stopRequested else { return }
        guard onlineDisplayIDs().contains(where: VirtualDisplay.isWorkspace) else {
            if options.recoverOrphan { return finishRecovery(recovered: true) }
            return createDisplay()
        }
        if recoveryAttempt >= 50 {
            return fail("a Frame Workspace display is still registered after wake; it may have another owner or macOS may be deferring removal. It was left untouched")
        }
        recoveryAttempt += 1
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + 0.1)
        timer.setEventHandler { [weak self] in
            self?.stopTimer?.cancel()
            self?.stopTimer = nil
            self?.waitForExistingWorkspaceToDisappear()
        }
        timer.resume()
        stopTimer = timer
    }

    private func finishRecovery(recovered: Bool) {
        releaseDisplaySleepAssertion()
        releaseWorkspaceLease()
        wakeProcess = nil
        printJSONLine(["state": "stopped", "recoveredOrphan": recovered, "removed": true,
                   "physicalLayoutUnchanged": true])
        finishApplicationRunLoop()
    }

    private func acquireWorkspaceLease() -> Bool {
        let path = "/tmp/frame-workspace-\(getuid()).lock"
        let fd = open(path, O_CREAT | O_RDWR | O_NOFOLLOW, mode_t(S_IRUSR | S_IWUSR))
        guard fd >= 0 else { return false }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else {
            close(fd)
            return false
        }
        workspaceLease = fd
        return true
    }

    private func releaseWorkspaceLease() {
        guard workspaceLease >= 0 else { return }
        _ = flock(workspaceLease, LOCK_UN)
        close(workspaceLease)
        workspaceLease = -1
    }

    private func createDisplay() {
        originalLayout = currentLayout()
        originalWindows = windowSnapshots()
        guard let created = VirtualDisplay(name: WorkspaceOptions.name, width: options.width, height: options.height,
                                           pixelScale: options.scale, purpose: .workspace) else {
            return fail("macOS did not create the virtual display")
        }
        display = created
        displayID = created.id
        verify(attempt: 0)
    }

    private func verify(attempt: Int) {
        guard !stopRequested, let display, let mode = CGDisplayCopyDisplayMode(display.id) else {
            if attempt >= 50 { return fail("macOS did not make the workspace display available") }
            return DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in self?.verify(attempt: attempt + 1) }
        }
        let matchesMode = Int(mode.width) == options.width && Int(mode.height) == options.height &&
            mode.pixelWidth == options.width * options.scale && mode.pixelHeight == options.height * options.scale
        if display.screen != nil && !matchesMode && !retriedModeAfterRegistration {
            retriedModeAfterRegistration = true
            _ = display.applyWorkspaceMode(width: options.width, height: options.height, pixelScale: options.scale)
        }
        guard CGDisplayIsOnline(display.id) != 0, display.screen != nil, matchesMode else {
            if attempt >= 50 {
                let actual = "\(mode.width)×\(mode.height) logical / \(mode.pixelWidth)×\(mode.pixelHeight) pixels"
                let modes = (CGDisplayCopyAllDisplayModes(display.id, [kCGDisplayShowDuplicateLowResolutionModes: true] as CFDictionary)
                    as? [CGDisplayMode] ?? []).prefix(12).map {
                        "\($0.width)×\($0.height)/\($0.pixelWidth)×\($0.pixelHeight)"
                    }.joined(separator: ",")
                let modeError = display.lastModeConfigurationError.map { "; mode apply: \($0)" } ?? ""
                return fail("macOS did not apply requested \(options.width)×\(options.height) @\(options.scale)x mode (actual \(actual); available \(modes)\(modeError))")
            }
            return DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in self?.verify(attempt: attempt + 1) }
        }
        let now = currentLayout()
        guard now == originalLayout else { return fail("creating the workspace changed an existing display layout") }
        let bounds = display.bounds
        guard !bounds.isEmpty, !originalLayout.contains(where: { $0.bounds.intersects(bounds) }) else {
            return fail("macOS did not place the workspace as a separate extended display")
        }
        guard CGDisplayMirrorsDisplay(display.id) == kCGNullDirectDisplay else {
            return fail("macOS mirrored the workspace display")
        }
        guard CGDisplayIsMain(display.id) == 0 else { return fail("macOS made the workspace the main display") }
        if options.captureProbe { return startCaptureProbe(display.id) }
        emitReady(capturable: nil)
    }

    private func startCaptureProbe(_ id: CGDirectDisplayID) {
        guard CGPreflightScreenCaptureAccess() else { return fail("Screen Recording permission is required for the capture probe") }
        let source = SCKSource(.display(id))
        probe = source
        source.onFrame = { [weak self] _, _, _ in
            DispatchQueue.main.async { self?.captureProbeFrameArrived() }
        }
        source.onEnded = { [weak self] reason in
            DispatchQueue.main.async {
                guard let self, !self.readyEmitted, !self.stopRequested else { return }
                self.fail("ScreenCaptureKit ended the workspace probe: \(reason)")
            }
        }
        source.start(maxLong: 1920, fps: 5) { [weak self] error in
            DispatchQueue.main.async {
                guard let self, !self.stopRequested, !self.readyEmitted else { return }
                if let error { self.fail("workspace capture probe failed: \(error)") }
            }
        }
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + 8)
        timer.setEventHandler { [weak self] in
            guard let self, !self.probeGotFrame, !self.stopRequested else { return }
            self.fail("ScreenCaptureKit did not produce a workspace frame within 8 seconds")
        }
        timer.resume()
        probeTimer = timer
    }

    private func captureProbeFrameArrived() {
        guard !probeGotFrame, !stopRequested else { return }
        probeGotFrame = true
        probeTimer?.cancel()
        probeTimer = nil
        probe?.stop()
        probe = nil
        if let wakeProcess, wakeProcess.isRunning { wakeProcess.terminate() }
        wakeProcess = nil
        emitReady(capturable: true)
    }

    private func emitReady(capturable: Bool?) {
        guard !readyEmitted, let display, let mode = CGDisplayCopyDisplayMode(display.id) else { return }
        readyEmitted = true
        let bounds = display.bounds
        var record: [String: Any] = [
            "state": "ready", "name": WorkspaceOptions.name, "src": "display:\(display.id)", "displayID": display.id,
            "requested": ["logicalWidth": options.width, "logicalHeight": options.height, "scale": options.scale],
            "actual": ["logicalWidth": Int(mode.width), "logicalHeight": Int(mode.height),
                       "bufferWidth": mode.pixelWidth, "bufferHeight": mode.pixelHeight,
                       "scale": Double(mode.pixelWidth) / Double(mode.width)],
            "bounds": rectJSON(bounds), "mirrored": false, "mirrorDisplayID": NSNull(),
            "main": false, "physicalLayoutUnchanged": currentLayout() == originalLayout,
            "recoveredOrphan": recoveredOrphan,
            "windowRestoreAvailable": AXIsProcessTrusted(), "snapshottedWindows": originalWindows.count,
            "captureProbe": capturable.map { $0 as Any } ?? NSNull()
        ]
        if let duration = options.duration {
            record["durationSeconds"] = duration
        }
        printJSONLine(record)
        if let duration = options.duration {
            DispatchQueue.main.asyncAfter(deadline: .now() + duration) { [weak self] in self?.stop(reason: "duration") }
        }
    }

    func stop(reason: String) {
        guard !stopRequested else { return }
        stopRequested = true
        probeTimer?.cancel()
        probeTimer = nil
        probe?.stop()
        probe = nil
        guard let id = displayID else {
            releaseWorkspaceLease()
            if options.recoverOrphan || recoveredOrphan {
                if onlineDisplayIDs().contains(where: VirtualDisplay.isWorkspace) {
                    return emitError(reason: reason, message: "Workspace removal is still deferred; the previously registered display was left untouched",
                                     removed: false)
                }
                printJSONLine(["state": "stopped", "reason": reason, "removed": true,
                               "recoveredOrphan": recoveredOrphan, "physicalLayoutUnchanged": true])
                finishApplicationRunLoop()
                return
            }
            emitError(reason: reason, message: "stopped before a display was created", removed: true)
            return
        }
        restoreWindowsStillOnWorkspace()
        display?.release()
        display = nil
        awaitRemoval(id, reason: reason, attempt: 0)
    }

    private func awaitRemoval(_ id: CGDirectDisplayID, reason: String, attempt: Int) {
        let removed = !onlineDisplayIDs().contains(id)
        let layoutUnchanged = currentLayout() == originalLayout
        if removed {
            if let startupFailure = stoppingForFailure {
                releaseDisplaySleepAssertion()
                return emitError(reason: reason, message: startupFailure, removed: true)
            }
            if !layoutUnchanged {
                releaseDisplaySleepAssertion()
                return emitError(reason: reason, message: "display was removed, but an existing display layout changed", removed: true)
            }
            releaseDisplaySleepAssertion()
            printJSONLine(["state": "stopped", "displayID": id, "reason": reason, "removed": true,
                       "physicalLayoutUnchanged": true, "windowsRestored": restoredWindowCount])
            releaseWorkspaceLease()
            finishApplicationRunLoop()
            return
        }
        if attempt >= 50 {
            releaseDisplaySleepAssertion()
            return emitError(reason: reason, message: "virtual display removal is still pending after 5 seconds; macOS may defer removal while displays sleep",
                             removed: false)
        }
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + 0.1)
        timer.setEventHandler { [weak self] in
            self?.stopTimer?.cancel()
            self?.stopTimer = nil
            self?.awaitRemoval(id, reason: reason, attempt: attempt + 1)
        }
        timer.resume()
        stopTimer = timer
    }

    private func fail(_ message: String) {
        guard !stopRequested else { return }
        if displayID == nil {
            releaseDisplaySleepAssertion()
            releaseWorkspaceLease()
            exitStatus = 1
            let existing = onlineDisplayIDs().contains(where: VirtualDisplay.isWorkspace)
            printJSONLine(["state": "error", "error": message, "removed": !existing,
                           "physicalLayoutUnchanged": true])
            finishApplicationRunLoop()
            return
        }
        // A failed verification must relinquish only this command's display.
        stopRequested = false
        stoppingForFailure = message
        stop(reason: "startup_error")
    }

    private var stoppingForFailure: String?

    private func emitError(reason: String, message: String, removed: Bool) {
        releaseDisplaySleepAssertion()
        releaseWorkspaceLease()
        if let wakeProcess, wakeProcess.isRunning { wakeProcess.terminate() }
        wakeProcess = nil
        exitStatus = 1
        var event: [String: Any] = ["state": "error", "reason": reason, "error": message, "removed": removed,
                                   "physicalLayoutUnchanged": currentLayout() == originalLayout]
        if let displayID { event["displayID"] = displayID }
        if let stoppingForFailure { event["startupError"] = stoppingForFailure }
        printJSONLine(event)
        finishApplicationRunLoop()
    }

    /// NSApplication.stop() alone can leave a prohibited, windowless app
    /// blocked in nextEventMatchingMask when no display notification arrives.
    /// Posting an application-defined event wakes that wait so the stop request
    /// is observed and the process can release its lease.
    private func finishApplicationRunLoop() {
        guard Thread.isMainThread else {
            DispatchQueue.main.async { [weak self] in self?.finishApplicationRunLoop() }
            return
        }
        if let wakeEvent = NSEvent.otherEvent(with: .applicationDefined, location: .zero,
                                              modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                              windowNumber: 0, context: nil, subtype: 0, data1: 0, data2: 0) {
            app.postEvent(wakeEvent, atStart: true)
        }
        app.stop(nil)
    }

    private func windowSnapshots() -> [WorkspaceWindowSnapshot] {
        guard AXIsProcessTrusted() else { return [] }
        return WindowInfo.all().filter { $0.layer == 0 && $0.pid != ProcessInfo.processInfo.processIdentifier }
            .compactMap { info in
                guard let frame = AXWindow(windowID: info.id, pid: info.pid)?.frame else { return nil }
                return WorkspaceWindowSnapshot(id: info.id, pid: info.pid, frame: frame)
            }
    }

    /// Restore only windows that existed before Workspace started and are
    /// still centered on the display this command owns. Other windows are
    /// never moved, even if their app or display layout changed meanwhile.
    private func restoreWindowsStillOnWorkspace() {
        guard AXIsProcessTrusted(), let bounds = display?.bounds else { return }
        for saved in originalWindows {
            guard let current = WindowInfo.find(saved.id),
                  bounds.contains(CGPoint(x: current.bounds.midX, y: current.bounds.midY)),
                  let window = AXWindow(windowID: saved.id, pid: saved.pid) else { continue }
            window.setFrame(saved.frame)
            restoredWindowCount += 1
        }
    }

    private func releaseDisplaySleepAssertion() {
        guard displaySleepAssertion != 0 else { return }
        IOPMAssertionRelease(displaySleepAssertion)
        displaySleepAssertion = 0
    }
}
