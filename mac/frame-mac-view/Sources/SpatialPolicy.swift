import Foundation
import CoreGraphics

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

    /// Restore only the saved layout or changes observed during our display setup.
    static func permitsRestore(snapshot: [SpatialLayoutDisplay], current: [SpatialLayoutDisplay],
                               spatialDisplayIDs: Set<UInt32>, knownSessionLayout: [SpatialLayoutDisplay]? = nil) -> Bool {
        guard hasExpectedDisplays(snapshotIDs: Set(snapshot.map(\.id)), onlineIDs: current.map(\.id),
                                   spatialDisplayIDs: spatialDisplayIDs) else { return false }
        let currentByID = Dictionary(uniqueKeysWithValues: current.map { ($0.id, $0) })
        let knownByID = knownSessionLayout.map { Dictionary(uniqueKeysWithValues: $0.map { ($0.id, $0) }) } ?? [:]
        return snapshot.allSatisfy { saved in
            guard let actual = currentByID[saved.id] else { return false }
            guard actual.mirrorTarget == saved.mirrorTarget else { return false }
            if actual == saved || knownByID[saved.id] == actual { return true }
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

}

enum SpatialCleanupGate {
    static func mayReleaseDisplays(windowsRestored: Bool) -> Bool { windowsRestored }

    static func confirmsRemoved(onlineSpatialDisplays: Int) -> Bool {
        onlineSpatialDisplays == 0
    }
}
