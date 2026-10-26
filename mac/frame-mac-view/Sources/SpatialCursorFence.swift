import ApplicationServices
import Foundation

/// Eyes highlight, an action commits (FW-15). The first physical click while
/// the eyes highlight a window other than the active one only focuses that
/// window: its down is swallowed, and so are its drags and its up, so an app
/// never sees half a click. Events the helper posted itself pass untouched.
/// Pure; the fence's tap keeps one under its lock.
struct GazeClickGate {
    enum Event: Equatable { case down(Int), drag(Int), up(Int) }
    enum Decision: Equatable { case pass, consume, focus(String) }
    private(set) var highlighted: String?
    private var highlightUntilMs = 0.0
    private(set) var active: String?
    /// Buttons whose down was swallowed; their drags and up follow it.
    private(set) var consumed = Set<Int>()

    /// The native host lit (or put out) `source`'s highlight. It lasts
    /// `ttlMs` unless refreshed, so a host that went quiet cannot leave one.
    mutating func highlight(_ source: String, on: Bool, nowMs: Double, ttlMs: Double) {
        if on { highlighted = source; highlightUntilMs = nowMs + ttlMs }
        else if highlighted == source { highlighted = nil }
    }
    mutating func setActive(_ source: String?) { active = source }

    mutating func event(_ event: Event, tagged: Bool, nowMs: Double) -> Decision {
        guard !tagged else { return .pass }
        switch event {
        case .down(let button):
            consumed.remove(button)  // its up never came (the tap was off): this is a new click
            guard consumed.isEmpty, let target = highlighted, nowMs < highlightUntilMs, target != active else {
                return .pass
            }
            consumed.insert(button)
            highlighted = nil  // one click focuses; the next goes to the window
            return .focus(target)
        case .drag(let button):
            return consumed.contains(button) ? .consume : .pass
        case .up(let button):
            return consumed.remove(button) != nil ? .consume : .pass
        }
    }

    mutating func clear() { highlighted = nil; active = nil; consumed.removeAll() }
}

/// The Mac's own keyboard in use, reported at most every `intervalMs` while
/// keys go down and up. A long stare never moves focus while the user types.
struct TypingSignal {
    static let intervalMs = 250.0
    private var lastMs = -Double.infinity
    mutating func key(nowMs: Double) -> Bool {
        guard nowMs - lastMs >= Self.intervalMs else { return false }
        lastMs = nowMs
        return true
    }
}

/// Pointer filtering has its own run loop: slow Accessibility calls on main
/// must not cause WindowServer to disable the event tap. The same tap watches
/// for pointer activity the helper did not post (the physical trackpad): it
/// only reports it, never suppresses it.
final class SpatialCursorFence {
    enum External: Equatable {
        case motion
        case button(Int, down: Bool)
    }

    /// Untagged events are physical (or another program's); tagged ones are ours.
    static func external(type: CGEventType, userData: Int64) -> External? {
        guard userData != InjectedEvent.tag else { return nil }
        switch type {
        case .mouseMoved, .leftMouseDragged, .rightMouseDragged, .otherMouseDragged, .scrollWheel: return .motion
        case .leftMouseDown: return .button(0, down: true)
        case .leftMouseUp: return .button(0, down: false)
        case .rightMouseDown: return .button(2, down: true)
        case .rightMouseUp: return .button(2, down: false)
        case .otherMouseDown: return .button(1, down: true)
        case .otherMouseUp: return .button(1, down: false)
        default: return nil
        }
    }

    /// A physical key (not one the helper posted).
    static func typed(type: CGEventType, userData: Int64) -> Bool {
        guard userData != InjectedEvent.tag else { return false }
        return type == .keyDown || type == .keyUp || type == .flagsChanged
    }

    static func gateEvent(_ type: CGEventType, buttonNumber: Int64) -> GazeClickGate.Event? {
        switch type {
        case .leftMouseDown: return .down(0)
        case .leftMouseUp: return .up(0)
        case .leftMouseDragged: return .drag(0)
        case .rightMouseDown: return .down(2)
        case .rightMouseUp: return .up(2)
        case .rightMouseDragged: return .drag(2)
        case .otherMouseDown: return .down(Int(max(buttonNumber, 1)))
        case .otherMouseUp: return .up(Int(max(buttonNumber, 1)))
        case .otherMouseDragged: return .drag(Int(max(buttonNumber, 1)))
        default: return nil
        }
    }

    /// Called on main with the source to focus, after the tap swallowed the click.
    var onGazeClick: ((String) -> Void)? {
        get { lock.lock(); defer { lock.unlock() }; return gazeClickHandler }
        set { lock.lock(); gazeClickHandler = newValue; lock.unlock() }
    }
    /// Called on main while physical keys are in use (TypingSignal).
    var onTyping: (() -> Void)? {
        get { lock.lock(); defer { lock.unlock() }; return typingHandler }
        set { lock.lock(); typingHandler = newValue; lock.unlock() }
    }
    private var gazeClickHandler: ((String) -> Void)?
    private var typingHandler: (() -> Void)?
    private var gate = GazeClickGate()
    private var typingSignal = TypingSignal()
    func highlight(_ source: String, on: Bool, ttlMs: Double) {
        lock.lock(); gate.highlight(source, on: on, nowMs: Self.nowMs, ttlMs: ttlMs); lock.unlock()
    }
    func setActive(_ source: String?) { lock.lock(); gate.setActive(source); lock.unlock() }
    var highlighted: String? { lock.lock(); defer { lock.unlock() }; return gate.highlighted }
    private static var nowMs: Double { ProcessInfo.processInfo.systemUptime * 1000 }

    /// Called on main. Motion is reported at most every 50 ms.
    var onExternal: ((External) -> Void)? {
        get { lock.lock(); defer { lock.unlock() }; return externalHandler }
        set { lock.lock(); externalHandler = newValue; lock.unlock() }
    }
    private var externalHandler: ((External) -> Void)?
    private var lastExternalMotion: CFAbsoluteTime = 0
    private let lock = NSLock()
    private let correctionQueue = DispatchQueue(label: "frame-mac-view.cursor-fence")
    private var tap: CFMachPort?
    private var runLoop: CFRunLoop?
    private var allowedRects: [CGRect] = []
    private var events = 0
    private var generation = 0
    var eventCount: Int { lock.lock(); defer { lock.unlock() }; return events }
    var enabled: Bool {
        lock.lock(); let tap = self.tap; lock.unlock()
        return tap.map { CGEvent.tapIsEnabled(tap: $0) } ?? false
    }
    var rects: [CGRect] { lock.lock(); defer { lock.unlock() }; return allowedRects }

    static func confined(_ point: CGPoint, to rects: [CGRect]) -> CGPoint {
        guard !rects.contains(where: { $0.contains(point) }) else { return point }
        return rects.filter { !$0.isEmpty }.map { rect in
            CGPoint(x: min(max(point.x, rect.minX), rect.maxX - 0.5),
                    y: min(max(point.y, rect.minY), rect.maxY - 0.5))
        }.min { hypot($0.x - point.x, $0.y - point.y) < hypot($1.x - point.x, $1.y - point.y) } ?? point
    }

    func update(_ rects: [CGRect]) {
        lock.lock(); allowedRects = rects.filter { !$0.isEmpty }; lock.unlock()
    }

    @discardableResult
    func start() -> Bool {
        lock.lock(); let active = tap != nil; lock.unlock()
        if active { return true }
        let types: [CGEventType] = [.mouseMoved, .leftMouseDragged, .rightMouseDragged, .otherMouseDragged,
                                    .leftMouseDown, .leftMouseUp, .rightMouseDown, .rightMouseUp,
                                    .otherMouseDown, .otherMouseUp, .scrollWheel,
                                    .keyDown, .keyUp, .flagsChanged]
        let mask = types.reduce(CGEventMask(0)) { $0 | (CGEventMask(1) << $1.rawValue) }
        let callback: CGEventTapCallBack = { _, type, event, context in
            guard let context else { return Unmanaged.passUnretained(event) }
            let fence = Unmanaged<SpatialCursorFence>.fromOpaque(context).takeUnretainedValue()
            if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                fence.lock.lock(); let tap = fence.tap; fence.lock.unlock()
                if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
            } else {
                let userData = event.getIntegerValueField(.eventSourceUserData)
                if type == .keyDown || type == .keyUp || type == .flagsChanged {
                    // Only observed: typing holds a long stare back on the headset.
                    guard SpatialCursorFence.typed(type: type, userData: userData) else { return Unmanaged.passUnretained(event) }
                    fence.lock.lock()
                    let handler = fence.typingSignal.key(nowMs: SpatialCursorFence.nowMs) ? fence.typingHandler : nil
                    fence.lock.unlock()
                    if let handler { DispatchQueue.main.async { handler() } }
                    return Unmanaged.passUnretained(event)
                }
                if let external = SpatialCursorFence.external(type: type, userData: userData) {
                    fence.lock.lock()
                    let now = CFAbsoluteTimeGetCurrent()
                    var handler = fence.externalHandler
                    if external == .motion {
                        if now - fence.lastExternalMotion < 0.05 { handler = nil } else { fence.lastExternalMotion = now }
                    }
                    fence.lock.unlock()
                    if let handler { DispatchQueue.main.async { handler(external) } }
                }
                if let gateEvent = SpatialCursorFence.gateEvent(type,
                        buttonNumber: event.getIntegerValueField(.mouseEventButtonNumber)) {
                    fence.lock.lock()
                    let click = fence.gazeClickHandler
                    let decision: GazeClickGate.Decision = click == nil ? .pass
                        : fence.gate.event(gateEvent, tagged: userData == InjectedEvent.tag, nowMs: SpatialCursorFence.nowMs)
                    fence.lock.unlock()
                    switch decision {
                    case .pass: break
                    case .consume: return nil
                    case .focus(let source):
                        if let click { DispatchQueue.main.async { click(source) } }
                        return nil
                    }
                }
                switch type {
                case .mouseMoved, .leftMouseDragged, .rightMouseDragged, .otherMouseDragged: break
                default: return Unmanaged.passUnretained(event)  // buttons and wheels are only observed
                }
                fence.lock.lock()
                fence.events += 1
                let rects = fence.allowedRects, epoch = fence.generation
                fence.lock.unlock()
                let point = SpatialCursorFence.confined(event.location, to: rects)
                if point != event.location {
                    event.location = point
                    // Warping from inside a HID tap can block WindowServer.
                    // Correct the actual cursor only after returning the event.
                    fence.correctionQueue.async { [weak fence] in
                        guard let fence else { return }
                        fence.lock.lock()
                        let current = fence.tap != nil && fence.generation == epoch
                        let rects = fence.allowedRects
                        fence.lock.unlock()
                        guard current, let cursor = CGEvent(source: nil)?.location else { return }
                        let confined = SpatialCursorFence.confined(cursor, to: rects)
                        if confined != cursor { CGWarpMouseCursorPosition(confined) }
                    }
                }
            }
            return Unmanaged.passUnretained(event)
        }
        guard let tap = CGEvent.tapCreate(tap: .cghidEventTap, place: .headInsertEventTap, options: .defaultTap,
                                          eventsOfInterest: mask, callback: callback,
                                          userInfo: Unmanaged.passUnretained(self).toOpaque()) else { return false }
        lock.lock(); self.tap = tap; generation += 1; lock.unlock()
        let ready = DispatchSemaphore(value: 0)
        let thread = Thread { [self] in
            let loop = CFRunLoopGetCurrent()
            let source = CFMachPortCreateRunLoopSource(nil, tap, 0)
            lock.lock(); runLoop = loop; lock.unlock()
            CFRunLoopAddSource(loop, source, .commonModes)
            CGEvent.tapEnable(tap: tap, enable: true)
            ready.signal()
            CFRunLoopRun()
            CFRunLoopRemoveSource(loop, source, .commonModes)
        }
        thread.name = "Spatial Cursor Fence"
        thread.start()
        if ready.wait(timeout: .now() + 3) == .success { return true }
        stop()
        return false
    }

    func stop() {
        lock.lock()
        let tap = self.tap, loop = runLoop
        self.tap = nil; runLoop = nil; allowedRects.removeAll(); generation += 1; gate.clear()
        lock.unlock()
        if let tap { CFMachPortInvalidate(tap) }
        if let loop { CFRunLoopStop(loop) }
    }
}
