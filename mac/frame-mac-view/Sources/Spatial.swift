import AppKit
import ApplicationServices
import CoreGraphics
import Darwin
import Foundation
import IOKit.pwr_mgt

struct SpatialWindowIdentity: Hashable {
    let id: CGWindowID
    let pid: pid_t
}

private struct SpatialWindowRecord: Equatable {
    let id: CGWindowID
    let pid: pid_t
    let app: String
    let title: String
    let bounds: CGRect
    let bundleID: String?

    var json: [String: Any] {
        var value: [String: Any] = ["id": id, "src": "spatial:\(id)", "pid": pid,
                                   "app": app, "title": title,
                                   "w": Int(bounds.width), "h": Int(bounds.height),
                                   "x": Double(bounds.minX), "y": Double(bounds.minY)]
        value["bundleID"] = bundleID ?? ""
        return value
    }
}

private struct SpatialPhysicalDisplaySnapshot {
    let id: CGDirectDisplayID
    let mode: CGDisplayMode
    let origin: CGPoint
    let mirrorTarget: CGDirectDisplayID
}

private struct SpatialPhysicalLayoutSnapshot {
    let displays: [SpatialPhysicalDisplaySnapshot]
    let mainDisplayID: CGDirectDisplayID
}

/// Window lifecycle observer. AX creation notifications wake reconciliation,
/// while WindowInfo remains the authority for whether a window still exists.
private func spatialAXObserverCallback(_ observer: AXObserver, _ element: AXUIElement,
                                       _ notification: CFString, _ context: UnsafeMutableRawPointer?) {
    guard let context else { return }
    let discovery = Unmanaged<SpatialWindowDiscovery>.fromOpaque(context).takeUnretainedValue()
    DispatchQueue.main.async { discovery.refresh() }
}

private final class SpatialWindowDiscovery {
    private weak var owner: SpatialCoordinator?
    private var timer: DispatchSourceTimer?
    private var launchObserver: NSObjectProtocol?
    private var observers: [pid_t: AXObserver] = [:]
    private var active = false
    init(owner: SpatialCoordinator) { self.owner = owner }

    func start() {
        guard Thread.isMainThread else { DispatchQueue.main.async { self.start() }; return }
        stop()
        active = true
        launchObserver = NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didLaunchApplicationNotification, object: nil, queue: .main
        ) { [weak self] _ in self?.refresh() }
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now(), repeating: 1)
        timer.setEventHandler { [weak self] in self?.refresh() }
        timer.resume()
        self.timer = timer
        refresh()
    }

    func stop() {
        guard Thread.isMainThread else { DispatchQueue.main.async { self.stop() }; return }
        active = false
        timer?.cancel()
        timer = nil
        if let launchObserver { NSWorkspace.shared.notificationCenter.removeObserver(launchObserver) }
        launchObserver = nil
        for observer in observers.values {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .defaultMode)
        }
        observers.removeAll()
    }

    fileprivate func refresh() {
        guard Thread.isMainThread, active, let owner else { return }
        let ownPID = ProcessInfo.processInfo.processIdentifier
        let applications = NSWorkspace.shared.runningApplications.filter {
            $0.processIdentifier != ownPID && $0.activationPolicy == .regular &&
                SpatialCoordinator.allowedPID($0.processIdentifier)
        }
        let livePIDs = Set(applications.map(\.processIdentifier))
        for pid in observers.keys.filter({ !livePIDs.contains($0) }) {
            if let observer = observers.removeValue(forKey: pid) {
                CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .defaultMode)
            }
        }
        for app in applications {
            attachObserver(to: app)
        }

        let all = WindowInfo.all(onScreenOnly: false)
        let eligible = all.filter(eligible)
        var records = [SpatialWindowRecord]()
        for info in eligible where info.onScreen { records.append(record(info)) }
        records.sort { lhs, rhs in
            if lhs.pid == rhs.pid { return lhs.id < rhs.id }
            return lhs.pid < rhs.pid
        }
        let alive = Set(all.map { SpatialWindowIdentity(id: $0.id, pid: $0.pid) })
        owner.reconcileWindows(records, alive: alive, infos: eligible, captureFrames: true)
    }

    private func eligible(_ info: WindowInfo) -> Bool {
        SpatialCoordinator.isEligible(info)
    }

    private func record(_ info: WindowInfo) -> SpatialWindowRecord {
        SpatialWindowRecord(id: info.id, pid: info.pid, app: info.app, title: info.title, bounds: info.bounds,
                            bundleID: NSRunningApplication(processIdentifier: info.pid)?.bundleIdentifier)
    }

    private func attachObserver(to app: NSRunningApplication) {
        let pid = app.processIdentifier
        guard observers[pid] == nil else { return }
        var observer: AXObserver?
        let context = Unmanaged.passUnretained(self).toOpaque()
        let created = AXObserverCreate(pid, spatialAXObserverCallback, &observer)
        guard created == .success, let observer else { return }
        let appElement = AXUIElementCreateApplication(pid)
        guard AXObserverAddNotification(observer, appElement, kAXWindowCreatedNotification as CFString, context) == .success else {
            return
        }
        CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .defaultMode)
        observers[pid] = observer
    }
}

/// One display and persisted window snapshots share a lifecycle. Captures end
/// before originals are restored and the shared display is removed.
final class SpatialCoordinator {
    private let lock = NSLock()
    private var state = "stopped"
    private var lastError: String?
    private var revision = 0
    private var windowJSON: [[String: Any]] = []
    private var fingerprint = Data()

    private var shared: VirtualDisplay?
    private var displaysBeingRemoved: [VirtualDisplay] = []
    private var builtIn: CGDirectDisplayID?
    private var sharedDisplayID: CGDirectDisplayID?
    private var physicalLayout: SpatialPhysicalLayoutSnapshot?
    private var activePhysicalLayout: [SpatialLayoutDisplay]?
    private var restoreLayoutBaseline: [SpatialLayoutDisplay]?
    private var originals: [SpatialWindowIdentity: CGRect] = [:]
    private var sleepAssertion: IOPMAssertionID = 0
    private var spatialLeaseFD: Int32 = -1
    private var workspaceLeaseFD: Int32 = -1
    private lazy var discovery = SpatialWindowDiscovery(owner: self)
    let focus = SpatialFocus()
    lazy var surfaces = MenuSurfaceWatcher(coordinator: self)
    private var skipped: [[String: Any]] = []
    var display: VirtualDisplay? { shared }
    var coverWindowID: CGWindowID? { cover.map { CGWindowID($0.windowNumber) } }
    var menuBarDisplayID: CGDirectDisplayID? {
        guard let shared, let builtIn else { return nil }
        return SpatialSharedPolicy.menuBarDisplay(shared: shared.id, builtIn: builtIn,
                                                   separateSpaces: NSScreen.screensHaveSeparateSpaces)
    }
    var menuBarRect: CGRect {
        guard let id = menuBarDisplayID else { return .zero }
        let bounds = CGDisplayBounds(id)
        let screen = NSScreen.screens.first { ($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value == id }
        let height = screen.map { $0.frame.maxY - $0.visibleFrame.maxY } ?? 36
        return CGRect(x: bounds.minX, y: bounds.minY, width: bounds.width, height: max(24, height))
    }
    private var queuedStop: ((@escaping () -> Void) -> Void, (Bool, String?, Bool) -> Void)?

    var stateValue: String { lock.lock(); defer { lock.unlock() }; return state }
    var isRunning: Bool { stateValue == "running" }
    var isStopping: Bool { stateValue == "stopping" }

    func statusJSON() -> [String: Any] {
        if !Thread.isMainThread { return DispatchQueue.main.sync { self.statusJSON() } }
        // Desktop status polling must not walk Accessibility windows. Apart
        // from being unnecessary while stopped, remote AX applications can
        // stall the otherwise lightweight /status response.
        if ["starting", "running"].contains(stateValue) {
            if Thread.isMainThread {
                refreshWindowSnapshot()
            } else {
                DispatchQueue.main.sync { refreshWindowSnapshot() }
            }
        }
        lock.lock(); defer { lock.unlock() }
        var result: [String: Any] = ["state": state, "revision": revision, "windows": windowJSON,
                                     "skipped": skipped, "menus": surfaces.menusJSON,
                                     "focusedSrc": focus.focusedSource ?? NSNull(), "cover": coverVisible]
        if let shared {
            let b = shared.bounds
            result["display"] = ["id": shared.id, "bounds": SpatialSharedPolicy.rectJSON(b),
                                 "scale": 2, "menuBarDisplay": menuBarDisplayID ?? shared.id]
        } else { result["display"] = NSNull() }
        if ProcessInfo.processInfo.environment["FRAME_SPATIAL_DIAGNOSTICS"] == "1" {
            result["cursorFence"] = ["enabled": focus.fence.enabled, "events": focus.fence.eventCount,
                                      "rects": focus.fence.rects.map(SpatialSharedPolicy.rectJSON)]
        }
        if let lastError { result["error"] = lastError }
        return result
    }

    func windowsJSON() -> [[String: Any]] {
        lock.lock(); defer { lock.unlock() }
        return windowJSON
    }

    /// Saves the pre-separation frame on main. Existing snapshots taken before
    /// moving win; newly opened windows get their first discovered frame.
    @discardableResult
    func captureOriginalIfNeeded(windowID: CGWindowID, pid: pid_t) -> Bool {
        guard Thread.isMainThread else {
            DispatchQueue.main.async { _ = self.captureOriginalIfNeeded(windowID: windowID, pid: pid) }
            return false
        }
        let identity = SpatialWindowIdentity(id: windowID, pid: pid)
        if originals[identity] != nil { return true }
        guard let frame = AXWindow(windowID: windowID, pid: pid)?.frame else { return false }
        originals[identity] = frame
        guard persistOriginals() else { originals[identity] = nil; return false }
        return true
    }

    func originalFrame(windowID: CGWindowID, pid: pid_t) -> CGRect? {
        guard Thread.isMainThread else { return nil }
        return originals[SpatialWindowIdentity(id: windowID, pid: pid)]
    }

    func contains(windowID: CGWindowID) -> Bool {
        windowsJSON().contains { ($0["id"] as? UInt32) == windowID }
    }

    private func place(_ info: WindowInfo) -> Bool {
        guard let shared, let window = AXWindow(windowID: info.id, pid: info.pid),
              captureOriginalIfNeeded(windowID: info.id, pid: info.pid) else { return false }
        let target = SpatialSharedPolicy.sharedFrame(size: info.bounds.size, display: shared.usableRect,
                                                       menuBar: menuBarRect)
        if let actual = window.frame, Self.closeEnough(actual, target) { return true }
        window.setFrame(target)
        guard let actual = window.frame else { return false }
        return shared.bounds.insetBy(dx: -2, dy: -2).contains(actual)
    }

    private func setState(_ value: String, error: String? = nil) {
        lock.lock(); state = value; lastError = error; lock.unlock()
    }

    private func refreshWindowSnapshot() {
        guard Thread.isMainThread else { return }
        let all = WindowInfo.all(onScreenOnly: false)
        let eligible = all.filter(Self.isEligible)
        var records = [SpatialWindowRecord]()
        for info in eligible where info.onScreen {
            records.append(SpatialWindowRecord(id: info.id, pid: info.pid, app: info.app, title: info.title,
                                               bounds: info.bounds,
                                               bundleID: NSRunningApplication(processIdentifier: info.pid)?.bundleIdentifier))
        }
        records.sort { lhs, rhs in
            if lhs.pid == rhs.pid { return lhs.id < rhs.id }
            return lhs.pid < rhs.pid
        }
        let alive = Set(all.map { SpatialWindowIdentity(id: $0.id, pid: $0.pid) })
        let shouldCaptureFrames = isRunning || stateValue == "starting"
        reconcileWindows(records, alive: alive, infos: shouldCaptureFrames ? eligible : [],
                         captureFrames: shouldCaptureFrames)
    }

    fileprivate func reconcileWindows(_ records: [SpatialWindowRecord], alive: Set<SpatialWindowIdentity>,
                                      infos: [WindowInfo], captureFrames: Bool) {
        guard Thread.isMainThread else {
            DispatchQueue.main.async { self.reconcileWindows(records, alive: alive, infos: infos, captureFrames: captureFrames) }
            return
        }
        var accepted = [SpatialWindowRecord]()
        var failures = [[String: Any]]()
        if captureFrames {
            for info in infos {
                guard info.onScreen else { failures.append(["id": info.id, "reason": "Other Space"]); continue }
                guard place(info), let actual = WindowInfo.find(info.id) else {
                    failures.append(["id": info.id, "reason": "Window Could Not Move"]); continue
                }
                accepted.append(SpatialWindowRecord(id: actual.id, pid: actual.pid, app: actual.app,
                    title: actual.title, bounds: actual.bounds,
                    bundleID: NSRunningApplication(processIdentifier: actual.pid)?.bundleIdentifier))
            }
            for info in WindowInfo.all(onScreenOnly: false) where Self.allowedPID(info.pid) && info.layer == 0 {
                if AXWindow(windowID: info.id, pid: info.pid)?.isFullScreen == true {
                    failures.append(["id": info.id, "reason": "Full Screen"])
                }
            }
            originals = originals.filter { alive.contains($0.key) }
            _ = persistOriginals()
        } else { accepted = records }
        accepted.sort { $0.pid == $1.pid ? $0.id < $1.id : $0.pid < $1.pid }
        if let key = focus.focusedSource, case .spatial(let id)? = Source(key),
           !accepted.contains(where: { $0.id == id }) { focus.removeWindow(id) }
        let json = accepted.map(\.json)
        let bytes = (try? JSONSerialization.data(withJSONObject: json, options: [.sortedKeys])) ?? Data()
        lock.lock()
        skipped = failures
        if bytes != fingerprint {
            fingerprint = bytes
            windowJSON = json
            revision &+= 1
        }
        lock.unlock()
    }

    func start(completion: @escaping (Bool, String?, Bool) -> Void) {
        DispatchQueue.main.async {
            switch self.stateValue {
            case "running": completion(true, nil, true); return
            case "starting", "stopping": completion(false, "spatial mode is already changing state", false); return
            case "error":
                guard self.shared == nil, self.displaysBeingRemoved.isEmpty,
                      self.physicalLayout == nil, self.originals.isEmpty,
                      Self.onlineSpatialIDs()?.isEmpty == true else {
                    completion(false, self.lastError ?? "spatial cleanup must complete before restarting", false)
                    return
                }
                self.setState("stopped")
            default: break
            }
            self.setState("starting")
            self.startOnMain(completion: completion)
        }
    }

    private func startOnMain(completion: @escaping (Bool, String?, Bool) -> Void) {
        guard AXIsProcessTrusted() else { return failStart("Accessibility permission is required for spatial windows", completion: completion) }
        guard CGPreflightScreenCaptureAccess() else { return failStart("Screen Recording permission is required for spatial windows", completion: completion) }
        guard let firstDisplayIDs = onlineDisplayIDs() else {
            return rejectStart("WindowServer could not confirm the online display list", removed: false, completion: completion)
        }
        guard !firstDisplayIDs.contains(where: VirtualDisplay.isWorkspace) else {
            return rejectStart("a Frame Workspace display is still registered; spatial mode was not started",
                               removed: Self.onlineSpatialIDs()?.isEmpty == true, completion: completion)
        }
        let lease = acquireLeases()
        guard lease.success else {
            return rejectStart(lease.error ?? "could not acquire the per-user spatial display lease",
                               removed: lease.removed, completion: completion)
        }
        guard let onlineIDs = onlineDisplayIDs() else {
            return rejectStart("WindowServer could not confirm the online display list", removed: false, completion: completion)
        }
        guard !onlineIDs.contains(where: VirtualDisplay.isWorkspace) else {
            return rejectStart("a Frame Workspace display became active; spatial mode was not started",
                               removed: Self.onlineSpatialIDs()?.isEmpty == true, completion: completion)
        }
        guard !onlineIDs.contains(where: VirtualDisplay.isOurs) else {
            return rejectStart("separated-window displays are still unregistering; retry spatial start", removed: true,
                               completion: completion)
        }
        guard let orphans = Self.onlineSpatialIDs() else {
            return rejectStart("WindowServer could not confirm whether spatial displays remain registered",
                               removed: false, completion: completion)
        }
        guard orphans.isEmpty else {
            return rejectStart("spatial displays from a previous helper are still registered; left untouched",
                               removed: false, completion: completion)
        }
        let builtIns = onlineIDs.filter { CGDisplayIsBuiltin($0) != 0 }
        guard let builtIn = builtIns.first, CGDisplayCopyDisplayMode(builtIn) != nil else {
            return failStart("macOS did not report a built-in display mode", completion: completion)
        }
        guard CGDisplayMirrorsDisplay(builtIn) == kCGNullDirectDisplay else {
            return rejectStart("the built-in display is already mirrored by another display; spatial mode left it untouched",
                               removed: true, completion: completion)
        }
        guard let snapshot = Self.capturePhysicalLayout(onlineIDs: onlineIDs) else {
            return rejectStart("could not snapshot the physical display layout before spatial setup", removed: true,
                               completion: completion)
        }
        physicalLayout = snapshot
        self.builtIn = builtIn
        guard originals.isEmpty else { return failStart("Pending Original Frames Must Be Restored First", completion: completion) }
        let existingWindows = WindowInfo.all(onScreenOnly: false).filter(Self.isEligible)
        for info in existingWindows {
            _ = captureOriginalIfNeeded(windowID: info.id, pid: info.pid)
        }
        let unsnapshotted = existingWindows.filter {
            originals[SpatialWindowIdentity(id: $0.id, pid: $0.pid)] == nil
        }
        guard unsnapshotted.isEmpty else {
            return failStart("could not save the original frame for every eligible window before moving",
                             completion: completion)
        }
        let assertion = IOPMAssertionCreateWithName(kIOPMAssertionTypePreventUserIdleDisplaySleep as CFString,
                                                    IOPMAssertionLevel(kIOPMAssertionLevelOn),
                                                    "Frame Control spatial displays are active" as CFString,
                                                    &sleepAssertion)
        guard assertion == kIOReturnSuccess else {
            return failStart("macOS would not prevent display sleep while spatial displays are active", completion: completion)
        }
        let size = SpatialSharedPolicy.displaySize(ProcessInfo.processInfo.environment["FRAME_SPATIAL_SHARED_SIZE"])
        guard let display = VirtualDisplay(name: "Frame Spatial Shared", width: Int(size.width), height: Int(size.height),
                                           pixelScale: 2, purpose: .spatialShared) else {
            return failStart("macOS Would Not Create The Shared Spatial Display", completion: completion)
        }
        shared = display
        sharedDisplayID = display.id
        if let current = currentPhysicalLayoutStates() { restoreLayoutBaseline = current.1 }
        waitForModeReady(display, width: Int(size.width), height: Int(size.height), pixelScale: 2, attempt: 0) { ready in
            guard ready else { return self.failStart("The Shared Spatial Display Did Not Become Ready", completion: completion) }
            let b = CGDisplayBounds(builtIn)
            var config: CGDisplayConfigRef?
            guard CGBeginDisplayConfiguration(&config) == .success, let config else {
                return self.failStart("Could Not Position The Shared Display", completion: completion)
            }
            guard CGConfigureDisplayOrigin(config, display.id, Int32(b.maxX - 128), Int32(b.maxY)) == .success else {
                CGCancelDisplayConfiguration(config)
                return self.failStart("Could Not Position The Shared Display", completion: completion)
            }
            guard CGCompleteDisplayConfiguration(config, .forAppOnly) == .success else {
                return self.failStart("macOS Rejected The Shared Display Position", completion: completion)
            }
            guard self.focus.fence.start() else {
                return self.failStart("macOS Would Not Create The Spatial Cursor Fence", completion: completion)
            }
            // Made now (so display captures can exclude it), shown only once
            // the companion reports a window panel in the headset.
            self.prepareCover(on: builtIn)
            self.activePhysicalLayout = self.currentPhysicalLayoutStates()?.1
            self.restoreLayoutBaseline = self.activePhysicalLayout
            self.setState("running")
            self.discovery.start()
            self.surfaces.start()
            completion(true, nil, true)
            self.runQueuedStopIfNeeded()
        }
    }

    private func failStart(_ message: String, removed: Bool = true,
                           completion: @escaping (Bool, String?, Bool) -> Void) {
        setState("stopping", error: message)
        cleanupAfterStreams(message: message) { clean, error in
            let resultError = error ?? message
            self.setState(clean ? "stopped" : "error", error: clean ? message : resultError)
            if clean { self.releaseLeases() }
            completion(false, resultError, clean ? true : removed && clean)
            self.runQueuedStopIfNeeded()
        }
    }

    /// Admission stops before captures, windows and the display are torn down.
    func stop(afterStreams: @escaping (@escaping () -> Void) -> Void,
              completion: @escaping (Bool, String?, Bool) -> Void) {
        DispatchQueue.main.async {
            let current = self.stateValue
            if current == "starting" {
                guard self.queuedStop == nil else {
                    return completion(false, "spatial cleanup is already queued", false)
                }
                self.queuedStop = (afterStreams, completion)
                return
            }
            if current == "stopping" {
                return completion(false, "spatial cleanup is already in progress", false)
            }
            let hasLocalOwnership = self.spatialLeaseFD >= 0 || self.shared != nil ||
                !self.displaysBeingRemoved.isEmpty
            if !hasLocalOwnership {
                guard let probe = Self.tryLock(Self.spatialLeasePath) else {
                    self.setState("error", error: "another spatial helper still holds the per-user lease")
                    return completion(false, "another spatial helper still holds the per-user lease", false)
                }
                self.spatialLeaseFD = probe
            }
            if current == "stopped" && self.shared == nil && self.displaysBeingRemoved.isEmpty {
                guard let orphans = Self.onlineSpatialIDs() else {
                    self.releaseLeases()
                    self.setState("error", error: "WindowServer could not confirm whether spatial displays remain registered")
                    return completion(false, "WindowServer could not confirm whether spatial displays remain registered", false)
                }
                if !orphans.isEmpty {
                    self.releaseLeases()
                    self.setState("error", error: "spatial displays remain registered without a live owner")
                    return completion(false, "spatial displays remain registered without a live owner", false)
                }
                self.releaseLeases()
                return completion(true, nil, true)
            }
            self.setState("stopping")
            self.discovery.stop()
            afterStreams { DispatchQueue.main.async { self.cleanupAfterStreams(message: nil) { clean, error in
                self.setState(clean ? "stopped" : "error", error: error)
                completion(clean, error, clean)
            } } }
        }
    }

    private func runQueuedStopIfNeeded() {
        guard Thread.isMainThread, let queuedStop else { return }
        self.queuedStop = nil
        stop(afterStreams: queuedStop.0, completion: queuedStop.1)
    }

    private func cleanupAfterStreams(message: String?, completion: @escaping (Bool, String?) -> Void) {
        hideCover()
        discovery.stop()
        surfaces.stop()
        focus.clear()
        if shared == nil, !displaysBeingRemoved.isEmpty {
            return waitForRemoval(attempt: 0) { removed in
                guard removed else { return completion(false, "spatial display removal is still pending") }
                self.restoreAfterRemoval(completion: completion)
            }
        }
        restorePhysicalLayout { restored in
            guard restored else { return completion(false, "the original physical display layout could not be restored") }
            self.restoreOriginals(attempt: 0) { restored in
                guard SpatialCleanupGate.mayReleaseDisplays(windowsRestored: restored) else {
                    return completion(false, "Some Original Window Frames Could Not Be Restored")
                }
                if let shared = self.shared {
                    shared.release()
                    self.displaysBeingRemoved = [shared]
                    self.shared = nil
                }
                self.waitForRemoval(attempt: 0) { removed in
                    guard removed else { return completion(false, "spatial display removal is still pending") }
                    self.restoreAfterRemoval(completion: completion)
                }
            }
        }
    }

    /// A plain black cover over the laptop screen, just above normal windows,
    /// so the menu bar, Dock and system dialogs stay visible and usable there.
    /// It exists from start but stays off screen until the companion says the
    /// headset shows a window (`POST /spatial/cover?show=1`), so a startup that
    /// never reaches the headset doesn't leave the laptop black.
    private var cover: NSWindow?
    private(set) var coverVisible = false
    private func prepareCover(on builtIn: CGDirectDisplayID) {
        guard cover == nil, let screen = NSScreen.screens.first(where: {
            ($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value == builtIn
        }) else { return }
        let window = NSWindow(contentRect: screen.frame, styleMask: .borderless, backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.backgroundColor = .black
        window.level = .floating
        window.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenAuxiliary]
        window.hasShadow = false
        window.setFrame(screen.frame, display: false)
        cover = window
        coverVisible = false
    }
    /// Shows or hides the prepared cover; false when Spatial Mode isn't running.
    func setCover(visible: Bool) -> Bool {
        if !Thread.isMainThread { return DispatchQueue.main.sync { self.setCover(visible: visible) } }
        guard isRunning, let cover else { return false }
        if visible { cover.orderFrontRegardless() } else { cover.orderOut(nil) }
        coverVisible = visible
        return true
    }
    private func hideCover() {
        coverVisible = false
        cover?.orderOut(nil)
        cover?.close()
        cover = nil
    }

    /// VirtualDisplay configures its mode once at creation; this polls only for
    /// WindowServer publication and NSScreen visibility.
    private func waitForModeReady(_ display: VirtualDisplay, width: Int, height: Int, pixelScale: Int,
                                  attempt: Int, completion: @escaping (Bool) -> Void) {
        let mode = CGDisplayCopyDisplayMode(display.id)
        let ready = display.screen != nil && mode?.width == width && mode?.height == height &&
            mode?.pixelWidth == pixelScale * width && mode?.pixelHeight == pixelScale * height
        if ready { completion(true) }
        else if attempt >= 30 {
            FileHandle.standardError.write(Data(("frame-mac-view: display \(display.id) not ready: wanted \(width)x\(height)@\(pixelScale)x, " +
                "has \(mode?.width ?? 0)x\(mode?.height ?? 0) (\(mode?.pixelWidth ?? 0)x\(mode?.pixelHeight ?? 0) px), " +
                "screen \(display.screen != nil), \(display.lastModeConfigurationError ?? "no mode error")\n").utf8))
            completion(false)
        } else {
            // As the Workspace does: macOS may only accept the mode once the
            // new display has appeared, so apply it again while waiting.
            // Each apply is a display configuration transaction that can block
            // for a while, so only a few times, not on every poll.
            if display.screen != nil && (attempt == 4 || attempt == 14 || attempt == 24) {
                display.applyWorkspaceMode(width: width, height: height, pixelScale: pixelScale)
            }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self, weak display] in
                guard let self, let display else { completion(false); return }
                self.waitForModeReady(display, width: width, height: height, pixelScale: pixelScale,
                                      attempt: attempt + 1, completion: completion)
            }
        }
    }

    private static func capturePhysicalLayout(onlineIDs: [CGDirectDisplayID]) -> SpatialPhysicalLayoutSnapshot? {
        guard !onlineIDs.isEmpty,
              let main = onlineIDs.first(where: { CGDisplayIsMain($0) != 0 }) else { return nil }
        var displays = [SpatialPhysicalDisplaySnapshot]()
        for id in onlineIDs {
            guard let mode = CGDisplayCopyDisplayMode(id) else { return nil }
            let bounds = CGDisplayBounds(id)
            displays.append(SpatialPhysicalDisplaySnapshot(id: id, mode: mode,
                                                             origin: CGPoint(x: bounds.minX, y: bounds.minY),
                                                             mirrorTarget: CGDisplayMirrorsDisplay(id)))
        }
        return SpatialPhysicalLayoutSnapshot(displays: displays, mainDisplayID: main)
    }

    private var ownedSpatialDisplayIDs: Set<CGDirectDisplayID> {
        var ids = Set(displaysBeingRemoved.map(\.id))
        if let shared { ids.insert(shared.id) }
        if let sharedDisplayID { ids.insert(sharedDisplayID) }
        return ids
    }

    private static func currentLayoutStates(_ ids: [CGDirectDisplayID]) -> [SpatialLayoutDisplay]? {
        var states = [SpatialLayoutDisplay]()
        for id in ids {
            guard let mode = CGDisplayCopyDisplayMode(id) else { return nil }
            let bounds = CGDisplayBounds(id)
            let mirror = CGDisplayMirrorsDisplay(id)
            states.append(SpatialLayoutDisplay(id: id, width: mode.width, height: mode.height,
                                               pixelWidth: mode.pixelWidth, pixelHeight: mode.pixelHeight,
                                               refreshRate: mode.refreshRate, x: Double(bounds.minX), y: Double(bounds.minY),
                                               mirrorTarget: mirror == kCGNullDirectDisplay ? nil : mirror,
                                               isMain: CGDisplayIsMain(id) != 0))
        }
        return states
    }

    private func snapshotLayoutStates() -> [SpatialLayoutDisplay]? {
        guard let physicalLayout else { return nil }
        return physicalLayout.displays.map { saved in
            SpatialLayoutDisplay(id: saved.id, width: saved.mode.width, height: saved.mode.height,
                                 pixelWidth: saved.mode.pixelWidth, pixelHeight: saved.mode.pixelHeight,
                                 refreshRate: saved.mode.refreshRate, x: Double(saved.origin.x), y: Double(saved.origin.y),
                                 mirrorTarget: saved.mirrorTarget == kCGNullDirectDisplay ? nil : saved.mirrorTarget,
                                 isMain: saved.id == physicalLayout.mainDisplayID)
        }
    }

    private func currentPhysicalLayoutStates() -> ([CGDirectDisplayID], [SpatialLayoutDisplay])? {
        guard let ids = onlineDisplayIDs(), let all = Self.currentLayoutStates(ids) else { return nil }
        let owned = ownedSpatialDisplayIDs
        return (ids, all.filter { !owned.contains($0.id) })
    }

    /// Replays the original layout in a single app-only display transaction.
    /// The snapshot is kept until post-removal verification succeeds, so Stop
    /// can be retried if WindowServer has not settled yet.
    private func restorePhysicalLayout(completion: @escaping (Bool) -> Void) {
        guard let snapshotStates = snapshotLayoutStates(), let physicalLayout,
              let current = currentPhysicalLayoutStates() else {
            completion(physicalLayout == nil)
            return
        }
        let currentStates = current.1
        let owned = ownedSpatialDisplayIDs
        let originalIDs = Set(physicalLayout.displays.map(\.id))
        guard SpatialLayoutPolicy.hasExpectedDisplays(snapshotIDs: originalIDs, onlineIDs: current.0,
                                                       spatialDisplayIDs: owned) else {
            completion(false)
            return
        }
        guard SpatialLayoutPolicy.permitsRestore(snapshot: snapshotStates, current: currentStates,
                                                  spatialDisplayIDs: owned,
                                                  knownSessionLayout: restoreLayoutBaseline) else {
            completion(false)
            return
        }
        let requireMain = shared == nil
        if SpatialLayoutPolicy.isRestored(snapshot: snapshotStates, current: currentStates,
                                          spatialDisplayIDs: owned, requireMain: requireMain) {
            completion(true)
            return
        }

        let currentByID = Dictionary(uniqueKeysWithValues: currentStates.map { ($0.id, $0) })
        var config: CGDisplayConfigRef?
        guard CGBeginDisplayConfiguration(&config) == .success, let config else {
            completion(false)
            return
        }
        for saved in physicalLayout.displays {
            guard let actual = currentByID[saved.id] else {
                CGCancelDisplayConfiguration(config)
                completion(false)
                return
            }
            if !Self.modeMatches(actual, saved.mode),
               CGConfigureDisplayWithDisplayMode(config, saved.id, saved.mode, nil) != .success {
                CGCancelDisplayConfiguration(config)
                completion(false)
                return
            }
        }
        // Clear changed mirror links first, then put back the original slaves.
        for saved in physicalLayout.displays {
            let actual = currentByID[saved.id]!
            let target = saved.mirrorTarget == kCGNullDirectDisplay ? nil : saved.mirrorTarget
            if actual.mirrorTarget != target && target == nil,
               CGConfigureDisplayMirrorOfDisplay(config, saved.id, kCGNullDirectDisplay) != .success {
                CGCancelDisplayConfiguration(config)
                completion(false)
                return
            }
        }
        for saved in physicalLayout.displays {
            let actual = currentByID[saved.id]!
            let target = saved.mirrorTarget == kCGNullDirectDisplay ? nil : saved.mirrorTarget
            if actual.mirrorTarget != target, let target,
               CGConfigureDisplayMirrorOfDisplay(config, saved.id, target) != .success {
                CGCancelDisplayConfiguration(config)
                completion(false)
                return
            }
        }
        for saved in physicalLayout.displays where saved.mirrorTarget == kCGNullDirectDisplay {
            let actual = currentByID[saved.id]!
            let isOriginalMain = saved.id == physicalLayout.mainDisplayID
            if isOriginalMain || abs(actual.x - Double(saved.origin.x)) > 1 || abs(actual.y - Double(saved.origin.y)) > 1,
               CGConfigureDisplayOrigin(config, saved.id, Int32(saved.origin.x.rounded()),
                                         Int32(saved.origin.y.rounded())) != .success {
                CGCancelDisplayConfiguration(config)
                completion(false)
                return
            }
        }
        guard CGCompleteDisplayConfiguration(config, .forAppOnly) == .success else {
            completion(false)
            return
        }
        if let current = currentPhysicalLayoutStates() { restoreLayoutBaseline = current.1 }
        verifyPhysicalLayoutRestored(requireMain: requireMain, attempt: 0, completion: completion)
    }

    private static func modeMatches(_ state: SpatialLayoutDisplay, _ mode: CGDisplayMode) -> Bool {
        state.width == mode.width && state.height == mode.height && state.pixelWidth == mode.pixelWidth &&
            state.pixelHeight == mode.pixelHeight && abs(state.refreshRate - mode.refreshRate) < 0.1
    }

    private func verifyPhysicalLayoutRestored(requireMain: Bool, attempt: Int, completion: @escaping (Bool) -> Void) {
        if physicalLayoutMatchesSnapshot(requireMain: requireMain) { completion(true); return }
        if attempt >= 30 { completion(false); return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in
            guard let self else { completion(false); return }
            self.verifyPhysicalLayoutRestored(requireMain: requireMain, attempt: attempt + 1, completion: completion)
        }
    }

    private func physicalLayoutMatchesSnapshot(requireMain: Bool = true) -> Bool {
        guard let snapshot = snapshotLayoutStates(), let current = currentPhysicalLayoutStates() else { return false }
        return SpatialLayoutPolicy.isRestored(snapshot: snapshot, current: current.1,
                                              spatialDisplayIDs: ownedSpatialDisplayIDs, requireMain: requireMain)
    }

    private func originalsMatchCurrentFrames() -> Bool {
        originals = originals.filter { WindowInfo.find($0.key.id)?.pid == $0.key.pid }
        for (identity, expected) in originals {
            guard let actual = AXWindow(windowID: identity.id, pid: identity.pid)?.frame,
                  Self.closeEnough(actual, expected) else { return false }
        }
        return true
    }

    private func restoreAfterRemoval(completion: @escaping (Bool, String?) -> Void) {
        guard physicalLayout != nil else {
            finishConfirmedCleanup()
            completion(true, nil)
            return
        }
        guard let current = currentPhysicalLayoutStates() else {
            return completion(false, "WindowServer could not confirm the physical layout after display removal")
        }
        restoreLayoutBaseline = current.1
        restorePhysicalLayout { layoutRestored in
            guard layoutRestored else {
                return completion(false, "the original physical display layout is still not confirmed after removal")
            }
            self.restoreOriginals(attempt: 0) { framesRestored in
                guard framesRestored, self.physicalLayoutMatchesSnapshot(), self.originalsMatchCurrentFrames() else {
                    return completion(false, "the original physical layout or window frames changed after display removal")
                }
                self.finishConfirmedCleanup()
                completion(true, nil)
            }
        }
    }

    private func restoreOriginals(attempt: Int, completion: @escaping (Bool) -> Void) {
        var pending = [(SpatialWindowIdentity, CGRect)]()
        for (identity, frame) in originals {
            guard let info = WindowInfo.find(identity.id), info.pid == identity.pid else { continue }
            guard let window = AXWindow(windowID: identity.id, pid: identity.pid) else {
                pending.append((identity, frame)); continue
            }
            if let actual = window.frame, Self.closeEnough(actual, frame) { continue }
            window.setFrame(frame)
            if let actual = window.frame, Self.closeEnough(actual, frame) { continue }
            pending.append((identity, frame))
        }
        if pending.isEmpty { completion(true); return }
        if attempt >= 10 { completion(false); return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in
            guard let self else { completion(false); return }
            // Keep only windows that still exist; closed windows cannot be restored.
            self.originals = self.originals.filter { WindowInfo.find($0.key.id)?.pid == $0.key.pid }
            self.restoreOriginals(attempt: attempt + 1, completion: completion)
        }
    }

    private static func closeEnough(_ lhs: CGRect, _ rhs: CGRect) -> Bool {
        abs(lhs.minX - rhs.minX) <= 2 && abs(lhs.minY - rhs.minY) <= 2 &&
            abs(lhs.width - rhs.width) <= 2 && abs(lhs.height - rhs.height) <= 2
    }

    private func waitForRemoval(attempt: Int, completion: @escaping (Bool) -> Void) {
        if let online = Self.onlineSpatialIDs(), SpatialCleanupGate.confirmsRemoved(onlineSpatialDisplays: online.count) {
            completion(true)
            return
        }
        if attempt >= 50 { completion(false); return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in
            self?.waitForRemoval(attempt: attempt + 1, completion: completion)
        }
    }

    private func releaseSleepAssertion() {
        guard sleepAssertion != 0 else { return }
        IOPMAssertionRelease(sleepAssertion)
        sleepAssertion = 0
    }

    static func isEligible(_ info: WindowInfo) -> Bool {
        guard allowedPID(info.pid) else { return false }
        let app = NSRunningApplication(processIdentifier: info.pid)
        guard let window = AXWindow(windowID: info.id, pid: info.pid) else { return false }
        let candidate = SpatialWindowCandidate(layer: info.layer, width: info.bounds.width, height: info.bounds.height,
                                               pid: info.pid, appName: info.app,
                                               isRegularApplication: app?.activationPolicy == .regular,
                                               isHelperProcess: info.pid == ProcessInfo.processInfo.processIdentifier,
                                               isStandardWindow: window.isStandardWindow,
                                               isFullScreen: window.isFullScreen,
                                               isMinimized: window.isMinimized,
                                               isApplicationHidden: app?.isHidden ?? false)
        return SpatialWindowPolicy.isEligible(candidate)
    }

    private static func onlineSpatialIDs() -> [CGDirectDisplayID]? {
        onlineDisplayIDs()?.filter(VirtualDisplay.isSpatial)
    }

    private func rejectStart(_ message: String, removed: Bool,
                             completion: @escaping (Bool, String?, Bool) -> Void) {
        releaseLeases()
        setState(removed ? "stopped" : "error", error: message)
        completion(false, message, removed)
    }

    private func acquireLeases() -> (success: Bool, error: String?, removed: Bool) {
        guard spatialLeaseFD < 0, workspaceLeaseFD < 0 else {
            return (false, "the helper already holds a spatial display lease", false)
        }
        guard let spatial = Self.tryLock(Self.spatialLeasePath) else {
            return (false, "another spatial helper holds the per-user spatial lease", false)
        }
        spatialLeaseFD = spatial
        guard let workspace = Self.tryLock(Self.workspaceLeasePath) else {
            _ = close(spatialLeaseFD)
            spatialLeaseFD = -1
            return (false, "a Workspace helper holds the per-user display lease", Self.onlineSpatialIDs()?.isEmpty == true)
        }
        workspaceLeaseFD = workspace
        return (true, nil, true)
    }

    private func releaseLeases() {
        if workspaceLeaseFD >= 0 { _ = close(workspaceLeaseFD); workspaceLeaseFD = -1 }
        if spatialLeaseFD >= 0 { _ = close(spatialLeaseFD); spatialLeaseFD = -1 }
    }

    private func finishConfirmedCleanup() {
        displaysBeingRemoved.removeAll()
        releaseSleepAssertion()
        builtIn = nil
        sharedDisplayID = nil
        physicalLayout = nil
        activePhysicalLayout = nil
        restoreLayoutBaseline = nil
        originals.removeAll()
        lock.lock(); windowJSON.removeAll(); skipped.removeAll(); fingerprint = Data(); revision &+= 1; lock.unlock()
        _ = persistOriginals()
        releaseLeases()
    }

    static func allowedPID(_ pid: pid_t) -> Bool {
        guard let raw = ProcessInfo.processInfo.environment["FRAME_SPATIAL_ONLY_PIDS"] else { return true }
        return raw.split(separator: ",").compactMap { Int32($0) }.contains(pid)
    }

    private var originalsURL: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(
            "Library/Application Support/Frame Control/spatial-originals.json")
    }

    private func persistOriginals() -> Bool {
        do {
            if originals.isEmpty {
                if FileManager.default.fileExists(atPath: originalsURL.path) { try FileManager.default.removeItem(at: originalsURL) }
                return true
            }
            let records = originals.map { identity, rect -> [String: Any] in
                ["id": identity.id, "pid": identity.pid, "rect": SpatialSharedPolicy.rectJSON(rect)]
            }
            try FileManager.default.createDirectory(at: originalsURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            try JSONSerialization.data(withJSONObject: records, options: [.sortedKeys]).write(to: originalsURL, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: originalsURL.path)
            return true
        } catch {
            FileHandle.standardError.write(Data("frame-mac-view: could not persist spatial originals: \(error)\n".utf8))
            return false
        }
    }

    func restorePersistedAtStartup() {
        guard Thread.isMainThread, FileManager.default.fileExists(atPath: originalsURL.path) else { return }
        guard acquireLeases().success else { setState("error", error: "Another Helper Owns Pending Spatial Originals"); return }
        do {
            let data = try Data(contentsOf: originalsURL)
            guard let records = try JSONSerialization.jsonObject(with: data) as? [[String: Any]] else {
                throw CocoaError(.fileReadCorruptFile)
            }
            for record in records {
                guard let id = record["id"] as? UInt32, let pid = record["pid"] as? Int32,
                      let rect = record["rect"] as? [Double], rect.count == 4,
                      rect.allSatisfy(\.isFinite), rect[2] > 0, rect[3] > 0 else { throw CocoaError(.fileReadCorruptFile) }
                originals[SpatialWindowIdentity(id: id, pid: pid)] = CGRect(x: rect[0], y: rect[1], width: rect[2], height: rect[3])
            }
            // Fixture restrictions also apply to recovery; never mutate an unlisted process.
            guard originals.keys.allSatisfy({ Self.allowedPID($0.pid) }) else {
                setState("error", error: "Pending Originals Include Processes Outside FRAME_SPATIAL_ONLY_PIDS")
                return
            }
            setState("stopping")
            restoreOriginals(attempt: 0) { restored in
                if restored { self.originals.removeAll(); _ = self.persistOriginals(); self.releaseLeases(); self.setState("stopped") }
                else { self.setState("error", error: "Startup Could Not Restore Spatial Originals") }
            }
        } catch { setState("error", error: "Could Not Read Persisted Spatial Originals: \(error)") }
    }

    private static var spatialLeasePath: String { "/tmp/frame-spatial-\(getuid()).lock" }
    private static var workspaceLeasePath: String { "/tmp/frame-workspace-\(getuid()).lock" }

    private static func tryLock(_ path: String) -> Int32? {
        let fd = open(path, O_CREAT | O_RDWR | O_NOFOLLOW, mode_t(S_IRUSR | S_IWUSR))
        guard fd >= 0 else { return nil }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else { _ = close(fd); return nil }
        return fd
    }
}
