// Native headset input across sessions: trackpad contention, held-input
// expiry and focus confirmation. Main queue only.
import AppKit
import Foundation

/// A session that speaks native input (main.swift's Session).
protocol NativeInputClient: AnyObject {
    var id: Int { get }
    func nativeInputNotify(_ message: [String: Any])
    /// Lets go of this connection's buttons when it holds one but has gone quiet.
    func nativeInputExpire(nowMs: Double)
}

final class NativeInputCoordinator {
    static let shared = NativeInputCoordinator()
    private(set) var contention = PointerContention()
    private var clients: [Int: () -> NativeInputClient?] = [:]
    private var timer: DispatchSourceTimer?

    func register(_ client: NativeInputClient) {
        dispatchPrecondition(condition: .onQueue(.main))
        clients[client.id] = { [weak client] in client }
        guard timer == nil else { return }
        let t = DispatchSource.makeTimerSource(queue: .main)
        t.schedule(deadline: .now() + 0.25, repeating: 0.25)
        t.setEventHandler { [weak self] in self?.tick() }
        t.resume()
        timer = t
    }

    func unregister(_ id: Int) {
        dispatchPrecondition(condition: .onQueue(.main))
        clients[id] = nil
        if clients.isEmpty { timer?.cancel(); timer = nil; contention.reset(); Input.physicallyHeld = [] }
    }

    private func tick() {
        let now = ProcessInfo.processInfo.systemUptime * 1000
        for client in clients.values.compactMap({ $0() }) { client.nativeInputExpire(nowMs: now) }
        clients = clients.filter { $0.value() != nil }
    }

    private func broadcast(owner: String) {
        for client in clients.values.compactMap({ $0() }) {
            client.nativeInputNotify(["t": "input-state", "owner": owner])
        }
    }

    /// From the fence tap, already on main.
    func external(_ activity: SpatialCursorFence.External) {
        let took: Bool
        let wasHeld = !contention.physicalButtons.isEmpty
        switch activity {
        case .motion: took = contention.physicalMotion()
        case .button(let button, let down): took = contention.physicalButton(button, down: down)
        }
        Input.physicallyHeld = contention.physicalButtons
        if took { broadcast(owner: "physical") }
        // A held trackpad button holds a long stare back on the headset.
        let held = !contention.physicalButtons.isEmpty
        if held != wasHeld { notifyAll(["t": "input-state", "physicalHeld": held]) }
    }

    /// The Mac's keyboard is in use (the fence tap, throttled): the headset
    /// does not let a long stare move focus while the user types.
    func typed() { notifyAll(["t": "input-state", "typing": true]) }

    private func notifyAll(_ message: [String: Any]) {
        for client in clients.values.compactMap({ $0() }) { client.nativeInputNotify(message) }
    }

    /// Whether a remote hover move may go to the Mac now.
    func remoteMove(to point: CGPoint) -> Bool {
        let had = contention.physicalOwns
        let allowed = contention.remoteMove(to: point, holding: !Input.heldButtons.isEmpty)
        if had && !contention.physicalOwns { broadcast(owner: "remote") }
        return allowed
    }

    func remoteEdge() -> Bool {
        let had = contention.physicalOwns
        let allowed = contention.remoteEdge()
        if had && !contention.physicalOwns { broadcast(owner: "remote") }
        return allowed
    }

    /// Polls macOS after a raise, which applies asynchronously, and answers
    /// once the window (or its modal child) is in front, or after `timeout`.
    static func confirmFocus(windowID: CGWindowID, pid: pid_t, timeout: TimeInterval = 0.4,
                             completion: @escaping (FocusObservation) -> Void) {
        let deadline = ProcessInfo.processInfo.systemUptime + timeout
        func check() {
            let bounds = WindowInfo.find(windowID)?.bounds ?? .zero
            let front = WindowInfo.all().first { $0.layer == 0 }.map {
                SpatialFocusPolicy.Front(id: $0.id, pid: $0.pid, bounds: $0.bounds)
            }
            let observed = SpatialFocusPolicy.observe(target: windowID, pid: pid, bounds: bounds,
                frontmostPID: NSWorkspace.shared.frontmostApplication?.processIdentifier, front: front)
            if observed != .pending || ProcessInfo.processInfo.systemUptime >= deadline { return completion(observed) }
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.03, execute: check)
        }
        check()
    }
}
