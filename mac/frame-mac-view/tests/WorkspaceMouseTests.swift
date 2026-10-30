import Foundation

@main private enum WorkspaceMouseTests {
    static func main() {
        var lease = WorkspaceMouseLease()
        precondition(!lease.active)
        precondition(!lease.start(session: "", host: "h", now: 0))
        precondition(lease.start(session: "s", host: "h", now: 100))
        precondition(!lease.start(session: "other", host: "h", now: 110))
        precondition(!lease.heartbeat(session: "old", host: "h", sequence: 1, now: 200))
        precondition(!lease.heartbeat(session: "s", host: "old", sequence: 1, now: 200))
        precondition(lease.heartbeat(session: "s", host: "h", sequence: 1, now: 200))
        precondition(!lease.heartbeat(session: "s", host: "h", sequence: 1, now: 300))
        precondition(lease.expires == 1700)
        let first = lease.event(kind: "motion", dx: 20, dy: -10)!
        let latest = lease.event(kind: "motion", dx: 30, dy: 4)!
        precondition(first["x"] as? Double == 20 && latest["x"] as? Double == 50)
        precondition(latest["y"] as? Double == -6 && latest["q"] as? UInt64 == 2)
        precondition(lease.event(kind: "down", button: 0)?["q"] as? UInt64 == 3)
        precondition(lease.event(kind: "down", button: 0) == nil)
        precondition(lease.event(kind: "wheel", dx: 0, dy: 4)?["q"] as? UInt64 == 4)
        precondition(lease.event(kind: "up", button: 0)?["q"] as? UInt64 == 5)
        precondition(lease.buttons.isEmpty)
        precondition(!lease.expired(1699) && lease.expired(1700))
        precondition(!lease.heartbeat(session: "s", host: "h", sequence: 2, now: 1700))
        lease.stop()
        precondition(lease.event(kind: "motion", dx: 1) == nil)
        precondition(lease.start(session: "fresh", host: "new", now: 1800))
        precondition(lease.x == 0 && lease.sequence == 0 && lease.buttons.isEmpty)
        precondition(lease.event(kind: "motion", dx: .nan) == nil)
        print("Workspace mouse: lease epochs, replay, cumulative motion, edge ordering, expiry, reconnect passed")
    }
}
