import Foundation

/// One workspace control connection, independent of all video send queues.
/// A disconnect consumes the session; a new connection is idle until fresh Start.
final class WorkspaceMouseControl {
    private let fence: SpatialCursorFence
    private let queue = DispatchQueue(label: "frame-mac-view.workspace-control")
    private let pendingLock = NSLock()
    private var pending: [[String: Any]] = []
    private var overflow = false
    private var socket: WebSocket?
    private var timer: DispatchSourceTimer?
    private var session = ""
    private var host = ""
    private var bound: [[String: Any]] = []
    private var ready = false
    private var startRequested = false
    private var ticket: (String, Date)?
    private var readyDeadline = Date.distantFuture
    private var commandSequence: UInt64 = 0
    var windowCatalog: (() -> [[String: Any]])?
    var admitWindow: (([String: Any]) -> Bool)?
    var validateBindings: (([[String: Any]]) -> Bool)?
    var onStop: (() -> Void)?
    var onCaptureChanged: (() -> Void)?

    init(fence: SpatialCursorFence) { self.fence = fence }
    func issueTicket(startRequested: Bool = false) -> String {
        dispatchPrecondition(condition: .onQueue(.main))
        let value = UUID().uuidString + UUID().uuidString
        self.startRequested = startRequested
        ticket = (value, Date().addingTimeInterval(60))
        DispatchQueue.main.asyncAfter(deadline: .now() + 60) { [weak self] in
            guard let self, self.ticket?.0 == value else { return }
            self.ticket = nil; self.startRequested = false
            self.onStop?() // no host arrived: restore the selected windows locally
        }
        return value
    }
    func accept(_ req: Request, _ connection: HTTPConnection) {
        DispatchQueue.main.async {
            guard let ticket = self.ticket, ticket.1 > Date(), req.query["ticket"] == ticket.0,
                  self.socket == nil else { return connection.respond(403, text: "invalid workspace control ticket") }
            self.ticket = nil
            guard let ws = connection.upgrade(req) else { return }
            self.socket = ws; self.session = UUID().uuidString
            self.host = ""; self.bound = []; self.ready = false; self.commandSequence = 0
            self.readyDeadline = Date().addingTimeInterval(30)
            ws.onText = { [weak self] text in DispatchQueue.main.async { self?.receive(text) } }
            ws.onClose = { [weak self, weak ws] in
                DispatchQueue.main.async { if self?.socket === ws { self?.stop() } }
            }
            ws.start()
            ws.sendJSON(["t": "workspace-hello", "session": self.session, "ttlMs": 1500, "heartbeatMs": 250])
            let timer = DispatchSource.makeTimerSource(queue: self.queue)
            timer.schedule(deadline: .now(), repeating: .milliseconds(4))
            timer.setEventHandler { [weak self] in self?.drain() }
            self.timer = timer; timer.resume()
        }
    }
    func start() -> Bool {
        dispatchPrecondition(condition: .onQueue(.main))
        guard ready, socket != nil, validateBindings?(bound) == true else { return false }
        let lease = session
        let captured = fence.capture(session: lease, host: host, onEvent: { [weak self] packet in
            self?.enqueue(packet)
        }, onLoss: { [weak self] in
            guard let self, self.session == lease else { return }
            self.stop()
        })
        if captured {
            readyDeadline = .distantFuture
            onCaptureChanged?()
            socket?.sendJSON(["t": "workspace-started", "session": session, "host": host])
        }
        return captured
    }
    func stop() {
        dispatchPrecondition(condition: .onQueue(.main))
        let wasConnected = socket != nil
        let old = socket
        socket = nil; ready = false; session = ""; host = ""; bound = []
        startRequested = false; ticket = nil
        timer?.cancel(); timer = nil
        fence.releaseCapture()
        pendingLock.lock(); pending.removeAll(); overflow = false; pendingLock.unlock()
        old?.sendJSON(["t": "workspace-stopped"])
        old?.close()
        onCaptureChanged?()
        if wasConnected { onStop?() }
    }
    private func receive(_ text: String) {
        guard let data = text.data(using: .utf8), data.count <= 16384,
              let message = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              message["session"] as? String == session, let kind = message["t"] as? String else { return }
        if kind == "workspace-windows" || kind == "workspace-admit" {
            guard ready, fence.mouseCaptured, message["host"] as? String == host,
                  let q = message["q"] as? NSNumber, q.uint64Value > commandSequence else { return }
            commandSequence = q.uint64Value
            if kind == "workspace-windows" {
                socket?.sendJSON(["t": "workspace-windows", "session": session, "host": host,
                                  "q": q, "windows": windowCatalog?() ?? []])
            } else {
                let selected = message["window"] as? [String: Any]
                let ok = selected.map { admitWindow?($0) == true } ?? false
                socket?.sendJSON(["t": "workspace-admitted", "session": session, "host": host,
                                  "q": q, "ok": ok, "message": ok ? "Window admitted — opening…" : "Window changed or closed. Refresh and choose again."])
            }
            return
        }
        if kind == "workspace-ready" {
            guard let host = message["host"] as? String, !host.isEmpty, host.count <= 128,
                  message["recoveryReady"] as? Bool == true,
                  let bindings = message["panels"] as? [[String: Any]], !bindings.isEmpty,
                  validateBindings?(bindings) == true else { return }
            guard !fence.mouseCaptured || self.host == host else { return }
            self.host = host; bound = bindings; ready = true
            socket?.sendJSON(["t": "workspace-ready", "session": session, "host": host, "bindingsQ": message["bindingsQ"] ?? 0])
            if startRequested && !fence.mouseCaptured {
                startRequested = false
                if !start() { stop() } // failed tap/readiness never leaves a pending capture
            }
        } else if kind == "workspace-heartbeat" {
            // A closed panel may race its host's next binding snapshot. Retire
            // only invalid bindings; losing the last window still stops capture.
            bound = bound.filter { validateBindings?([$0]) == true }
            guard !bound.isEmpty, message["host"] as? String == host,
                  let q = message["q"] as? NSNumber, q.uint64Value > 0,
                  fence.heartbeat(session: session, host: host, sequence: q.uint64Value) else {
                if fence.mouseCaptured { stop() }
                return
            }
            socket?.sendJSON(["t": "workspace-heartbeat", "session": session, "host": host, "q": q])
        } else if kind == "workspace-stop" { stop() }
    }
    private func enqueue(_ packet: [String: Any]) {
        pendingLock.lock(); defer { pendingLock.unlock() }
        if packet["kind"] as? String == "motion", pending.last?["kind"] as? String == "motion" {
            pending[pending.count - 1] = packet
        } else if pending.count < 256 { pending.append(packet) }
        else { overflow = true }
    }
    private func drain() {
        pendingLock.lock(); let records = pending; pending.removeAll(keepingCapacity: true)
        let failed = overflow; pendingLock.unlock()
        // Main owns connection identity. A stopped session cannot send old edges
        // to its successor; packets retain both epochs until the host validates.
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            if failed { self.stop(); return }
            guard let socket = self.socket else { return }
            if Date() >= self.readyDeadline { self.stop(); return }
            if socket.pendingBytes > 64 * 1024 { self.stop(); return }
            for record in records where record["session"] as? String == self.session {
                socket.sendJSON(record)
            }
        }
    }
}
