// Native headset input (FW-15): the helper side of pointer, click, drag and
// scroll from the Frame's own host. These are the pure rules; main.swift and
// NativeInputSession.swift post the events they decide on. Kept free of
// AppKit and ScreenCaptureKit so the rules can be tested on their own.
import CoreGraphics
import Foundation

enum NativeInputLimits {
    static let version = 1
    /// A connection holding a button that sends nothing for this long lets go.
    static let expiryMs = 1500
    static let retainedGeometry = 8
    static let maxWheel = 10_000.0
}

/// Every helper-posted CGEvent carries this in `eventSourceUserData`, so the
/// cursor fence's tap can tell the physical trackpad from our own input.
enum InjectedEvent {
    static let tag: Int64 = 0x4657_4E49  // "FWNI"
}

enum NativeFocusIntent: String {
    /// Gaze dwell: raise, then restore the window's remembered cursor.
    case gaze
    /// Controller hover: raise, then place the cursor at the laser point.
    case pointer
    /// A held drag reached this window: raise it and move the fence, with no
    /// cursor memory and no synthetic button edges.
    case drag
}

enum NativeMouseKind: String {
    case move, down, up
}

enum NativeInputMessage: Equatable {
    /// `deliberate`: a press, a wheel or clear laser motion led to it, so it
    /// may take the pointer back from the trackpad.
    case focus(u: Double, v: Double, intent: NativeFocusIntent, deliberate: Bool)
    case mouse(NativeMouseKind, button: Int, u: Double, v: Double, geometry: UInt64)
    case wheel(dx: Double, dy: Double, u: Double, v: Double, geometry: UInt64)
    case barrier
    case keepalive
    case release
}

struct NativeInputEnvelope: Equatable {
    let q: UInt64
    let message: NativeInputMessage
}

enum NativeInputRejection: String, Error {
    case epoch, sequence, malformed, geometry, padding, hidden, physical, source, permission, focus
}

enum NativeInputParser {
    /// Native messages always name their connection epoch; legacy browser
    /// JSON never does and keeps its existing handling.
    static func isNative(_ m: [String: Any]) -> Bool { m["epoch"] != nil }

    static func randomEpoch() -> String {
        var generator = SystemRandomNumberGenerator()
        return (0..<4).map { _ in String(format: "%08x", UInt32.random(in: .min ... .max, using: &generator)) }.joined()
    }

    private static func integer(_ value: Any?) -> UInt64? {
        guard let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() else { return nil }
        let d = n.doubleValue
        guard d.isFinite, d >= 1, d <= 9_007_199_254_740_991, d.rounded() == d else { return nil }
        return UInt64(d)
    }

    private static func number(_ value: Any?) -> Double? {
        guard let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() else { return nil }
        return n.doubleValue.isFinite ? n.doubleValue : nil
    }

    private static func unit(_ value: Any?) -> Double? {
        guard let d = number(value), (0...1).contains(d) else { return nil }
        return d
    }

    static func parse(_ m: [String: Any], epoch: String) -> Result<NativeInputEnvelope, NativeInputRejection> {
        guard let given = m["epoch"] as? String, !epoch.isEmpty, given == epoch else { return .failure(.epoch) }
        guard let t = m["t"] as? String, let q = integer(m["q"]) else { return .failure(.malformed) }
        switch t {
        case "focus":
            guard let u = unit(m["u"]), let v = unit(m["v"]),
                  let intent = NativeFocusIntent(rawValue: m["intent"] as? String ?? "gaze") else { return .failure(.malformed) }
            let deliberate = m["deliberate"] as? Bool ?? false
            return .success(.init(q: q, message: .focus(u: u, v: v, intent: intent, deliberate: deliberate)))
        case "m":
            guard let kind = NativeMouseKind(rawValue: m["e"] as? String ?? ""),
                  let u = unit(m["x"]), let v = unit(m["y"]), let geometry = integer(m["geometry"]) else {
                return .failure(.malformed)
            }
            let button: Int
            if let raw = m["b"] {
                guard let b = number(raw), [0.0, 1.0, 2.0].contains(b) else { return .failure(.malformed) }
                button = Int(b)
            } else { button = 0 }
            return .success(.init(q: q, message: .mouse(kind, button: button, u: u, v: v, geometry: geometry)))
        case "wheel":
            guard let dx = number(m["dx"] ?? 0), let dy = number(m["dy"] ?? 0),
                  abs(dx) <= NativeInputLimits.maxWheel, abs(dy) <= NativeInputLimits.maxWheel,
                  let u = unit(m["x"]), let v = unit(m["y"]), let geometry = integer(m["geometry"]) else {
                return .failure(.malformed)
            }
            return .success(.init(q: q, message: .wheel(dx: dx, dy: dy, u: u, v: v, geometry: geometry)))
        case "input-barrier": return .success(.init(q: q, message: .barrier))
        case "input-keepalive": return .success(.init(q: q, message: .keepalive))
        case "release": return .success(.init(q: q, message: .release))
        default: return .failure(.malformed)
        }
    }
}

/// Per-connection order: `q` only ever increases (gaps are fine, the viewer
/// coalesces moves before numbering them).
struct NativeInputSequencer {
    private(set) var last: UInt64 = 0
    mutating func accept(_ q: UInt64) -> Bool {
        guard q > last else { return false }
        last = q
        return true
    }
}

/// What part of a displayed picture is which part of the Mac. `rect` is the
/// global-point rectangle shown by `contentUV` of the picture; capture padding
/// lies outside it. A new revision follows any identity, frame or crop change.
struct InputGeometry: Equatable {
    var revision: UInt64 = 0
    var pictureSeq: UInt32 = 0
    var rect = CGRect.zero
    /// The window (or surface) frame when published; a later move of the
    /// same size keeps the revision usable.
    var frame = CGRect.zero
    var contentUV = CGRect(x: 0, y: 0, width: 1, height: 1)
    var visible = false
    /// Changes when an auxiliary slot is retargeted, even to a reused ID.
    var identity = ""

    var json: [String: Any] {
        ["t": "input-geometry", "v": NativeInputLimits.version, "revision": revision, "pictureSeq": pictureSeq,
         "rect": [Double(rect.minX), Double(rect.minY), Double(rect.width), Double(rect.height)],
         "contentUV": [Double(contentUV.minX), Double(contentUV.minY), Double(contentUV.width), Double(contentUV.height)],
         "visible": visible]
    }

    func sameMapping(_ other: InputGeometry) -> Bool {
        rect == other.rect && frame == other.frame && contentUV == other.contentUV && visible == other.visible &&
            identity == other.identity
    }
}

/// One delivered frame's SCStreamFrameInfo placement values.
struct CaptureFrameGeometry: Equatable {
    var contentRect: CGRect?
    var scaleFactor: Double?
    var contentScale: Double?
    var screenRect: CGRect?
    var bufferWidth: Int
    var bufferHeight: Int
}

enum InputGeometryPolicy {
    static let full = CGRect(x: 0, y: 0, width: 1, height: 1)

    /// Where the content sits in an output picture, from SCStreamFrameInfo:
    /// `contentRect` is in surface points, so `scaleFactor` makes it pixels.
    /// A value that does not fit the buffer is treated as already in pixels,
    /// and anything else as the whole picture.
    static func contentUV(contentRect: CGRect?, scaleFactor: Double?, bufferWidth: Int, bufferHeight: Int) -> CGRect {
        guard let contentRect, bufferWidth > 0, bufferHeight > 0, contentRect.width > 0, contentRect.height > 0,
              contentRect.minX.isFinite, contentRect.minY.isFinite, contentRect.width.isFinite, contentRect.height.isFinite
        else { return full }
        func fit(_ scale: Double) -> CGRect? {
            let uv = CGRect(x: contentRect.minX * scale / Double(bufferWidth), y: contentRect.minY * scale / Double(bufferHeight),
                            width: contentRect.width * scale / Double(bufferWidth),
                            height: contentRect.height * scale / Double(bufferHeight))
            let slack = 0.02
            guard uv.minX >= -slack, uv.minY >= -slack, uv.maxX <= 1 + slack, uv.maxY <= 1 + slack,
                  uv.width * uv.height >= 0.05 else { return nil }
            return uv.intersection(full)
        }
        if let s = scaleFactor, s.isFinite, s >= 1, s <= 4, let uv = fit(s) { return uv }
        return fit(1) ?? full
    }

    /// The global rectangle the content shows. The window frame, unless the
    /// content is visibly larger (child windows past the parent) and the frame
    /// info's screen rect matches that size.
    static func geometry(frame: CGRect, info: CaptureFrameGeometry?) -> (rect: CGRect, contentUV: CGRect) {
        guard let info else { return (frame, full) }
        return (globalRect(frame: frame, contentRect: info.contentRect, contentScale: info.contentScale,
                           screenRect: info.screenRect),
                contentUV(contentRect: info.contentRect, scaleFactor: info.scaleFactor,
                          bufferWidth: info.bufferWidth, bufferHeight: info.bufferHeight))
    }

    static func globalRect(frame: CGRect, contentRect: CGRect?, contentScale: Double?, screenRect: CGRect?) -> CGRect {
        guard let contentRect, let contentScale, contentScale > 0, contentScale.isFinite else { return frame }
        let points = CGSize(width: contentRect.width / contentScale, height: contentRect.height / contentScale)
        if abs(points.width - frame.width) <= 2 && abs(points.height - frame.height) <= 2 { return frame }
        if let screenRect, abs(screenRect.width - points.width) <= 2, abs(screenRect.height - points.height) <= 2,
           screenRect.intersects(frame) { return screenRect }
        return frame
    }
}

enum GeometryResolution: Equatable {
    case point(CGPoint)
    case stale, padding, hidden
}

/// The revisions a stream has published, so the viewer's echoed revision can
/// be checked against what it was showing.
struct InputGeometryBook {
    private(set) var current: InputGeometry?
    private var history: [InputGeometry] = []
    private var nextRevision: UInt64 = 1

    /// A new revision to publish, or nil when nothing about the mapping changed.
    mutating func observe(frame: CGRect, rect: CGRect, contentUV: CGRect, visible: Bool, identity: String,
                          pictureSeq: UInt32) -> InputGeometry? {
        var next = InputGeometry(revision: 0, pictureSeq: pictureSeq, rect: rect, frame: frame, contentUV: contentUV,
                                 visible: visible, identity: identity)
        if let current, current.sameMapping(next) { return nil }
        next.revision = nextRevision
        nextRevision &+= 1
        current = next
        history.append(next)
        if history.count > NativeInputLimits.retainedGeometry { history.removeFirst(history.count - NativeInputLimits.retainedGeometry) }
        return next
    }

    func revision(_ revision: UInt64) -> InputGeometry? { history.last { $0.revision == revision } }

    /// Maps picture `(u, v)` under `revision` onto the stream's current frame.
    /// A window that only moved keeps its revision; a resize, a retarget or a
    /// hidden surface does not. Moves and ups clamp into the content; presses
    /// and wheels on capture padding are refused.
    func resolve(u: Double, v: Double, revision: UInt64, currentFrame: CGRect, currentIdentity: String,
                 clampToContent: Bool) -> GeometryResolution {
        guard let g = self.revision(revision) else { return .stale }
        guard g.visible, current?.visible == true else { return .hidden }
        guard g.identity == currentIdentity, abs(g.frame.width - currentFrame.width) <= 1,
              abs(g.frame.height - currentFrame.height) <= 1, g.rect.width > 0, g.rect.height > 0 else { return .stale }
        let currentRect = g.rect.offsetBy(dx: currentFrame.minX - g.frame.minX, dy: currentFrame.minY - g.frame.minY)
        let cu = g.contentUV
        guard cu.width > 0, cu.height > 0 else { return .stale }
        var nu = (u - Double(cu.minX)) / Double(cu.width), nv = (v - Double(cu.minY)) / Double(cu.height)
        let epsilon = 1e-6
        if nu < -epsilon || nv < -epsilon || nu > 1 + epsilon || nv > 1 + epsilon {
            guard clampToContent else { return .padding }
        }
        nu = min(max(nu, 0), 1); nv = min(max(nv, 0), 1)
        guard let point = NormalizedPoint(u: nu, v: nv) else { return .stale }
        let mapped = point.point(x: Double(currentRect.minX), y: Double(currentRect.minY),
                                 width: Double(currentRect.width), height: Double(currentRect.height))
        return .point(CGPoint(x: mapped.x, y: mapped.y))
    }
}

/// The physical trackpad wins. Its motion ends remote hover at once; the
/// headset gets the pointer back only through a deliberate act: a press, a
/// wheel, or moving its laser a clear distance, never a resting ray.
struct PointerContention {
    static let reacquireDistance = 24.0
    private(set) var physicalOwns = false
    private(set) var physicalButtons = Set<Int>()
    private var anchor: CGPoint?

    /// Returns true when this takes the pointer from the headset.
    mutating func physicalMotion() -> Bool {
        anchor = nil
        guard !physicalOwns else { return false }
        physicalOwns = true
        return true
    }

    mutating func physicalButton(_ button: Int, down: Bool) -> Bool {
        if down { physicalButtons.insert(button) } else { physicalButtons.remove(button) }
        return down ? physicalMotion() : false
    }

    /// Whether a remote hover move may be posted. A drag the headset already
    /// holds carries on.
    mutating func remoteMove(to point: CGPoint, holding: Bool) -> Bool {
        guard physicalOwns else { return true }
        if holding { return true }
        guard let start = anchor else { anchor = point; return false }
        guard hypot(point.x - start.x, point.y - start.y) >= Self.reacquireDistance else { return false }
        physicalOwns = false
        anchor = nil
        return true
    }

    /// A press or wheel is deliberate; it is refused only while the trackpad
    /// holds a button.
    mutating func remoteEdge() -> Bool {
        guard physicalButtons.isEmpty else { return false }
        physicalOwns = false
        anchor = nil
        return true
    }

    /// Remote cleanup never lets go of a button the user is holding.
    func mayRelease(_ button: Int) -> Bool { !physicalButtons.contains(button) }

    mutating func reset() { physicalOwns = false; physicalButtons.removeAll(); anchor = nil }
}

/// A connection holding a button must keep talking; a stalled one lets go.
struct HeldInputExpiry {
    var expiryMs = Double(NativeInputLimits.expiryMs)
    private(set) var lastActivity: Double = 0
    mutating func touch(_ nowMs: Double) { lastActivity = nowMs }
    func expired(nowMs: Double, holding: Bool) -> Bool { holding && lastActivity > 0 && nowMs - lastActivity > expiryMs }
}

enum FocusObservation: String {
    case confirmed, modal, pending
}

enum SpatialFocusPolicy {
    struct Front: Equatable {
        var id: CGWindowID
        var pid: pid_t
        var bounds: CGRect
    }

    /// What macOS reports after a raise: the window itself in front, or a
    /// sheet/modal child of the same app over it (kept, not forced aside).
    static func observe(target: CGWindowID, pid: pid_t, bounds: CGRect,
                        frontmostPID: pid_t?, front: Front?) -> FocusObservation {
        guard frontmostPID == pid, let front else { return .pending }
        if front.id == target { return .confirmed }
        if front.pid == pid && front.bounds.intersects(bounds) { return .modal }
        return .pending
    }

    /// The fence follows the input target: its window (with any child
    /// content shown past its frame), the menu bar and visible menu surfaces.
    static func fenceRects(window: CGRect, content: CGRect?, menuBar: CGRect, surfaces: [CGRect]) -> [CGRect] {
        var target = window
        if let content, !content.isEmpty, !window.isEmpty, content.intersects(window) { target = window.union(content) }
        return ([target, menuBar] + surfaces).filter { !$0.isEmpty }
    }
}

struct NativeInputAck {
    static func json(q: UInt64, ok: Bool, reason: NativeInputRejection? = nil, extra: [String: Any] = [:]) -> [String: Any] {
        var value: [String: Any] = ["t": "input-ack", "q": q, "ok": ok]
        if let reason { value["reason"] = reason.rawValue }
        return value.merging(extra) { a, _ in a }
    }
}
