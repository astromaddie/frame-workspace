// Adapts each stream to its network, latency first. The viewer acknowledges
// every frame as it arrives ("rx"); from those acks the controller knows how
// long frames take to get through and how fast the link delivers them.
//
// - The gate: a new frame is sent only while the oldest unacknowledged one is
//   younger than the path's usual round trip plus a little slack. So frames
//   never queue up in SSH, TCP or the Wi-Fi driver; while the link is stuck
//   the newest picture waits and goes out as soon as it moves again.
// - The bitrate: when frames start queueing (the round trip grows) or the
//   gate has to hold frames back, it drops to a bit under what the link
//   actually delivered; once things are clear it probes up again slowly, never
//   above the quality setting's bitrate (the ceiling).
// - The tier: as the bitrate falls, fewer frames per second (60, 45, 30), then
//   a smaller picture (75%, then 50% of the panel's pixels).
// See docs/mac-in-headset.md ("Adapting to the network"). Thread-safe.
import Foundation

final class RateController {
    struct Tier: Equatable {
        let fps: Int
        let scale: Double  // of the picture's long side
    }

    static let tiers = [Tier(fps: 60, scale: 1), Tier(fps: 45, scale: 1), Tier(fps: 30, scale: 1),
                        Tier(fps: 30, scale: 0.75), Tier(fps: 30, scale: 0.5)]
    /// A tier is used while the target bitrate is at least this share of the ceiling.
    static let floors = [0.45, 0.28, 0.16, 0.08, 0]
    static let enabled = ProcessInfo.processInfo.environment["FRAME_MAC_VIEW_ADAPT"] != "0"

    let maxFps: Int
    private let lock = NSLock()
    private let sessionID: Int?
    private let activity: StreamActivityRegistry?
    private var ceiling = 0  // bits/s at full size and frame rate
    private(set) var target = 0
    private(set) var tier = 0
    private var unacked: [(seq: UInt32, sent: Int64, bytes: Int)] = []
    /// Round trips (send -> ack arrives here), for the baseline: the lowest
    /// in the last 10 s is the path without any queue.
    private var rtts: [(t: Int64, v: Int64)] = []
    private var acked: [(t: Int64, bytes: Int)] = []  // the last second
    private var sentLog: [(t: Int64, bytes: Int)] = []
    private var captures: [Int64] = []  // the last second
    private var frameBytes = 0  // average recent frame, kept while the gate holds everything back
    private var held = 0  // frames the gate held back since the last update
    private var lastSignal = false
    private var sawAck = false
    private var lastDecrease: Int64 = 0
    private var lastIncrease: Int64 = 0
    private var belowSince: Int64 = 0, aboveSince: Int64 = 0
    /// What changed, for the timeline: (time, event).
    private(set) var events: [(Int64, String)] = []

    init(maxFps: Int, sessionID: Int? = nil, activity: StreamActivityRegistry? = nil) {
        self.maxFps = maxFps
        self.sessionID = sessionID
        self.activity = activity
    }

    private func locked<T>(_ f: () -> T) -> T { lock.lock(); defer { lock.unlock() }; return f() }

    /// The quality setting's bitrate at full size; the first call also starts there.
    func setCeiling(_ bps: Int) {
        locked {
            if target == 0 || target > bps { target = bps }
            ceiling = bps
        }
    }

    var fps: Int { fps(at: ProcessInfo.processInfo.systemUptime) }
    func fps(at now: TimeInterval) -> Int { locked { fpsLocked(at: now) } }
    private func adaptiveFpsLocked() -> Int { min(maxFps, RateController.tiers[tier].fps) }
    private func fpsLocked(at now: TimeInterval) -> Int {
        let decision = priorityLocked(at: now)
        return min(adaptiveFpsLocked(), decision.fpsCap ?? maxFps)
    }
    func priority(at now: TimeInterval = ProcessInfo.processInfo.systemUptime) -> StreamPriorityDecision {
        locked { priorityLocked(at: now) }
    }
    private func priorityLocked(at now: TimeInterval) -> StreamPriorityDecision {
        guard let sessionID, let activity else {
            return StreamPriorityDecision(role: "normal", reason: "not-configured", fpsCap: nil, remainingMs: nil)
        }
        return activity.decision(for: sessionID, at: now)
    }
    var scale: Double { locked { RateController.tiers[tier].scale } }
    var baseRtt: Int64 { locked { baseline() } }

    private func baseline() -> Int64 { rtts.map(\.v).min() ?? 0 }

    /// How late a frame may be before the gate holds the next one: one frame
    /// interval, plus room for the jitter this link normally has (1.5 times
    /// its recent spread), so ordinary Wi-Fi jitter doesn't cost frames but a
    /// real queue does. Updated in update().
    private var slack: Int64 = 40_000

    /// Whether a frame may be sent now without queueing behind earlier ones.
    /// `counts`: a held frame is a sign of congestion (not when merely
    /// re-checking whether a held frame can go yet).
    func maySend(now: Int64, counts: Bool = true) -> Bool {
        locked {
            guard RateController.enabled, sawAck else { return true }  // not heard from the viewer yet
            // Unacknowledged for 2 s: gone with a reconnection, not in a queue.
            unacked.removeAll { now - $0.sent > 2_000_000 }
            guard let oldest = unacked.first else { return true }
            // Age is what bounds latency. The count only stops a burst, and it
            // allows a full round trip of frames, so a long but clear path
            // (100 ms away) still gets every frame.
            let interval = Int64(1_000_000 / max(1, fpsLocked(at: ProcessInfo.processInfo.systemUptime)))
            let window = max(3, Int((baseline() + slack) / interval) + 1)
            if unacked.count < window, now - oldest.sent <= baseline() + slack { return true }
            if counts { held += 1 }
            return false
        }
    }

    /// A picture was captured (sent or not): with the frame sizes, what this
    /// stream would send if the link allowed.
    func captured(at t: Int64) {
        locked {
            captures.append(t)
            if captures.count > 256 { captures.removeFirst(captures.count - 256) }
        }
    }

    func sent(seq: UInt32, bytes: Int, at t: Int64) {
        locked {
            // Bounded even if the viewer never acknowledges (an old viewer, or
            // the controller is off).
            unacked.append((seq, t, bytes))
            if unacked.count > 512 { unacked.removeFirst(unacked.count - 512) }
            sentLog.append((t, bytes))
            if sentLog.count > 1024 { sentLog.removeFirst(sentLog.count - 1024) }
        }
    }

    /// The viewer has frame `seq`. Returns true if that may let a held frame go.
    func acked(seq: UInt32, at now: Int64) -> Bool {
        locked {
            sawAck = true
            guard let i = unacked.firstIndex(where: { $0.seq == seq }) else { return false }
            let f = unacked[i]
            unacked.removeSubrange(0...i)  // TCP delivers in order: earlier ones arrived too
            rtts.append((now, now - f.sent))
            acked.append((now, f.bytes))
            return true
        }
    }

    /// Called every 100 ms. Returns the new bitrate target, or nil if the
    /// controller is off.
    func update(now: Int64) -> Int? {
        locked {
            rtts.removeAll { now - $0.t > 10_000_000 }
            acked.removeAll { now - $0.t > 500_000 }
            sentLog.removeAll { now - $0.t > 500_000 }
            unacked.removeAll { now - $0.sent > 2_000_000 }
            captures.removeAll { now - $0 > 1_000_000 }
            guard RateController.enabled, ceiling > 0 else { return nil }
            let base = baseline()
            let spread = rtts.filter { now - $0.t < 2_000_000 }.map(\.v).sorted()
            let jitter = spread.isEmpty ? 0 : spread[spread.count * 9 / 10] - base
            let currentFps = fpsLocked(at: ProcessInfo.processInfo.systemUptime)
            let interval = Int64(1_000_000 / max(1, currentFps))
            slack = interval + min(max(jitter * 3 / 2, 25_000), 80_000)
            let recent = rtts.filter { now - $0.t < 300_000 }.map(\.v).sorted()
            let queueing = recent.isEmpty ? 0 : recent[recent.count / 2] - base
            let oldestAge = unacked.first.map { now - $0.sent } ?? 0
            let stuck = oldestAge > base + 100_000
            let delivered = acked.reduce(0) { $0 + $1.bytes } * 16  // bits/s over the last half second
            let sending = sentLog.reduce(0) { $0 + $1.bytes } * 16
            // Demand: captures per second (up to the tier's rate) times the
            // average frame. A test card or a mostly still window wants far
            // less than its budget; when the link hiccups, cutting its bitrate
            // can't help, and it would only look link-limited afterwards.
            if !sentLog.isEmpty { frameBytes = sentLog.reduce(0) { $0 + $1.bytes } / sentLog.count }
            let demand = min(captures.count, currentFps) * frameBytes * 8
            // Delay alone isn't our queue: Wi-Fi jitters by itself. It only
            // counts while this stream uses a good part of its budget (so its
            // own data could be what's queueing). Frames the gate had to hold,
            // or one stuck in flight, show demand the link isn't carrying
            // whatever was sent (the gate itself keeps what's sent low).
            let busy = sending >= target / 2
            // Twice in a row (200 ms), so one late ack doesn't count.
            let signal = (busy && queueing > 40_000) || held >= 3 || stuck
            let congested = signal && lastSignal
            lastSignal = signal
            let heldNow = held
            held = 0
            let floorBps = 300_000
            if congested, now - lastDecrease > 300_000 {
                // Down to a bit under what got through: at least a fifth off, at
                // most half (a stall delivers nothing, but the link is still there).
                let measured = Int(Double(delivered) * 0.9)
                var next = max(floorBps, min(target * 4 / 5, max(measured, target / 2)))
                // App-limited (it wants about half its budget or less): never
                // below twice what it wants, however many cuts in a row. The
                // extra quarter is hysteresis, so frame sizes wobbling at the
                // floor don't switch the protection off.
                if demand > 0, demand * 2 <= target * 5 / 4 { next = max(next, min(target, demand * 2)) }
                target = next
                lastDecrease = now
                events.append((now, "down to \(target / 1000) kbit/s: queue \(queueing / 1000) ms, held \(heldNow), "
                                    + "oldest \(oldestAge / 1000) ms, base \(base / 1000) ms, sent \(sending / 1000) got \(delivered / 1000) "
                                    + "wants \(demand / 1000)"))
            } else if !congested, now - lastDecrease > 1_000_000, now - lastIncrease > 250_000, target < ceiling,
                      sending > target * 6 / 10 || now - lastDecrease > 3_000_000 {
                // Clear for a second and using what it has: probe up.
                target = min(ceiling, Int(Double(target) * 1.1) + 50_000)
                lastIncrease = now
            }
            // Fewer frames or pixels only help a stream that fills its budget;
            // a small one (a still window, a test card) keeps its tier.
            retier(now: now, linkLimited: sending >= target * 7 / 10)
            if events.count > 200 { events.removeFirst(events.count - 200) }
            return target
        }
    }

    /// Steps down quickly, straight to the tier the bitrate supports, and back
    /// up one tier at a time only when there's clearly room (hysteresis).
    private func retier(now: Int64, linkLimited: Bool) {
        let share = Double(target) / Double(max(ceiling, 1))
        if tier < RateController.tiers.count - 1, share < RateController.floors[tier], linkLimited {
            if belowSince == 0 { belowSince = now }
            if now - belowSince > 500_000 {
                tier = RateController.floors.firstIndex { share >= $0 } ?? RateController.tiers.count - 1
                belowSince = 0
                events.append((now, "tier \(tier)"))
            }
        } else {
            belowSince = 0
        }
        if tier > 0, share > RateController.floors[tier - 1] * 1.25 {
            if aboveSince == 0 { aboveSince = now }
            if now - aboveSince > 2_000_000 {
                tier -= 1
                aboveSince = 0
                events.append((now, "tier \(tier)"))
            }
        } else {
            aboveSince = 0
        }
    }

    func state(at now: TimeInterval = ProcessInfo.processInfo.systemUptime) -> [String: Any] {
        locked {
            let priority = priorityLocked(at: now)
            return ["target": target, "ceiling": ceiling, "tier": tier, "fps": fpsLocked(at: now),
                    "adaptiveFps": adaptiveFpsLocked(), "scale": RateController.tiers[tier].scale,
                    "priority": priority.role, "priorityReason": priority.reason,
                    "priorityFpsCap": priority.fpsCap ?? 0, "priorityLeaseMs": priority.remainingMs ?? 0,
                    "baseRtt": Double(baseline()) / 1000, "inFlight": unacked.count,
                    "slack": Double(slack) / 1000, "adapt": RateController.enabled]
        }
    }

    func eventList() -> [[String: Any]] { locked { events.map { ["t": $0.0, "e": $0.1] } } }
}
