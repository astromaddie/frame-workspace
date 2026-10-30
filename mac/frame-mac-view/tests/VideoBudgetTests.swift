import Foundation

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else { fatalError("FAIL: \(message)") }
}

@main
struct VideoBudgetTests {
    static func main() {
        aggregateRouteCapReservesControlTraffic()
        roleSharesAreFairAndUnusedCapacityMovesToDemand()
        newSessionsJoinImmediatelyAndFocusCanSwitch()
        zeroCapRemainsUncapped()
        captureLimitsComposeWithoutPerTickKeyframes()
        print("PASS shared video budget caps, redistribution, focus changes and uncapped routes")
    }

    private static func aggregateRouteCapReservesControlTraffic() {
        let requests = [
            VideoBudgetRequest(sessionID: 1, role: "focused", demand: 20_000_000, ceiling: 20_000_000),
            VideoBudgetRequest(sessionID: 2, role: "background", demand: 20_000_000, ceiling: 20_000_000),
            VideoBudgetRequest(sessionID: 3, role: "background", demand: 20_000_000, ceiling: 20_000_000)
        ]
        let allocations = SharedVideoBudgetPolicy.allocate(routeCap: 10_000_000, requests: requests)
        expect(allocations.values.reduce(0, +) == 9_000_000,
               "capped allocations must fit the route's ninety-percent video budget")
        expect(allocations[1] == 6_300_000, "focused stream should receive seventy percent while all demand remains")
        expect(allocations[2] == 1_350_000 && allocations[3] == 1_350_000,
               "background streams with equal demand should share their offer equally")
    }

    private static func roleSharesAreFairAndUnusedCapacityMovesToDemand() {
        let requests = [
            VideoBudgetRequest(sessionID: 1, role: "focused", demand: 200_000, ceiling: 1_000_000),
            VideoBudgetRequest(sessionID: 2, role: "background", demand: 100_000, ceiling: 1_000_000),
            VideoBudgetRequest(sessionID: 3, role: "background", demand: 1_000_000, ceiling: 1_000_000)
        ]
        let allocations = SharedVideoBudgetPolicy.allocate(routeCap: 1_000_000, requests: requests)
        expect(allocations[1] == 200_000 && allocations[2] == 100_000,
               "streams below their offers should receive only their demand")
        expect(allocations[3] == 600_000 && allocations.values.reduce(0, +) == 900_000,
               "unused focused and small-background shares should move to the remaining demand")

        let noFocus = SharedVideoBudgetPolicy.allocate(routeCap: 1_000_000, requests: [
            VideoBudgetRequest(sessionID: 8, role: "normal", demand: 700_000, ceiling: 900_000),
            VideoBudgetRequest(sessionID: 9, role: "normal", demand: 700_000, ceiling: 900_000)
        ])
        expect(noFocus[8] == 450_000 && noFocus[9] == 450_000,
               "no explicit selection should retain fair legacy priority across active streams")
    }

    private static func newSessionsJoinImmediatelyAndFocusCanSwitch() {
        let budget = SharedVideoBudget()
        budget.setRouteCap(1_000_000)
        for id in 1...2 { budget.register(id) }
        expect(budget.target(sessionID: 1, role: "focused", demand: 1_000_000, ceiling: 1_000_000) == 900_000,
               "a sole active stream should receive capacity beyond its unused background offer")
        expect(budget.target(sessionID: 2, role: "background", demand: 1_000_000, ceiling: 1_000_000) == 270_000,
               "background should receive the remainder of its offered share")
        expect(budget.allocation(for: 1) == 630_000,
               "the existing focus should yield its redistributable share when background demand arrives")

        budget.register(3)
        let newSession = budget.target(sessionID: 3, role: "background", demand: 1_000_000, ceiling: 1_000_000)
        expect(newSession == 135_000 && budget.allocation(for: 2) == 135_000,
               "a newly active background should join the current fair share without stale reservation")
        _ = budget.target(sessionID: 1, role: "background", demand: 1_000_000, ceiling: 1_000_000)
        let switched = budget.target(sessionID: 2, role: "focused", demand: 1_000_000, ceiling: 1_000_000)
        expect(switched == 630_000 && budget.allocation(for: 1) > 0 && budget.allocation(for: 3) > 0,
               "focus switching should recalculate all shares while every active stream keeps service")
        expect((1...3).map { budget.allocation(for: $0) }.reduce(0, +) <= 900_000,
               "all current targets must remain within the shared route cap")
    }

    private static func zeroCapRemainsUncapped() {
        let requests = [
            VideoBudgetRequest(sessionID: 1, role: "focused", demand: 30_000_000, ceiling: 25_000_000),
            VideoBudgetRequest(sessionID: 2, role: "background", demand: 12_000_000, ceiling: 8_000_000)
        ]
        let allocations = SharedVideoBudgetPolicy.allocate(routeCap: 0, requests: requests)
        expect(allocations[1] == 25_000_000 && allocations[2] == 8_000_000,
               "zero route cap should return each requested quality ceiling without a USB limit")
    }

    private static func captureLimitsComposeWithoutPerTickKeyframes() {
        expect(CaptureBudgetPolicy.effectiveMaxLong(requested: 2560, congestionScale: 1,
                                                    budgetMaxLong: 1280) == 1280,
               "the decoder budget must reduce capture even when congestion adaptation is off")
        expect(CaptureBudgetPolicy.effectiveMaxLong(requested: 2560, congestionScale: 0.5,
                                                    budgetMaxLong: 1600) == 1280,
               "capture resolution should combine the tighter congestion and budget limits")
        expect(CaptureBudgetPolicy.effectiveMaxLong(requested: 800, congestionScale: 1,
                                                    budgetMaxLong: 1280) == 800,
               "a higher budget target must not upscale lower requested quality")
        let base = ("background", "background", 1280)
        expect(!CaptureBudgetPolicy.requiresKeyFrame(previousPriorityRole: base.0, nextPriorityRole: base.0,
                                                     previousBudgetRole: base.1, nextBudgetRole: base.1,
                                                     previousMaxLong: base.2, nextMaxLong: base.2),
               "allocation ticks alone must not request a keyframe")
        expect(CaptureBudgetPolicy.requiresKeyFrame(previousPriorityRole: "background", nextPriorityRole: "selected",
                                                    previousBudgetRole: base.1, nextBudgetRole: base.1,
                                                    previousMaxLong: base.2, nextMaxLong: base.2),
               "input role switches must request a fresh reference frame")
        expect(CaptureBudgetPolicy.requiresKeyFrame(previousPriorityRole: base.0, nextPriorityRole: base.0,
                                                    previousBudgetRole: base.1, nextBudgetRole: "focused",
                                                    previousMaxLong: base.2, nextMaxLong: 1600),
               "budget role or capture scale changes must request a fresh reference frame")
    }
}
