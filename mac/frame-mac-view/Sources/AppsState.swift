// Pure state for the Frame-owned window switcher. This file has no AppKit or
// event-tap dependencies so its ordering and lifecycle rules can be tested
// without touching the user's desktop.
import Foundation

struct SwitcherPanel: Equatable {
    let key: String
    let bundleID: String
}

enum SwitcherInputError: Error, Equatable {
    case invalidEntries
    case invalidPanel
}

extension SwitcherPanel {
    /// Parses the companion's query-string JSON and rejects malformed or
    /// ambiguous panel identities before they enter switcher state.
    static func parseEntries(_ json: String) throws -> [SwitcherPanel] {
        guard json.utf8.count <= 96 * 1024,
              let data = json.data(using: .utf8),
              let raw = try? JSONSerialization.jsonObject(with: data),
              let rows = raw as? [[String: Any]], rows.count <= 256 else {
            throw SwitcherInputError.invalidEntries
        }
        var seen = Set<String>()
        return try rows.map { row in
            guard let key = row["key"] as? String,
                  let bundleID = row["bundleID"] as? String,
                  Self.validWindowKey(key), Self.validBundleID(bundleID), seen.insert(key).inserted else {
                throw SwitcherInputError.invalidPanel
            }
            return SwitcherPanel(key: key, bundleID: bundleID)
        }
    }

    static func validWindowKey(_ key: String) -> Bool {
        let prefix = "com.frame-workspace.native."
        guard key.hasPrefix(prefix) else { return false }
        let suffix = key.dropFirst(prefix.count).split(separator: ".", omittingEmptySubsequences: false)
        guard suffix.count == 2, (1...15).contains(suffix[0].utf8.count),
              suffix[0].utf8.allSatisfy({ $0 >= 48 && $0 <= 57 }),
              suffix[1].hasPrefix("panel-w") else { return false }
        let windowID = suffix[1].dropFirst("panel-w".count)
        return (1...15).contains(windowID.utf8.count) && windowID.utf8.allSatisfy { $0 >= 48 && $0 <= 57 }
    }

    static func validBundleID(_ bundleID: String) -> Bool {
        let bytes = Array(bundleID.utf8)
        guard !bytes.isEmpty, bytes.count <= 200 else { return false }
        func alphanumeric(_ byte: UInt8) -> Bool {
            (byte >= 65 && byte <= 90) || (byte >= 97 && byte <= 122) || (byte >= 48 && byte <= 57)
        }
        return alphanumeric(bytes[0]) && bytes.dropFirst().allSatisfy {
            alphanumeric($0) || $0 == 46 || $0 == 95 || $0 == 45
        }
    }
}

struct SwitcherSnapshot {
    let open: Bool
    let index: Int
    let seq: Int
    let chosen: String?
    let windows: [String]
    let tapActive: Bool
    let error: String?

    var json: [String: Any] {
        var result: [String: Any] = [
            "open": open, "index": index, "seq": seq,
            "chosen": chosen as Any? ?? NSNull(), "windows": windows,
            "tapActive": tapActive
        ]
        if let error { result["error"] = error }
        return result
    }
}

/// Thread-safe because HTTP requests arrive on the server queue while the
/// system event tap and workspace notifications arrive on the main run loop.
final class SwitcherState {
    static let spatialLease: TimeInterval = 5

    private let lock = NSLock()
    private var spatial = false
    private var openValue = false
    private var selectedIndex = 0
    private var sequence = 0
    private var chosenKey: String?
    private var highlightedKey: String?
    private var keyboardCycle = false
    private var tapIsActive = false
    private var errorValue: String?
    private var panels: [SwitcherPanel] = []
    private var appMRU: [String] = []
    private var frozenMRU: [String]?
    private var leaseDeadline: TimeInterval?

    private func locked<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }

    private struct Visible: Equatable {
        let open: Bool
        let index: Int
        let chosen: String?
        let windows: [String]
        let tapActive: Bool
        let error: String?
    }

    private func visibleLocked() -> Visible {
        Visible(open: openValue, index: selectedIndex, chosen: chosenKey,
                windows: windowsLocked(), tapActive: tapIsActive, error: errorValue)
    }

    private func finishChangeLocked(from before: Visible) {
        if before != visibleLocked() { sequence += 1 }
    }

    private func windowsLocked() -> [String] {
        var order = frozenMRU ?? appMRU
        for panel in panels where !order.contains(panel.bundleID) { order.append(panel.bundleID) }
        var result: [String] = []
        var included = Set<String>()
        for bundleID in order where included.insert(bundleID).inserted {
            result.append(contentsOf: panels.lazy.filter { $0.bundleID == bundleID }.map(\.key))
        }
        return result
    }

    private func normalizeSelectionLocked() {
        let windows = windowsLocked()
        guard !windows.isEmpty else {
            selectedIndex = 0
            highlightedKey = nil
            chosenKey = nil
            return
        }
        if let highlightedKey, let at = windows.firstIndex(of: highlightedKey) {
            selectedIndex = at
        } else if !openValue, let chosenKey, let at = windows.firstIndex(of: chosenKey) {
            selectedIndex = at
        } else {
            selectedIndex = min(max(selectedIndex, 0), windows.count - 1)
            highlightedKey = openValue ? windows[selectedIndex] : nil
        }
        if let chosenKey, !windows.contains(chosenKey) { self.chosenKey = nil }
        if openValue { chosenKey = nil }
    }

    /// Sets spatial mode and replaces the panel list. Repeated companion
    /// renewals with identical entries preserve open state, MRU and selection.
    func enableSpatial(entries: [SwitcherPanel]?, at now: TimeInterval) {
        locked {
            let before = visibleLocked()
            spatial = true
            leaseDeadline = now + Self.spatialLease
            if let entries, panels != entries { panels = entries }
            normalizeSelectionLocked()
            finishChangeLocked(from: before)
        }
    }

    func currentPanels() -> [SwitcherPanel] { locked { panels } }

    /// Desktop mode is a complete switcher reset and invalidates its lease.
    func disableSpatial() {
        locked {
            let before = visibleLocked()
            spatial = false
            openValue = false
            selectedIndex = 0
            chosenKey = nil
            highlightedKey = nil
            keyboardCycle = false
            panels = []
            frozenMRU = nil
            leaseDeadline = nil
            tapIsActive = false
            errorValue = nil
            finishChangeLocked(from: before)
        }
    }

    func renewSpatialLease(at now: TimeInterval) {
        locked {
            guard spatial else { return }
            leaseDeadline = now + Self.spatialLease
        }
    }

    /// Returns true once when a live spatial lease expires.
    @discardableResult
    func expireLease(at now: TimeInterval) -> Bool {
        locked {
            guard spatial, let deadline = leaseDeadline, now >= deadline else { return false }
            let before = visibleLocked()
            spatial = false
            openValue = false
            selectedIndex = 0
            chosenKey = nil
            highlightedKey = nil
            keyboardCycle = false
            panels = []
            frozenMRU = nil
            leaseDeadline = nil
            tapIsActive = false
            errorValue = nil
            finishChangeLocked(from: before)
            return true
        }
    }

    func setTapActive(_ active: Bool, error: String? = nil) {
        locked {
            let before = visibleLocked()
            tapIsActive = active
            errorValue = error
            finishChangeLocked(from: before)
        }
    }

    /// Replaces activation history with a unique, most-recent-first list.
    func seedMRU(_ bundleIDs: [String]) {
        updateMRU(bundleIDs, frontmost: nil)
    }

    func noteActivation(_ bundleID: String) {
        updateMRU([], frontmost: bundleID)
    }

    func noteTermination(_ bundleID: String) {
        locked {
            let before = visibleLocked()
            appMRU.removeAll { $0 == bundleID }
            panels.removeAll { $0.bundleID == bundleID }
            normalizeSelectionLocked()
            finishChangeLocked(from: before)
        }
    }

    func mruIDs() -> [String] {
        locked { appMRU }
    }

    private func updateMRU(_ bundleIDs: [String], frontmost: String?) {
        locked {
            let before = visibleLocked()
            if let frontmost {
                appMRU.removeAll { $0 == frontmost }
                appMRU.insert(frontmost, at: 0)
            } else {
                var seen = Set<String>()
                appMRU = bundleIDs.filter { seen.insert($0).inserted }
            }
            normalizeSelectionLocked()
            finishChangeLocked(from: before)
        }
    }

    /// Starts or advances one Cmd-Tab cycle. Index addresses the flattened
    /// panel strip; panels stay grouped by app MRU and each app's entry order.
    @discardableResult
    func cycle(reverse: Bool) -> Bool {
        locked {
            guard spatial else { return false }
            let before = visibleLocked()
            let continuing = openValue
            keyboardCycle = true
            if !continuing {
                frozenMRU = appMRU
                openValue = true
                chosenKey = nil
            }
            let windows = windowsLocked()
            guard !windows.isEmpty else {
                selectedIndex = 0
                chosenKey = nil
                finishChangeLocked(from: before)
                return true
            }

            let direction = reverse ? -1 : 1
            let base: Int
            if continuing {
                base = selectedIndex
            } else if let activeBundle = appMRU.first,
                      let activePanel = panels.first(where: { $0.bundleID == activeBundle })?.key,
                      let activeIndex = windows.firstIndex(of: activePanel) {
                base = activeIndex
            } else {
                base = reverse ? 0 : -1
            }
            selectedIndex = (base + direction + windows.count) % windows.count
            highlightedKey = windows[selectedIndex]
            finishChangeLocked(from: before)
            return true
        }
    }

    func commandReleased() {
        locked {
            guard openValue, keyboardCycle else { return }
            let before = visibleLocked()
            openValue = false
            frozenMRU = nil
            chosenKey = highlightedKey
            highlightedKey = nil
            keyboardCycle = false
            normalizeSelectionLocked()
            finishChangeLocked(from: before)
        }
    }

    func controllerOpen() {
        locked {
            guard spatial else { return }
            let before = visibleLocked()
            frozenMRU = appMRU
            openValue = true
            chosenKey = nil
            keyboardCycle = false
            let windows = windowsLocked()
            if let activeBundle = appMRU.first,
               let activePanel = panels.first(where: { $0.bundleID == activeBundle })?.key,
               let activeIndex = windows.firstIndex(of: activePanel) {
                selectedIndex = activeIndex
            } else {
                selectedIndex = 0
            }
            highlightedKey = windows.indices.contains(selectedIndex) ? windows[selectedIndex] : nil
            finishChangeLocked(from: before)
        }
    }

    func controllerClose() {
        locked {
            // Close also acknowledges a delivered Cmd-release choice.
            guard openValue || chosenKey != nil else { return }
            let before = visibleLocked()
            openValue = false
            frozenMRU = nil
            chosenKey = nil
            highlightedKey = nil
            keyboardCycle = false
            normalizeSelectionLocked()
            finishChangeLocked(from: before)
        }
    }

    func snapshot() -> SwitcherSnapshot {
        locked {
            SwitcherSnapshot(open: openValue, index: selectedIndex, seq: sequence,
                             chosen: chosenKey, windows: windowsLocked(),
                             tapActive: tapIsActive, error: errorValue)
        }
    }
}
