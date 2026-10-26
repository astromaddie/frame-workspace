import ApplicationServices
import Foundation

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
                                    .otherMouseDown, .otherMouseUp, .scrollWheel]
        let mask = types.reduce(CGEventMask(0)) { $0 | (CGEventMask(1) << $1.rawValue) }
        let callback: CGEventTapCallBack = { _, type, event, context in
            guard let context else { return Unmanaged.passUnretained(event) }
            let fence = Unmanaged<SpatialCursorFence>.fromOpaque(context).takeUnretainedValue()
            if type == .tapDisabledByTimeout || type == .tapDisabledByUserInput {
                fence.lock.lock(); let tap = fence.tap; fence.lock.unlock()
                if let tap { CGEvent.tapEnable(tap: tap, enable: true) }
            } else {
                if let external = SpatialCursorFence.external(type: type,
                        userData: event.getIntegerValueField(.eventSourceUserData)) {
                    fence.lock.lock()
                    let now = CFAbsoluteTimeGetCurrent()
                    var handler = fence.externalHandler
                    if external == .motion {
                        if now - fence.lastExternalMotion < 0.05 { handler = nil } else { fence.lastExternalMotion = now }
                    }
                    fence.lock.unlock()
                    if let handler { DispatchQueue.main.async { handler(external) } }
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
        self.tap = nil; runLoop = nil; allowedRects.removeAll(); generation += 1
        lock.unlock()
        if let tap { CFMachPortInvalidate(tap) }
        if let loop { CFRunLoopStop(loop) }
    }
}
