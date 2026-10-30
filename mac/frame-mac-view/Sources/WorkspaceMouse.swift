import Foundation

/// Pure lease and event accumulator. Motion is cumulative, so replacing an
/// unsent snapshot never loses distance. Edges and wheel records stay ordered.
struct WorkspaceMouseLease {
    static let ttlMs = 1500.0
    private(set) var session = ""
    private(set) var host = ""
    private(set) var expires = 0.0
    private(set) var sequence: UInt64 = 0
    private(set) var x = 0.0
    private(set) var y = 0.0
    private(set) var buttons = Set<Int>()
    private var lastHeartbeat: UInt64 = 0
    var active: Bool { !session.isEmpty }

    mutating func start(session: String, host: String, now: Double) -> Bool {
        guard !active, !session.isEmpty, !host.isEmpty, now.isFinite else { return false }
        self = Self(); self.session = session; self.host = host; expires = now + Self.ttlMs
        return true
    }
    mutating func heartbeat(session: String, host: String, sequence: UInt64, now: Double) -> Bool {
        guard active, self.session == session, self.host == host,
              now < expires, sequence > lastHeartbeat else { return false }
        lastHeartbeat = sequence; expires = now + Self.ttlMs; return true
    }
    mutating func stop() { self = Self() }
    func expired(_ now: Double) -> Bool { active && now >= expires }
    mutating func event(kind: String, dx: Double = 0, dy: Double = 0, button: Int = 0) -> [String: Any]? {
        guard active, dx.isFinite, dy.isFinite else { return nil }
        if kind == "motion" { x += dx; y += dy }
        else if kind == "down" { guard buttons.insert(button).inserted else { return nil } }
        else if kind == "up" { guard buttons.remove(button) != nil else { return nil } }
        else if kind != "wheel" { return nil }
        sequence += 1
        var result: [String: Any] = ["t": "mouse", "session": session, "host": host,
            "q": sequence, "kind": kind, "x": x, "y": y, "button": button]
        if kind == "wheel" { result["dx"] = dx; result["dy"] = dy }
        return result
    }
}
