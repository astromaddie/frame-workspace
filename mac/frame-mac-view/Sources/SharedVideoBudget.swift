import Foundation

struct VideoBudgetRequest: Equatable {
    let sessionID: Int
    let role: String
    let demand: Int
    let ceiling: Int
}

enum SharedVideoBudgetPolicy {
    static let videoFractionNumerator = 9
    static let videoFractionDenominator = 10
    static let focusedShare = 0.70

    static func videoBudget(routeCap: Int) -> Int? {
        routeCap > 0 ? routeCap * videoFractionNumerator / videoFractionDenominator : nil
    }

    /// Returns per-session targets. Zero route cap means uncapped; it never
    /// invents a USB limit. Capped routes reserve one tenth for control and
    /// protocol traffic, then max-min allocate each role's offered share.
    static func allocate(routeCap: Int, requests: [VideoBudgetRequest]) -> [Int: Int] {
        let wants = Dictionary(uniqueKeysWithValues: requests.map {
            ($0.sessionID, max(0, min($0.demand, $0.ceiling)))
        })
        guard let budget = videoBudget(routeCap: routeCap) else {
            // A zero route cap preserves the historical uncapped path: the
            // activity estimate must not lower a stream's requested target.
            return Dictionary(uniqueKeysWithValues: requests.map { ($0.sessionID, max(0, $0.ceiling)) })
        }
        guard budget > 0, !requests.isEmpty else { return Dictionary(uniqueKeysWithValues: requests.map { ($0.sessionID, 0) }) }

        let focused = requests.filter { $0.role == "focused" }
        guard !focused.isEmpty else {
            // With no explicit focus, preserve the legacy equal-priority model.
            return fairAllocation(items: requests, wants: wants, budget: budget)
        }
        let backgrounds = requests.filter { $0.role == "background" }
        let unclassified = requests.filter { $0.role != "focused" && $0.role != "background" }
        let backgroundGroup = backgrounds + unclassified
        var result = Dictionary(uniqueKeysWithValues: requests.map { ($0.sessionID, 0) })
        let focusedOffer = Int(Double(budget) * focusedShare)
        let backgroundOffer = budget - focusedOffer
        add(fairAllocation(items: focused, wants: wants, budget: focusedOffer), to: &result)
        add(fairAllocation(items: backgroundGroup, wants: wants, budget: backgroundOffer), to: &result)

        var spare = budget - result.values.reduce(0, +)
        while spare > 0 {
            let focusNeed = focused.reduce(0) { $0 + max(0, wants[$1.sessionID, default: 0] - result[$1.sessionID, default: 0]) }
            let backgroundNeed = backgroundGroup.reduce(0) {
                $0 + max(0, wants[$1.sessionID, default: 0] - result[$1.sessionID, default: 0])
            }
            guard focusNeed > 0 || backgroundNeed > 0 else { break }
            if focusNeed == 0 {
                add(fairAllocation(items: backgroundGroup, wants: wants, budget: spare,
                                   already: result), to: &result)
            } else if backgroundNeed == 0 {
                add(fairAllocation(items: focused, wants: wants, budget: spare,
                                   already: result), to: &result)
            } else {
                let focusedOffer = max(1, Int(Double(spare) * focusedShare))
                let backgroundOffer = spare - focusedOffer
                add(fairAllocation(items: focused, wants: wants, budget: focusedOffer,
                                   already: result), to: &result)
                add(fairAllocation(items: backgroundGroup, wants: wants, budget: backgroundOffer,
                                   already: result), to: &result)
            }
            let nextSpare = budget - result.values.reduce(0, +)
            if nextSpare >= spare { break }
            spare = nextSpare
        }
        return result
    }

    /// Equal max-min service within a role: a small demand is met first and
    /// its unused share is redistributed to streams that still need capacity.
    private static func fairAllocation(items: [VideoBudgetRequest], wants: [Int: Int], budget: Int,
                                       already: [Int: Int] = [:]) -> [Int: Int] {
        guard budget > 0, !items.isEmpty else { return [:] }
        let ordered = items.sorted { $0.sessionID < $1.sessionID }
        var remaining = budget
        var unmet = ordered.filter { wants[$0.sessionID, default: 0] > already[$0.sessionID, default: 0] }
        var result: [Int: Int] = [:]
        while remaining > 0, !unmet.isEmpty {
            let base = remaining / unmet.count
            let extra = remaining % unmet.count
            var spent = 0
            for (index, item) in unmet.enumerated() {
                let offer = base + (index < extra ? 1 : 0)
                let need = max(0, wants[item.sessionID, default: 0] - already[item.sessionID, default: 0]
                               - result[item.sessionID, default: 0])
                let granted = min(need, offer)
                result[item.sessionID, default: 0] += granted
                spent += granted
            }
            guard spent > 0 else { break }
            remaining -= spent
            unmet = unmet.filter {
                wants[$0.sessionID, default: 0] > already[$0.sessionID, default: 0] + result[$0.sessionID, default: 0]
            }
        }
        return result
    }

    private static func add(_ values: [Int: Int], to result: inout [Int: Int]) {
        for (id, value) in values { result[id, default: 0] += value }
    }
}

enum CaptureBudgetPolicy {
    static func effectiveMaxLong(requested: Int, congestionScale: Double, budgetMaxLong: Int?) -> Int {
        let congestionLimit = Int(Double(requested) * max(0, congestionScale))
        return max(320, min(congestionLimit, budgetMaxLong ?? requested))
    }

    static func requiresKeyFrame(previousPriorityRole: String?, nextPriorityRole: String,
                                 previousBudgetRole: String?, nextBudgetRole: String,
                                 previousMaxLong: Int, nextMaxLong: Int) -> Bool {
        (previousPriorityRole.map { $0 != nextPriorityRole } ?? false)
            || (previousBudgetRole.map { $0 != nextBudgetRole } ?? false)
            || (previousMaxLong > 0 && previousMaxLong != nextMaxLong)
    }
}

enum EncoderDataRatePolicy {
    static func shouldLimit(routeCap: Int, configuredCap: Bool, previouslyLimited: Bool) -> Bool {
        routeCap > 0 || configuredCap || previouslyLimited
    }
}

/// A process-wide snapshot allocator. Each session refreshes its own demand
/// every adaptation tick; a new session is included on its first tick, and all
/// existing sessions recalculate against the same current set.
final class SharedVideoBudget {
    private let lock = NSLock()
    private var routeCapValue = 0
    private var requests: [Int: VideoBudgetRequest] = [:]

    private func locked<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }

    var routeCap: Int { locked { routeCapValue } }
    var videoBudget: Int? { locked { SharedVideoBudgetPolicy.videoBudget(routeCap: routeCapValue) } }

    func setRouteCap(_ bps: Int) {
        locked { routeCapValue = max(0, bps) }
    }

    func register(_ sessionID: Int) {
        locked { requests[sessionID] = VideoBudgetRequest(sessionID: sessionID, role: "normal", demand: 0, ceiling: 0) }
    }

    func unregister(_ sessionID: Int) {
        locked { requests[sessionID] = nil }
    }

    func target(sessionID: Int, role: String, demand: Int, ceiling: Int) -> Int {
        locked {
            requests[sessionID] = VideoBudgetRequest(sessionID: sessionID, role: role,
                                                     demand: max(0, demand), ceiling: max(0, ceiling))
            let allocations = SharedVideoBudgetPolicy.allocate(routeCap: routeCapValue,
                                                               requests: Array(requests.values))
            return allocations[sessionID] ?? 0
        }
    }

    func allocation(for sessionID: Int) -> Int {
        locked {
            SharedVideoBudgetPolicy.allocate(routeCap: routeCapValue, requests: Array(requests.values))[sessionID] ?? 0
        }
    }
}
