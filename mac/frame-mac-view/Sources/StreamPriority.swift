// A short in-memory lease from explicit viewer focus/input messages. This is
// a source-scoped activity request; it does not claim that a headset can see
// a stream. With no live selection, sessions keep their normal frame rate.
import Foundation

struct StreamPriorityDecision {
    let role: String
    let reason: String
    let fpsCap: Int?
    let remainingMs: Int?
}

/// Shared by all streams in one agent process. Session IDs are ephemeral and
/// only the currently selected session is retained, so disconnect and expiry
/// cleanup do not leave per-session history behind.
final class StreamActivityRegistry {
    static let leaseDuration: TimeInterval = 30
    static let backgroundFpsCap = 10
    private let enabled: Bool

    init(enabled: Bool = ProcessInfo.processInfo.environment["FRAME_MAC_VIEW_PRIORITY"] != "0") {
        self.enabled = enabled
    }

    private struct Selection {
        let sessionID: Int
        let reason: String
        let expiresAt: TimeInterval
    }

    private let lock = NSLock()
    private var live = Set<Int>()
    private var selection: Selection?
    private var noSelectionReason = "no-selection"

    private func locked<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }

    func register(_ sessionID: Int) {
        locked {
            if live.isEmpty { noSelectionReason = "no-selection" }
            live.insert(sessionID)
        }
    }

    func noteActivity(sessionID: Int, reason: String, at now: TimeInterval = ProcessInfo.processInfo.systemUptime) {
        locked {
            guard enabled, live.contains(sessionID) else { return }
            selection = Selection(sessionID: sessionID, reason: reason,
                                  expiresAt: now + Self.leaseDuration)
            noSelectionReason = "no-selection"
        }
    }

    /// Records only viewer messages that establish focus or user interaction.
    /// `release` is cleanup (for example, blur), so it must not steal priority.
    @discardableResult
    func recordMessage(sessionID: Int, type: String,
                       at now: TimeInterval = ProcessInfo.processInfo.systemUptime) -> Bool {
        let reason: String
        switch type {
        case "focus": reason = "focus"
        case "m", "wheel", "k", "text": reason = "input"
        default: return false
        }
        noteActivity(sessionID: sessionID, reason: reason, at: now)
        return true
    }

    func unregister(_ sessionID: Int) {
        locked {
            live.remove(sessionID)
            if selection?.sessionID == sessionID {
                selection = nil
                noSelectionReason = "selected-disconnected"
            }
        }
    }

    func decision(for sessionID: Int, at now: TimeInterval = ProcessInfo.processInfo.systemUptime) -> StreamPriorityDecision {
        locked {
            guard enabled else {
                return StreamPriorityDecision(role: "normal", reason: "disabled", fpsCap: nil, remainingMs: nil)
            }
            if let current = selection, current.expiresAt <= now {
                selection = nil
                noSelectionReason = "lease-expired"
            }

            guard live.contains(sessionID) else {
                return StreamPriorityDecision(role: "normal", reason: "session-ended", fpsCap: nil, remainingMs: nil)
            }
            guard live.count > 1 else {
                return StreamPriorityDecision(role: "normal", reason: "single-stream", fpsCap: nil, remainingMs: nil)
            }
            guard let current = selection, live.contains(current.sessionID) else {
                return StreamPriorityDecision(role: "normal", reason: noSelectionReason, fpsCap: nil, remainingMs: nil)
            }

            let remaining = max(0, Int(((current.expiresAt - now) * 1000).rounded(.up)))
            if current.sessionID == sessionID {
                return StreamPriorityDecision(role: "selected", reason: current.reason, fpsCap: nil,
                                              remainingMs: remaining)
            }
            return StreamPriorityDecision(role: "background", reason: "other-stream-\(current.reason)",
                                          fpsCap: Self.backgroundFpsCap, remainingMs: remaining)
        }
    }
}
