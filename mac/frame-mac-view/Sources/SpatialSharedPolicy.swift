import Foundation
import CoreGraphics

/// Geometry decisions shared by capture, input and fixture checks.
enum SpatialSharedPolicy {
    static func sharedFrame(size: CGSize, display: CGRect, menuBar: CGRect) -> CGRect {
        let top = menuBar.intersects(display) ? max(display.minY, menuBar.maxY) : display.minY
        return CGRect(x: display.minX, y: top, width: min(max(1, size.width), display.width),
                      height: min(max(1, size.height), max(1, display.maxY - top)))
    }

    static func menuBarSplit(statusItemFrames: [CGRect]) -> CGFloat? {
        statusItemFrames.filter { !$0.isEmpty }.map(\.minX).min()
    }

    static func isMenuBarSurface(frame: CGRect, layer: Int, owner: String, menuBarRect: CGRect) -> Bool {
        guard !frame.isEmpty, frame.intersects(CGRect(x: menuBarRect.minX, y: menuBarRect.minY,
                                                     width: menuBarRect.width, height: 3000)) else { return false }
        if layer == 101 { return abs(frame.minY - menuBarRect.maxY) <= 8 }
        return layer == 22 && ["control center", "control centre"].contains(owner.lowercased())
    }

    /// BGRA pixels in top-left row order; padding bytes are never inspected.
    static func alphaBounds(bgra: [UInt8], width: Int, height: Int, bytesPerRow: Int? = nil) -> CGRect? {
        let stride = bytesPerRow ?? width * 4
        guard width > 0, height > 0, stride >= width * 4, bgra.count >= stride * height else { return nil }
        var left = width, top = height, right = -1, bottom = -1
        for y in 0..<height {
            for x in 0..<width where bgra[y * stride + x * 4 + 3] >= 250 {
                left = min(left, x); top = min(top, y); right = max(right, x); bottom = max(bottom, y)
            }
        }
        guard right >= left else { return nil }
        return CGRect(x: left, y: top, width: right - left + 1, height: bottom - top + 1)
    }

    static func panelToGlobal(u: Double, v: Double, captureRect: CGRect) -> CGPoint {
        CGPoint(x: captureRect.minX + min(max(u, 0), 1) * captureRect.width,
                y: captureRect.minY + min(max(v, 0), 1) * captureRect.height)
    }

    static func menuBarDisplay(shared: UInt32, builtIn: UInt32, separateSpaces: Bool) -> UInt32 {
        separateSpaces ? shared : builtIn
    }

    static func displaySize(_ raw: String?) -> CGSize {
        let parts = (raw ?? "").lowercased().split(separator: "x").compactMap { Int($0) }
        guard parts.count == 2, (640...8192).contains(parts[0]), (480...8192).contains(parts[1]) else {
            return CGSize(width: 3600, height: 2250)
        }
        return CGSize(width: parts[0], height: parts[1])
    }

    static func rectJSON(_ rect: CGRect) -> [Double] {
        [Double(rect.minX), Double(rect.minY), Double(rect.width), Double(rect.height)]
    }
}
