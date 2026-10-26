import Foundation

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else { fatalError("FAIL: \(message)") }
}

@main
struct AppsStateTests {
    static func main() throws {
        try validatesCompanionEntries()
        ordersPanelsByMRUAndCyclesInBothDirections()
        freezesOrderAndKeepsChosenAfterRelease()
        renewalsPreserveVisibleCycleState()
        includesPanelsForAppsMissingFromSeedMRU()
        controllerRenewalWithoutEntriesPreservesPanels()
        controllerOpenReleaseAndCloseDoNotChoose()
        desktopModeAndLeaseExpiryResetState()
        companionChoiceBringsAnExistingPanelForward()
        print("PASS app switcher validation, MRU ordering, cycling, lease state and choices")
    }

    private static func tryInvalid(_ json: String) -> Bool {
        do { _ = try SwitcherPanel.parseEntries(json); return false }
        catch { return true }
    }

    private static func validatesCompanionEntries() throws {
        let entries = try SwitcherPanel.parseEntries("""
        [{"key":"com.frame-workspace.native.1000.panel-w10","bundleID":"com.example.alpha"},
         {"key":"com.frame-workspace.native.1000.panel-w11","bundleID":"org.example.beta"}]
        """)
        expect(entries.map(\.key) == ["com.frame-workspace.native.1000.panel-w10",
                                      "com.frame-workspace.native.1000.panel-w11"],
               "owned Steam panel keys must parse")
        expect(tryInvalid("{\"key\":\"window:10\",\"bundleID\":\"com.example.alpha\"}"),
               "the companion must send an array")
        expect(tryInvalid("[{\"key\":\"window:10\",\"bundleID\":\"com.example.alpha\"}]"),
               "CGWindow source keys must not be confused with Frame panel keys")
        expect(tryInvalid("[{\"key\":\"com.frame-workspace.native.1234567890123456.panel-w1\",\"bundleID\":\"com.example.alpha\"}]"),
               "panel owner IDs longer than the companion's bound must be rejected")
        expect(tryInvalid("[{\"key\":\"window:10\",\"bundleID\":\"bad id\"}]"),
               "malformed bundle IDs must be rejected")
        expect(tryInvalid("[{\"key\":\"com.frame-workspace.native.1000.panel-w10\",\"bundleID\":\"com.example.alpha\"}," +
                          "{\"key\":\"com.frame-workspace.native.1000.panel-w10\",\"bundleID\":\"org.example.beta\"}]"),
               "duplicate panel keys must be rejected")
        let tooMany = (1...257).map {
            SwitcherPanel(key: "com.frame-workspace.native.1000.panel-w\($0)", bundleID: "com.example.alpha")
        }
        let tooManyJSON = String(data: try JSONSerialization.data(withJSONObject: tooMany.map {
            ["key": $0.key, "bundleID": $0.bundleID]
        }), encoding: .utf8)!
        expect(tryInvalid(tooManyJSON), "the companion's 256-panel bound must be enforced")
        expect(tryInvalid("[]" + String(repeating: " ", count: 96 * 1024)),
               "the decoded entries payload must stay within 96 KiB")
    }

    private static func ordersPanelsByMRUAndCyclesInBothDirections() {
        let state = SwitcherState()
        state.seedMRU(["com.example.alpha", "org.example.beta", "net.example.gamma"])
        let entries = [
            SwitcherPanel(key: "com.frame-workspace.native.1000.panel-w30", bundleID: "org.example.beta"),
            SwitcherPanel(key: "com.frame-workspace.native.1000.panel-w11", bundleID: "com.example.alpha"),
            SwitcherPanel(key: "com.frame-workspace.native.1000.panel-w12", bundleID: "com.example.alpha"),
            SwitcherPanel(key: "com.frame-workspace.native.1000.panel-w40", bundleID: "net.example.gamma")
        ]
        state.enableSpatial(entries: entries, at: 100)
        state.setTapActive(true)
        expect(state.snapshot().windows == ["com.frame-workspace.native.1000.panel-w11",
                                          "com.frame-workspace.native.1000.panel-w12",
                                          "com.frame-workspace.native.1000.panel-w30",
                                          "com.frame-workspace.native.1000.panel-w40"],
               "panels must group in app MRU order while preserving each app's panel order")

        expect(state.cycle(reverse: false), "spatial Cmd-Tab must be handled")
        var current = state.snapshot()
        expect(current.open && current.index == 1 && current.chosen == nil,
               "forward cycling starts after the active app's most recent panel")
        state.commandReleased()
        current = state.snapshot()
        expect(!current.open && current.chosen == "com.frame-workspace.native.1000.panel-w12",
               "release commits and retains the highlighted key")

        expect(state.cycle(reverse: true), "spatial reverse Cmd-Tab must be handled")
        current = state.snapshot()
        expect(current.index == 3 && current.chosen == nil,
               "reverse cycling from the active app wraps to the last panel")
        state.commandReleased()
        expect(state.snapshot().chosen == "com.frame-workspace.native.1000.panel-w40",
               "reverse selection commits only after modifier release")
    }

    private static func freezesOrderAndKeepsChosenAfterRelease() {
        let state = SwitcherState()
        state.seedMRU(["com.example.alpha", "org.example.beta"])
        state.enableSpatial(entries: [
            SwitcherPanel(key: "com.frame-workspace.native.1000.panel-w1", bundleID: "com.example.alpha"),
            SwitcherPanel(key: "com.frame-workspace.native.1000.panel-w2", bundleID: "org.example.beta")
        ], at: 1)
        _ = state.cycle(reverse: false)
        let beforeActivation = state.snapshot()
        state.noteActivation("org.example.beta")
        let whileOpen = state.snapshot()
        expect(whileOpen.windows == beforeActivation.windows && whileOpen.seq == beforeActivation.seq,
               "workspace activation must not reorder or visibly change an open strip")
        expect(whileOpen.chosen == nil && whileOpen.index == 1,
               "the highlighted key is not exposed as a committed choice while Cmd is held")

        state.commandReleased()
        let closed = state.snapshot()
        expect(closed.windows == ["com.frame-workspace.native.1000.panel-w2",
                                  "com.frame-workspace.native.1000.panel-w1"],
               "the next cycle uses the updated MRU order")
        expect(closed.chosen == "com.frame-workspace.native.1000.panel-w2" && closed.index == 0,
               "the selected key survives release even when it moves in the new order")

        _ = state.cycle(reverse: false)
        let next = state.snapshot()
        expect(next.chosen == nil && next.index == 1, "a new cycle advances from the now-active app")
        state.commandReleased()
        expect(state.snapshot().chosen == "com.frame-workspace.native.1000.panel-w1",
               "the new cycle commits its highlighted panel when Cmd is released")
    }

    private static func renewalsPreserveVisibleCycleState() {
        let state = SwitcherState()
        state.seedMRU(["com.example.alpha", "org.example.beta"])
        let entries = [
            SwitcherPanel(key: "com.frame-workspace.native.1000.panel-w5", bundleID: "com.example.alpha"),
            SwitcherPanel(key: "com.frame-workspace.native.1000.panel-w6", bundleID: "org.example.beta")
        ]
        state.enableSpatial(entries: entries, at: 10)
        _ = state.cycle(reverse: false)
        let before = state.snapshot()
        state.enableSpatial(entries: entries, at: 11)
        let after = state.snapshot()
        expect(after.open && after.index == before.index && after.chosen == before.chosen,
               "same-entry one-second renewals must not reset the switcher")
        expect(after.seq == before.seq, "a lease renewal without a visible change must not advance seq")
        expect(!state.expireLease(at: 15.9), "a renewed spatial lease must remain active for five seconds")
    }

    private static func includesPanelsForAppsMissingFromSeedMRU() {
        let state = SwitcherState()
        state.seedMRU(["com.example.alpha"])
        state.enableSpatial(entries: [
            SwitcherPanel(key: "com.frame-workspace.native.1000.panel-w1", bundleID: "com.example.alpha"),
            SwitcherPanel(key: "com.frame-workspace.native.1000.panel-w2", bundleID: "org.example.newapp")
        ], at: 1)
        expect(state.snapshot().windows == ["com.frame-workspace.native.1000.panel-w1",
                                            "com.frame-workspace.native.1000.panel-w2"],
               "a newly launched app panel must remain in the strip before its first activation notification")
    }

    private static func controllerRenewalWithoutEntriesPreservesPanels() {
        let state = SwitcherState()
        state.seedMRU(["com.example.alpha", "org.example.beta"])
        let entries = [
            SwitcherPanel(key: "com.frame-workspace.native.1000.panel-w1", bundleID: "com.example.alpha"),
            SwitcherPanel(key: "com.frame-workspace.native.1000.panel-w2", bundleID: "org.example.beta")
        ]
        state.enableSpatial(entries: entries, at: 1)
        state.enableSpatial(entries: nil, at: 2)
        expect(state.snapshot().windows == entries.map(\.key),
               "a spatial open/close lease renewal without entries must preserve the last panel list")
        state.enableSpatial(entries: [], at: 3)
        expect(state.snapshot().windows.isEmpty, "an explicit empty entries array must clear the panel list")
    }

    private static func controllerOpenReleaseAndCloseDoNotChoose() {
        let state = SwitcherState()
        state.seedMRU(["com.example.alpha", "org.example.beta"])
        state.enableSpatial(entries: [
            SwitcherPanel(key: "com.frame-workspace.native.1000.panel-w1", bundleID: "com.example.alpha"),
            SwitcherPanel(key: "com.frame-workspace.native.1000.panel-w2", bundleID: "org.example.beta")
        ], at: 1)

        state.controllerOpen()
        expect(state.snapshot().open && state.snapshot().chosen == nil,
               "controller open exposes a highlight without committing it")
        state.commandReleased()
        expect(state.snapshot().open && state.snapshot().chosen == nil,
               "a bare Cmd release must not commit a controller-open highlight")

        state.controllerClose()
        let closed = state.snapshot()
        expect(!closed.open && closed.chosen == nil,
               "controller close dismisses the highlight without choosing a panel")

        _ = state.cycle(reverse: false)
        state.commandReleased()
        expect(state.snapshot().chosen != nil, "keyboard release commits a choice")
        state.controllerClose()
        state.noteActivation("org.example.beta")
        expect(state.snapshot().chosen == nil,
               "acknowledged choices must not replay after a later app activation")
    }

    private static func companionChoiceBringsAnExistingPanelForward() {
        let state = SwitcherState()
        let first = "com.frame-workspace.native.1000.panel-w1"
        let second = "com.frame-workspace.native.1000.panel-w2"
        state.seedMRU(["com.example.alpha", "org.example.beta"])
        state.enableSpatial(entries: [SwitcherPanel(key: first, bundleID: "com.example.alpha"),
                                      SwitcherPanel(key: second, bundleID: "org.example.beta")], at: 1)
        let start = state.snapshot().seq
        expect(state.choose(second), "a known panel can be chosen")
        var current = state.snapshot()
        expect(!current.open && current.chosen == second && current.seq > start, "a choice is delivered with a new sequence")
        state.controllerClose()
        let acknowledged = state.snapshot().seq
        expect(state.choose(second), "the same panel can be chosen again")
        current = state.snapshot()
        expect(current.chosen == second && current.seq > acknowledged, "a repeated choice is a new event")
        expect(!state.choose("com.frame-workspace.native.1000.panel-w9"), "unknown panels are never chosen")
        state.disableSpatial()
        expect(!state.choose(first), "desktop mode ignores choices")
    }

    private static func desktopModeAndLeaseExpiryResetState() {
        let state = SwitcherState()
        state.seedMRU(["com.example.alpha", "org.example.beta"])
        let entries = [
            SwitcherPanel(key: "com.frame-workspace.native.1000.panel-w7", bundleID: "com.example.alpha"),
            SwitcherPanel(key: "com.frame-workspace.native.1000.panel-w8", bundleID: "org.example.beta")
        ]
        state.enableSpatial(entries: entries, at: 20)
        state.setTapActive(true)
        _ = state.cycle(reverse: false)
        expect(state.expireLease(at: 25), "an unrenewed spatial lease must expire")
        var current = state.snapshot()
        expect(!current.open && current.chosen == nil && current.windows.isEmpty && !current.tapActive,
               "lease expiry removes tap state and resets the panel strip")
        expect(!state.expireLease(at: 26), "an expired lease must not repeatedly report expiry")

        state.enableSpatial(entries: entries, at: 30)
        state.setTapActive(true)
        _ = state.cycle(reverse: false)
        state.disableSpatial()
        current = state.snapshot()
        expect(!current.open && current.chosen == nil && current.windows.isEmpty && !current.tapActive,
               "desktop mode must remove tap state and reset the switcher")
    }
}
