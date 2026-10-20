import Foundation
import CoreGraphics

/// Bookkeeping for the bounded virtual-display pool. WindowServer objects stay
/// alive for the whole spatial session; releasing a lease only makes its slot
/// available for another window.
struct SpatialPoolPolicy {
    static let initialPoolSize = 3
    static let warmSpareCount = 3
    /// Safety ceiling for active displays plus bounded idle/failed cache slots.
    static let maximumPoolSize = 128

    enum Assignment: Equatable {
        case lease(slot: Int)
        case create(slot: Int)
        case alreadyLeased(slot: Int)
        case alreadyPreparing(slot: Int)
        case full
    }

    private enum SlotState {
        case free
        case creating(UUID?)
        case leased(UUID)
        case failed
    }

    let capacity: Int
    private var states: [Int: SlotState] = [:]
    private var preparingOwners = Set<UUID>()

    init(capacity: Int) {
        self.capacity = max(0, capacity)
    }

    var slotCount: Int { states.count }
    var freeCount: Int { states.values.filter { if case .free = $0 { true } else { false } }.count }
    var leasedCount: Int { states.values.filter { if case .leased = $0 { true } else { false } }.count }
    var failedCount: Int { states.values.filter { if case .failed = $0 { true } else { false } }.count }
    var preparingCount: Int { preparingOwners.count }
    var backgroundPreparationCount: Int {
        states.values.filter { if case .creating(nil) = $0 { true } else { false } }.count
    }
    var outstandingCount: Int { leasedCount + preparingCount + backgroundPreparationCount }

    mutating func beginLeasePreparation(owner: UUID) -> Bool {
        preparingOwners.insert(owner).inserted
    }

    mutating func finishLeasePreparation(owner: UUID) -> Bool {
        preparingOwners.remove(owner) != nil
    }

    func leaseIsCurrent(slot: Int, owner: UUID) -> Bool {
        guard case .leased(let current)? = states[slot] else { return false }
        return current == owner
    }

    mutating func registerFree(slot: Int) -> Bool {
        guard slot >= 0, slot < capacity, states[slot] == nil else { return false }
        states[slot] = .free
        return true
    }

    mutating func acquire(owner: UUID) -> Assignment {
        if let (slot, _) = states.first(where: {
            if case .leased(let current) = $0.value { return current == owner }
            return false
        }) {
            return .alreadyLeased(slot: slot)
        }
        if let (slot, _) = states.first(where: {
            if case .creating(let current) = $0.value { return current == owner }
            return false
        }) {
            return .alreadyPreparing(slot: slot)
        }
        if let slot = states.keys.sorted().first(where: {
            if case .free = states[$0]! { return true }
            return false
        }) {
            states[slot] = .leased(owner)
            return .lease(slot: slot)
        }
        guard states.count < capacity else { return .full }
        let slot = (0..<capacity).first(where: { states[$0] == nil })!
        states[slot] = .creating(owner)
        return .create(slot: slot)
    }

    /// Reserve one background slot only when the ready idle cache is below its
    /// target. A single creating(nil) state serializes refill work.
    mutating func beginWarmRefill() -> Int? {
        guard freeCount < Self.warmSpareCount, backgroundPreparationCount == 0,
              states.count < capacity else { return nil }
        let slot = (0..<capacity).first(where: { states[$0] == nil })!
        states[slot] = .creating(nil)
        return slot
    }

    mutating func finishWarmRefill(slot: Int) -> Bool {
        guard case .creating(nil)? = states[slot] else { return false }
        states[slot] = .free
        return true
    }

    /// Failed cache entries remain bounded and are not retried indefinitely.
    mutating func failWarmRefill(slot: Int) -> Bool {
        guard case .creating(nil)? = states[slot] else { return false }
        states[slot] = .failed
        return true
    }

    mutating func finishCreation(slot: Int, owner: UUID) -> Bool {
        guard case .creating(let current)? = states[slot], current == owner else { return false }
        states[slot] = .leased(owner)
        return true
    }

    /// A failed creation consumes its bounded slot for this spatial session;
    /// repeated churn cannot turn a display failure into unbounded retries.
    mutating func failCreation(slot: Int, owner: UUID) -> Bool {
        guard case .creating(let current)? = states[slot], current == owner else { return false }
        states[slot] = .failed
        return true
    }

    mutating func release(slot: Int, owner: UUID) -> Bool {
        guard case .leased(let current)? = states[slot], current == owner else { return false }
        states[slot] = .free
        return true
    }
}

struct SpatialWindowCandidate {
    let layer: Int
    let width: Double
    let height: Double
    let pid: Int32
    let appName: String
    let isRegularApplication: Bool
    let isHelperProcess: Bool
    let isStandardWindow: Bool
    let isFullScreen: Bool
    let isMinimized: Bool
    let isApplicationHidden: Bool
}

struct SpatialLayoutDisplay: Equatable {
    let id: UInt32
    let width: Int
    let height: Int
    let pixelWidth: Int
    let pixelHeight: Int
    let refreshRate: Double
    let x: Double
    let y: Double
    let mirrorTarget: UInt32?
    let isMain: Bool
}

enum SpatialLayoutPolicy {
    static func hasExpectedDisplays(snapshotIDs: Set<UInt32>, onlineIDs: [UInt32],
                                    spatialDisplayIDs: Set<UInt32>) -> Bool {
        Set(onlineIDs.filter { !spatialDisplayIDs.contains($0) }) == snapshotIDs
    }

    static func isRestored(snapshot: [SpatialLayoutDisplay], current: [SpatialLayoutDisplay],
                           spatialDisplayIDs: Set<UInt32>, requireMain: Bool = true) -> Bool {
        guard hasExpectedDisplays(snapshotIDs: Set(snapshot.map(\.id)), onlineIDs: current.map(\.id),
                                   spatialDisplayIDs: spatialDisplayIDs) else { return false }
        let currentByID = Dictionary(uniqueKeysWithValues: current.map { ($0.id, $0) })
        return snapshot.allSatisfy { saved in
            guard let actual = currentByID[saved.id] else { return false }
            return actual.width == saved.width && actual.height == saved.height &&
                actual.pixelWidth == saved.pixelWidth && actual.pixelHeight == saved.pixelHeight &&
                abs(actual.refreshRate - saved.refreshRate) < 0.1 &&
                abs(actual.x - saved.x) <= 1 && abs(actual.y - saved.y) <= 1 &&
                actual.mirrorTarget == saved.mirrorTarget && (!requireMain || actual.isMain == saved.isMain)
        }
    }

    /// Only our own built-in-to-home mirror (or its already-unmirrored state)
    /// is allowed to differ while restoring the pre-session layout.
    static func permitsRestore(snapshot: [SpatialLayoutDisplay], current: [SpatialLayoutDisplay],
                               spatialDisplayIDs: Set<UInt32>, ownedMirrorSource: UInt32,
                               ownedMirrorTarget: UInt32?, knownSessionLayout: [SpatialLayoutDisplay]? = nil) -> Bool {
        guard hasExpectedDisplays(snapshotIDs: Set(snapshot.map(\.id)), onlineIDs: current.map(\.id),
                                   spatialDisplayIDs: spatialDisplayIDs) else { return false }
        let currentByID = Dictionary(uniqueKeysWithValues: current.map { ($0.id, $0) })
        let knownByID = knownSessionLayout.map { Dictionary(uniqueKeysWithValues: $0.map { ($0.id, $0) }) } ?? [:]
        return snapshot.allSatisfy { saved in
            guard let actual = currentByID[saved.id] else { return false }
            let mirrorIsOurs = saved.id == ownedMirrorSource && saved.mirrorTarget == nil &&
                (actual.mirrorTarget == nil || actual.mirrorTarget == ownedMirrorTarget)
            guard actual.mirrorTarget == saved.mirrorTarget || mirrorIsOurs else { return false }
            if actual == saved || knownByID[saved.id] == actual || mirrorIsOurs { return true }
            let sameMode = actual.width == saved.width && actual.height == saved.height &&
                actual.pixelWidth == saved.pixelWidth && actual.pixelHeight == saved.pixelHeight &&
                abs(actual.refreshRate - saved.refreshRate) < 0.1
            if saved.isMain && sameMode && actual.mirrorTarget == saved.mirrorTarget { return true }
            if actual.isMain != saved.isMain && sameMode && actual.mirrorTarget == saved.mirrorTarget &&
                abs(actual.x - saved.x) <= 1 && abs(actual.y - saved.y) <= 1 { return true }
            return actual == saved
        }
    }
}

enum SpatialWindowPolicy {
    private static let excludedNames: Set<String> = [
        "window server", "dock", "control centre", "control center", "notification centre",
        "notification center", "spotlight", "systemuiserver", "wallpaper", "windowmanager",
        "frame control", "frame-mac-view"
    ]

    static func isEligible(_ candidate: SpatialWindowCandidate) -> Bool {
        candidate.layer == 0 && candidate.width >= 120 && candidate.height >= 80 &&
            candidate.pid > 0 && candidate.isRegularApplication && !candidate.isHelperProcess &&
            candidate.isStandardWindow && !candidate.isFullScreen && !candidate.isMinimized &&
            !candidate.isApplicationHidden &&
            !excludedNames.contains(candidate.appName.lowercased())
    }

    /// Parks a stopped spatial window on the shared home display. A stable,
    /// small offset keeps successive windows from landing at identical origins
    /// while clamping guarantees the frame stays in the non-leased display.
    static func parkedFrame(windowID: UInt32, size: CGSize, homeRect: CGRect) -> CGRect? {
        guard homeRect.width > 0, homeRect.height > 0 else { return nil }
        let width = min(max(size.width, 1), homeRect.width)
        let height = min(max(size.height, 1), homeRect.height)
        let offset = CGFloat(windowID % 8) * 18
        let x = min(homeRect.minX + offset, homeRect.maxX - width)
        let y = min(homeRect.minY + offset, homeRect.maxY - height)
        return CGRect(x: max(homeRect.minX, x), y: max(homeRect.minY, y), width: width, height: height)
    }
}

enum SpatialCleanupGate {
    static func mayReleaseLease(windowParkedOnHome: Bool, windowClosed: Bool) -> Bool {
        windowParkedOnHome || windowClosed
    }

    static func hasNoOutstandingLeases(_ outstandingLeases: Int) -> Bool {
        outstandingLeases == 0
    }

    static func mayReleaseDisplays(unmirrored: Bool, windowsRestored: Bool, outstandingLeases: Int) -> Bool {
        unmirrored && windowsRestored && hasNoOutstandingLeases(outstandingLeases)
    }

    static func confirmsRemoved(onlineSpatialDisplays: Int) -> Bool {
        onlineSpatialDisplays == 0
    }
}
