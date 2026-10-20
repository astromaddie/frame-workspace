// A small HTTP/1.1 + WebSocket server on Network.framework, loopback only.
// Enough for the agent's JSON endpoints, the viewer page and one WebSocket per
// stream; not a general web server.
import CryptoKit
import Foundation
import Network

struct Request {
    let method: String
    let path: String
    let query: [String: String]
    let headers: [String: String]  // lower-cased names
}

final class Server {
    let queue = DispatchQueue(label: "frame-mac-view.server")
    private let listener: NWListener
    private let handle: (Request, HTTPConnection) -> Void

    init(port: UInt16, handle: @escaping (Request, HTTPConnection) -> Void) throws {
        let tcp = NWProtocolTCP.Options()
        tcp.noDelay = true
        let params = NWParameters(tls: nil, tcp: tcp)
        // Port 0 lets the system pick; `port` then says which.
        params.requiredLocalEndpoint = .hostPort(host: "127.0.0.1", port: NWEndpoint.Port(rawValue: port) ?? .any)
        params.allowLocalEndpointReuse = true
        listener = try NWListener(using: params)
        self.handle = handle
    }

    var port: UInt16? { listener.port?.rawValue }

    func start(ready: @escaping (Error?) -> Void) {
        listener.stateUpdateHandler = { state in
            switch state {
            case .ready: ready(nil)
            case .failed(let e): ready(e)
            default: break
            }
        }
        listener.newConnectionHandler = { [weak self] conn in
            guard let self else { return }
            HTTPConnection(conn, queue: self.queue, handle: self.handle).start()
        }
        listener.start(queue: queue)
    }
}

final class HTTPConnection {
    // The switcher carries its bounded panel list in the authenticated query.
    // Keep a finite cap while leaving room for URL-encoded JSON entries.
    private static let maxRequestHeadBytes = 128 * 1024
    let conn: NWConnection
    let queue: DispatchQueue
    private let handle: (Request, HTTPConnection) -> Void
    private var buffer = Data()
    private var retained: HTTPConnection?  // alive until the response is sent

    init(_ conn: NWConnection, queue: DispatchQueue, handle: @escaping (Request, HTTPConnection) -> Void) {
        self.conn = conn
        self.queue = queue
        self.handle = handle
    }

    func start() {
        retained = self
        conn.start(queue: queue)
        readHead()
    }

    private func readHead() {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 16384) { [self] data, _, done, error in
            if let data { buffer.append(data) }
            if buffer.count > Self.maxRequestHeadBytes {
                return respond(400, text: "request headers too large")
            }
            if let end = buffer.range(of: Data("\r\n\r\n".utf8)) {
                guard let req = Self.parse(buffer[..<end.lowerBound]) else { return respond(400, text: "bad request") }
                handle(req, self)
            } else if error != nil || done {
                close()
            } else {
                readHead()
            }
        }
    }

    static func parse(_ head: Data) -> Request? {
        guard let text = String(data: head, encoding: .utf8) else { return nil }
        let lines = text.components(separatedBy: "\r\n")
        let parts = lines[0].split(separator: " ")
        guard parts.count >= 2, let url = URLComponents(string: String(parts[1])) else { return nil }
        var headers: [String: String] = [:]
        for line in lines.dropFirst() {
            guard let colon = line.firstIndex(of: ":") else { continue }
            headers[line[..<colon].lowercased()] = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
        }
        var query: [String: String] = [:]
        for item in url.queryItems ?? [] { query[item.name] = item.value ?? "" }
        return Request(method: String(parts[0]), path: url.path, query: query, headers: headers)
    }

    func respond(_ status: Int, body: Data, type: String) {
        let reason = [200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 409: "Conflict", 500: "Internal Server Error"][status] ?? "Error"
        var head = "HTTP/1.1 \(status) \(reason)\r\nContent-Type: \(type)\r\nContent-Length: \(body.count)\r\n"
        head += "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
        conn.send(content: Data(head.utf8) + body, isComplete: true, completion: .contentProcessed { [self] _ in close() })
    }

    func respond(_ status: Int, text: String) {
        respond(status, body: Data(text.utf8), type: "text/plain; charset=utf-8")
    }

    func respond(_ status: Int = 200, json: Any) {
        let body = (try? JSONSerialization.data(withJSONObject: json, options: [.sortedKeys])) ?? Data("{}".utf8)
        respond(status, body: body, type: "application/json")
    }

    func close() {
        conn.cancel()
        retained = nil
    }

    /// Completes the WebSocket handshake and hands the connection over.
    func upgrade(_ req: Request) -> WebSocket? {
        guard req.headers["upgrade"]?.lowercased() == "websocket", let key = req.headers["sec-websocket-key"] else {
            respond(400, text: "expected a WebSocket upgrade")
            return nil
        }
        let accept = Data(Insecure.SHA1.hash(data: Data((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").utf8))).base64EncodedString()
        let head = "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: \(accept)\r\n\r\n"
        conn.send(content: Data(head.utf8), completion: .contentProcessed { _ in })
        let ws = WebSocket(conn, queue: queue)
        retained = nil
        return ws
    }
}

/// Server side of RFC 6455. Sends are counted until the network stack has
/// taken them, so the stream can skip frames instead of queueing seconds of
/// video on a slow link.
final class WebSocket {
    static let maxMessage: UInt64 = 1 << 20
    let conn: NWConnection
    let queue: DispatchQueue
    var onText: ((String) -> Void)?
    var onClose: (() -> Void)?
    private var buffer = Data()
    private var fragments = Data()
    private var fragmentOpcode: UInt8 = 0
    private var closed = false
    private var retained: WebSocket?
    private let lock = NSLock()
    private var _pending = 0
    var onDrain: (() -> Void)?

    /// Bytes handed to the connection that it hasn't sent yet. Any thread.
    var pendingBytes: Int { lock.lock(); defer { lock.unlock() }; return _pending }

    init(_ conn: NWConnection, queue: DispatchQueue) {
        self.conn = conn
        self.queue = queue
    }

    func start() {
        retained = self
        read()
    }

    func sendText(_ s: String) { send(opcode: 1, Data(s.utf8)) }
    func sendJSON(_ obj: Any) {
        if let d = try? JSONSerialization.data(withJSONObject: obj), let s = String(data: d, encoding: .utf8) { sendText(s) }
    }
    /// `taken` runs once the network stack has taken the whole frame.
    func sendBinary(_ d: Data, taken: (() -> Void)? = nil) { send(opcode: 2, d, taken: taken) }

    func send(opcode: UInt8, _ payload: Data, taken: (() -> Void)? = nil) {
        var frame = Data([0x80 | opcode])
        let n = payload.count
        if n < 126 {
            frame.append(UInt8(n))
        } else if n < 65536 {
            frame.append(126)
            frame.append(contentsOf: [UInt8(n >> 8), UInt8(n & 0xff)])
        } else {
            frame.append(127)
            for shift in stride(from: 56, through: 0, by: -8) { frame.append(UInt8((UInt64(n) >> UInt64(shift)) & 0xff)) }
        }
        frame.append(payload)
        let size = frame.count
        lock.lock(); _pending += size; lock.unlock()
        conn.send(content: frame, completion: .contentProcessed { [weak self] _ in
            guard let self else { return }
            self.lock.lock(); self._pending -= size; self.lock.unlock()
            taken?()
            self.onDrain?()
        })
    }

    func close() {
        guard !closed else { return }
        closed = true
        send(opcode: 8, Data([0x03, 0xe8]))  // 1000, normal closure
        conn.send(content: nil, isComplete: true, completion: .contentProcessed { [weak self] _ in self?.conn.cancel() })
        finish()
    }

    private func finish() {
        let cb = onClose
        onClose = nil
        onText = nil
        onDrain = nil
        cb?()
        retained = nil
    }

    private func read() {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, done, error in
            guard let self, !self.closed else { return }
            if let data { self.buffer.append(data) }
            self.parse()
            if error != nil || done {
                self.closed = true
                self.conn.cancel()
                self.finish()
            } else if !self.closed {
                self.read()
            }
        }
    }

    private func parse() {
        while buffer.count >= 2 {
            let b = [UInt8](buffer.prefix(14))
            let fin = b[0] & 0x80 != 0, opcode = b[0] & 0x0f, masked = b[1] & 0x80 != 0
            // Lengths are unsigned and clients only send small control
            // messages, so anything big (or a 64-bit length with the top bit
            // set) is refused before it's turned into an Int.
            var len64 = UInt64(b[1] & 0x7f), off = 2
            if len64 == 126 {
                guard b.count >= 4 else { return }
                len64 = UInt64(b[2]) << 8 | UInt64(b[3]); off = 4
            } else if len64 == 127 {
                guard b.count >= 10 else { return }
                len64 = 0
                for i in 2..<10 { len64 = len64 << 8 | UInt64(b[i]) }
                off = 10
            }
            guard len64 <= WebSocket.maxMessage else { return close() }
            let len = Int(len64)
            let maskOff = off
            if masked { off += 4 }
            guard buffer.count >= off + len else { return }
            let start = buffer.startIndex
            var payload = Data(buffer[(start + off)..<(start + off + len)])
            if masked {
                let mask = [UInt8](buffer[(start + maskOff)..<(start + maskOff + 4)])
                payload.withUnsafeMutableBytes { p in
                    for i in 0..<len { p[i] ^= mask[i & 3] }
                }
            }
            buffer.removeFirst(off + len)
            switch opcode {
            case 0, 1, 2:
                if opcode != 0 { fragmentOpcode = opcode; fragments = Data() }
                guard fragments.count + payload.count <= Int(WebSocket.maxMessage) else { return close() }
                fragments.append(payload)
                if fin, fragmentOpcode == 1, let s = String(data: fragments, encoding: .utf8) { onText?(s) }
            case 8: close(); return
            case 9: send(opcode: 10, payload)
            default: break
            }
        }
    }
}
