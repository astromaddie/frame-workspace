import Foundation

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else { fatalError("FAIL: \(message)") }
}

@main
struct AdaptDisabledBudgetTests {
    static func main() {
        let controller = RateController(maxFps: 30)
        controller.setCeiling(8_000_000)
        let budget = SharedVideoBudget()
        budget.setRouteCap(2_000_000)
        budget.register(100)
        expect(budget.target(sessionID: 100, role: "focused", demand: controller.budgetDemand,
                             ceiling: controller.requestedTarget) == 300_000,
               "an idle session should release most of its capped-route share")
        for i in 0..<30 { controller.captured(at: 500_000 + Int64(i) * 15_000) }
        controller.sent(seq: 1, bytes: 10_000, at: 950_000)
        expect(controller.update(now: 1_000_000) == nil,
               "FRAME_MAC_VIEW_ADAPT=0 must keep the congestion controller disabled")
        expect(controller.budgetDemand == 8_000_000,
               "active source demand must use requested quality, not bytes already limited by allocation")
        expect(budget.target(sessionID: 100, role: "focused", demand: controller.budgetDemand,
                             ceiling: controller.requestedTarget) == 1_800_000,
               "a new capture must promptly restore the full active-route share despite small prior encoded frames")
        expect(CaptureBudgetPolicy.effectiveMaxLong(requested: 2560, congestionScale: controller.scale,
                                                    budgetMaxLong: 1280) == 1280,
               "the independent capture budget must still constrain full quality")
        _ = controller.update(now: 2_000_001)
        expect(controller.budgetDemand == 300_000,
               "a quiet stream should release its shared allocation after the activity window expires")
        expect(budget.target(sessionID: 100, role: "focused", demand: controller.budgetDemand,
                             ceiling: controller.requestedTarget) == 300_000,
               "quiet content should release the capped-route share again")
        controller.captured(at: 2_100_000)
        _ = controller.update(now: 2_200_000)
        expect(controller.budgetDemand == 8_000_000,
               "the first new capture after a quiet period must promptly restore requested demand")
        budget.setRouteCap(0)
        expect(budget.target(sessionID: 100, role: "focused", demand: controller.budgetDemand,
                             ceiling: controller.requestedTarget) == 8_000_000,
               "an uncapped route must bootstrap at the requested target despite an idle estimate")
        print("PASS independent demand and capture budget with congestion adaptation disabled")
    }
}
