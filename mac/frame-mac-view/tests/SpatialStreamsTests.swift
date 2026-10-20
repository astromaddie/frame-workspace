import Foundation

@main
struct SpatialStreamsTests {
    static func require(_ condition: @autoclosure () -> Bool, _ message: String) {
        guard condition() else {
            FileHandle.standardError.write(Data("FAIL: \(message)\n".utf8))
            exit(1)
        }
    }

    static func main() {
        normalizedCoordinatesAreStrict()
        cursorMemoryRestoresPerPanelWithoutHoverClobbering()
        sharedButtonsCarryDragsAcrossSessionsAndCleanUpOwners()
        liveGateOrdersFirstPictureStillAndResumeEpochs()
        streamObservationsKeepDimensionsAndRealChangeTimes()
        print("SpatialStreamsTests: all checks passed")
    }

    private static func normalizedCoordinatesAreStrict() {
        require(NormalizedPoint(u: 0, v: 1) != nil, "normalized boundary coordinates are accepted")
        require(NormalizedPoint(u: -0.01, v: 0.5) == nil, "negative coordinates are rejected")
        require(NormalizedPoint(u: 0.5, v: 1.01) == nil, "coordinates above one are rejected")
        require(NormalizedPoint(u: .nan, v: 0.5) == nil, "NaN is rejected")
        require(NormalizedPoint(u: 0.5, v: .infinity) == nil, "infinity is rejected")
        let midpoint = NormalizedPoint(u: 0.5, v: 0.5)!.point(x: 10, y: 20, width: 100, height: 50)
        require(midpoint == SpatialPoint(x: 60, y: 45), "interior normalized positions map without drift")
        let roundTrip = NormalizedPoint(u: (midpoint.x - 10) / 100, v: (midpoint.y - 20) / 50)!
            .point(x: 10, y: 20, width: 100, height: 50)
        require(roundTrip == midpoint, "normalizing and restoring a saved cursor position is stable")
        let corner = NormalizedPoint(u: 1, v: 1)!.point(x: 10, y: 20, width: 100, height: 50)
        require(corner.x < 110 && corner.y < 70, "boundary gaze restores inside the window bounds")
    }

    private static func cursorMemoryRestoresPerPanelWithoutHoverClobbering() {
        let left = NormalizedPoint(u: 0.2, v: 0.3)!
        let rightFirst = NormalizedPoint(u: 0.8, v: 0.7)!
        let leavingLeft = NormalizedPoint(u: 0.4, v: 0.6)!
        let memory = FocusCursorMemory()

        let first = memory.focus("window:10", firstVisit: left)
        require(first.point == left && first.shouldMoveCursor, "first focus uses the supplied gaze point")
        memory.rememberIfFocused(NormalizedPoint(u: 0.1, v: 0.1)!, for: "window:11")
        let second = memory.focus("separate:11", firstVisit: rightFirst, leavingPoint: leavingLeft)
        require(second.point == rightFirst && second.shouldMoveCursor, "first visit to the other panel uses gaze")
        require(memory.focusedSource == "separate:11", "focus source is tracked")

        let returnToLeft = memory.focus("window:10", firstVisit: NormalizedPoint(u: 0.9, v: 0.9)!)
        require(returnToLeft.point == leavingLeft, "leaving cursor is saved and restored")
        let repeated = memory.focus("window:10", firstVisit: NormalizedPoint(u: 0.7, v: 0.7)!)
        require(!repeated.shouldMoveCursor && repeated.point == leavingLeft, "same-source focus does not teleport")
        memory.remove("window:10")
        require(memory.focusedSource == nil, "removing the focused source clears its identity")

        let legacy = FocusCursorMemory()
        legacy.noteFocus("window:12")
        let gaze = NormalizedPoint(u: 0.65, v: 0.25)!
        let initialized = legacy.focus("window:12", firstVisit: gaze)
        require(initialized.shouldMoveCursor && initialized.point == gaze,
                "first gaze initializes a source already selected by legacy focus")
        let repeatedLegacy = legacy.focus("window:12", firstVisit: left)
        require(!repeatedLegacy.shouldMoveCursor && repeatedLegacy.point == gaze,
                "initialized same-source gaze does not teleport on repeat")
    }

    private static func sharedButtonsCarryDragsAcrossSessionsAndCleanUpOwners() {
        var buttons = SharedPointerButtons()
        require(buttons.down(0, owner: 1), "first session posts a shared button down")
        require(!buttons.down(0, owner: 2), "duplicate down does not post twice")
        require(buttons.held.contains(0), "another session sees the shared drag held")
        require(SharedPointerButtons.dragButton(in: buttons.held) == 0,
                "mouse moves from another panel remain left-drag events")
        require(buttons.up(0), "a different session can release the global button")
        require(!buttons.held.contains(0), "cross-session up clears the global drag")
        require(SharedPointerButtons.dragButton(in: buttons.held) == nil, "moves return to ordinary pointer motion after up")

        _ = buttons.down(1, owner: 1)
        _ = buttons.down(2, owner: 2)
        require(Set(buttons.releaseAll(owner: 1)) == [1], "disconnect releases only buttons first held by that owner")
        require(buttons.held == [2], "other sessions' shared buttons remain held")
        require(Set(buttons.releaseAll()) == [2], "global mode shutdown releases all remaining buttons")
    }

    private static func liveGateOrdersFirstPictureStillAndResumeEpochs() {
        var gate = StreamLiveGate()
        guard case .waitForFirstPicture = gate.request(false) else {
            return require(false, "initial still waits for the first picture")
        }
        let initialEpoch = gate.epoch
        require(gate.accepts(frameEpoch: initialEpoch), "initial picture remains allowed while still is pending")
        require(gate.didQueuePicture(frameEpoch: initialEpoch, key: true), "first queued picture completes pending still")
        let stillEpoch = gate.epoch
        require(!gate.live && !gate.accepts(frameEpoch: initialEpoch), "still blocks stale frame callbacks")
        require(!gate.didQueuePicture(frameEpoch: initialEpoch, key: true), "a late old picture cannot cross the still boundary")

        guard case .resume(let request) = gate.request(true) else {
            return require(false, "resuming from still requests capture restoration")
        }
        require(!gate.live && !gate.accepts(frameEpoch: stillEpoch), "resume waits for restored capture configuration")
        require(gate.activateLive(request: request), "the current resume request activates live")
        require(gate.live && gate.epoch != stillEpoch, "resumed stream receives a fresh frame epoch")
        require(!gate.canQueuePicture(frameEpoch: gate.epoch, key: false), "non-key picture is rejected after resume")
        require(gate.awaitingKeyFrame, "non-key rejection keeps waiting for an IDR")
        require(gate.canQueuePicture(frameEpoch: gate.epoch, key: true), "the resumed keyframe is accepted")
        require(!gate.didQueuePicture(frameEpoch: gate.epoch, key: true), "resumed keyframe does not trigger still")
        require(!gate.awaitingKeyFrame, "accepted keyframe satisfies the resume gate")
        require(!gate.activateLive(request: request - 1), "superseded resume completion is ignored")
        guard case .unchanged = gate.request(true) else {
            return require(false, "repeated live policy is idempotent")
        }
    }

    private static func streamObservationsKeepDimensionsAndRealChangeTimes() {
        let state = SpatialStreamState()
        state.observe("window:10", width: 640, height: 360, changed: true, unixSeconds: 100)
        state.observe("display:1", width: 640, height: 360, changed: true, unixSeconds: 110)
        state.observe("window:10", width: 1280, height: 720, changed: false, unixSeconds: 200)
        let observed = state.observation(for: "window:10")
        require(observed.width == 1280 && observed.height == 720, "latest configured dimensions are retained")
        require(observed.lastChanged == 100, "dimension changes alone do not change the content timestamp")
        require(state.observation(for: "display:1").lastChanged == 110, "non-window captures can report content observations")
        state.remove("window:10")
        require(state.observation(for: "window:10").width == 0, "closed-source observations are pruned")
    }
}
