import Foundation

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else { fatalError("FAIL: \(message)") }
}

@main
struct AdaptDisabledBudgetTests {
    static func main() {
        let controller = RateController(maxFps: 30)
        controller.setCeiling(8_000_000)
        for i in 0..<30 { controller.captured(at: 500_000 + Int64(i) * 15_000) }
        controller.sent(seq: 1, bytes: 10_000, at: 950_000)
        expect(controller.update(now: 1_000_000) == nil,
               "FRAME_MAC_VIEW_ADAPT=0 must keep the congestion controller disabled")
        expect(controller.budgetDemand == 2_400_000,
               "shared allocation demand must still follow recent frame load when adaptation is off")
        expect(CaptureBudgetPolicy.effectiveMaxLong(requested: 2560, congestionScale: controller.scale,
                                                    budgetMaxLong: 1280) == 1280,
               "the independent capture budget must still constrain full quality")
        print("PASS independent demand and capture budget with congestion adaptation disabled")
    }
}
