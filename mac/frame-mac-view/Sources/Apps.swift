// Read-only Dock app discovery and explicit app actions for the spatial
// companion, plus the leased Cmd-Tab event tap used only in spatial mode.
import AppKit
import ApplicationServices
import CoreFoundation
import Foundation

final class MacApps {
    let switcher = SwitcherController()
    private let workspace = NSWorkspace.shared
    private var observers: [NSObjectProtocol] = []
    private let iconCache = NSCache<NSString, NSData>()
    private var launchOrder: [String] = []

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
            guard let data = self.iconPNG(bundleID: bundleID) else {
                return connection.respond(404, text: "application icon not found")
            }
            connection.respond(json: ["png": data.base64EncodedString()])
        }
    }

    func actionResponse(_ request: Request, _ connection: HTTPConnection) {
        guard let bundleID = request.query["bundleID"], SwitcherPanel.validBundleID(bundleID) else {
            return connection.respond(400, text: "bundleID is required")
        }
        guard let verb = request.query["verb"], ["open", "hide", "unhide", "quit", "show_windows"].contains(verb) else {
            return connection.respond(400, text: "verb=open|hide|unhide|quit|show_windows")
        }
        DispatchQueue.main.async {
            self.perform(verb, bundleID: bundleID, connection: connection)
        }
    }

    func switcherResponse(_ request: Request, _ connection: HTTPConnection) {
        connection.respond(json: switcher.state.snapshot().json)
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

    private func iconPNG(bundleID: String) -> Data? {
        if let cached = iconCache.object(forKey: bundleID as NSString) { return cached as Data }
        let pinned = pinnedApps()
        guard let url = applicationURL(bundleID: bundleID, pinned: pinned) else { return nil }
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
        iconCache.setObject(png as NSData, forKey: bundleID as NSString)
        return png
    }

    private func perform(_ verb: String, bundleID: String, connection: HTTPConnection) {
        let running = regularApps().first { $0.bundleIdentifier == bundleID }
        switch verb {
        case "open":
            let pinned = pinnedApps()
            guard let url = applicationURL(bundleID: bundleID, pinned: pinned) else {
                return connection.respond(404, text: "application not found")
            }
            workspace.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration()) { app, error in
                if let error {
                    connection.respond(409, json: ["ok": false, "error": String(describing: error)])
                } else {
                    connection.respond(json: ["ok": app != nil])
                }
            }
        case "hide":
            guard let running else { return connection.respond(409, json: ["ok": false, "error": "application is not running"]) }
            connection.respond(json: ["ok": running.hide()])
        case "unhide":
            guard let running else { return connection.respond(409, json: ["ok": false, "error": "application is not running"]) }
            connection.respond(json: ["ok": running.unhide()])
        case "quit":
            guard let running else { return connection.respond(409, json: ["ok": false, "error": "application is not running"]) }
            connection.respond(json: ["ok": running.terminate()])
        case "show_windows":
            guard let running else { return connection.respond(409, json: ["ok": false, "error": "application is not running"]) }
            _ = running.unhide()
            connection.respond(json: ["ok": running.activate(options: [])])
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
    private var tap: CFMachPort?
    private var runLoopSource: CFRunLoopSource?
    private var leaseTimer: DispatchSourceTimer?
    private var suppressTabUp = false
    private var stopping = false

    func configure(mode: String, entries: [SwitcherPanel]?, verb: String?, key: String? = nil) {
        if mode == "desktop" {
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
        removeTap()
        cancelLeaseTimer()
        state.disableSpatial()
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
            guard let self,
                  self.state.expireLease(at: ProcessInfo.processInfo.systemUptime) else { return }
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
