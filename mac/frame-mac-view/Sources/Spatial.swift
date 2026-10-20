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
        var value: [String: Any] = ["id": id, "src": "window:\(id)", "pid": pid,
                                   "app": app, "title": title,
                                   "w": Int(bounds.width), "h": Int(bounds.height),
                                   "x": Double(bounds.minX), "y": Double(bounds.minY)]
        if let bundleID { value["bundleID"] = bundleID }
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

/// A lease is returned only after its display has adopted the requested mode
/// and WindowServer exposes the corresponding NSScreen.
final class SpatialDisplayLease {
    let display: VirtualDisplay
    let slot: Int
    let owner: UUID
    private let releaseBody: (SpatialDisplayLease, @escaping () -> Void) -> Void
    private var released = false

    init(display: VirtualDisplay, slot: Int, owner: UUID,
         release: @escaping (SpatialDisplayLease, @escaping () -> Void) -> Void) {
        self.display = display
        self.slot = slot
        self.owner = owner
        releaseBody = release
    }

    func release(completion: @escaping () -> Void) {
        DispatchQueue.main.async {
            guard !self.released else { completion(); return }
            self.released = true
            self.releaseBody(self, completion)
        }
    }
}

/// Owns a small set of stable virtual-display identities. Displays are
/// reconfigured and reused between streams; none are removed during churn.
final class SpatialDisplayPool {
    static let initialCount = SpatialPoolPolicy.initialPoolSize
    static let warmSpareCount = SpatialPoolPolicy.warmSpareCount
    static let maximumCount = SpatialPoolPolicy.maximumPoolSize
    static let seedWidth = 1280
    static let seedHeight = 800

    private var policy = SpatialPoolPolicy(capacity: maximumCount)
    private var displays: [Int: VirtualDisplay] = [:]
    private var refillsStopped = false
    private var refillScheduled = false
    private var refillWaiters: [() -> Void] = []
    private var lastRefillError: String?

    var displayIDs: [CGDirectDisplayID] { displays.values.map(\.id).sorted() }
    var activeLeaseCount: Int { policy.outstandingCount }

    func prewarm(completion: @escaping (String?) -> Void) {
        guard Thread.isMainThread else {
            DispatchQueue.main.async { self.prewarm(completion: completion) }
            return
        }
        createInitialSlot(0, completion: completion)
    }

    private func createInitialSlot(_ slot: Int, completion: @escaping (String?) -> Void) {
        guard slot < Self.initialCount else { completion(nil); return }
        guard let display = VirtualDisplay(name: "Frame Spatial Window \(slot + 1)", width: Self.seedWidth,
                                           height: Self.seedHeight, pixelScale: 2, purpose: .spatialWindow) else {
            return completion("macOS would not create a pooled window display")
        }
        displays[slot] = display
        waitForModeReady(display, width: Self.seedWidth, height: Self.seedHeight, pixelScale: 2, attempt: 0) { ready in
            guard ready else { return completion("a pooled window display did not become ready") }
            guard self.policy.registerFree(slot: slot) else { return completion("could not register a pooled display") }
            self.createInitialSlot(slot + 1, completion: completion)
        }
    }

    func lease(windowID: CGWindowID, width: Int, height: Int, owner: UUID,
               completion: @escaping (SpatialDisplayLease?, String?) -> Void) {
        guard Thread.isMainThread else {
            DispatchQueue.main.async { self.lease(windowID: windowID, width: width, height: height,
                                                  owner: owner, completion: completion) }
            return
        }
        guard policy.beginLeasePreparation(owner: owner) else {
            return completion(nil, "this spatial stream already has a pending display lease")
        }
        let finish: (SpatialDisplayLease?, String?) -> Void = { [weak self] lease, error in
            guard let self else {
                lease?.release(completion: {})
                completion(nil, "the spatial display pool stopped during lease preparation")
                return
            }
            _ = self.policy.finishLeasePreparation(owner: owner)
            completion(lease, error)
        }
        switch policy.acquire(owner: owner) {
        case .alreadyLeased:
            finish(nil, "this spatial stream already has a display lease")
        case .alreadyPreparing:
            finish(nil, "this spatial stream already has a pending display lease")
        case .full:
            let detail = lastRefillError.map { "; last spare refill failed: \($0)" } ?? ""
            finish(nil, "the spatial display safety ceiling of \(Self.maximumCount) slots was reached (\(policy.failedCount) failed slots cached)\(detail)")
        case .lease(let slot):
            guard let display = displays[slot] else {
                _ = policy.release(slot: slot, owner: owner)
                return finish(nil, "a pooled display is unavailable")
            }
            requestWarmRefillIfNeeded()
            configure(display, slot: slot, owner: owner, width: width, height: height, completion: finish)
        case .create(let slot):
            guard let display = VirtualDisplay(name: "Frame Spatial Window \(slot + 1)", width: width,
                                               height: height, pixelScale: 2, purpose: .spatialWindow) else {
                _ = policy.failCreation(slot: slot, owner: owner)
                return finish(nil, "macOS would not add a pooled window display")
            }
            displays[slot] = display
            requestWarmRefillIfNeeded()
            waitForModeReady(display, width: width, height: height, pixelScale: 2, attempt: 0) { ready in
                guard ready else {
                    _ = self.policy.failCreation(slot: slot, owner: owner)
                    finish(nil, "a refilled window display did not become ready")
                    return
                }
                guard self.policy.finishCreation(slot: slot, owner: owner) else {
                    finish(nil, "could not register a refilled display")
                    return
                }
                finish(self.makeLease(display, slot: slot, owner: owner), nil)
            }
        }
    }

    private func configure(_ display: VirtualDisplay, slot: Int, owner: UUID, width: Int, height: Int,
                           completion: @escaping (SpatialDisplayLease?, String?) -> Void) {
        guard policy.leaseIsCurrent(slot: slot, owner: owner) else {
            return completion(nil, "the spatial display lease was lost")
        }
        let configured = display.reconfigure(width: width, height: height, pixelScale: 2)
        guard configured else {
            _ = policy.release(slot: slot, owner: owner)
            completion(nil, display.lastModeConfigurationError ?? "macOS did not apply the requested window display mode")
            requestWarmRefillIfNeeded()
            return
        }
        waitForModeReady(display, width: width, height: height, pixelScale: 2, attempt: 0) { ready in
            guard ready else {
                _ = self.policy.release(slot: slot, owner: owner)
                self.requestWarmRefillIfNeeded()
                return completion(nil, display.lastModeConfigurationError ?? "macOS did not apply the requested window display mode")
            }
            completion(self.makeLease(display, slot: slot, owner: owner), nil)
        }
    }

    private func makeLease(_ display: VirtualDisplay, slot: Int, owner: UUID) -> SpatialDisplayLease {
        SpatialDisplayLease(display: display, slot: slot, owner: owner) { [weak self] lease, done in
            guard let self else { return done() }
            let released = self.policy.release(slot: lease.slot, owner: lease.owner)
            if released { self.requestWarmRefillIfNeeded() }
            done()
        }
    }

    /// Polls publication of a mode that was configured once by creation or
    /// `reconfigure`; polling must not repeat display configuration calls.
    private func waitForModeReady(_ display: VirtualDisplay, width: Int, height: Int, pixelScale: Int, attempt: Int,
                                  completion: @escaping (Bool) -> Void) {
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
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in
                self?.waitForModeReady(display, width: width, height: height, pixelScale: pixelScale,
                                       attempt: attempt + 1, completion: completion)
            }
        }
    }

    private func requestWarmRefillIfNeeded() {
        guard Thread.isMainThread, !refillsStopped, !refillScheduled else { return }
        refillScheduled = true
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.refillScheduled = false
            guard !self.refillsStopped, let slot = self.policy.beginWarmRefill() else { return }
            self.createWarmSpare(slot: slot)
        }
    }

    private func createWarmSpare(slot: Int) {
        let name = "Frame Spatial Window \(slot + 1)"
        guard let display = VirtualDisplay(name: name, width: Self.seedWidth, height: Self.seedHeight,
                                           pixelScale: 2, purpose: .spatialWindow) else {
            _ = policy.failWarmRefill(slot: slot)
            lastRefillError = "macOS would not create a warm spare display"
            completeRefillWaitersIfReady()
            return
        }
        displays[slot] = display
        waitForModeReady(display, width: Self.seedWidth, height: Self.seedHeight, pixelScale: 2, attempt: 0) { ready in
            if ready {
                if !self.policy.finishWarmRefill(slot: slot) {
                    self.lastRefillError = "warm spare ownership changed during creation"
                } else { self.lastRefillError = nil }
            } else {
                _ = self.policy.failWarmRefill(slot: slot)
                self.lastRefillError = "a warm spare display did not become ready"
            }
            self.completeRefillWaitersIfReady()
            if ready { self.requestWarmRefillIfNeeded() }
        }
    }

    func stopRefillingAndWait(_ completion: @escaping () -> Void) {
        guard Thread.isMainThread else {
            DispatchQueue.main.async { self.stopRefillingAndWait(completion) }
            return
        }
        refillsStopped = true
        if policy.backgroundPreparationCount == 0 { completion() }
        else { refillWaiters.append(completion) }
    }

    private func completeRefillWaitersIfReady() {
        guard policy.backgroundPreparationCount == 0 else { return }
        let callbacks = refillWaiters
        refillWaiters.removeAll()
        callbacks.forEach { $0() }
    }

    func releaseAllDisplays() -> [VirtualDisplay] {
        let values = displays.values.sorted { $0.id < $1.id }
        for display in values { display.release() }
        displays.removeAll()
        return values
    }
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
            $0.processIdentifier != ownPID && $0.activationPolicy == .regular
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

/// The hidden home display, mirror transaction, window snapshots and pool all
/// share one lifecycle so every moved window is returned before display removal.
final class SpatialCoordinator {
    private let lock = NSLock()
    private var state = "stopped"
    private var lastError: String?
    private var revision = 0
    private var windowJSON: [[String: Any]] = []
    private var fingerprint = Data()

    private var home: VirtualDisplay?
    private var pool: SpatialDisplayPool?
    private var displaysBeingRemoved: [VirtualDisplay] = []
    private var builtIn: CGDirectDisplayID?
    private var homeDisplayID: CGDirectDisplayID?
    private var physicalLayout: SpatialPhysicalLayoutSnapshot?
    private var activePhysicalLayout: [SpatialLayoutDisplay]?
    private var restoreLayoutBaseline: [SpatialLayoutDisplay]?
    private var originals: [SpatialWindowIdentity: CGRect] = [:]
    private var mirrorApplied = false
    private var sleepAssertion: IOPMAssertionID = 0
    private var spatialLeaseFD: Int32 = -1
    private var workspaceLeaseFD: Int32 = -1
    private lazy var discovery = SpatialWindowDiscovery(owner: self)
    private var queuedStop: ((@escaping () -> Void) -> Void, (Bool, String?, Bool) -> Void)?

    var stateValue: String { lock.lock(); defer { lock.unlock() }; return state }
    var isRunning: Bool { stateValue == "running" }
    var isStopping: Bool { stateValue == "stopping" }

    func statusJSON() -> [String: Any] {
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
        var result: [String: Any] = ["state": state, "revision": revision, "windows": windowJSON]
        if let lastError { result["error"] = lastError }
        return result
    }

    func windowsJSON() -> [[String: Any]] {
        lock.lock(); defer { lock.unlock() }
        return windowJSON
    }

    /// Saves the pre-separation frame on main. Existing snapshots taken before
    /// mirroring win; newly opened windows get their first discovered frame.
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
        return true
    }

    func originalFrame(windowID: CGWindowID, pid: pid_t) -> CGRect? {
        guard Thread.isMainThread else { return nil }
        return originals[SpatialWindowIdentity(id: windowID, pid: pid)]
    }

    /// Move a stopped/reconnecting stream's Mac window onto the shared home
    /// display before its private pool slot can be reassigned. The mode-wide
    /// original frame remains in `originals` for final restoration after the
    /// physical display is unmirrored.
    func parkWindowOnHomeForLeaseRelease(windowID: CGWindowID, pid: pid_t, window: AXWindow) -> Bool {
        guard Thread.isMainThread, stateValue == "running" || stateValue == "stopping",
              let home, let targetFrame = SpatialWindowPolicy.parkedFrame(windowID: windowID,
                                                                         size: window.frame?.size ?? home.usableRect.size,
                                                                         homeRect: home.usableRect) else { return false }
        guard let info = WindowInfo.find(windowID), info.pid == pid else { return true }
        window.setFrame(targetFrame)
        guard let actual = window.frame else { return false }
        return home.usableRect.insetBy(dx: -2, dy: -2).contains(actual)
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
        if captureFrames {
            for info in infos { captureOriginalIfNeeded(windowID: info.id, pid: info.pid) }
            originals = originals.filter { alive.contains($0.key) }
        }
        let json = records.map(\.json)
        let bytes = (try? JSONSerialization.data(withJSONObject: json, options: [.sortedKeys])) ?? Data()
        lock.lock()
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
                guard self.home == nil, self.pool == nil, self.displaysBeingRemoved.isEmpty,
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
        originals.removeAll()
        let existingWindows = WindowInfo.all(onScreenOnly: false).filter(Self.isEligible)
        for info in existingWindows {
            _ = captureOriginalIfNeeded(windowID: info.id, pid: info.pid)
        }
        let unsnapshotted = existingWindows.filter {
            originals[SpatialWindowIdentity(id: $0.id, pid: $0.pid)] == nil
        }
        guard unsnapshotted.isEmpty else {
            return failStart("could not save the original frame for every eligible window before mirroring",
                             completion: completion)
        }
        let assertion = IOPMAssertionCreateWithName(kIOPMAssertionTypePreventUserIdleDisplaySleep as CFString,
                                                    IOPMAssertionLevel(kIOPMAssertionLevelOn),
                                                    "Frame Control spatial displays are active" as CFString,
                                                    &sleepAssertion)
        guard assertion == kIOReturnSuccess else {
            return failStart("macOS would not prevent display sleep while spatial displays are active", completion: completion)
        }
        guard let display = VirtualDisplay(name: "Frame Spatial Home", width: 1280, height: 800,
                                           pixelScale: 1, purpose: .spatialHome) else {
            return failStart("macOS would not create the spatial home display", completion: completion)
        }
        home = display
        homeDisplayID = display.id
        if let current = currentPhysicalLayoutStates() { restoreLayoutBaseline = current.1 }
        waitForModeReady(display, width: 1280, height: 800, pixelScale: 1, attempt: 0) { ready in
            guard ready else { return self.failStart("the spatial home display did not become ready", completion: completion) }
            let pool = SpatialDisplayPool()
            self.pool = pool
            pool.prewarm { error in
                if let current = self.currentPhysicalLayoutStates() { self.restoreLayoutBaseline = current.1 }
                if let error { return self.failStart(error, completion: completion) }
                guard self.applyMirror(builtIn: builtIn, home: display.id) else {
                    return self.failStart("macOS rejected the built-in display mirror transaction", completion: completion)
                }
                self.mirrorApplied = true
                self.verifyMirror(builtIn: builtIn, home: display.id, attempt: 0) { verified in
                    guard verified else { return self.failStart("the spatial home display did not become the main display", completion: completion) }
                    guard let activeLayout = self.currentPhysicalLayoutStates()?.1 else {
                        return self.failStart("WindowServer could not confirm the physical layout after mirroring", completion: completion)
                    }
                    self.activePhysicalLayout = activeLayout
                    self.restoreLayoutBaseline = activeLayout
                    self.discovery.start()
                    self.setState("running")
                    completion(true, nil, true)
                    self.runQueuedStopIfNeeded()
                }
            }
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

    func leaseDisplay(windowID: CGWindowID, width: Int, height: Int, owner: UUID,
                      completion: @escaping (SpatialDisplayLease?, String?) -> Void) {
        DispatchQueue.main.async {
            guard self.stateValue == "running", let pool = self.pool else {
                return completion(nil, "spatial display pool is not running")
            }
            pool.lease(windowID: windowID, width: width, height: height, owner: owner, completion: completion)
        }
    }

    /// Stop admission first, then let Agent await every SeparateSource restore
    /// before unmirroring the physical screen and removing the pool.
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
            let hasLocalOwnership = self.spatialLeaseFD >= 0 || self.home != nil || self.pool != nil ||
                !self.displaysBeingRemoved.isEmpty
            if !hasLocalOwnership {
                guard let probe = Self.tryLock(Self.spatialLeasePath) else {
                    self.setState("error", error: "another spatial helper still holds the per-user lease")
                    return completion(false, "another spatial helper still holds the per-user lease", false)
                }
                self.spatialLeaseFD = probe
            }
            if current == "stopped" && self.home == nil && self.pool == nil && self.displaysBeingRemoved.isEmpty {
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
        discovery.stop()
        if let pool {
            pool.stopRefillingAndWait { self.finishCleanupAfterRefills(message: message, completion: completion) }
        } else {
            finishCleanupAfterRefills(message: message, completion: completion)
        }
    }

    private func finishCleanupAfterRefills(message: String?, completion: @escaping (Bool, String?) -> Void) {
        let outstandingLeases = pool?.activeLeaseCount ?? 0
        guard SpatialCleanupGate.hasNoOutstandingLeases(outstandingLeases) else {
            return completion(false, "one or more spatial window displays are still leased")
        }
        if home == nil, pool == nil, !displaysBeingRemoved.isEmpty {
            return waitForRemoval(attempt: 0) { didRemove in
                guard didRemove else { return completion(false, "spatial display removal is still pending") }
                self.restoreAfterRemoval(completion: completion)
            }
        }
        unmirrorIfNeeded { unmirrored, unmirrorError in
            guard unmirrored else { return completion(false, unmirrorError ?? "could not undo display mirroring") }
            guard let unmirroredLayout = self.currentPhysicalLayoutStates()?.1 else {
                return completion(false, "WindowServer could not confirm the physical layout after unmirroring")
            }
            self.restoreLayoutBaseline = unmirroredLayout
            self.restorePhysicalLayout { layoutRestored in
                guard layoutRestored else {
                    return completion(false, "the original physical display layout could not be restored; retained it for retry")
                }
                self.mirrorApplied = false
                self.restoreOriginals(attempt: 0) { framesRestored in
                    let stillLeased = self.pool?.activeLeaseCount ?? 0
                    guard SpatialCleanupGate.mayReleaseDisplays(unmirrored: unmirrored, windowsRestored: framesRestored,
                                                                 outstandingLeases: stillLeased) else {
                        return completion(false, framesRestored ? "one or more spatial window displays are still leased" :
                            "some window frames could not be restored after unmirroring")
                    }
                    guard self.physicalLayoutMatchesSnapshot(requireMain: false) && self.originalsMatchCurrentFrames() else {
                        return completion(false, "display layout or window frames changed before spatial display removal")
                    }
                    var removed = self.pool?.releaseAllDisplays() ?? []
                    if let home = self.home { home.release(); removed.append(home) }
                    self.home = nil
                    self.pool = nil
                    self.displaysBeingRemoved = removed
                    self.waitForRemoval(attempt: 0) { didRemove in
                        guard didRemove else {
                            return completion(false, "spatial display removal is still pending; the helper retained restoration state")
                        }
                        self.restoreAfterRemoval(completion: completion)
                    }
                }
            }
        }
    }

    private func verifyMirror(builtIn: CGDirectDisplayID, home: CGDirectDisplayID, attempt: Int,
                              completion: @escaping (Bool) -> Void) {
        let bounds = CGDisplayBounds(home)
        if CGDisplayMirrorsDisplay(builtIn) == home, CGDisplayIsMain(home) != 0,
           abs(bounds.minX) < 1, abs(bounds.minY) < 1 { completion(true) }
        else if attempt >= 30 { completion(false) }
        else { DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { self.verifyMirror(builtIn: builtIn, home: home, attempt: attempt + 1, completion: completion) } }
    }

    private func applyMirror(builtIn: CGDirectDisplayID, home: CGDirectDisplayID) -> Bool {
        var config: CGDisplayConfigRef?
        guard CGBeginDisplayConfiguration(&config) == .success, let config else { return false }
        let mirror = CGConfigureDisplayMirrorOfDisplay(config, builtIn, home)
        let origin = CGConfigureDisplayOrigin(config, home, 0, 0)
        guard mirror == .success, origin == .success else {
            CGCancelDisplayConfiguration(config)
            return false
        }
        return CGCompleteDisplayConfiguration(config, .forAppOnly) == .success
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

    private func unmirrorIfNeeded(completion: @escaping (Bool, String?) -> Void) {
        guard let builtIn, let home else {
            guard let ids = Self.onlineSpatialIDs() else {
                completion(false, "WindowServer could not confirm whether spatial displays remain registered")
                return
            }
            completion(ids.isEmpty, ids.isEmpty ? nil : "spatial displays remain registered without a live owner")
            return
        }
        if mirrorApplied, let activePhysicalLayout {
            // Something else changed the layout during the session. Undoing our
            // own mirror of the built-in is still safe (it only reverses what we
            // did) and leaving the laptop screen mirrored would strand the user,
            // so log what differs and carry on.
            let current = currentPhysicalLayoutStates()
            if current == nil || !SpatialLayoutPolicy.isRestored(snapshot: activePhysicalLayout, current: current!.1,
                                                                 spatialDisplayIDs: ownedSpatialDisplayIDs) {
                FileHandle.standardError.write(Data(("frame-mac-view: layout differs from the session's at stop; " +
                    "undoing our mirror anyway. session=\(activePhysicalLayout) current=\(String(describing: current?.1)) " +
                    "owned=\(ownedSpatialDisplayIDs.sorted())\n").utf8))
            }
        }
        let current = CGDisplayMirrorsDisplay(builtIn)
        guard current == home.id || current == kCGNullDirectDisplay else {
            completion(false, "the built-in display is mirrored by another display; left untouched")
            return
        }
        guard current == home.id else { completion(true, nil); return }
        var config: CGDisplayConfigRef?
        guard CGBeginDisplayConfiguration(&config) == .success, let config else {
            completion(false, "could not begin the display unmirror transaction")
            return
        }
        let changed = CGConfigureDisplayMirrorOfDisplay(config, builtIn, kCGNullDirectDisplay)
        guard changed == .success else {
            CGCancelDisplayConfiguration(config)
            completion(false, "could not undo the built-in display mirror")
            return
        }
        let applied = CGCompleteDisplayConfiguration(config, .forAppOnly) == .success
        if applied {
            mirrorApplied = false
            completion(true, nil)
        } else { completion(false, "macOS did not apply the display unmirror transaction") }
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
        if let home { ids.insert(home.id) }
        if let homeDisplayID { ids.insert(homeDisplayID) }
        if let pool { ids.formUnion(pool.displayIDs) }
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
              let builtIn, let current = currentPhysicalLayoutStates() else {
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
                                                  spatialDisplayIDs: owned, ownedMirrorSource: builtIn,
                                                  ownedMirrorTarget: homeDisplayID,
                                                  knownSessionLayout: restoreLayoutBaseline) else {
            completion(false)
            return
        }
        let requireMain = home == nil && pool == nil
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
            self.mirrorApplied = false
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
        homeDisplayID = nil
        physicalLayout = nil
        activePhysicalLayout = nil
        restoreLayoutBaseline = nil
        originals.removeAll()
        releaseLeases()
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
