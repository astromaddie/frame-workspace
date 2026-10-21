// FW-15 option B spike: one ScreenCaptureKit stream per window of a process (child windows included),
// counting frames; run beside SpatialFixture.swift and watch CPU (ps) for the probe and WindowServer.
//   /tmp/many <fixture pid> <seconds>
import AppKit
import ScreenCaptureKit
let nsApp = NSApplication.shared
let pid = pid_t(CommandLine.arguments[1])!, seconds = Double(CommandLine.arguments[2]) ?? 10
final class Out: NSObject, SCStreamOutput { var frames = 0
  func stream(_ s: SCStream, didOutputSampleBuffer b: CMSampleBuffer, of t: SCStreamOutputType) { frames += 1 } }
var streams: [(SCStream, Out)] = []
Task {
  let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
  let wins = content.windows.filter { $0.owningApplication?.processID == pid && $0.frame.width > 100 }
  for w in wins {
    let cfg = SCStreamConfiguration(); cfg.width = Int(w.frame.width * 2); cfg.height = Int(w.frame.height * 2)
    cfg.minimumFrameInterval = CMTime(value: 1, timescale: 30); cfg.showsCursor = false
    if #available(macOS 14.2, *) { cfg.includeChildWindows = true }
    let s = SCStream(filter: SCContentFilter(desktopIndependentWindow: w), configuration: cfg, delegate: nil)
    let o = Out(); try s.addStreamOutput(o, type: .screen, sampleHandlerQueue: .global()); try await s.startCapture()
    streams.append((s, o))
  }
  print("capturing \(streams.count) windows"); fflush(stdout)
  try await Task.sleep(nanoseconds: UInt64(seconds * 1e9))
  print("frames per window:", streams.map { $0.1.frames }); fflush(stdout)
  exit(0)
}
RunLoop.main.run()
