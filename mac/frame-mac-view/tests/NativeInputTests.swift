import CoreGraphics
import Foundation

// Sources: NativeInput.swift, SpatialStreams.swift, SpatialCursorFence.swift
@main
struct NativeInputTests {
    static var checks = 0
    static func require(_ condition: @autoclosure () -> Bool, _ message: String) {
        checks += 1
        guard condition() else {
            FileHandle.standardError.write(Data("FAIL: \(message)\n".utf8))
            exit(1)
        }
    }

    static func main() {
        parsingIsStrict()
        sequenceOnlyMovesForward()
        frameInfoPlacesContent()
        geometryRevisionsResolve()
        fenceFollowsTargetAndSeesTrackpad()
        trackpadTakesPriority()
        releaseOwnership()
        focusObservation()
        print("NativeInputTests: \(checks) protocol, ordering, geometry, fence, physical takeover and release checks passed")
    }

    static func parse(_ json: String, epoch: String = "e1") -> Result<NativeInputEnvelope, NativeInputRejection> {
        let m = try! JSONSerialization.jsonObject(with: Data(json.utf8)) as! [String: Any]
        return NativeInputParser.parse(m, epoch: epoch)
    }

    static func message(_ json: String) -> NativeInputMessage? {
        if case .success(let envelope) = parse(json) { return envelope.message }
        return nil
    }

    static func failure(_ json: String) -> NativeInputRejection? {
        if case .failure(let reason) = parse(json) { return reason }
        return nil
    }

    static func parsingIsStrict() {
        require(!NativeInputParser.isNative(["t": "m", "e": "move", "x": 0.1, "y": 0.1]), "legacy browser JSON stays legacy")
        require(NativeInputParser.isNative(["t": "m", "epoch": "x"]), "an epoch marks native input")
        let a = NativeInputParser.randomEpoch(), b = NativeInputParser.randomEpoch()
        require(a.count == 32 && a != b, "epochs are random per connection")
        require(message(#"{"t":"focus","u":0.31,"v":0.12,"intent":"pointer","epoch":"e1","q":20}"#) ==
                .focus(u: 0.31, v: 0.12, intent: .pointer, deliberate: false), "pointer focus")
        require(message(#"{"t":"focus","u":0,"v":1,"epoch":"e1","q":2,"deliberate":true}"#) ==
                .focus(u: 0, v: 1, intent: .gaze, deliberate: true), "focus defaults to gaze")
        require(message(#"{"t":"focus","u":0.5,"v":0.5,"intent":"drag","epoch":"e1","q":3}"#) ==
                .focus(u: 0.5, v: 0.5, intent: .drag, deliberate: false), "drag focus")
        require(message(#"{"t":"m","e":"move","x":0.31,"y":0.12,"epoch":"e1","q":21,"geometry":9}"#) ==
                .mouse(.move, button: 0, u: 0.31, v: 0.12, geometry: 9), "move")
        require(message(#"{"t":"m","e":"down","b":2,"x":0.31,"y":0.12,"epoch":"e1","q":22,"geometry":9,"i":7,"tv":1}"#) ==
                .mouse(.down, button: 2, u: 0.31, v: 0.12, geometry: 9), "right down")
        require(message(#"{"t":"wheel","dx":0,"dy":16,"x":0.31,"y":0.12,"epoch":"e1","q":23,"geometry":9}"#) ==
                .wheel(dx: 0, dy: 16, u: 0.31, v: 0.12, geometry: 9), "wheel")
        require(message(#"{"t":"input-barrier","epoch":"e1","q":24}"#) == .barrier, "barrier")
        require(message(#"{"t":"input-keepalive","epoch":"e1","q":25}"#) == .keepalive, "keepalive")
        require(message(#"{"t":"release","epoch":"e1","q":26}"#) == .release, "release")
        require(failure(#"{"t":"release","epoch":"old","q":26}"#) == .epoch, "a stale epoch is refused")
        require(failure(#"{"t":"release","q":26}"#) == .epoch, "a missing epoch is refused")
        require(failure(#"{"t":"release","epoch":"e1"}"#) == .malformed, "a missing sequence is refused")
        for bad in [#""q":0"#, #""q":-1"#, #""q":1.5"#, #""q":"3""#, #""q":true"#, #""q":1e300"#] {
            require(failure(#"{"t":"release","epoch":"e1",\#(bad)}"#) == .malformed, "bad sequence \(bad)")
        }
        for bad in [#""x":-0.01,"y":0.5"#, #""x":0.5,"y":1.01"#, #""x":"0.5","y":0.5"#, #""x":true,"y":0.5"#,
                    #""y":0.5"#, #""x":1e999,"y":0.5"#] {
            let raw = #"{"t":"m","e":"move",\#(bad),"epoch":"e1","q":5,"geometry":1}"#
            if let data = raw.data(using: .utf8), (try? JSONSerialization.jsonObject(with: data)) == nil { continue }
            require(failure(raw) == .malformed, "bad coordinate \(bad)")
        }
        require(failure(#"{"t":"m","e":"move","x":0.5,"y":0.5,"epoch":"e1","q":5}"#) == .malformed, "geometry is required")
        require(failure(#"{"t":"m","e":"tap","x":0.5,"y":0.5,"epoch":"e1","q":5,"geometry":1}"#) == .malformed, "unknown kind")
        require(failure(#"{"t":"m","e":"down","b":3,"x":0.5,"y":0.5,"epoch":"e1","q":5,"geometry":1}"#) == .malformed,
                "button out of range")
        require(failure(#"{"t":"wheel","dx":20000,"dy":0,"x":0.5,"y":0.5,"epoch":"e1","q":5,"geometry":1}"#) == .malformed,
                "wheel delta bounded")
        require(failure(#"{"t":"focus","u":0.5,"v":0.5,"intent":"teleport","epoch":"e1","q":5}"#) == .malformed,
                "unknown intent")
        require(failure(#"{"t":"k","e":"down","code":"KeyA","epoch":"e1","q":5}"#) == .malformed,
                "keys are not native input (the physical keyboard types)")
    }

    static func sequenceOnlyMovesForward() {
        var sequencer = NativeInputSequencer()
        require(sequencer.accept(1) && sequencer.accept(2) && sequencer.accept(9), "increasing with gaps")
        require(!sequencer.accept(9) && !sequencer.accept(3), "replays and reordering are refused")
        require(sequencer.accept(10) && sequencer.last == 10, "continues after a refusal")
    }

    static func frameInfoPlacesContent() {
        let full = InputGeometryPolicy.full
        // A 2400-point window at 2x, capped from 4800 to 1920 pixels.
        let capped = InputGeometryPolicy.contentUV(contentRect: CGRect(x: 0, y: 0, width: 960, height: 540),
                                                   scaleFactor: 2, bufferWidth: 1920, bufferHeight: 1080)
        require(capped == full, "capped capture still fills the picture")
        require(InputGeometryPolicy.globalRect(frame: CGRect(x: 0, y: 30, width: 2400, height: 1350),
                                               contentRect: CGRect(x: 0, y: 0, width: 960, height: 540),
                                               contentScale: 0.4, screenRect: nil) ==
                CGRect(x: 0, y: 30, width: 2400, height: 1350), "content scale recovers the window's logical size")
        // Letterboxed content: a child popover makes the content wider than tall.
        let boxed = InputGeometryPolicy.contentUV(contentRect: CGRect(x: 0, y: 135, width: 960, height: 270),
                                                  scaleFactor: 2, bufferWidth: 1920, bufferHeight: 1080)
        require(abs(boxed.minY - 0.25) < 1e-9 && abs(boxed.height - 0.5) < 1e-9 && boxed.width == 1, "padding stays outside")
        let pixels = InputGeometryPolicy.contentUV(contentRect: CGRect(x: 0, y: 0, width: 1920, height: 1080),
                                                   scaleFactor: 2, bufferWidth: 1920, bufferHeight: 1080)
        require(pixels == full, "a pixel-space content rect is accepted too")
        require(InputGeometryPolicy.contentUV(contentRect: nil, scaleFactor: 2, bufferWidth: 1920, bufferHeight: 1080) == full,
                "no frame info means the whole picture")
        require(InputGeometryPolicy.contentUV(contentRect: CGRect(x: 0, y: 0, width: 9000, height: 9000), scaleFactor: 2,
                                              bufferWidth: 1920, bufferHeight: 1080) == full, "implausible info is ignored")
        require(InputGeometryPolicy.contentUV(contentRect: CGRect(x: 0, y: 0, width: 1, height: 1), scaleFactor: 2,
                                              bufferWidth: 1920, bufferHeight: 1080) == full, "a sliver is ignored")
        let frame = CGRect(x: 100, y: 136, width: 800, height: 600)
        let screen = CGRect(x: 100, y: 136, width: 1000, height: 600)
        require(InputGeometryPolicy.globalRect(frame: frame, contentRect: CGRect(x: 0, y: 0, width: 1000, height: 600),
                                               contentScale: 1, screenRect: screen) == screen,
                "child content past the frame uses the matching screen rect")
        require(InputGeometryPolicy.globalRect(frame: frame, contentRect: CGRect(x: 0, y: 0, width: 1000, height: 600),
                                               contentScale: 1, screenRect: CGRect(x: 0, y: 0, width: 50, height: 50)) == frame,
                "a mismatched screen rect falls back to the frame")
        let both = InputGeometryPolicy.geometry(frame: frame, info: nil)
        require(both.rect == frame && both.contentUV == full, "no info: frame and whole picture")
    }

    static func geometryRevisionsResolve() {
        var book = InputGeometryBook()
        let frame = CGRect(x: 100, y: 136, width: 800, height: 600)
        let first = book.observe(frame: frame, rect: frame, contentUV: InputGeometryPolicy.full, visible: true,
                                 identity: "spatial:7", pictureSeq: 44)!
        require(first.revision == 1 && first.pictureSeq == 44, "first revision")
        require(book.observe(frame: frame, rect: frame, contentUV: InputGeometryPolicy.full, visible: true,
                             identity: "spatial:7", pictureSeq: 45) == nil, "unchanged geometry is not republished")
        let json = first.json
        require(json["t"] as? String == "input-geometry" && (json["rect"] as? [Double]) == [100, 136, 800, 600] &&
                json["pictureSeq"] as? UInt32 == 44 && json["visible"] as? Bool == true, "wire form")
        func point(_ r: GeometryResolution) -> CGPoint? { if case .point(let p) = r { return p }; return nil }
        let mid = book.resolve(u: 0.5, v: 0.5, revision: 1, currentFrame: frame, currentIdentity: "spatial:7", clampToContent: false)
        require(point(mid) == CGPoint(x: 500, y: 436), "centre maps to the window centre")
        let edge = point(book.resolve(u: 1, v: 1, revision: 1, currentFrame: frame, currentIdentity: "spatial:7",
                                      clampToContent: false))!
        require(edge.x < frame.maxX && edge.y < frame.maxY, "the far edge stays inside the window")
        let moved = frame.offsetBy(dx: 40, dy: 0)
        require(point(book.resolve(u: 0.5, v: 0.5, revision: 1, currentFrame: moved, currentIdentity: "spatial:7",
                                   clampToContent: false)) == CGPoint(x: 540, y: 436), "a moved window keeps its revision")
        let resized = CGRect(x: 100, y: 136, width: 900, height: 600)
        require(book.resolve(u: 0.5, v: 0.5, revision: 1, currentFrame: resized, currentIdentity: "spatial:7",
                             clampToContent: false) == .stale, "a resized window refuses the old revision")
        require(book.resolve(u: 0.5, v: 0.5, revision: 99, currentFrame: frame, currentIdentity: "spatial:7",
                             clampToContent: true) == .stale, "unknown revision")
        // Padding: content in the middle half of the picture.
        let boxed = CGRect(x: 0, y: 0.25, width: 1, height: 0.5)
        let second = book.observe(frame: frame, rect: frame, contentUV: boxed, visible: true, identity: "spatial:7",
                                  pictureSeq: 50)!
        require(second.revision == 2, "a crop change is a new revision")
        require(book.resolve(u: 0.5, v: 0.1, revision: 2, currentFrame: frame, currentIdentity: "spatial:7",
                             clampToContent: false) == .padding, "a press on padding is refused")
        require(point(book.resolve(u: 0.5, v: 0.1, revision: 2, currentFrame: frame, currentIdentity: "spatial:7",
                                   clampToContent: true))?.y == 136, "moves clamp into the content")
        require(point(book.resolve(u: 0.5, v: 0.5, revision: 1, currentFrame: frame, currentIdentity: "spatial:7",
                                   clampToContent: false)) != nil, "an older retained revision still maps")
        // Menu slot: hidden, then retargeted to a reused ID.
        var menu = InputGeometryBook()
        let rect = CGRect(x: 120, y: 36, width: 200, height: 300)
        _ = menu.observe(frame: rect, rect: rect, contentUV: InputGeometryPolicy.full, visible: true, identity: "menu:0:3:55",
                         pictureSeq: 3)
        require(point(menu.resolve(u: 0.5, v: 0.5, revision: 1, currentFrame: rect, currentIdentity: "menu:0:3:55",
                                   clampToContent: false)) == CGPoint(x: 220, y: 186), "visible slot maps")
        _ = menu.observe(frame: .zero, rect: .zero, contentUV: InputGeometryPolicy.full, visible: false,
                         identity: "menu:0:4:55", pictureSeq: 0)
        require(menu.resolve(u: 0.5, v: 0.5, revision: 1, currentFrame: rect, currentIdentity: "menu:0:4:55",
                             clampToContent: false) == .hidden, "a hidden slot refuses clicks at once")
        _ = menu.observe(frame: rect, rect: rect, contentUV: InputGeometryPolicy.full, visible: true, identity: "menu:0:5:55",
                         pictureSeq: 9)
        require(menu.resolve(u: 0.5, v: 0.5, revision: 1, currentFrame: rect, currentIdentity: "menu:0:5:55",
                             clampToContent: false) == .stale, "a retarget to the same ID refuses the old picture")
        require(point(menu.resolve(u: 0.5, v: 0.5, revision: 3, currentFrame: rect, currentIdentity: "menu:0:5:55",
                                   clampToContent: false)) != nil, "the fresh picture maps")
        var long = InputGeometryBook()
        for i in 0..<20 {
            _ = long.observe(frame: frame, rect: frame.insetBy(dx: CGFloat(i), dy: 0), contentUV: InputGeometryPolicy.full,
                             visible: true, identity: "w", pictureSeq: UInt32(i))
        }
        require(long.revision(1) == nil && long.revision(20) != nil && long.revision(13) != nil, "history is bounded")
    }

    static func fenceFollowsTargetAndSeesTrackpad() {
        let window = CGRect(x: 100, y: 136, width: 640, height: 400)
        let child = CGRect(x: 600, y: 300, width: 300, height: 200)
        let bar = CGRect(x: 100, y: 100, width: 3600, height: 36)
        let menu = CGRect(x: 900, y: 136, width: 150, height: 300)
        let rects = SpatialFocusPolicy.fenceRects(window: window, content: child, menuBar: bar, surfaces: [menu, .zero])
        require(rects.count == 3 && rects[0] == window.union(child), "fence covers child content past the parent")
        require(SpatialCursorFence.confined(CGPoint(x: 850, y: 450), to: rects) == CGPoint(x: 850, y: 450),
                "a child popover past the frame is reachable")
        require(SpatialCursorFence.confined(CGPoint(x: 1000, y: 120), to: rects) == CGPoint(x: 1000, y: 120),
                "the menu bar stays reachable")
        require(SpatialFocusPolicy.fenceRects(window: window, content: CGRect(x: 3000, y: 3000, width: 10, height: 10),
                                              menuBar: bar, surfaces: [])[0] == window, "unrelated content is not admitted")
        require(SpatialCursorFence.external(type: .mouseMoved, userData: InjectedEvent.tag) == nil, "our moves are ours")
        require(SpatialCursorFence.external(type: .leftMouseDown, userData: InjectedEvent.tag) == nil, "our clicks are ours")
        require(SpatialCursorFence.external(type: .mouseMoved, userData: 0) == .motion, "trackpad motion")
        require(SpatialCursorFence.external(type: .scrollWheel, userData: 0) == .motion, "trackpad scroll")
        require(SpatialCursorFence.external(type: .leftMouseDragged, userData: 12) == .motion, "other software counts as local")
        require(SpatialCursorFence.external(type: .rightMouseDown, userData: 0) == .button(2, down: true), "physical right down")
        require(SpatialCursorFence.external(type: .leftMouseUp, userData: 0) == .button(0, down: false), "physical left up")
        require(SpatialCursorFence.external(type: .keyDown, userData: 0) == nil, "keys are not pointer activity")
    }

    static func trackpadTakesPriority() {
        var c = PointerContention()
        require(c.remoteMove(to: .zero, holding: false), "the headset drives an idle pointer")
        require(c.physicalMotion(), "trackpad motion takes over")
        require(!c.physicalMotion(), "and reports it once")
        require(!c.remoteMove(to: CGPoint(x: 10, y: 10), holding: false), "a resting laser cannot take it back")
        require(!c.remoteMove(to: CGPoint(x: 20, y: 10), holding: false), "nor small jitter")
        require(c.remoteMove(to: CGPoint(x: 40, y: 10), holding: false) && !c.physicalOwns, "clear laser motion does")
        _ = c.physicalMotion()
        require(c.remoteMove(to: CGPoint(x: 1, y: 1), holding: true), "a held remote drag carries on")
        require(c.remoteEdge() && !c.physicalOwns, "a press is deliberate")
        require(c.physicalButton(0, down: true) && c.physicalOwns, "a physical press takes over")
        require(!c.remoteEdge(), "no remote press while the trackpad holds a button")
        require(!c.mayRelease(0) && c.mayRelease(2), "cleanup never releases a physically held button")
        require(!c.physicalButton(0, down: false) && c.remoteEdge(), "after the physical release the headset may press")
        c.reset()
        require(!c.physicalOwns && c.physicalButtons.isEmpty, "reset")
    }

    static func releaseOwnership() {
        var buttons = SharedPointerButtons()
        require(buttons.down(0, owner: 1) && buttons.holds(owner: 1) && !buttons.holds(owner: 2), "owner recorded")
        require(!buttons.down(0, owner: 2), "a second press of the held button is ignored")
        require(buttons.up(0) && !buttons.holds(owner: 1), "an up from the drop target releases the drag")
        _ = buttons.down(2, owner: 3)
        require(buttons.releaseAll(owner: 1).isEmpty && buttons.releaseAll(owner: 3) == [2], "disconnect releases its own")
        var expiry = HeldInputExpiry()
        require(!expiry.expired(nowMs: 10_000, holding: true), "never-active connection does not expire")
        expiry.touch(1000)
        require(!expiry.expired(nowMs: 2400, holding: true), "within the interval")
        require(expiry.expired(nowMs: 2600, holding: true), "a quiet connection holding a button expires")
        require(!expiry.expired(nowMs: 9000, holding: false), "nothing held, nothing to expire")
        let ack = NativeInputAck.json(q: 24, ok: false, reason: .physical, extra: ["focusedSrc": "spatial:1"])
        require(ack["t"] as? String == "input-ack" && ack["q"] as? UInt64 == 24 && ack["ok"] as? Bool == false &&
                ack["reason"] as? String == "physical" && ack["focusedSrc"] as? String == "spatial:1", "ack form")
    }

    static func focusObservation() {
        let bounds = CGRect(x: 0, y: 36, width: 800, height: 600)
        typealias Front = SpatialFocusPolicy.Front
        require(SpatialFocusPolicy.observe(target: 7, pid: 70, bounds: bounds, frontmostPID: 70,
                                           front: Front(id: 7, pid: 70, bounds: bounds)) == .confirmed, "raised")
        require(SpatialFocusPolicy.observe(target: 7, pid: 70, bounds: bounds, frontmostPID: 70,
                                           front: Front(id: 8, pid: 70, bounds: bounds.insetBy(dx: 100, dy: 100))) == .modal,
                "a sheet over it is kept")
        require(SpatialFocusPolicy.observe(target: 7, pid: 70, bounds: bounds, frontmostPID: 70,
                                           front: Front(id: 9, pid: 70, bounds: CGRect(x: 2000, y: 0, width: 5, height: 5))) == .pending,
                "another window of the app is not focus")
        require(SpatialFocusPolicy.observe(target: 7, pid: 70, bounds: bounds, frontmostPID: 71,
                                           front: Front(id: 7, pid: 70, bounds: bounds)) == .pending, "app not active yet")
        require(SpatialFocusPolicy.observe(target: 7, pid: 70, bounds: bounds, frontmostPID: 70, front: nil) == .pending,
                "nothing in front")
    }
}
