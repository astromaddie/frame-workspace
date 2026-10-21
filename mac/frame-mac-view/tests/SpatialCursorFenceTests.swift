import Foundation

@main
private enum SpatialCursorFenceTests {
    static func main() {
        let window = CGRect(x: 100, y: 136, width: 640, height: 400)
        let bar = CGRect(x: 100, y: 100, width: 3600, height: 36)
        let menu = CGRect(x: 900, y: 136, width: 150, height: 300)
        let allowed = [window, bar, menu]
        precondition(SpatialCursorFence.confined(CGPoint(x: 200, y: 200), to: allowed) == CGPoint(x: 200, y: 200))
        precondition(SpatialCursorFence.confined(CGPoint(x: 200, y: 120), to: allowed) == CGPoint(x: 200, y: 120))
        precondition(SpatialCursorFence.confined(CGPoint(x: 950, y: 200), to: allowed) == CGPoint(x: 950, y: 200))
        precondition(SpatialCursorFence.confined(CGPoint(x: 750, y: 500), to: allowed) == CGPoint(x: 739.5, y: 500))
        precondition(SpatialCursorFence.confined(CGPoint(x: 10, y: 10), to: []) == CGPoint(x: 10, y: 10))
        print("SpatialCursorFence: 5 window, strip, menu and outside-pointer checks passed")
    }
}
