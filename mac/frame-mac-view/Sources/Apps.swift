// Read-only Dock app discovery and explicit app actions for the spatial
// companion, plus the leased Cmd-Tab event tap used only in spatial mode, and
// the launcher's app catalogue and keyboard lease (LauncherCatalog.swift,
// LauncherKeys.swift).
import AppKit
import ApplicationServices
import CoreFoundation
import CoreServices
import Foundation

final class MacApps {
    let switcher = SwitcherController()
    private let workspace = NSWorkspace.shared
    private var observers: [NSObjectProtocol] = []
    private let iconCache = NSCache<NSString, NSData>()
    private var launchOrder: [String] = []
    private let catalogQueue = DispatchQueue(label: "frame-mac-view.launcher", qos: .utility)
    private var catalog: (built: TimeInterval, json: [String: Any])?

    init() {
        let running = regularApps()
        let front = workspace.frontmostApplication?.bundleIdentifier
        let launchOrdered = running.enumerated().sorted {
            let first = $0.element.launchDate ?? .distantPast
            let second = $1.element.launchDate ?? .distantPast
            return first == second ? $0.offset < $1.offset : first < second
        }.compactMap { $0.element.bundleIdentifier }
        launchOrder = launchOrdered.unique()
        let rest = Array(launchOrdered.reversed())
        switcher.state.seedMRU(([front].compactMap { $0 } + rest).unique())

        let center = workspace.notificationCenter
        observers.append(center.addObserver(forName: NSWorkspace.didActivateApplicationNotification,
                                            object: nil, queue: .main) { [weak self] notification in
            guard let app = (notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication)
                ?? (notification.object as? NSRunningApplication),
                  let bundleID = app.bundleIdentifier else { return }
            guard let self else { return }
            if !self.launchOrder.contains(bundleID) { self.launchOrder.append(bundleID) }
            self.switcher.state.noteActivation(bundleID)
        })
        observers.append(center.addObserver(forName: NSWorkspace.didLaunchApplicationNotification,
                                            object: nil, queue: .main) { [weak self] notification in
            guard let app = (notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication)
                ?? (notification.object as? NSRunningApplication),
                  let bundleID = app.bundleIdentifier else { return }
            if self?.launchOrder.contains(bundleID) == false { self?.launchOrder.append(bundleID) }
        })
        observers.append(center.addObserver(forName: NSWorkspace.didTerminateApplicationNotification,
                                            object: nil, queue: .main) { [weak self] notification in
            guard let app = (notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication)
                ?? (notification.object as? NSRunningApplication),
                  let bundleID = app.bundleIdentifier else { return }
            self?.launchOrder.removeAll { $0 == bundleID }
            self?.switcher.state.noteTermination(bundleID)
        })
    }

    func stop() {
        switcher.stop()
        for observer in observers { workspace.notificationCenter.removeObserver(observer) }
        observers.removeAll()
    }

    func appsResponse(_ connection: HTTPConnection) {
        DispatchQueue.main.async { connection.respond(json: ["apps": self.appsJSON()]) }
    }

    func iconResponse(_ request: Request, _ connection: HTTPConnection) {
        guard let bundleID = request.query["bundleID"], SwitcherPanel.validBundleID(bundleID) else {
            return connection.respond(400, text: "bundleID is required")
        }
        DispatchQueue.main.async {
            guard let (data, version) = self.iconPNG(bundleID: bundleID) else {
                return connection.respond(404, text: "application icon not found")
            }
            connection.respond(json: ["png": data.base64EncodedString(), "version": version])
        }
    }

    // MARK: Launcher (FW-15 A)

    /// GET /launcher: the catalogue. POST /launcher/keys?verb=open|close: the
    /// keyboard lease. GET /launcher/keys?after=N: typed keys, renewing the lease.
    func launcherResponse(_ request: Request, _ connection: HTTPConnection) {
        switch (request.method, request.path) {
        case ("GET", "/launcher"):
            catalogResponse(fresh: request.query["fresh"] == "1", connection)
        case ("GET", "/launcher/keys"):
            let after = Int(request.query["after"] ?? "") ?? 0
            connection.respond(json: switcher.launcherKeys.snapshot(after: after,
                                                                    renewAt: ProcessInfo.processInfo.systemUptime).json)
        case ("POST", "/launcher/keys"):
            guard let verb = request.query["verb"], verb == "open" || verb == "close" else {
                return connection.respond(400, text: "verb=open|close")
            }
            DispatchQueue.main.async {
                connection.respond(json: self.switcher.launcherLease(open: verb == "open"))
            }
        default:
            connection.respond(404, text: "not found")
        }
    }

    private func catalogResponse(fresh: Bool, _ connection: HTTPConnection) {
        catalogQueue.async {
            let now = ProcessInfo.processInfo.systemUptime
            if !fresh, let catalog = self.catalog, now - catalog.built < 10 {
                return connection.respond(json: catalog.json)
            }
            let json = self.buildCatalog()
            self.catalog = (ProcessInfo.processInfo.systemUptime, json)
            connection.respond(json: json)
        }
    }

    /// Launch Services' application folders, the Launchpad database's order
    /// and folders where it still exists, and Spotlight's last-used dates.
    private func buildCatalog() -> [String: Any] {
        let apps = LauncherCatalog.scan(roots: LauncherCatalog.defaultRoots, extras: LauncherCatalog.defaultExtras,
                                        preferred: { NSWorkspace.shared.urlForApplication(withBundleIdentifier: $0) })
        let launchpad = LaunchpadDatabase.defaultURL().map { LaunchpadDatabase.read($0) } ?? .missing
        let items = LauncherCatalog.merge(launchpad: launchpad.entries, apps: apps)
        var used: [(String, Date)] = []
        for app in apps {
            guard let item = MDItemCreateWithURL(kCFAllocatorDefault, URL(fileURLWithPath: app.path) as CFURL),
                  let date = MDItemCopyAttribute(item, kMDItemLastUsedDate) as? Date else { continue }
            used.append((app.bundleID, date))
        }
        return LauncherCatalog.json(apps: apps, items: items, recent: LauncherCatalog.recent(used),
                                    launchpad: launchpad.label)
    }

    /// One line for a hop of a shelf click, in the helper's log (mac-helper.log beside the
    /// app's server.log; stderr, so it is never held back in a buffer).
    static func dockLog(_ text: String) {
        FileHandle.standardError.write(Data("frame-mac-view: dock: \(text)\n".utf8))
    }

    func actionResponse(_ request: Request, _ connection: HTTPConnection) {
        switch DockAction.parse(bundleID: request.query["bundleID"], verb: request.query["verb"], id: request.query["id"]) {
        case .failure(.bundleID):
            MacApps.dockLog("action rejected id=\(request.query["id"] ?? "-"): bundleID is required")
            return connection.respond(400, text: "bundleID is required")
        case .failure(.verb):
            MacApps.dockLog("action rejected id=\(request.query["id"] ?? "-"): verb=open|hide|unhide|quit|show_windows")
            return connection.respond(400, text: "verb=open|hide|unhide|quit|show_windows")
        case .success(let action):
            MacApps.dockLog("action received \(action.label)")
            DispatchQueue.main.async {
                self.perform(action, connection: connection)
            }
        }
    }

    func switcherResponse(_ request: Request, _ connection: HTTPConnection, extras: [String: Any] = [:]) {
        connection.respond(json: switcher.state.snapshot().json.merging(extras) { _, value in value })
    }

    func configureSwitcherResponse(_ request: Request, _ connection: HTTPConnection) {
        guard let mode = request.query["mode"], mode == "spatial" || mode == "desktop" else {
            return connection.respond(400, text: "mode=spatial|desktop")
        }
        let entries: [SwitcherPanel]?
        if let rawEntries = request.query["entries"] {
            do {
                entries = try SwitcherPanel.parseEntries(rawEntries)
            } catch {
                return connection.respond(400, text: "Invalid switcher entries")
            }
        } else {
            entries = nil
        }
        let verb = request.query["verb"]
        guard verb == nil || verb == "open" || verb == "close" || verb == "choose" else {
            return connection.respond(400, text: "verb=open|close|choose")
        }
        guard mode == "spatial" || (verb != "open" && verb != "choose") else {
            return connection.respond(400, text: "verb=\(verb ?? "") requires mode=spatial")
        }
        // choose: bring this panel forward in the headset, as a Cmd-Tab release does.
        let key = request.query["key"]
        guard verb != "choose" || key.map(SwitcherPanel.validWindowKey) == true else {
            return connection.respond(400, text: "verb=choose needs a panel key")
        }
        DispatchQueue.main.async {
            self.switcher.configure(mode: mode, entries: entries, verb: verb, key: key)
            connection.respond(json: self.switcher.state.snapshot().json)
        }
    }

    private func regularApps() -> [NSRunningApplication] {
        workspace.runningApplications.filter {
            $0.activationPolicy == .regular && !$0.isTerminated && $0.bundleIdentifier != nil
        }
    }

    private struct PinnedApplication {
        let bundleID: String
        let url: URL?
    }

    /// Reads Dock's existing persistent-apps preference. This path never
    /// writes or synchronizes the Dock domain.
    private func pinnedApps() -> [PinnedApplication] {
        guard let value = CFPreferencesCopyAppValue("persistent-apps" as CFString, "com.apple.dock" as CFString),
              let rows = value as? [[String: Any]] else { return [] }
        var seen = Set<String>()
        return rows.compactMap { row in
            let tile = row["tile-data"] as? [String: Any] ?? [:]
            let fileData = tile["file-data"] as? [String: Any] ?? [:]
            var url: URL?
            if let raw = fileData["_CFURLString"] as? String {
                url = URL(string: raw)
            }
            let bundleID = (tile["bundle-identifier"] as? String)
                ?? url.flatMap { Bundle(url: $0)?.bundleIdentifier }
            guard let bundleID, SwitcherPanel.validBundleID(bundleID), seen.insert(bundleID).inserted else { return nil }
            return PinnedApplication(bundleID: bundleID, url: url)
        }
    }

    private func applicationURL(bundleID: String, pinned: [PinnedApplication]) -> URL? {
        workspace.urlForApplication(withBundleIdentifier: bundleID)
            ?? pinned.first(where: { $0.bundleID == bundleID })?.url
    }

    private func displayName(bundleID: String, url: URL?, running: NSRunningApplication?) -> String {
        if let running, let name = running.localizedName, !name.isEmpty { return name }
        if let url, let bundle = Bundle(url: url) {
            for key in ["CFBundleDisplayName", "CFBundleName"] {
                if let name = bundle.object(forInfoDictionaryKey: key) as? String, !name.isEmpty { return name }
            }
        }
        return url?.deletingPathExtension().lastPathComponent ?? bundleID
    }

    private func appsJSON() -> [[String: Any]] {
        let pinned = pinnedApps()
        let running = regularApps()
        let byID = Dictionary(running.compactMap { app -> (String, NSRunningApplication)? in
            guard let bundleID = app.bundleIdentifier else { return nil }
            return (bundleID, app)
        }, uniquingKeysWith: { first, next in first.isActive ? first : next })
        var order = pinned.map(\.bundleID)
        order.append(contentsOf: launchOrder.filter { byID[$0] != nil })
        order.append(contentsOf: running.compactMap(\.bundleIdentifier))
        var seen = Set<String>()
        return order.filter { seen.insert($0).inserted }.map { bundleID in
            let app = byID[bundleID]
            let url = applicationURL(bundleID: bundleID, pinned: pinned)
            return ["bundleID": bundleID,
                    "name": displayName(bundleID: bundleID, url: url, running: app),
                    "running": app != nil,
                    "pinned": pinned.contains(where: { $0.bundleID == bundleID }),
                    "hidden": app?.isHidden ?? false,
                    "active": app?.isActive ?? false]
        }
    }

    /// The 128 px icon and the version it was drawn from (cached by both, so an
    /// updated app's new icon replaces the old one).
    private func iconPNG(bundleID: String) -> (Data, String)? {
        let pinned = pinnedApps()
        guard let url = applicationURL(bundleID: bundleID, pinned: pinned) else { return nil }
        let version = LauncherCatalog.read(bundle: url)?.version ?? ""
        let key = "\(bundleID)|\(version)" as NSString
        if let cached = iconCache.object(forKey: key) { return (cached as Data, version) }
        let source = workspace.icon(forFile: url.path)
        guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 128, pixelsHigh: 128,
                                            bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                            isPlanar: false, colorSpaceName: .deviceRGB,
                                            bytesPerRow: 0, bitsPerPixel: 0),
              let context = NSGraphicsContext(bitmapImageRep: bitmap) else { return nil }
        NSGraphicsContext.saveGraphicsState()
        NSGraphicsContext.current = context
        context.imageInterpolation = .high
        source.draw(in: NSRect(x: 0, y: 0, width: 128, height: 128),
                    from: .zero, operation: .copy, fraction: 1)
        context.flushGraphics()
        NSGraphicsContext.restoreGraphicsState()
        guard let png = bitmap.representation(using: .png, properties: [:]) else { return nil }
        iconCache.setObject(png as NSData, forKey: key)
        return (png, version)
    }

    private func perform(_ action: DockAction, connection: HTTPConnection) {
        let (verb, bundleID) = (action.verb, action.bundleID)
        let running = regularApps().first { $0.bundleIdentifier == bundleID }
        let log = { (text: String) in MacApps.dockLog("\(text) \(action.label)") }
        /// Answers and says how it went.
        func finish(_ json: [String: Any], status: Int = 200) {
            log("answered \(status) \(json)")
            connection.respond(status, json: json)
        }
        /// Asks Launch Services to open or bring forward the app, the way `open -a` does.
        /// That works from a background process, which NSRunningApplication.activate
        /// (cooperative since macOS 14) does not.
        func launch(_ url: URL) {
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = true
            workspace.openApplication(at: url, configuration: configuration) { app, error in
                if let error {
                    log("openApplication failed: \(String(describing: error))")
                    finish(["ok": false, "error": String(describing: error)], status: 409)
                } else {
                    log("openApplication ok pid=\(app.map { String($0.processIdentifier) } ?? "nil") active=\(app?.isActive ?? false)")
                    finish(["ok": app != nil])
                }
            }
        }
        switch verb {
        case "open":
            let pinned = pinnedApps()
            guard let url = applicationURL(bundleID: bundleID, pinned: pinned) else {
                log("application not found")
                return connection.respond(404, text: "application not found")
            }
            log("resolved \(url.path) running=\(running != nil)")
            launch(url)
        case "hide":
            guard let running else { return finish(["ok": false, "error": "application is not running"], status: 409) }
            finish(["ok": running.hide()])
        case "unhide":
            guard let running else { return finish(["ok": false, "error": "application is not running"], status: 409) }
            finish(["ok": running.unhide()])
        case "quit":
            guard let running else { return finish(["ok": false, "error": "application is not running"], status: 409) }
            finish(["ok": running.terminate()])
        case "show_windows":
            guard let running else { return finish(["ok": false, "error": "application is not running"], status: 409) }
            _ = running.unhide()
            if let url = running.bundleURL {
                log("resolved \(url.path) running=true")
                launch(url)
            } else {
                finish(["ok": running.activate(options: [])])
            }
        default:
            connection.respond(400, text: "unknown app action")
        }
    }
}

private extension Array where Element: Hashable {
    func unique() -> [Element] {
        var seen = Set<Element>()
        return filter { seen.insert($0).inserted }
    }
}

final class SwitcherController {
    let state = SwitcherState()
    let launcherKeys = LauncherKeyLease()
    private var tap: CFMachPort?
    private var runLoopSource: CFRunLoopSource?
    private var leaseTimer: DispatchSourceTimer?
    private var suppressTabUp = false
    private var stopping = false

    func configure(mode: String, entries: [SwitcherPanel]?, verb: String?, key: String? = nil) {
        if mode == "desktop" {
            launcherKeys.close()
            removeTap()
            cancelLeaseTimer()
            state.disableSpatial()
            return
        }

        state.enableSpatial(entries: entries, at: ProcessInfo.processInfo.systemUptime)
        installTapIfNeeded()
        startLeaseTimerIfNeeded()
        state.renewSpatialLease(at: ProcessInfo.processInfo.systemUptime)
        if let verb {
            switch verb {
            case "open": state.controllerOpen()
            case "close": state.controllerClose()
            case "choose": if let key { state.choose(key) }
            default: break
            }
        }
    }

    func stop() {
        stopping = true
        launcherKeys.close()
        removeTap()
        cancelLeaseTimer()
        state.disableSpatial()
    }

    /// The launcher's keyboard lease needs the Spatial Mode tap; without it
    /// (Desktop Mode, or no permission) typing simply stays with the Mac.
    func launcherLease(open: Bool) -> [String: Any] {
        if open, tap != nil, !stopping {
            launcherKeys.open(at: ProcessInfo.processInfo.systemUptime)
        } else {
            launcherKeys.close()
        }
        return ["active": launcherKeys.active, "tapActive": tap != nil]
    }

    private func installTapIfNeeded() {
        guard tap == nil, !stopping else { return }
        let mask = (1 << CGEventType.keyDown.rawValue) |
            (1 << CGEventType.keyUp.rawValue) |
            (1 << CGEventType.flagsChanged.rawValue)
        guard let created = CGEvent.tapCreate(tap: .cgSessionEventTap,
                                              place: .headInsertEventTap,
                                              options: .defaultTap,
                                              eventsOfInterest: CGEventMask(mask),
                                              callback: switcherEventTap,
                                              userInfo: Unmanaged.passUnretained(self).toOpaque()) else {
            state.setTapActive(false, error: "keyboard event tap unavailable")
            return
        }
        guard let source = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, created, 0) else {
            CFMachPortInvalidate(created)
            state.setTapActive(false, error: "keyboard event tap source unavailable")
            return
        }
        tap = created
        runLoopSource = source
        CFRunLoopAddSource(CFRunLoopGetMain(), source, .commonModes)
        CGEvent.tapEnable(tap: created, enable: true)
        state.setTapActive(true)
    }

    private func removeTap() {
        launcherKeys.close()
        if let tap { CGEvent.tapEnable(tap: tap, enable: false) }
        if let runLoopSource { CFRunLoopRemoveSource(CFRunLoopGetMain(), runLoopSource, .commonModes) }
        if let tap { CFMachPortInvalidate(tap) }
        tap = nil
        runLoopSource = nil
        suppressTabUp = false
        state.setTapActive(false)
    }

    private func startLeaseTimerIfNeeded() {
        guard leaseTimer == nil else { return }
        let timer = DispatchSource.makeTimerSource(queue: .main)
        timer.schedule(deadline: .now() + 1, repeating: 1)
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            // A companion that stopped polling gives the keyboard back.
            self.launcherKeys.expire(at: ProcessInfo.processInfo.systemUptime)
            guard self.state.expireLease(at: ProcessInfo.processInfo.systemUptime) else { return }
            self.removeTap()
            self.cancelLeaseTimer()
        }
        leaseTimer = timer
        timer.resume()
    }

    private func cancelLeaseTimer() {
        leaseTimer?.setEventHandler {}
        leaseTimer?.cancel()
        leaseTimer = nil
    }

    fileprivate func handle(_ type: CGEventType, event: CGEvent) -> Unmanaged<CGEvent>? {
        if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
            if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
            return Unmanaged.passUnretained(event)
        }
        let code = CGKeyCode(event.getIntegerValueField(.keyboardEventKeycode))
        let phase: LauncherKeyPhase = type == .keyDown ? .down : type == .keyUp ? .up : .flags
        var length = 0
        var units = [UniChar](repeating: 0, count: 8)
        if phase == .down { event.keyboardGetUnicodeString(maxStringLength: 8, actualStringLength: &length, unicodeString: &units) }
        if launcherKeys.handle(phase, code: code, command: event.flags.contains(.maskCommand),
                               control: event.flags.contains(.maskControl),
                               text: String(utf16CodeUnits: units, count: max(0, min(length, 8)))) == .consume {
            return nil
        }
        if type == .keyDown, code == 0x30, event.flags.contains(.maskCommand) {
            suppressTabUp = state.cycle(reverse: event.flags.contains(.maskShift))
            return suppressTabUp ? nil : Unmanaged.passUnretained(event)
        }
        if type == .keyUp, code == 0x30, suppressTabUp {
            suppressTabUp = false
            return nil
        }
        if type == .flagsChanged, (code == 0x36 || code == 0x37),
           !event.flags.contains(.maskCommand) {
            state.commandReleased()
        }
        return Unmanaged.passUnretained(event)
    }
}

private let switcherEventTap: CGEventTapCallBack = { _, type, event, userInfo in
    guard let userInfo else { return Unmanaged.passUnretained(event) }
    return Unmanaged<SwitcherController>.fromOpaque(userInfo).takeUnretainedValue().handle(type, event: event)
}
