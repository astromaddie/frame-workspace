// frame-mac-view: streams Mac windows or displays to viewers on the Steam
// Frame and plays their pointer and key input back on the Mac.
//
//   frame-mac-view serve --port 47811 --page ui/mac-view.html   (token in FRAME_MAC_VIEW_TOKEN)
//   frame-mac-view windows | displays | permissions              (JSON on stdout)
//   frame-mac-view request-permissions                           (shows macOS's prompts)
//
// It listens on 127.0.0.1 only. Frame Control reaches it from the Frame
// through an SSH reverse tunnel, and every request must carry the token.
//
// HTTP. Frame Control's key (?k=, from FRAME_MAC_VIEW_TOKEN) never leaves the
// Mac; the Frame gets a single-use ticket per viewer instead.
//   GET  /ping, /view                     open: tunnel check, viewer page
//   GET  /stream?src=...&t=TICKET|r=KEY   WebSocket (&codec=h264|jpeg&max=&fps=&bpp=)
//   GET  /status, /windows, /displays     ?k=: permissions, streams, sources
//   POST /ticket?src=...                  ?k=: a ticket for one viewer of src
//   POST /close[?src=...]                 ?k=: end those streams, close their windows
//   POST /permissions                     ?k=: show macOS's permission prompts
//   GET  /stats[?id=&since=]              ?k=: per-frame timing records (for benchmarks)
//   POST /bench?src=&action=...           ?k=: ask viewers of src to type, click or show their overlay
// src is window:<CGWindowID>, display:<CGDirectDisplayID> or test.
import AppKit
import ApplicationServices
import Foundation
import IOKit.pwr_mgt
import ScreenCaptureKit
import Security

let version = "1"

func windowsJSON() -> [[String: Any]] {
    let me = ProcessInfo.processInfo.processIdentifier
    let skip: Set<String> = ["Window Server", "Dock", "Control Centre", "Control Center", "Notification Centre",
                             "Notification Center", "Spotlight", "SystemUIServer", "Wallpaper", "WindowManager"]
    return WindowInfo.all().filter {
        $0.layer == 0 && $0.pid != me && !skip.contains($0.app) && $0.bounds.width >= 120 && $0.bounds.height >= 80
    }.map {
        var info: [String: Any] = ["id": $0.id, "src": "window:\($0.id)", "pid": $0.pid,
                                   "app": $0.app, "title": $0.title,
                                   "w": Int($0.bounds.width), "h": Int($0.bounds.height)]
        if let bundleID = NSRunningApplication(processIdentifier: $0.pid)?.bundleIdentifier {
            info["bundleID"] = bundleID
        }
        return info
    }
}

func displaysJSON() -> [[String: Any]] {
    var ids = [CGDirectDisplayID](repeating: 0, count: 16)
    var n: UInt32 = 0
    // Online, not active: a display that's asleep is still one you can stream.
    CGGetOnlineDisplayList(16, &ids, &n)
    return ids.prefix(Int(n)).filter {
        CGDisplayMirrorsDisplay($0) == kCGNullDirectDisplay && !VirtualDisplay.isOurs($0)  // not a separated window's
    }.map { id in
        let b = CGDisplayBounds(id)
        let mode = CGDisplayCopyDisplayMode(id)
        let mirrored = CGDisplayMirrorsDisplay(id)
        return ["id": id, "src": "display:\(id)", "name": displayName(id), "w": Int(b.width), "h": Int(b.height),
                "x": Double(b.minX), "y": Double(b.minY), "pixelW": mode?.pixelWidth ?? 0, "pixelH": mode?.pixelHeight ?? 0,
                "mirrored": mirrored != kCGNullDirectDisplay,
                "mirrorDisplayID": mirrored == kCGNullDirectDisplay ? NSNull() : mirrored as Any,
                "main": CGDisplayIsMain(id) != 0]
    }
}

func permissionsJSON() -> [String: Any] {
    ["screen": CGPreflightScreenCaptureAccess(), "accessibility": AXIsProcessTrusted()]
}

func printJSON(_ obj: Any) {
    let d = (try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys, .prettyPrinted])) ?? Data()
    FileHandle.standardOutput.write(d + Data("\n".utf8))
}

/// Lifecycle records are consumed while the Workspace process remains alive,
/// so each record must be a single parseable line.
func printJSONLine(_ obj: Any) {
    let d = (try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys])) ?? Data()
    FileHandle.standardOutput.write(d + Data("\n".utf8))
}

/// A number from a JSON message, whatever its JSON type.
func num(_ v: Any?) -> Double? { (v as? NSNumber)?.doubleValue }

/// One viewer watching one source.
final class Session {
    let id: Int
    let source: Source
    let capture: CaptureSource
    let encoder: Encoder
    let ws: WebSocket
    let codec: Codec
    let reconnectKey: String
    private let activity: StreamActivityRegistry
    let stats = StreamStats()
    let controller: RateController
    private var appliedScale = 1.0
    private var appliedCaptureFps: Int
    private let testCaptureFps: Int?
    private var appliedPriorityRole: String?
    private var lastSubmit: Int64 = 0
    private var paceScheduled = false
    private let lock = NSLock()
    /// A captured picture waiting to be encoded (or the last one encoded).
    private struct Picture {
        let pb: CVPixelBuffer
        let pts: CMTime
        let capture: Int64
        let arrived: Int64
        var layout: FoveationLayout? = nil
        var directEpoch: UInt64? = nil
        var echo: UInt32  // the input this picture is the first reply to
    }
    private var last: Picture?
    private let foveation: Bool
    private let foveationAtlas: Bool
    private let foveationDirect: Bool
    private let gazeSession = UUID().uuidString
    private var gaze = FoveationGaze()
    private var packer: FoveationPacker?
    private var packingFailed = false
    private let allowDiagnosticGaze = ProcessInfo.processInfo.environment["FRAME_MAC_VIEW_DIAGNOSTIC_GAZE"] == "1"
    private var skipped = false
    private var stopped = false
    private var inFlight = 0
    /// Input posted to the Mac whose effect hasn't been captured yet: the
    /// next picture composited after `after` is tagged with it.
    private var pendingEcho: (id: UInt32, after: Int64)?
    private var statsTimer: DispatchSourceTimer?
    private var gazeTimer: DispatchSourceTimer?
    private var submittedLayout: FoveationLayout?
    /// Frames already queued for the network; beyond this, or with two frames
    /// already in the encoder, new frames are skipped (before encoding, so no
    /// reference frame goes missing) and the newest picture is sent once
    /// things catch up. Only that newest picture is kept meanwhile.
    let maxPending: Int
    static let maxInFlight = 2
    var onEnd: ((Session) -> Void)?
    var onAck: (() -> Void)?
    var onFinished: ((String) -> Void)?
    private let encodeQueue = DispatchQueue(label: "frame-mac-view.encode", qos: .userInteractive)

    init(id: Int, source: Source, capture: CaptureSource, ws: WebSocket, codec: Codec, fps: Int, bitsPerPixel: Double,
         reconnectKey: String, activity: StreamActivityRegistry, foveation: Bool = false, foveationAtlas: Bool = false, foveationDirect: Bool = false) {
        self.foveationDirect = foveation && foveationDirect
        self.foveation = foveation
        self.foveationAtlas = foveation && foveationAtlas
        self.id = id
        self.source = source
        self.capture = capture
        self.ws = ws
        self.codec = codec
        self.reconnectKey = reconnectKey
        self.activity = activity
        // Diagnostic TestSource only: model a source cadence below the target
        // without changing SCK/window/separate capture or the pacing budget.
        let testRate = Int(ProcessInfo.processInfo.environment["FRAME_MAC_VIEW_TEST_CAPTURE_FPS"] ?? "")
        testCaptureFps = source == .test && testRate.map { (1...fps).contains($0) } == true ? testRate : nil
        appliedCaptureFps = testCaptureFps ?? fps
        encoder = Encoder(codec: codec, fps: fps, bitsPerPixel: bitsPerPixel)
        controller = RateController(maxFps: fps, sessionID: id, activity: activity)
        activity.register(id)
        maxPending = codec == .jpeg ? 3 << 20 : 1 << 20
    }

    /// Frames come faster than the current tier's frame rate: this one waits,
    /// and goes when its turn comes (unless a newer one replaces it).
    /// Call with `lock` held.
    private func paced(now: Int64) -> Bool {
        let fps = controller.fps
        guard (foveation || fps < controller.maxFps), lastSubmit > 0 else { return false }
        let interval = Int64(1_000_000 / fps)
        let due = lastSubmit + (foveation ? interval : interval * 9 / 10)
        guard now < due else { return false }
        if !paceScheduled {
            paceScheduled = true
            encodeQueue.asyncAfter(deadline: .now() + .microseconds(Int(due - now))) { [weak self] in
                guard let self else { return }
                self.lock.lock(); self.paceScheduled = false; self.lock.unlock()
                self.resendIfRoom()
            }
        }
        return true
    }

    /// Encodes `pb` unless the link or the encoder is busy, in which case it
    /// becomes the picture to send next.
    private func offer(_ pb: CVPixelBuffer, pts: CMTime, capture shown: Int64, layout: FoveationLayout? = nil, directEpoch: UInt64? = nil) {
        let arrived = nowUs()
        stats.withLock { stats.captured += 1 }
        controller.captured(at: arrived)
        lock.lock()
        // A reply to input stays with whichever picture ends up being sent.
        var echo = skipped ? last?.echo ?? 0 : 0
        if let p = pendingEcho, shown >= p.after {
            echo = p.id
            pendingEcho = nil
        }
        let picture = Picture(pb: pb, pts: pts, capture: shown, arrived: arrived, layout: layout, directEpoch: directEpoch, echo: echo)
        last = picture
        let busy = stopped || ws.pendingBytes > maxPending || inFlight >= Session.maxInFlight
            || paced(now: arrived) || !controller.maySend(now: arrived)
        if busy { skipped = true } else {
            // This fresh picture replaces any pending foveated refresh; its
            // completion must not resend it because of an older skipped flag.
            if foveation { skipped = false }
            inFlight += 1; last?.echo = 0; lastSubmit = arrived
        }
        lock.unlock()
        if busy { stats.withLock { stats.skipped += 1 } } else { submit(picture) }
    }

    private func submit(_ p: Picture) {
        encodeQueue.async {
            var r = FrameRecord()
            r.capture = p.capture
            r.arrived = p.arrived
            r.echo = p.echo
            r.bitrate = self.encoder.bitrate
            r.tier = self.controller.tier
            r.encodeStart = nowUs()
            let seq = self.stats.newFrame(r)
            if p.echo != 0 { self.stats.updateInput(p.echo) { $0.frame = seq; $0.capture = p.capture } }
            var buffer = p.pb
            var layout = p.layout
            if self.foveationDirect {
                self.lock.lock()
                let next = self.gaze.layout(width: CVPixelBufferGetWidth(p.pb), height: CVPixelBufferGetHeight(p.pb), now: nowUs(), direct: true)
                let accept = !self.stopped && (self.gaze.active || p.layout == next)
                self.lock.unlock()
                // A picture paced before loss must not restore an old eye ROI.
                guard accept, (self.capture as? DirectSCKSource)?.acceptsPicture(epoch:p.directEpoch) == true else { self.finished(); return }
            } else if self.foveation {
                self.lock.lock()
                var chosen = self.gaze.layout(width: CVPixelBufferGetWidth(p.pb), height: CVPixelBufferGetHeight(p.pb), now: nowUs(), atlas: self.foveationAtlas)
                self.lock.unlock()
                if chosen.warped && !self.packingFailed {
                    do {
                        if self.packer == nil { self.packer = try FoveationPacker() }
                        buffer = try self.packer!.pack(p.pb, layout: chosen)
                    } catch {
                        self.packingFailed = true
                        self.ws.sendJSON(["t": "foveation-status", "state": "full-quality", "reason": "packing-unavailable"])
                    }
                }
                if self.packingFailed { chosen = .full(chosen.width, chosen.height) }
                layout = chosen
            }
            self.submittedLayout = layout
            if !self.encoder.encode(buffer, pts: p.pts, seq: seq, layout: layout) { self.finished() }
        }
    }

    /// An encode finished (or failed): send the newest skipped picture if
    /// there's room now.
    private func finished() {
        lock.lock()
        inFlight = max(0, inFlight - 1)
        lock.unlock()
        resendIfRoom()
    }

    private func resendIfRoom() {
        lock.lock()
        var next: Picture?
        let now = nowUs()
        if !stopped, skipped, ws.pendingBytes <= maxPending / 2, inFlight < Session.maxInFlight, let l = last,
           !paced(now: now), controller.maySend(now: now, counts: false) {
            next = l
            skipped = false
            inFlight += 1
            last?.echo = 0
            lastSubmit = now
        }
        lock.unlock()
        // Stamped now: VideoToolbox needs increasing times, and the capture
        // time stays in the record, so the wait counts as latency.
        if let p = next {
            submit(Picture(pb: p.pb, pts: CMClockGetTime(CMClockGetHostTimeClock()), capture: p.capture,
                           arrived: p.arrived, layout: p.layout, directEpoch: p.directEpoch, echo: p.echo))
        }
    }

    func start(maxLong: Int, fps: Int) {
        encoder.onFrame = { [weak self] data, key, pts, seq, layout in
            guard let self else { return }
            let t = nowUs()
            // The quality setting's bitrate, at full size, is the most it gets.
            if self.appliedScale == 1 {
                self.controller.setCeiling(self.encoder.defaultBitrate(width: self.encoder.width, height: self.encoder.height))
            }
            self.controller.sent(seq: seq, bytes: data.count + 17 + (layout == nil ? 0 : 20), at: t)
            var echo: UInt32 = 0
            let (w, h) = (layout?.encodedWidth ?? self.encoder.width, layout?.encodedHeight ?? self.encoder.height)
            self.stats.update(seq) {
                $0.encodeEnd = t
                $0.sent = t
                $0.bytes = data.count
                $0.key = key
                $0.width = w
                $0.height = h
                echo = $0.echo
            }
            // Header: flags (1 = keyframe), pts µs, sequence number, and the
            // input this frame is the first reply to; all big-endian.
            var msg = Data(capacity: data.count + 37)
            msg.append((key ? 1 : 0) | (layout == nil ? 0 : 0x80))
            var us = UInt64(max(0, CMTimeGetSeconds(pts)) * 1_000_000).bigEndian
            msg.append(Data(bytes: &us, count: 8))
            var s = seq.bigEndian, e = echo.bigEndian
            msg.append(Data(bytes: &s, count: 4))
            msg.append(Data(bytes: &e, count: 4))
            if let layout { msg.append(layout.wire) }
            msg.append(data)
            let stats = self.stats
            self.ws.sendBinary(msg) { stats.update(seq) { $0.wire = nowUs() } }
        }
        encoder.onDone = { [weak self] _ in self?.finished() }
        encoder.onError = { [weak self] message in self?.ws.sendJSON(["t": "error", "message": message]) }
        capture.onFrame = { [weak self] pb, pts, shown in self?.offer(pb, pts: pts, capture: shown) }
        if let direct = capture as? DirectSCKSource {
            direct.selectLayout = { [weak self] w,h in
                guard let self else { return (.makeDirect(w,h,u:0.5,v:0.5),false) }
                self.lock.lock(); defer { self.lock.unlock() }
                let layout = self.gaze.layout(width:w,height:h,now:nowUs(),direct:true)
                return (layout,self.gaze.active)
            }
            direct.onDirectFrame = { [weak self] pb,pts,shown,layout,epoch in self?.offer(pb,pts:pts,capture:shown,layout:layout,directEpoch:epoch) }
        }
        capture.onEnded = { [weak self] reason in
            guard let self else { return }
            self.ws.sendJSON(["t": "closed", "reason": reason])
            self.onFinished?(self.source.key)
            self.end()
        }
        ws.onDrain = { [weak self] in self?.resendIfRoom() }
        ws.onText = { [weak self] text in self?.handle(text) }
        ws.onClose = { [weak self] in self?.end() }
        ws.start()
        // Reconnect with this (kept in the page's memory, never on a command line).
        var hello: [String: Any] = ["t": "hello", "r": reconnectKey]
        if foveation { hello["foveation"] = 1; hello["gazeSession"] = gazeSession }
        if foveationAtlas { hello["atlas"] = 1 }
        // Direct capability is advertised only after both actual SCK streams
        // have supplied validated buffers and the first composition succeeded.
        if !foveationDirect { ws.sendJSON(hello) }
        capture.onChange = { [weak self] in self?.sendInfo() }
        // The numbers, for the viewer's overlay.
        // Adapt ten times a second; the numbers go to the viewer once a second.
        let t = DispatchSource.makeTimerSource(queue: encodeQueue)
        t.schedule(deadline: .now() + 0.1, repeating: 0.1)
        var ticks = 0
        t.setEventHandler { [weak self] in
            guard let self, !self.isStopped else { return }
            self.adapt(maxLong: maxLong)
            ticks += 1
            guard ticks % 10 == 0 else { return }
            var s = self.stats.summary()
            s.merge(self.controller.state()) { a, _ in a }
            s["t"] = "stats"
            s["bitrate"] = self.encoder.bitrate
            s["size"] = "\(self.encoder.width)×\(self.encoder.height)"
            self.ws.sendJSON(s)
        }
        t.resume()
        statsTimer = t
        if foveation && !foveationDirect {
            // ScreenCaptureKit need not emit a new picture on a static desktop.
            // Repack its newest picture when gaze moves, or restore full quality
            // when gaze expires, without keeping any additional picture queue.
            let refresh = DispatchSource.makeTimerSource(queue: encodeQueue)
            refresh.schedule(deadline: .now(), repeating: 1.0 / Double(min(fps, 60)))
            refresh.setEventHandler { [weak self] in
                guard let self, !self.packingFailed, let previous = self.submittedLayout else { return }
                self.lock.lock()
                if !self.stopped, let p = self.last {
                    let now = nowUs()
                    let next = self.gaze.layout(width: CVPixelBufferGetWidth(p.pb), height: CVPixelBufferGetHeight(p.pb), now: now, atlas: self.foveationAtlas)
                    // Healthy capture carries the newest gaze layout itself.
                    // Repacking just before its next arrival spends a paced
                    // slot on old content. Two current frame periods without
                    // delivery distinguish a quiet desktop from normal jitter.
                    let captureQuiet = now - p.arrived > Int64(2_000_000 / self.controller.fps)
                    let restoreFullQuality = previous.warped && !next.warped
                    if next != previous && (captureQuiet || restoreFullQuality) { self.skipped = true }
                }
                self.lock.unlock()
                self.resendIfRoom()
            }
            refresh.resume(); gazeTimer = refresh
        }
        capture.start(maxLong: maxLong, fps: appliedCaptureFps) { [weak self] error in
            guard let self else { return }
            if let error {
                if self.capture.gone {
                    // Final, like a window closing mid-stream: the viewer closes
                    // and its key is revoked, rather than retrying for ever.
                    self.ws.sendJSON(["t": "closed", "reason": error])
                    self.onFinished?(self.source.key)
                } else {
                    self.ws.sendJSON(["t": "error", "message": error])
                }
                self.end()
                return
            }
            if self.foveationDirect {
                var directHello = hello
                directHello["direct"] = 1
                self.ws.sendJSON(directHello)
            }
            self.sendInfo()
        }
    }

    /// Applies the controller's bitrate and tier. On the encoding queue.
    private func adapt(maxLong: Int) {
        let now = nowUs()
        if let bps = controller.update(now: now) { encoder.setBitrate(bps) }
        let scale = controller.scale
        if !foveationDirect && scale != appliedScale {
            appliedScale = scale
            capture.setMaxLong(max(320, Int(Double(maxLong) * scale)))
        }
        let priority = controller.priority()
        if let previous = appliedPriorityRole, previous != priority.role {
            // A priority switch resumes a fresh reference frame before the
            // viewer receives frames at the restored or capped rate.
            encoder.requestKeyFrame()
        }
        appliedPriorityRole = priority.role
        let captureFps = min(controller.maxFps, priority.fpsCap ?? controller.maxFps, testCaptureFps ?? controller.maxFps)
        if captureFps != appliedCaptureFps {
            appliedCaptureFps = captureFps
            capture.setMaxFps(captureFps)
        }
        resendIfRoom()  // a lower tier may let a held frame go now
    }

    /// While someone is typing or clicking, the viewer sends a tiny message this
    /// often (ms, 0 = off), so the Frame's Wi-Fi doesn't doze between the input
    /// and the frame that answers it (power saving is on there).
    static let warmMs = Int(ProcessInfo.processInfo.environment["FRAME_MAC_VIEW_WARM"] ?? "") ?? 0

    func sendInfo() {
        ws.sendJSON(["t": "info", "src": source.key, "title": capture.title, "app": capture.app, "codec": codec.rawValue,
                     "input": source == .test || Input.allowed, "warm": Session.warmMs,
                     "aspect": Double(capture.frameRect.width / max(capture.frameRect.height, 1))])
    }

    var isStopped: Bool { lock.lock(); defer { lock.unlock() }; return stopped }

    func end() {
        lock.lock()
        let was = stopped
        stopped = true
        last = nil
        lock.unlock()
        guard !was else { return }
        activity.unregister(id)
        statsTimer?.cancel()
        gazeTimer?.cancel()
        capture.stop()
        encodeQueue.async { self.encoder.invalidate() }
        let owner = id
        DispatchQueue.main.async { Input.releaseAll(owner: owner) }
        ws.close()
        onEnd?(self)
    }

    /// A point in the picture (0...1 each way) -> Mac global coordinates.
    private func point(_ x: Double, _ y: Double) -> CGPoint {
        let r = capture.frameRect
        return CGPoint(x: r.minX + min(max(x, 0), 1) * r.width, y: r.minY + min(max(y, 0), 1) * r.height)
    }

    /// Input with an id (from the viewer) has been posted: the next picture
    /// the Mac composites is its first chance to show.
    private func injected(_ m: [String: Any], kind: String) {
        guard let iid = (m["i"] as? NSNumber)?.uint32Value, iid != 0 else { return }
        let now = nowUs()
        stats.addInput(InputRecord(id: iid, kind: kind, viewer: Int64(num(m["tv"]) ?? 0), injected: now))
        lock.lock(); pendingEcho = (iid, now); lock.unlock()
    }

    /// Timing reports from the viewer, in the agent's clock.
    private func report(_ t: String, _ m: [String: Any]) -> Bool {
        switch t {
        case "ping":
            ws.sendJSON(["t": "pong", "c": m["c"] ?? 0, "a": nowUs()])
        case "rx":
            guard let s = (m["s"] as? NSNumber)?.uint32Value else { break }
            if let r = num(m["r"]) { stats.update(s) { $0.received = Int64(r) } }
            if controller.acked(seq: s, at: nowUs()) { resendIfRoom() }
        case "fd":
            for f in m["f"] as? [[NSNumber]] ?? [] where f.count >= 4 {
                stats.update(f[0].uint32Value) {
                    $0.decoded = f[1].int64Value
                    $0.drawn = f[2].int64Value
                    $0.vsync = f[3].int64Value
                }
            }
            let dropped = Int(num(m["drop"]) ?? 0)
            stats.withLock { stats.viewerDropped += dropped }
        case "w":
            break  // keep-warm filler, see sendInfo
        case "clock":
            stats.withLock {
                stats.rtt = num(m["rtt"]) ?? 0
                stats.clockSynced = true
                if let d = m["dec"] as? String { stats.decoder = d }
            }
        default:
            return false
        }
        return true
    }

    // Authenticated HTTP stats only; no diagnostics are added to viewer UI.
    func streamSummary() -> [String: Any] {
        var result = stats.summary()
        if foveation {
            lock.lock(); result["gaze"] = gaze.diagnostics; lock.unlock()
        }
        if let direct = capture as? DirectSCKSource { result["directCapture"] = direct.diagnostics }
        return result
    }

    /// Asks the viewer to do something for a benchmark (type, click, show its overlay).
    func bench(_ m: [String: Any]) { ws.sendJSON(m.merging(["t": "bench"]) { a, _ in a }) }

    private func handle(_ text: String) {
        guard !isStopped, let d = text.data(using: .utf8),
              let m = try? JSONSerialization.jsonObject(with: d) as? [String: Any], let t = m["t"] as? String else { return }
        if t == "gaze" {
            guard foveation else { return }
            lock.lock(); gaze.receive(m, session: gazeSession, now: nowUs(), allowDiagnostic: allowDiagnosticGaze); lock.unlock()
            return
        }
        if report(t, m) { return }
        activity.recordMessage(sessionID: id, type: t)
        let x = m["x"] as? Double ?? -1, y = m["y"] as? Double ?? -1
        if t == "ack" {
            onAck?()
            onAck = nil
            return
        }
        if t == "key-frame" {
            encodeQueue.async { self.encoder.requestKeyFrame() }  // before the resend, same queue
            lock.lock(); skipped = last != nil; lock.unlock()
            resendIfRoom()
            return
        }
        if source == .test {
            let desc: String?
            switch t {
            case "m": desc = (m["e"] as? String) == "move" ? nil : "mouse \(m["e"] ?? "") button \(m["b"] ?? 0)"
            case "wheel": desc = "wheel \(m["dx"] ?? 0), \(m["dy"] ?? 0)"
            case "k": desc = (m["e"] as? String) == "down" ? "key \(m["code"] ?? "") \"\(m["key"] ?? "")\"" : nil
            case "text": desc = "text \"\(m["s"] ?? "")\""
            default: desc = nil
            }
            capture.pointer(x: x, y: y, text: desc)
            if desc != nil { injected(m, kind: t) }
            return
        }
        DispatchQueue.main.async { [self] in
            guard !isStopped else { return }  // Stop revokes input at once
            switch t {
            case "m":
                let kind = m["e"] as? String ?? "move", b = m["b"] as? Int ?? 0
                let p = point(x, y)
                if kind == "down", case .window(let wid) = source, let pid = capture.pid {
                    Input.focus(window: wid, pid: pid)
                }
                Input.mouse(kind, button: b, at: p, owner: id)
                if kind == "down" { injected(m, kind: "click") }
            case "wheel":
                Input.scroll(dx: m["dx"] as? Double ?? 0, dy: m["dy"] as? Double ?? 0, at: point(x, y), owner: id)
                injected(m, kind: "wheel")
            case "k":
                let down = (m["e"] as? String) == "down"
                Input.key(code: m["code"] as? String ?? "", key: m["key"] as? String ?? "",
                          down: down, mods: m["mods"] as? [String] ?? [], owner: id)
                if down { injected(m, kind: "key") }
            case "text":
                if let s = m["s"] as? String, s.count <= 4096 { Input.text(s); injected(m, kind: "text") }
            case "release":
                Input.releaseAll(owner: id)
            case "focus":
                if case .window(let wid) = source, let pid = capture.pid { Input.focus(window: wid, pid: pid) }
            default: break
            }
        }
    }
}

func randomKey() -> String {
    var bytes = [UInt8](repeating: 0, count: 24)
    _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
    return Data(bytes).base64EncodedString().replacingOccurrences(of: "+", with: "-")
        .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
}

func sameSecret(_ a: String, _ b: String) -> Bool {
    guard !a.isEmpty, a.utf8.count == b.utf8.count else { return false }
    return zip(a.utf8, b.utf8).reduce(0, { $0 | ($1.0 ^ $1.1) }) == 0
}

final class Agent {
    /// Frame Control's own key. It stays on the Mac: the Frame only ever sees
    /// single-use tickets and per-stream reconnect keys, both tied to one source.
    let token: String
    let page: URL?
    var sessions: [Int: Session] = [:] { didSet { keepDisplayAwake(!sessions.isEmpty) } }
    var nextId = 1
    let lock = NSLock()
    private let activity = StreamActivityRegistry()
    private var assertion: IOPMAssertionID = 0
    /// ticket -> (source, expiry, reconnect key once redeemed). A ticket opens
    /// one viewer within a minute; it may be redeemed again, for the same key,
    /// only until that viewer confirms it has the key ("ack").
    private var tickets: [String: (src: String, expiry: Date, key: String?)] = [:]
    /// reconnect key -> source, until Stop for that source.
    private var reconnectKeys: [String: String] = [:]
    /// Sources that ended for good (the window closed), for Frame Control.
    private var finished = Set<String>()

    /// Ends every stream, then exits once separated windows are back on the
    /// Mac's own screens (they're moved back, then their displays go 0.3 s later).
    /// Idempotent: SIGTERM and the parent going away can both call it, and a
    /// session already ended by /close may still be putting its window back.
    private var quitting = false
    func quit() {
        lock.lock()
        let all = Array(sessions.values), again = quitting
        quitting = true
        lock.unlock()
        guard !again else { return }
        for s in all { s.ws.sendJSON(["t": "close"]); s.end() }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { exit(0) }
    }

    /// While anyone watches, keep the Mac's display on: a sleeping display
    /// stops being drawn, so there'd be nothing to capture (and it would lock).
    private func keepDisplayAwake(_ on: Bool) {
        if on, assertion == 0 {
            IOPMAssertionCreateWithName(kIOPMAssertionTypePreventUserIdleDisplaySleep as CFString,
                                        IOPMAssertionLevel(kIOPMAssertionLevelOn),
                                        "Frame Control is showing this Mac in a Steam Frame" as CFString, &assertion)
        } else if !on, assertion != 0 {
            IOPMAssertionRelease(assertion)
            assertion = 0
        }
    }

    init(token: String, page: URL?) {
        self.token = token
        self.page = page
    }

    /// Whether this request may open a stream of `src`: Frame Control's key,
    /// an unused ticket for that source, or a live reconnect key for it.
    private func mayStream(_ req: Request, src: String) -> String? {
        lock.lock()
        defer { lock.unlock() }
        let now = Date()
        tickets = tickets.filter { $0.value.expiry > now }
        if sameSecret(req.query["k"] ?? "", token) { return randomKey() }
        if let t = req.query["t"], let entry = tickets[t], entry.src == src {
            // A retry before the viewer got its key gets the same key back.
            let key = entry.key ?? randomKey()
            tickets[t] = (src, entry.expiry, key)
            reconnectKeys[key] = src
            return key
        }
        if let r = req.query["r"], reconnectKeys[r] == src { return r }
        return nil
    }

    /// The viewer has its reconnect key, so the ticket that led to it can't
    /// be used again (also when the ack comes over a reconnection).
    func acknowledged(key: String) {
        lock.lock(); tickets = tickets.filter { $0.value.key != key }; lock.unlock()
    }

    func handle(_ req: Request, _ c: HTTPConnection) {
        // Open to anything that reaches the port: a liveness check for the
        // tunnel, and the viewer page, which holds no secrets.
        switch (req.method, req.path) {
        case ("GET", "/ping"): return c.respond(200, text: "frame-mac-view")
        case ("GET", "/view"):
            guard let page, let body = try? Data(contentsOf: page) else { return c.respond(404, text: "no viewer page") }
            return c.respond(200, body: body, type: "text/html; charset=utf-8")
        case ("GET", "/stream"):
            guard let src = Source(req.query["src"] ?? "") else { return c.respond(400, text: "bad src") }
            guard let key = mayStream(req, src: src.key) else { return c.respond(403, text: "forbidden") }
            return stream(req, c, src: src, key: key)
        default: break
        }
        guard sameSecret(req.query["k"] ?? req.headers["x-token"] ?? "", token) else {
            return c.respond(403, text: "forbidden")
        }
        switch (req.method, req.path) {
        case ("GET", "/status"):
            lock.lock()
            let list = sessions.values.map { s -> [String: Any] in
                var e: [String: Any] = ["id": s.id, "src": s.source.key, "title": s.capture.title, "app": s.capture.app,
                                        "stats": s.streamSummary(), "bitrate": s.encoder.bitrate,
                                        "controller": s.controller.state()]
                if let d = (s.capture as? SeparateSource)?.displayID { e["display"] = d }
                return e
            }
            lock.unlock()
            var s = permissionsJSON()
            s["version"] = version
            s["streams"] = list
            lock.lock(); s["finished"] = Array(finished); lock.unlock()
            c.respond(json: s)
        case ("GET", "/windows"):
            c.respond(json: ["windows": windowsJSON(), "screen": CGPreflightScreenCaptureAccess()])
        case ("GET", "/displays"):
            c.respond(json: ["displays": displaysJSON()])
        case ("POST", "/ticket"):
            guard let src = Source(req.query["src"] ?? "") else { return c.respond(400, text: "bad src") }
            let t = randomKey()
            lock.lock()
            tickets[t] = (src.key, Date().addingTimeInterval(60), nil)
            finished.remove(src.key)
            lock.unlock()
            c.respond(json: ["ticket": t])
        case ("POST", "/close"):
            // Tell viewers to close their windows, but don't rely on them:
            // the sessions end here and can't reconnect.
            let src = req.query["src"]
            lock.lock()
            let matching = sessions.values.filter { src == nil || $0.source.key == src }
            reconnectKeys = reconnectKeys.filter { src != nil && $0.value != src }
            tickets = tickets.filter { src != nil && $0.value.src != src }  // not yet used ones too
            lock.unlock()
            for s in matching { s.ws.sendJSON(["t": "close"]) }
            DispatchQueue.global().asyncAfter(deadline: .now() + 0.3) { for s in matching { s.end() } }
            c.respond(json: ["closed": matching.count])
        case ("GET", "/stats"):
            // Frames the viewer has had time to report on, oldest first.
            let since = UInt32(req.query["since"] ?? "") ?? 0
            let settle = Int64(req.query["settle"] ?? "") ?? 1_500_000
            lock.lock()
            let list = sessions.values.filter { req.query["id"] == nil || "\($0.id)" == req.query["id"] }
            lock.unlock()
            c.respond(json: ["now": nowUs(), "streams": list.map { s -> [String: Any] in
                ["id": s.id, "src": s.source.key, "frames": s.stats.settled(since: since, settle: settle).map(\.json),
                 "inputs": s.stats.inputList().map(\.json), "summary": s.streamSummary(),
                 "captured": s.stats.withLock { s.stats.captured }, "controller": s.controller.state(),
                 "events": s.controller.eventList()]
            }])
        case ("POST", "/bench"):
            lock.lock()
            let list = sessions.values.filter { $0.source.key == req.query["src"] }
            lock.unlock()
            var m: [String: Any] = [:]
            for (k, v) in req.query where k != "k" && k != "src" { m[k] = Double(v) ?? v as Any }
            for s in list { s.bench(m) }
            c.respond(json: ["sent": list.count])
        case ("GET", "/snapshot"):
            // One JPEG of a display (for checks and thumbnails): ?display=ID
            guard let id = UInt32(req.query["display"] ?? "") else { return c.respond(400, text: "display=ID") }
            snapshot(display: id) { data, error in
                if let data { c.respond(200, body: data, type: "image/jpeg") } else { c.respond(500, text: error ?? "failed") }
            }
        case ("POST", "/permissions"):
            DispatchQueue.main.async { requestPermissions() }
            c.respond(json: permissionsJSON())
        default:
            c.respond(404, text: "not found")
        }
    }

    private func stream(_ req: Request, _ c: HTTPConnection, src: Source, key: String) {
        let codec = Codec(rawValue: req.query["codec"] ?? "h264") ?? .h264
        let maxLong = min(max(Int(req.query["max"] ?? "") ?? 1920, 320), 4096)
        let fps = min(max(Int(req.query["fps"] ?? "") ?? 60, 5), 120)
        let bpp = min(max(Double(req.query["bpp"] ?? "") ?? 0.1, 0.02), 0.5)
        guard let ws = c.upgrade(req) else { return }
        let wantsDirect = req.query["direct"] == "1"
        let foveation = codec == .h264 && req.query["foveation"] == "1" && ProcessInfo.processInfo.environment["FRAME_WORKSPACE_FOVEATION"] == "1"
        if wantsDirect {
            guard foveation, case .display = src else {
                ws.start()
                ws.sendJSON(["t":"error","message":"direct foveated capture requires an enabled H264 display source; independent windows, Separate and test sources are unsupported"])
                ws.close()
                return
            }
        }
        let capture: CaptureSource
        switch src {
        case .display(let id) where wantsDirect: capture = DirectSCKSource(displayID:id)
        case .test: capture = TestSource()
        case .separate(let id): capture = SeparateSource(windowID: id)
        default: capture = SCKSource(src)
        }
        lock.lock()
        // One live viewer per key: a reconnection (or a second use of an
        // unacknowledged ticket) replaces the one before.
        let replaced = sessions.values.filter { $0.reconnectKey == key }
        let session = Session(id: nextId, source: src, capture: capture, ws: ws, codec: codec, fps: fps,
                              bitsPerPixel: bpp, reconnectKey: key, activity: activity,
                              foveation: foveation,
                              foveationAtlas: !wantsDirect && req.query["atlas"] == "1", foveationDirect: wantsDirect)
        nextId += 1
        sessions[session.id] = session
        lock.unlock()
        for old in replaced { old.end() }
        session.onEnd = { [weak self] s in
            guard let self else { return }
            self.lock.lock(); self.sessions[s.id] = nil; self.lock.unlock()
        }
        // The source is gone (its window closed): its viewers can't come back.
        session.onFinished = { [weak self] src in
            guard let self else { return }
            self.lock.lock()
            self.finished.insert(src)
            self.reconnectKeys = self.reconnectKeys.filter { $0.value != src }
            self.lock.unlock()
        }
        session.onAck = { [weak self] in self?.acknowledged(key: key) }
        session.start(maxLong: maxLong, fps: fps)
    }
}

func snapshot(display id: CGDirectDisplayID, completion: @escaping (Data?, String?) -> Void) {
    SCShareableContent.getExcludingDesktopWindows(false, onScreenWindowsOnly: true) { content, error in
        guard let d = content?.displays.first(where: { $0.displayID == id }) else {
            return completion(nil, error?.localizedDescription ?? "no such display")
        }
        let filter = SCContentFilter(display: d, excludingWindows: [])
        let cfg = SCStreamConfiguration()
        cfg.width = Int(Double(d.width) * Double(filter.pointPixelScale))
        cfg.height = Int(Double(d.height) * Double(filter.pointPixelScale))
        cfg.showsCursor = true
        SCScreenshotManager.captureImage(contentFilter: filter, configuration: cfg) { image, error in
            guard let image else { return completion(nil, error?.localizedDescription ?? "no image") }
            let rep = NSBitmapImageRep(cgImage: image)
            completion(rep.representation(using: .jpeg, properties: [.compressionFactor: 0.85]), nil)
        }
    }
}

func requestPermissions() {
    if !CGPreflightScreenCaptureAccess() { CGRequestScreenCaptureAccess() }
    if !AXIsProcessTrusted() {
        AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary)
    }
}

func argument(_ name: String, in args: [String]) -> String? {
    guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
    return args[i + 1]
}

let args = Array(CommandLine.arguments.dropFirst())
switch args.first {
case "windows":
    printJSON(["windows": windowsJSON(), "screen": CGPreflightScreenCaptureAccess()])
case "displays":
    printJSON(["displays": displaysJSON()])
case "permissions":
    printJSON(permissionsJSON())
case "request-permissions":
    requestPermissions()
    printJSON(permissionsJSON())
case "workspace":
    let workspaceArgs = Array(args.dropFirst())
    if workspaceArgs.contains("--help") {
        print(WorkspaceOptions.usage)
        exit(0)
    }
    let options: WorkspaceOptions
    do {
        options = try WorkspaceOptions(arguments: workspaceArgs)
    } catch {
        printJSONLine(["state": "error", "error": String(describing: error), "usage": WorkspaceOptions.usage])
        exit(2)
    }
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    let workspace = WorkspaceCommand(options: options, app: app)
    signal(SIGINT, SIG_IGN)
    signal(SIGTERM, SIG_IGN)
    let sigint = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
    let sigterm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
    sigint.setEventHandler { workspace.stop(reason: "SIGINT") }
    sigterm.setEventHandler { workspace.stop(reason: "SIGTERM") }
    sigint.resume()
    sigterm.resume()
    if options.exitOnStdinEOF {
        DispatchQueue.global(qos: .utility).async {
            while !FileHandle.standardInput.availableData.isEmpty {}
            DispatchQueue.main.async { workspace.stop(reason: "stdin_eof") }
        }
    }
    DispatchQueue.main.async { workspace.start() }
    withExtendedLifetime((workspace, sigint, sigterm)) { app.run() }
    exit(workspace.exitStatus)
case "serve":
    let port = UInt16(argument("--port", in: args) ?? "") ?? 0
    let token = ProcessInfo.processInfo.environment["FRAME_MAC_VIEW_TOKEN"] ?? ""
    guard !token.isEmpty else {
        FileHandle.standardError.write(Data("frame-mac-view: set FRAME_MAC_VIEW_TOKEN\n".utf8))
        exit(2)
    }
    let page = argument("--page", in: args).map { URL(fileURLWithPath: $0) }
    let agent = Agent(token: token, page: page)
    // Quit when the parent goes away (it holds our stdin open), or on SIGTERM,
    // but first end every stream, so separated windows go back where they
    // were before their displays disappear.
    if args.contains("--exit-on-eof") {
        DispatchQueue.global().async {
            while FileHandle.standardInput.availableData.count > 0 {}
            agent.quit()
        }
    }
    signal(SIGTERM, SIG_IGN)
    let sigterm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
    sigterm.setEventHandler { agent.quit() }
    sigterm.resume()
    let server: Server
    do {
        server = try Server(port: port) { req, c in agent.handle(req, c) }
    } catch {
        FileHandle.standardError.write(Data("frame-mac-view: \(error)\n".utf8))
        exit(1)
    }
    server.start { [server] error in
        if let error {
            FileHandle.standardError.write(Data("frame-mac-view: can't listen on 127.0.0.1:\(port): \(error)\n".utf8))
            exit(1)
        }
        print("frame-mac-view listening on 127.0.0.1:\(server.port ?? port)")
        fflush(stdout)
    }
    // A real (background, no Dock icon) AppKit event loop, not just a run
    // loop: without it this process never hears that displays were added or
    // changed mode, so NSScreen and CGDisplayCopyDisplayMode stay stale for
    // the displays Separate mode creates.
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    withExtendedLifetime((server, sigterm)) { app.run() }
default:
    FileHandle.standardError.write(Data("usage: frame-mac-view serve|workspace|windows|displays|permissions|request-permissions\n".utf8))
    exit(2)
}
