import Foundation

@main
private enum SpatialCursorFenceTests {
    static func main() {
        let window = CGRect(x: 100, y: 136, width: 640, height: 400)
        let bar = CGRect(x: 100, y: 100, width: 3600, height: 36)
        let menu = CGRect(x: 900, y: 136, width: 150, height: 300)
        let allowed = [window, bar, menu]
        precondition(SpatialCursorFence.confined(CGPoint(x: 200, y: 200), to: allowed) == CGPoint(x: 200, y: 200))
        precondition(SpatialCursorFence.confined(CGPoint(x: 200, y: 120), to: allowed) == CGPoint(x: 200, y: 120))
        precondition(SpatialCursorFence.confined(CGPoint(x: 950, y: 200), to: allowed) == CGPoint(x: 950, y: 200))
        precondition(SpatialCursorFence.confined(CGPoint(x: 750, y: 500), to: allowed) == CGPoint(x: 739.5, y: 500))
        precondition(SpatialCursorFence.confined(CGPoint(x: 10, y: 10), to: []) == CGPoint(x: 10, y: 10))
        print("SpatialCursorFence: 5 window, strip, menu and outside-pointer checks passed")
        gazeClick()
        typing()
    }

    /// Eyes highlight, an action commits: the first click on the highlighted
    /// window only focuses it, and never leaves an app half a click.
    static func gazeClick() {
        var checks = 0
        func expect(_ ok: Bool, _ why: String) { checks += 1; precondition(ok, why) }
        var g = GazeClickGate()
        g.setActive("spatial:1")
        expect(g.event(.down(0), tagged: false, nowMs: 0) == .pass, "no highlight: the click goes through")
        expect(g.event(.up(0), tagged: false, nowMs: 10) == .pass, "and its up")
        g.highlight("spatial:2", on: true, nowMs: 100, ttlMs: 1500)
        expect(g.event(.down(0), tagged: true, nowMs: 110) == .pass, "the helper's own clicks are never consumed")
        expect(g.event(.up(0), tagged: true, nowMs: 120) == .pass, "nor their ups")
        expect(g.highlighted == "spatial:2", "an injected click leaves the highlight alone")
        expect(g.event(.down(0), tagged: false, nowMs: 200) == .focus("spatial:2"), "a click on the highlighted window focuses it")
        expect(g.event(.drag(0), tagged: false, nowMs: 210) == .consume, "its drags are swallowed")
        expect(g.event(.drag(2), tagged: false, nowMs: 215) == .pass, "another button's drag is not")
        expect(g.event(.up(0), tagged: false, nowMs: 220) == .consume, "and its up, so no app sees half a click")
        expect(g.consumed.isEmpty, "nothing left consumed")
        expect(g.event(.down(0), tagged: false, nowMs: 230) == .pass, "one click focuses; the next goes to the window")
        expect(g.event(.up(0), tagged: false, nowMs: 240) == .pass, "with its up")
        // A click on the active window passes through.
        g.setActive("spatial:2")
        g.highlight("spatial:2", on: true, nowMs: 300, ttlMs: 1500)
        expect(g.event(.down(0), tagged: false, nowMs: 310) == .pass, "the active window takes its click")
        // A click that went through stays paired even if a highlight arrives mid-click.
        g.highlight("spatial:3", on: true, nowMs: 320, ttlMs: 1500)
        expect(g.event(.drag(0), tagged: false, nowMs: 330) == .pass, "a passed click's drags pass")
        expect(g.event(.up(0), tagged: false, nowMs: 340) == .pass, "and its up")
        // Right click on the highlighted window: the same.
        expect(g.event(.down(2), tagged: false, nowMs: 350) == .focus("spatial:3"), "a right click focuses too")
        expect(g.event(.down(0), tagged: false, nowMs: 355) == .pass, "a second button while one is consumed passes")
        expect(g.event(.up(0), tagged: false, nowMs: 356) == .pass, "with its up")
        expect(g.event(.up(2), tagged: false, nowMs: 360) == .consume, "the consumed right button's up")
        // A lost up (the tap was off) does not wedge the next click.
        g.highlight("spatial:4", on: true, nowMs: 400, ttlMs: 1500)
        expect(g.event(.down(0), tagged: false, nowMs: 410) == .focus("spatial:4"), "consumed down")
        expect(g.event(.down(0), tagged: false, nowMs: 900) == .pass, "a new down after a lost up is a fresh click")
        expect(g.event(.up(0), tagged: false, nowMs: 910) == .pass, "and goes through whole")
        // Highlights expire, and go out on request.
        g.setActive("spatial:1")
        g.highlight("spatial:5", on: true, nowMs: 1000, ttlMs: 1500)
        expect(g.event(.down(0), tagged: false, nowMs: 2600) == .pass, "a highlight not refreshed expires")
        _ = g.event(.up(0), tagged: false, nowMs: 2610)
        g.highlight("spatial:5", on: true, nowMs: 3000, ttlMs: 1500)
        g.highlight("spatial:6", on: false, nowMs: 3010, ttlMs: 1500)
        expect(g.highlighted == "spatial:5", "another window going out leaves this one")
        g.highlight("spatial:5", on: false, nowMs: 3020, ttlMs: 1500)
        expect(g.event(.down(0), tagged: false, nowMs: 3030) == .pass, "an extinguished highlight takes no click")
        _ = g.event(.up(0), tagged: false, nowMs: 3040)
        g.highlight("spatial:5", on: true, nowMs: 3100, ttlMs: 1500)
        _ = g.event(.down(0), tagged: false, nowMs: 3110)
        g.clear()
        expect(g.consumed.isEmpty && g.highlighted == nil, "stopping clears everything")
        // The tap's event classification.
        expect(SpatialCursorFence.gateEvent(.leftMouseDown, buttonNumber: 0) == .down(0), "left down")
        expect(SpatialCursorFence.gateEvent(.rightMouseDragged, buttonNumber: 1) == .drag(2), "right drag")
        expect(SpatialCursorFence.gateEvent(.otherMouseUp, buttonNumber: 3) == .up(3), "other up keeps its number")
        expect(SpatialCursorFence.gateEvent(.mouseMoved, buttonNumber: 0) == nil, "moves are not clicks")
        expect(SpatialCursorFence.gateEvent(.scrollWheel, buttonNumber: 0) == nil, "nor wheels")
        print("SpatialCursorFence: \(checks) gaze click (consume and focus, pairing, injected, expiry) checks passed")
    }

    static func typing() {
        var checks = 0
        func expect(_ ok: Bool, _ why: String) { checks += 1; precondition(ok, why) }
        expect(SpatialCursorFence.typed(type: .keyDown, userData: 0), "a physical key down is typing")
        expect(SpatialCursorFence.typed(type: .keyUp, userData: 0), "and its up")
        expect(SpatialCursorFence.typed(type: .flagsChanged, userData: 0), "and a modifier")
        expect(!SpatialCursorFence.typed(type: .keyDown, userData: InjectedEvent.tag), "keys the helper posts are not")
        expect(!SpatialCursorFence.typed(type: .leftMouseDown, userData: 0), "clicks are not typing")
        var signal = TypingSignal()
        expect(signal.key(nowMs: 0), "the first key reports")
        expect(!signal.key(nowMs: 100), "then at most every quarter second")
        expect(signal.key(nowMs: 250), "while keys keep coming")
        print("SpatialCursorFence: \(checks) typing signal checks passed")
    }
}
