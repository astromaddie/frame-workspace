// The launcher's keyboard lease (FW-15, Launcher A). While the launcher is open
// in the headset, typing on the Mac's keyboard goes to its Search Applications
// field instead of the focused window: printable keys, Delete, Esc, Return and
// the arrows are consumed here and queued for the companion; Command and
// Control shortcuts, Tab and everything else pass through. Pure state, no
// event tap, so the unit tests can drive every path. The companion renews the
// lease while it polls; close, Stop, a lost companion (lease expiry) and the
// helper's exit all end it.
import Foundation

enum LauncherKey: Equatable {
    case text(String)
    case backspace, escape, submit, left, right, up, down

    var json: [String: Any] {
        switch self {
        case .text(let value): return ["k": "text", "s": value]
        case .backspace: return ["k": "back"]
        case .escape: return ["k": "esc"]
        case .submit: return ["k": "return"]
        case .left: return ["k": "left"]
        case .right: return ["k": "right"]
        case .up: return ["k": "up"]
        case .down: return ["k": "down"]
        }
    }
}

enum LauncherKeyPhase { case down, up, flags }

enum LauncherKeyDecision: Equatable { case pass, consume }

struct LauncherKeySnapshot {
    let active: Bool
    let events: [(n: Int, key: LauncherKey)]
    let last: Int

    var json: [String: Any] {
        ["active": active, "last": last,
         "events": events.map { event -> [String: Any] in
             var row = event.key.json
             row["n"] = event.n
             return row
         }]
    }
}

final class LauncherKeyLease {
    static let lease: TimeInterval = 3
    static let maxEvents = 128

    private let lock = NSLock()
    private var activeValue = false
    private var deadline: TimeInterval = 0
    private var counter = 0
    private var queue: [(n: Int, key: LauncherKey)] = []
    /// Keys whose down was consumed: their up is consumed too, even after the
    /// lease ends, so the focused app never sees half a keystroke.
    private var held = Set<UInt16>()

    private func locked<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }

    var active: Bool { locked { activeValue } }

    func open(at now: TimeInterval) {
        locked {
            if !activeValue { queue.removeAll() }
            activeValue = true
            deadline = now + Self.lease
        }
    }

    func renew(at now: TimeInterval) {
        locked { if activeValue { deadline = now + Self.lease } }
    }

    func close() {
        locked {
            activeValue = false
            queue.removeAll()
        }
    }

    /// True once when the companion stopped renewing an open lease.
    @discardableResult
    func expire(at now: TimeInterval) -> Bool {
        locked {
            guard activeValue, now >= deadline else { return false }
            activeValue = false
            queue.removeAll()
            return true
        }
    }

    /// Events after `after`; polling renews an open lease.
    func snapshot(after: Int, renewAt now: TimeInterval? = nil) -> LauncherKeySnapshot {
        locked {
            if let now, activeValue { deadline = now + Self.lease }
            return LauncherKeySnapshot(active: activeValue, events: queue.filter { $0.n > after }, last: counter)
        }
    }

    /// Characters, Space and keypad keys (the ANSI/ISO/JIS key codes that type).
    static func typingKey(_ code: UInt16) -> Bool {
        switch code {
        case 0x00...0x32: return code != 0x24 && code != 0x30   // not Return, not Tab
        case 0x41...0x5C: return code != 0x4C && code != 0x47   // keypad, not Enter or Clear
        case 0x5D, 0x5E, 0x5F: return true                      // JIS yen, underscore, keypad comma
        default: return false
        }
    }

    func handle(_ phase: LauncherKeyPhase, code: UInt16, command: Bool, control: Bool, text: String) -> LauncherKeyDecision {
        locked {
            switch phase {
            case .flags:
                return .pass
            case .up:
                return held.remove(code) != nil ? .consume : .pass
            case .down:
                guard activeValue, !command, !control else {
                    // A key that now passes (a repeat after the lease ended, or
                    // with Command added) also gets its up.
                    held.remove(code)
                    return .pass
                }
                let key: LauncherKey?
                switch code {
                case 0x33: key = .backspace
                case 0x35: key = .escape
                case 0x24, 0x4C: key = .submit
                case 0x7B: key = .left
                case 0x7C: key = .right
                case 0x7D: key = .down
                case 0x7E: key = .up
                default:
                    guard Self.typingKey(code) else {
                        held.remove(code)
                        return .pass
                    }
                    // Dead keys type nothing yet; still consumed, so the focused
                    // app does not start a composition the field never sees.
                    let printable = String(text.unicodeScalars.filter {
                        !CharacterSet.controlCharacters.contains($0) && !(0xF700...0xF8FF).contains($0.value)
                    })
                    key = printable.isEmpty ? nil : .text(String(printable.prefix(8)))
                }
                held.insert(code)
                if let key {
                    counter += 1
                    queue.append((counter, key))
                    if queue.count > Self.maxEvents { queue.removeFirst(queue.count - Self.maxEvents) }
                }
                return .consume
            }
        }
    }
}
