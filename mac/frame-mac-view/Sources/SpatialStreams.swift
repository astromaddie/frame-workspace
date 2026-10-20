// Small state machines used by Spatial Mode. Kept independent of AppKit and
// ScreenCaptureKit so their selection and change rules can be tested locally.
import Foundation

struct NormalizedPoint: Equatable {
    let u: Double
    let v: Double

    init?(u: Double, v: Double) {
        guard u.isFinite, v.isFinite, (0...1).contains(u), (0...1).contains(v) else { return nil }
        self.u = u
        self.v = v
    }

    /// Maps to a window-relative point while keeping the exact 1.0 edge inside
    /// the right/bottom boundary, where a cursor could land off-window.
    func point(x: Double, y: Double, width: Double, height: Double) -> SpatialPoint {
        let mappedX = min(u * width, max(0, width - 0.5))
        let mappedY = min(v * height, max(0, height - 0.5))
        return SpatialPoint(x: x + mappedX, y: y + mappedY)
    }
}

struct SpatialPoint: Equatable {
    let x: Double
    let y: Double
}

struct CursorFocusSelection: Equatable {
    let point: NormalizedPoint
    let shouldMoveCursor: Bool
}

/// Remembers each panel's last normalized pointer and the panel most recently
/// focused. Callers serialize actual CGEvent work on the main queue.
final class FocusCursorMemory {
    private let lock = NSLock()
    private var points: [String: NormalizedPoint] = [:]
    private var focused: String?

    var focusedSource: String? {
        lock.lock(); defer { lock.unlock() }
        return focused
    }

    func remember(_ point: NormalizedPoint, for source: String) {
        lock.lock(); points[source] = point; lock.unlock()
    }

    /// Existing panels return to their last position; the supplied gaze point
    /// initializes a panel the first time it receives focus.
    func focus(_ source: String, firstVisit: NormalizedPoint,
               leavingPoint: NormalizedPoint? = nil) -> CursorFocusSelection {
        lock.lock(); defer { lock.unlock() }
        // Legacy focus notifications can identify the active source before a
        // gaze position has initialized it. The first gaze request must still
        // place the cursor, even when it targets that already-focused source.
        let shouldMove = focused != source || points[source] == nil
        if shouldMove, let previous = focused, let leavingPoint { points[previous] = leavingPoint }
        let point = points[source] ?? firstVisit
        points[source] = point
        focused = source
        return CursorFocusSelection(point: point, shouldMoveCursor: shouldMove)
    }

    func noteFocus(_ source: String, leavingPoint: NormalizedPoint? = nil) {
        lock.lock()
        if focused != source, let previous = focused, let leavingPoint { points[previous] = leavingPoint }
        focused = source
        lock.unlock()
    }

    /// Hovering another panel must not overwrite the active panel's saved
    /// position before a focus transition arrives.
    func rememberIfFocused(_ point: NormalizedPoint, for source: String) {
        lock.lock()
        if focused == source { points[source] = point }
        lock.unlock()
    }

    func remove(_ source: String) {
        lock.lock()
        points[source] = nil
        if focused == source { focused = nil }
        lock.unlock()
    }

    func clear() {
        lock.lock(); points.removeAll(); focused = nil; lock.unlock()
    }
}

enum StreamModeRequest: Equatable {
    case unchanged(UInt64)
    case waitForFirstPicture(UInt64)
    case still(UInt64)
    case resume(UInt64)
}

/// The protocol gate for a window stream. Encoded callbacks carry `epoch`;
/// once still is requested, earlier callbacks fail accepts() and cannot cross
/// the boundary. A first image is queued before honoring an initial still.
struct StreamLiveGate {
    private(set) var live = true
    private(set) var desiredLive = true
    private(set) var hasSentPicture = false
    private(set) var pendingStill = false
    private(set) var awaitingKeyFrame = false
    private(set) var epoch: UInt64 = 0
    private(set) var requestID: UInt64 = 0

    mutating func request(_ shouldBeLive: Bool) -> StreamModeRequest {
        if shouldBeLive == desiredLive {
            if shouldBeLive || !live { return .unchanged(requestID) }
            if !hasSentPicture { return .waitForFirstPicture(requestID) }
        }
        requestID &+= 1
        desiredLive = shouldBeLive
        if shouldBeLive {
            pendingStill = false
            return live ? .unchanged(requestID) : .resume(requestID)
        }
        awaitingKeyFrame = false
        guard hasSentPicture else {
            pendingStill = true
            return .waitForFirstPicture(requestID)
        }
        if live {
            live = false
            epoch &+= 1
        }
        return .still(requestID)
    }

    mutating func activateLive(request: UInt64) -> Bool {
        guard request == requestID, desiredLive else { return false }
        if !live {
            live = true
            epoch &+= 1
            awaitingKeyFrame = true
        }
        pendingStill = false
        return true
    }

    func accepts(frameEpoch: UInt64) -> Bool { live && epoch == frameEpoch }

    func canQueuePicture(frameEpoch: UInt64, key: Bool) -> Bool {
        accepts(frameEpoch: frameEpoch) && (!awaitingKeyFrame || key)
    }

    /// Call only after the encoded picture has been queued to the socket.
    /// Returns true when that first picture completes a pending still request.
    mutating func didQueuePicture(frameEpoch: UInt64, key: Bool) -> Bool {
        guard canQueuePicture(frameEpoch: frameEpoch, key: key) else { return false }
        if awaitingKeyFrame { awaitingKeyFrame = false }
        hasSentPicture = true
        guard pendingStill, !desiredLive else { return false }
        pendingStill = false
        live = false
        epoch &+= 1
        requestID &+= 1
        return true
    }

    mutating func stop() {
        desiredLive = false
        pendingStill = false
        awaitingKeyFrame = false
        live = false
        epoch &+= 1
        requestID &+= 1
    }
}

/// A single pointer shared by all panel sessions. A mouse-up from another
/// session releases the same global drag; disconnect releases buttons first
/// held by that session.
struct SharedPointerButtons {
    private(set) var held = Set<Int>()
    private var ownerByButton: [Int: Int] = [:]

    static func dragButton(in held: Set<Int>) -> Int? {
        if held.contains(0) { return 0 }
        if held.contains(2) { return 2 }
        if held.contains(1) { return 1 }
        return nil
    }

    mutating func down(_ button: Int, owner: Int) -> Bool {
        guard held.insert(button).inserted else { return false }
        ownerByButton[button] = owner
        return true
    }

    mutating func up(_ button: Int) -> Bool {
        guard held.remove(button) != nil else { return false }
        ownerByButton[button] = nil
        return true
    }

    mutating func releaseAll(owner: Int) -> [Int] {
        let buttons = ownerByButton.filter { $0.value == owner }.map(\.key)
        for button in buttons { _ = up(button) }
        return buttons
    }

    mutating func releaseAll() -> [Int] {
        let buttons = Array(held)
        held.removeAll()
        ownerByButton.removeAll()
        return buttons
    }
}

struct SpatialStreamObservation {
    var width = 0
    var height = 0
    var lastChanged: Double = 0
}

/// Per-source values survive a viewer reconnect, allowing /status to report
/// the last usable dimensions and content-change time before the new viewer's
/// first captured frame arrives.
final class SpatialStreamState {
    private let lock = NSLock()
    private var observations: [String: SpatialStreamObservation] = [:]

    func observe(_ source: String, width: Int, height: Int, changed: Bool,
                 unixSeconds: Double = Date().timeIntervalSince1970) {
        lock.lock(); defer { lock.unlock() }
        var state = observations[source, default: SpatialStreamObservation()]
        if width > 0, height > 0 {
            state.width = width
            state.height = height
        }
        if changed { state.lastChanged = unixSeconds }
        observations[source] = state
    }

    func observation(for source: String) -> SpatialStreamObservation {
        lock.lock(); defer { lock.unlock() }
        return observations[source, default: SpatialStreamObservation()]
    }

    func remove(_ source: String) {
        lock.lock(); observations[source] = nil; lock.unlock()
    }

    func clear() {
        lock.lock(); observations.removeAll(); lock.unlock()
    }
}
