import Foundation

/// The identity of the window selected when a capture starts. Window numbers
/// can be reused, so a known owner PID is part of the identity as well.
struct WindowIdentity: Equatable {
    let windowID: UInt32
    let processID: Int32?

    func matches(_ candidate: WindowIdentity?) -> Bool {
        guard let candidate, candidate.windowID == windowID else { return false }
        guard let processID else { return true }
        return candidate.processID == processID
    }
}

/// Debounces temporary omissions from WindowServer while requiring each
/// terminal-close observation to come from a fresh ScreenCaptureKit listing.
/// The caller supplies both the fresh CGWindowList result and the corresponding
/// SCShareableContent result, keeping this policy independent of the OS APIs.
struct WindowCloseRecovery {
    enum Decision: Equatable {
        case present
        case waiting
        case closed
        case cancelled
    }

    private let target: WindowIdentity
    private let grace: TimeInterval
    private var missingSince: TimeInterval?
    private var isCancelled = false

    init(target: WindowIdentity, grace: TimeInterval = 5) {
        self.target = target
        self.grace = max(0, grace)
    }

    mutating func cancel() {
        isCancelled = true
    }

    mutating func observe(
        cgWindow: WindowIdentity?,
        shareableWindow: WindowIdentity?,
        shareableContentFresh: Bool,
        at time: TimeInterval
    ) -> Decision {
        guard !isCancelled else { return .cancelled }

        // Either independent source can prove that this exact window is still
        // live. In particular, ScreenCaptureKit retains fullscreen windows
        // across a Space transition when CGWindowList briefly omits them.
        if target.matches(cgWindow) || (shareableContentFresh && target.matches(shareableWindow)) {
            missingSince = nil
            return .present
        }

        // A failed/stale ScreenCaptureKit lookup is not evidence of closure.
        guard shareableContentFresh else { return .waiting }

        guard let start = missingSince else {
            missingSince = time
            return grace == 0 ? .closed : .waiting
        }
        return time - start >= grace ? .closed : .waiting
    }
}
