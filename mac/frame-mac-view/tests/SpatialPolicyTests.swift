import Foundation

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else {
        fputs("SpatialPolicyTests: \(message)\n", stderr)
        exit(1)
    }
}

private func candidate(layer: Int = 0, width: Double = 800, height: Double = 600, pid: Int32 = 42,
                       appName: String = "Notes", regular: Bool = true, helper: Bool = false,
                       standard: Bool = true, fullScreen: Bool = false, minimized: Bool = false,
                       hidden: Bool = false) -> SpatialWindowCandidate {
    SpatialWindowCandidate(layer: layer, width: width, height: height, pid: pid, appName: appName,
                           isRegularApplication: regular, isHelperProcess: helper, isStandardWindow: standard,
                           isFullScreen: fullScreen, isMinimized: minimized, isApplicationHidden: hidden)
}

@main
private enum SpatialPolicyTests {
    static func main() {
        expect(SpatialWindowPolicy.isEligible(candidate(width: 120, height: 80)),
               "a regular standard window at the minimum size is eligible")
        expect(!SpatialWindowPolicy.isEligible(candidate(layer: 1)), "non-normal windows are excluded")
        expect(!SpatialWindowPolicy.isEligible(candidate(width: 119)), "undersized windows are excluded")
        expect(!SpatialWindowPolicy.isEligible(candidate(appName: "Dock")), "desktop and system UI is excluded")
        expect(!SpatialWindowPolicy.isEligible(candidate(regular: false)), "non-regular apps are excluded")
        expect(!SpatialWindowPolicy.isEligible(candidate(appName: "Frame Control", helper: true)), "the helper process is excluded")
        expect(!SpatialWindowPolicy.isEligible(candidate(standard: false)), "dialogs and sheets are not independent spatial windows")
        expect(!SpatialWindowPolicy.isEligible(candidate(fullScreen: true)), "full-screen windows are excluded")
        expect(!SpatialWindowPolicy.isEligible(candidate(minimized: true)), "minimized windows are excluded")
        expect(!SpatialWindowPolicy.isEligible(candidate(hidden: true)), "hidden applications are excluded")

        let layout = [
            SpatialLayoutDisplay(id: 11, width: 1920, height: 1080, pixelWidth: 1920, pixelHeight: 1080,
                                 refreshRate: 60, x: 0, y: 0, mirrorTarget: nil, isMain: true),
            SpatialLayoutDisplay(id: 12, width: 2560, height: 1440, pixelWidth: 2560, pixelHeight: 1440,
                                 refreshRate: 60, x: 1920, y: 0, mirrorTarget: nil, isMain: false)
        ]
        let ownedVirtualIDs: Set<UInt32> = [90, 91]
        let withVirtuals = layout + [
            SpatialLayoutDisplay(id: 90, width: 1280, height: 800, pixelWidth: 1280, pixelHeight: 800,
                                 refreshRate: 60, x: 0, y: 0, mirrorTarget: nil, isMain: false),
            SpatialLayoutDisplay(id: 91, width: 1920, height: 1200, pixelWidth: 3840, pixelHeight: 2400,
                                 refreshRate: 60, x: 1280, y: 0, mirrorTarget: nil, isMain: false)
        ]
        expect(SpatialLayoutPolicy.isRestored(snapshot: layout, current: withVirtuals,
                                               spatialDisplayIDs: ownedVirtualIDs),
               "owned virtual displays do not hide a correctly restored physical layout")
        expect(!SpatialLayoutPolicy.isRestored(snapshot: layout, current: [layout[0]],
                                                spatialDisplayIDs: ownedVirtualIDs),
               "a missing physical display prevents cleanup confirmation")
        var changedMode = layout
        changedMode[1] = SpatialLayoutDisplay(id: 12, width: 1920, height: 1080, pixelWidth: 1920,
                                              pixelHeight: 1080, refreshRate: 60, x: 1920, y: 0,
                                              mirrorTarget: nil, isMain: false)
        expect(!SpatialLayoutPolicy.isRestored(snapshot: layout, current: changedMode,
                                                spatialDisplayIDs: []),
               "a reconfigured physical mode prevents cleanup confirmation")
        let externalOriginalMain = [
            layout[0],
            SpatialLayoutDisplay(id: 12, width: 2560, height: 1440, pixelWidth: 2560, pixelHeight: 1440,
                                 refreshRate: 60, x: 1280, y: 0, mirrorTarget: nil, isMain: false)
        ]
        expect(SpatialLayoutPolicy.permitsRestore(snapshot: layout, current: externalOriginalMain,
                                                   spatialDisplayIDs: ownedVirtualIDs,
                                                   knownSessionLayout: externalOriginalMain),
               "a virtual home may temporarily become main while the original external main is restored")
        let foreignMirror = [
            SpatialLayoutDisplay(id: 11, width: 1920, height: 1080, pixelWidth: 1920, pixelHeight: 1080,
                                 refreshRate: 60, x: 0, y: 0, mirrorTarget: 12, isMain: false),
            layout[1]
        ]
        expect(!SpatialLayoutPolicy.permitsRestore(snapshot: layout, current: foreignMirror,
                                                    spatialDisplayIDs: ownedVirtualIDs),
               "a mirror established by another owner is left untouched")
        var foreignOrigin = layout
        foreignOrigin[1] = SpatialLayoutDisplay(id: 12, width: 2560, height: 1440, pixelWidth: 2560,
                                                pixelHeight: 1440, refreshRate: 60, x: 2000, y: 0,
                                                mirrorTarget: nil, isMain: false)
        expect(!SpatialLayoutPolicy.permitsRestore(snapshot: layout, current: foreignOrigin,
                                                    spatialDisplayIDs: []),
               "a changed unrelated physical display is not taken over during cleanup")
        expect(SpatialLayoutPolicy.permitsRestore(snapshot: layout, current: foreignOrigin,
                                                   spatialDisplayIDs: [], knownSessionLayout: foreignOrigin),
               "a physical origin change observed during our owned virtual-display layout can be restored")

        expect(!SpatialCleanupGate.mayReleaseDisplays(windowsRestored: false), "unrestored windows block display removal")
        expect(SpatialCleanupGate.mayReleaseDisplays(windowsRestored: true), "restored windows allow removal")
        expect(!SpatialCleanupGate.confirmsRemoved(onlineSpatialDisplays: 1),
               "stop cannot report removed while any spatial display remains registered")
        expect(SpatialCleanupGate.confirmsRemoved(onlineSpatialDisplays: 0), "stop confirms cleanup after every spatial display vanishes")

        print("SpatialPolicy: eligibility, layout restore, ownership, and cleanup gates passed")
    }
}
