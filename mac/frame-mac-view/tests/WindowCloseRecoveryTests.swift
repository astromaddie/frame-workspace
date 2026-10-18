import Foundation

@main
enum WindowCloseRecoveryTests {
    private static let target = WindowIdentity(windowID: 42, processID: 700)

    static func main() {
        transientAbsenceAndReappearanceResetTheGracePeriod()
        confirmedAbsenceClosesOnlyAfterTheBound()
        mismatchedWindowOrOwnerDoesNotCountAsTheTarget()
        staleContentCannotConfirmARealClose()
        cancellationRejectsLateObservations()
        print("PASS ScreenCaptureKit window-close recovery policy")
    }

    private static func transientAbsenceAndReappearanceResetTheGracePeriod() {
        var recovery = WindowCloseRecovery(target: target, grace: 5)
        expect(.waiting, recovery.observe(cgWindow: nil, shareableWindow: nil, shareableContentFresh: true, at: 10))
        expect(.present, recovery.observe(cgWindow: nil, shareableWindow: target, shareableContentFresh: true, at: 14))
        expect(.waiting, recovery.observe(cgWindow: nil, shareableWindow: nil, shareableContentFresh: true, at: 14.1))
        expect(.present, recovery.observe(cgWindow: target, shareableWindow: nil, shareableContentFresh: false, at: 14.2))
        expect(.waiting, recovery.observe(cgWindow: nil, shareableWindow: nil, shareableContentFresh: true, at: 14.3))
        expect(.waiting, recovery.observe(cgWindow: nil, shareableWindow: nil, shareableContentFresh: true, at: 15.1))
        expect(.closed, recovery.observe(cgWindow: nil, shareableWindow: nil, shareableContentFresh: true, at: 19.3))
    }

    private static func confirmedAbsenceClosesOnlyAfterTheBound() {
        var recovery = WindowCloseRecovery(target: target, grace: 5)
        expect(.waiting, recovery.observe(cgWindow: nil, shareableWindow: nil, shareableContentFresh: true, at: 0))
        expect(.waiting, recovery.observe(cgWindow: nil, shareableWindow: nil, shareableContentFresh: true, at: 4.999))
        expect(.closed, recovery.observe(cgWindow: nil, shareableWindow: nil, shareableContentFresh: true, at: 5))
    }

    private static func mismatchedWindowOrOwnerDoesNotCountAsTheTarget() {
        var recovery = WindowCloseRecovery(target: target, grace: 2)
        let reusedID = WindowIdentity(windowID: 42, processID: 701)
        let differentID = WindowIdentity(windowID: 43, processID: 700)
        expect(.waiting, recovery.observe(cgWindow: reusedID, shareableWindow: differentID,
                                          shareableContentFresh: true, at: 3))
        expect(.closed, recovery.observe(cgWindow: reusedID, shareableWindow: differentID,
                                         shareableContentFresh: true, at: 5))

        let unknownOwner = WindowIdentity(windowID: 42, processID: nil)
        var unknownOwnerRecovery = WindowCloseRecovery(target: unknownOwner)
        expect(.present, unknownOwnerRecovery.observe(cgWindow: target, shareableWindow: nil,
                                                     shareableContentFresh: false, at: 6))
    }

    private static func staleContentCannotConfirmARealClose() {
        var recovery = WindowCloseRecovery(target: target, grace: 5)
        expect(.waiting, recovery.observe(cgWindow: nil, shareableWindow: nil, shareableContentFresh: false, at: 0))
        expect(.waiting, recovery.observe(cgWindow: nil, shareableWindow: nil, shareableContentFresh: false, at: 100))
        expect(.waiting, recovery.observe(cgWindow: nil, shareableWindow: nil, shareableContentFresh: true, at: 100))
    }

    private static func cancellationRejectsLateObservations() {
        var recovery = WindowCloseRecovery(target: target, grace: 1)
        expect(.waiting, recovery.observe(cgWindow: nil, shareableWindow: nil, shareableContentFresh: true, at: 0))
        recovery.cancel()
        expect(.cancelled, recovery.observe(cgWindow: target, shareableWindow: target, shareableContentFresh: true, at: 1))
    }

    private static func expect(_ expected: WindowCloseRecovery.Decision,
                               _ actual: WindowCloseRecovery.Decision,
                               file: StaticString = #file, line: UInt = #line) {
        guard expected == actual else {
            fatalError("expected \(expected), got \(actual)", file: file, line: line)
        }
    }
}
