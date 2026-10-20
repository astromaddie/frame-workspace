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
        var policy = SpatialPoolPolicy(capacity: 3)
        let first = UUID()
        let second = UUID()
        let third = UUID()
        let fourth = UUID()
        let fifth = UUID()

        expect(SpatialPoolPolicy.initialPoolSize == 3 && SpatialPoolPolicy.warmSpareCount == 3 &&
               SpatialPoolPolicy.maximumPoolSize >= 75,
               "the pool keeps three warm displays and admits at least 72 active windows")
        expect(policy.registerFree(slot: 0), "prewarmed slots register free")
        expect(policy.registerFree(slot: 1), "second prewarmed slot registers free")
        expect(policy.acquire(owner: first) == .lease(slot: 0), "first window leases the lowest free slot")
        expect(policy.acquire(owner: first) == .alreadyLeased(slot: 0), "duplicate acquisition cannot alias an active lease")
        expect(policy.release(slot: 0, owner: second) == false, "a different stream cannot release the lease")
        expect(policy.release(slot: 0, owner: first), "the owning stream releases its lease")
        expect(policy.acquire(owner: second) == .lease(slot: 0), "a released display is reused")
        expect(policy.acquire(owner: third) == .lease(slot: 1), "the next window uses the other prewarmed slot")
        expect(policy.acquire(owner: fourth) == .create(slot: 2), "active admission can refill when warm slots are consumed")
        expect(policy.finishCreation(slot: 2, owner: fifth) == false, "only the creating owner can finish a refill")
        expect(policy.finishCreation(slot: 2, owner: fourth), "the created display becomes leased")
        expect(policy.acquire(owner: UUID()) == .full, "a deliberately small test pool refuses unbounded creation")
        expect(policy.release(slot: 2, owner: fourth), "the refilled slot can return to the pool")
        expect(policy.acquire(owner: UUID()) == .lease(slot: 2), "a refilled slot is reused after release")

        var warmPolicy = SpatialPoolPolicy(capacity: SpatialPoolPolicy.maximumPoolSize)
        for slot in 0..<SpatialPoolPolicy.initialPoolSize {
            expect(warmPolicy.registerFree(slot: slot), "initial warm display registers")
        }
        expect(warmPolicy.beginWarmRefill() == nil, "the pool does not refill while three warm spares are ready")
        let warmOwner = UUID()
        expect(warmPolicy.acquire(owner: warmOwner) == .lease(slot: 0), "consuming a spare creates one active lease")
        let pendingRefill = warmPolicy.beginWarmRefill()
        expect(pendingRefill == 3, "consuming a spare reserves one serialized background refill")
        expect(warmPolicy.backgroundPreparationCount == 1 && warmPolicy.outstandingCount > 0,
               "background refill preparation blocks cleanup")
        expect(warmPolicy.beginWarmRefill() == nil, "only one warm refill may be in flight")
        expect(warmPolicy.finishWarmRefill(slot: pendingRefill!), "completed refill becomes a ready idle spare")
        expect(warmPolicy.freeCount == SpatialPoolPolicy.warmSpareCount,
               "a successful refill restores the ready-spare target")
        expect(warmPolicy.backgroundPreparationCount == 0, "completed refill clears pending ownership")

        var seventyTwoPolicy = SpatialPoolPolicy(capacity: SpatialPoolPolicy.maximumPoolSize)
        for slot in 0..<SpatialPoolPolicy.initialPoolSize {
            expect(seventyTwoPolicy.registerFree(slot: slot), "initial slot registers for high concurrency")
        }
        var owners = [UUID]()
        var activeSlots = Set<Int>()
        for _ in 0..<72 {
            let owner = UUID()
            owners.append(owner)
            expect(seventyTwoPolicy.beginLeasePreparation(owner: owner), "each window tracks its own pending lease")
            let slot: Int
            switch seventyTwoPolicy.acquire(owner: owner) {
            case .lease(let existing): slot = existing
            case .create(let created):
                expect(seventyTwoPolicy.finishCreation(slot: created, owner: owner), "new high-concurrency slot becomes leased")
                slot = created
            default: fatalError("the 128-slot ceiling must admit 72 simultaneous windows")
            }
            expect(activeSlots.insert(slot).inserted, "simultaneous windows never alias one display")
            expect(seventyTwoPolicy.leaseIsCurrent(slot: slot, owner: owner), "each active slot retains its owner")
            expect(seventyTwoPolicy.finishLeasePreparation(owner: owner), "lease readiness clears its pending marker")
        }
        expect(seventyTwoPolicy.leasedCount == 72 && activeSlots.count == 72,
               "the pool supports 72 simultaneous active window leases")
        var refillCount = 0
        while seventyTwoPolicy.freeCount < SpatialPoolPolicy.warmSpareCount,
              let slot = seventyTwoPolicy.beginWarmRefill() {
            expect(seventyTwoPolicy.finishWarmRefill(slot: slot), "bounded warm refill completes")
            refillCount += 1
        }
        expect(refillCount == 3 && seventyTwoPolicy.freeCount == 3,
               "the pool refills three ready spares after active admission")
        let recycledOwner = owners[0]
        let recycledSlot = activeSlots.min()!
        expect(seventyTwoPolicy.release(slot: recycledSlot, owner: recycledOwner),
               "a lease can be returned by its original owner after high concurrency")
        expect(seventyTwoPolicy.acquire(owner: UUID()) == .lease(slot: recycledSlot),
               "released leases are reused without expanding the pool")

        var failedPolicy = SpatialPoolPolicy(capacity: 4)
        for slot in 0..<3 { expect(failedPolicy.registerFree(slot: slot), "failed-cache test prewarms slots") }
        expect(failedPolicy.acquire(owner: UUID()) == .lease(slot: 0), "a consumed spare creates room for refill")
        let failedWarmSlot = failedPolicy.beginWarmRefill()!
        expect(failedWarmSlot == 3 && failedPolicy.failWarmRefill(slot: failedWarmSlot),
               "failed background creation is cached as a bounded slot")
        expect(failedPolicy.failedCount == 1 && failedPolicy.beginWarmRefill() == nil,
               "the pool neither retries a failed slot nor exceeds its safety ceiling")

        var failedActivePolicy = SpatialPoolPolicy(capacity: 4)
        for _ in 0..<4 {
            let owner = UUID()
            if case .create(let slot) = failedActivePolicy.acquire(owner: owner) {
                expect(failedActivePolicy.failCreation(slot: slot, owner: owner),
                       "failed active display creation is recorded once")
            } else {
                fatalError("each bounded failed slot should be attempted once")
            }
        }
        expect(failedActivePolicy.failedCount == 4 && failedActivePolicy.slotCount == 4 &&
               failedActivePolicy.acquire(owner: UUID()) == .full,
               "failed active creations are cached within the ceiling instead of retried without bound")

        var pendingPolicy = SpatialPoolPolicy(capacity: 1)
        let pendingOwner = UUID()
        expect(pendingPolicy.beginLeasePreparation(owner: pendingOwner), "lease setup is marked pending before display work")
        expect(pendingPolicy.outstandingCount == 1, "pending setup blocks cleanup before a lease is delivered")
        expect(pendingPolicy.beginLeasePreparation(owner: pendingOwner) == false, "one stream cannot start two concurrent lease preparations")
        expect(pendingPolicy.finishLeasePreparation(owner: UUID()) == false, "a different owner cannot clear pending setup")
        expect(pendingPolicy.finishLeasePreparation(owner: pendingOwner), "the requesting stream completes its pending setup")
        expect(pendingPolicy.outstandingCount == 0, "completed setup leaves cleanup unblocked")

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
        let activeMirror = [
            SpatialLayoutDisplay(id: 11, width: 1280, height: 800, pixelWidth: 1280, pixelHeight: 800,
                                 refreshRate: 60, x: 0, y: 0, mirrorTarget: 90, isMain: false),
            layout[1]
        ]
        expect(SpatialLayoutPolicy.permitsRestore(snapshot: layout, current: activeMirror,
                                                   spatialDisplayIDs: ownedVirtualIDs, ownedMirrorSource: 11,
                                                   ownedMirrorTarget: 90),
               "the helper may undo its own built-in-to-home mirror")
        let externalOriginalMain = [
            layout[0],
            SpatialLayoutDisplay(id: 12, width: 2560, height: 1440, pixelWidth: 2560, pixelHeight: 1440,
                                 refreshRate: 60, x: 1280, y: 0, mirrorTarget: nil, isMain: false)
        ]
        expect(SpatialLayoutPolicy.permitsRestore(snapshot: layout, current: externalOriginalMain,
                                                   spatialDisplayIDs: ownedVirtualIDs, ownedMirrorSource: 11,
                                                   ownedMirrorTarget: 90,
                                                   knownSessionLayout: externalOriginalMain),
               "a virtual home may temporarily become main while the original external main is restored")
        let foreignMirror = [
            SpatialLayoutDisplay(id: 11, width: 1920, height: 1080, pixelWidth: 1920, pixelHeight: 1080,
                                 refreshRate: 60, x: 0, y: 0, mirrorTarget: 12, isMain: false),
            layout[1]
        ]
        expect(!SpatialLayoutPolicy.permitsRestore(snapshot: layout, current: foreignMirror,
                                                    spatialDisplayIDs: ownedVirtualIDs, ownedMirrorSource: 11,
                                                    ownedMirrorTarget: 90),
               "a mirror established by another owner is left untouched")
        var foreignOrigin = layout
        foreignOrigin[1] = SpatialLayoutDisplay(id: 12, width: 2560, height: 1440, pixelWidth: 2560,
                                                pixelHeight: 1440, refreshRate: 60, x: 2000, y: 0,
                                                mirrorTarget: nil, isMain: false)
        expect(!SpatialLayoutPolicy.permitsRestore(snapshot: layout, current: foreignOrigin,
                                                    spatialDisplayIDs: [], ownedMirrorSource: 11,
                                                    ownedMirrorTarget: 90),
               "a changed unrelated physical display is not taken over during cleanup")
        expect(SpatialLayoutPolicy.permitsRestore(snapshot: layout, current: foreignOrigin,
                                                   spatialDisplayIDs: [], ownedMirrorSource: 11,
                                                   ownedMirrorTarget: 90, knownSessionLayout: foreignOrigin),
               "a physical origin change observed during our owned virtual-display layout can be restored")

        let homeRect = CGRect(x: 100, y: 200, width: 800, height: 600)
        let parked = SpatialWindowPolicy.parkedFrame(windowID: 3, size: CGSize(width: 500, height: 400), homeRect: homeRect)
        expect(parked.map { homeRect.contains($0) } == true, "a released spatial window is parked on the non-leased home display")
        let oversizedParked = SpatialWindowPolicy.parkedFrame(windowID: 7, size: CGSize(width: 1200, height: 900), homeRect: homeRect)
        expect(oversizedParked == homeRect, "parking clamps an oversized window to the home display")
        expect(SpatialWindowPolicy.parkedFrame(windowID: 1, size: CGSize(width: 10, height: 10), homeRect: .zero) == nil,
               "parking refuses a missing home display rectangle")
        expect(!SpatialCleanupGate.mayReleaseLease(windowParkedOnHome: false, windowClosed: false),
               "a live window that failed parking keeps its pooled lease")
        expect(SpatialCleanupGate.mayReleaseLease(windowParkedOnHome: true, windowClosed: false),
               "a safely parked window releases its pooled lease")
        expect(SpatialCleanupGate.mayReleaseLease(windowParkedOnHome: false, windowClosed: true),
               "a confirmed closed window releases its pooled lease")

        expect(!SpatialCleanupGate.hasNoOutstandingLeases(1), "a pending or active stream lease blocks teardown")
        expect(SpatialCleanupGate.hasNoOutstandingLeases(0), "teardown may proceed with no outstanding leases")
        expect(!SpatialCleanupGate.mayReleaseDisplays(unmirrored: false, windowsRestored: true, outstandingLeases: 0),
               "display resources stay live until unmirroring succeeds")
        expect(!SpatialCleanupGate.mayReleaseDisplays(unmirrored: true, windowsRestored: false, outstandingLeases: 0),
               "display resources stay live until original frames are restored")
        expect(!SpatialCleanupGate.mayReleaseDisplays(unmirrored: true, windowsRestored: true, outstandingLeases: 1),
               "display resources stay live until every stream and refill releases its lease")
        expect(SpatialCleanupGate.mayReleaseDisplays(unmirrored: true, windowsRestored: true, outstandingLeases: 0),
               "all display cleanup prerequisites permit removal")
        expect(!SpatialCleanupGate.confirmsRemoved(onlineSpatialDisplays: 1),
               "stop cannot report removed while any spatial display remains registered")
        expect(SpatialCleanupGate.confirmsRemoved(onlineSpatialDisplays: 0), "stop confirms cleanup after every spatial display vanishes")

        print("SpatialPolicy: 72-window admission, bounded refill, eligibility, layout restore, ownership, parking, and cleanup gates passed")
    }
}
