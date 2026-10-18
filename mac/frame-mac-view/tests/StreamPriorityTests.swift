import Foundation

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else { fatalError("FAIL: \(message)") }
}

@main
struct StreamPriorityTests {
    static func main() {
        selectionAndRapidSwitches()
        blurReleaseDoesNotStealSelection()
        expiryAndDisconnectRestoreFps()
        singleStreamAndExistingControllerStayUnchanged()
        disabledPolicyPreservesRates()
        print("PASS stream priority leases, restoration and RateController behavior")
    }

    private static func disabledPolicyPreservesRates() {
        let registry = StreamActivityRegistry(enabled: false)
        registry.register(1)
        registry.register(2)
        registry.recordMessage(sessionID: 1, type: "focus", at: 1)
        let peer = RateController(maxFps: 60, sessionID: 2, activity: registry)
        expect(peer.fps(at: 2) == 60, "disabled priority must preserve the normal rate")
        expect(peer.state(at: 2)["priorityReason"] as? String == "disabled",
               "stats must identify the disabled policy for matched comparison")
    }

    private static func blurReleaseDoesNotStealSelection() {
        let registry = StreamActivityRegistry()
        registry.register(1) // A, previously selected
        registry.register(2) // B, newly focused
        let first = RateController(maxFps: 60, sessionID: 1, activity: registry)
        let second = RateController(maxFps: 60, sessionID: 2, activity: registry)

        expect(registry.recordMessage(sessionID: 1, type: "k", at: 1),
               "a real key event must select its source as active")
        expect(registry.recordMessage(sessionID: 2, type: "focus", at: 2),
               "the viewer's focus message must select the newly focused source")
        expect(!registry.recordMessage(sessionID: 1, type: "release", at: 3),
               "release is blur cleanup and must not renew or select its source")
        expect(first.fps(at: 3) == StreamActivityRegistry.backgroundFpsCap && second.fps(at: 3) == 60,
               "focus B followed by release A must leave B selected")

        expect(registry.recordMessage(sessionID: 1, type: "wheel", at: 4),
               "subsequent real input must still switch priority normally")
        expect(first.fps(at: 4) == 60 && second.fps(at: 4) == StreamActivityRegistry.backgroundFpsCap,
               "input on A must select A and cap B")
    }

    private static func selectionAndRapidSwitches() {
        let registry = StreamActivityRegistry()
        registry.register(1)
        registry.register(2)
        let first = RateController(maxFps: 60, sessionID: 1, activity: registry)
        let second = RateController(maxFps: 60, sessionID: 2, activity: registry)

        expect(first.fps(at: 10) == 60 && second.fps(at: 10) == 60,
               "streams without an explicit selection must remain uncapped")
        registry.noteActivity(sessionID: 1, reason: "focus", at: 10)
        expect(first.fps(at: 10) == 60 && second.fps(at: 10) == StreamActivityRegistry.backgroundFpsCap,
               "focus must preserve the selected stream and cap its background peer")
        expect(second.state(at: 10)["priorityReason"] as? String == "other-stream-focus",
               "background telemetry must identify the source of its priority")
        expect(second.state(at: 10)["priorityFpsCap"] as? Int == StreamActivityRegistry.backgroundFpsCap,
               "background telemetry must report the applied frame-rate cap")

        registry.noteActivity(sessionID: 2, reason: "input", at: 11)
        expect(first.fps(at: 11) == StreamActivityRegistry.backgroundFpsCap && second.fps(at: 11) == 60,
               "input on another stream must switch the shared selection immediately")
        registry.noteActivity(sessionID: 1, reason: "focus", at: 12)
        expect(first.fps(at: 12) == 60 && second.fps(at: 12) == StreamActivityRegistry.backgroundFpsCap,
               "rapid focus changes must leave only the latest stream uncapped")
    }

    private static func expiryAndDisconnectRestoreFps() {
        let registry = StreamActivityRegistry()
        registry.register(1)
        registry.register(2)
        let first = RateController(maxFps: 60, sessionID: 1, activity: registry)
        let second = RateController(maxFps: 60, sessionID: 2, activity: registry)

        registry.noteActivity(sessionID: 1, reason: "input", at: 100)
        expect(second.fps(at: 129.999) == StreamActivityRegistry.backgroundFpsCap,
               "activity lease must remain active for its full duration")
        expect(second.fps(at: 130) == 60, "expired activity must restore every stream's normal frame rate")
        expect(second.state(at: 130)["priorityReason"] as? String == "lease-expired",
               "stats must explain why the background cap was released")
        expect(first.fps(at: 130) == 60, "the previously selected stream must also restore normally")

        registry.noteActivity(sessionID: 1, reason: "focus", at: 200)
        expect(second.fps(at: 200) == StreamActivityRegistry.backgroundFpsCap,
               "a fresh selection must reapply the cap")
        registry.unregister(1)
        expect(second.fps(at: 200.1) == 60,
               "disconnecting the selected session must clear its lease and release the cap")
        expect(second.state(at: 200.1)["priorityReason"] as? String == "single-stream",
               "one remaining stream must report normal single-stream operation")

        registry.register(3)
        let third = RateController(maxFps: 60, sessionID: 3, activity: registry)
        expect(second.fps(at: 201) == 60 && third.fps(at: 201) == 60,
               "additional sessions must not inherit a disconnected stream's selection")
        expect(second.state(at: 201)["priorityReason"] as? String == "selected-disconnected",
               "multi-stream telemetry must explain that the previous selection disconnected")
        registry.unregister(2)
        registry.unregister(3)
        registry.register(8)
        registry.register(9)
        expect(RateController(maxFps: 60, sessionID: 8, activity: registry).state(at: 202)["priorityReason"] as? String == "no-selection",
               "a fresh set of sessions must not inherit a stale disconnect reason")

        let unregistered = StreamActivityRegistry()
        unregistered.register(4)
        unregistered.noteActivity(sessionID: 5, reason: "focus", at: 10)
        unregistered.register(6)
        expect(RateController(maxFps: 60, sessionID: 6, activity: unregistered).fps(at: 10) == 60,
               "activity from a session outside the live registry must be ignored")
    }

    private static func singleStreamAndExistingControllerStayUnchanged() {
        let registry = StreamActivityRegistry()
        registry.register(7)
        registry.noteActivity(sessionID: 7, reason: "input", at: 1)
        let only = RateController(maxFps: 60, sessionID: 7, activity: registry)
        expect(only.fps(at: 1) == 60, "a selected single stream must never be capped")
        expect(only.state(at: 1)["priorityReason"] as? String == "single-stream",
               "single-stream telemetry must make the no-cap behavior clear")

        let existing = RateController(maxFps: 30)
        existing.setCeiling(8_000_000)
        expect(existing.fps == 30 && existing.scale == 1,
               "an unconfigured RateController must preserve its existing FPS and full-resolution tier")
        expect(existing.maySend(now: 1_000_000), "the normal controller must allow an initial unacknowledged frame")
        expect(existing.update(now: 1_000_000) == 8_000_000,
               "the normal controller's uncongested bitrate target must stay at its ceiling")
        expect(existing.state()["tier"] as? Int == 0,
               "the normal controller must remain at its full-resolution tier")

        let congested = RateController(maxFps: 60)
        congested.setCeiling(10_000_000)
        congested.sent(seq: 1, bytes: 400_000, at: 100_000)
        expect(congested.acked(seq: 1, at: 120_000), "the controller must accept its first path sample")
        congested.sent(seq: 2, bytes: 400_000, at: 200_000)
        expect(congested.acked(seq: 2, at: 280_000), "the controller must accept a delayed path sample")
        _ = congested.update(now: 300_000)
        let reduced = congested.update(now: 400_000)
        expect((reduced ?? 10_000_000) < 10_000_000,
               "the existing controller must still lower bitrate after sustained queueing")
        expect(congested.state()["tier"] as? Int == 0 && congested.scale == 1,
               "one congestion response must not downgrade a still-healthy resolution tier")
    }
}
