// Launcher catalogue and keyboard lease (FW-15 A). Fixture folders and a
// fixture Launchpad database are made in a temporary directory; nothing reads
// the user's applications, database or keyboard.
//   xcrun swiftc -swift-version 5 Sources/AppsState.swift Sources/LauncherCatalog.swift \
//     Sources/LauncherKeys.swift tests/LauncherTests.swift -lsqlite3 -o /tmp/LauncherTests
import Foundation
import SQLite3

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else { fatalError("FAIL: \(message)") }
}

@main
struct LauncherTests {
    static func main() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("launcher-tests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        try scansOnlyTopLevelUserFacingApps(root)
        try prefersLaunchServicesCopyOfDuplicates(root)
        try versionFollowsTheBundle(root)
        try importsLaunchpadOrderAndFolders(root)
        mergesMissingAndExtraApps()
        ordersRecentApps()
        leaseConsumesTypingAndPassesShortcuts()
        leaseReleasesOnEveryExitPath()
        leaseKeepsHalfKeystrokesWhole()
        leaseBoundsItsQueue()
        print("PASS launcher catalogue (folders, filtering, duplicates, versions, Launchpad import, merge, recent) and key lease (consume, pass-through, release paths)")
    }

    // MARK: Fixtures

    @discardableResult
    static func bundle(_ folder: URL, _ name: String, id: String?, info: [String: Any] = [:]) throws -> URL {
        let app = folder.appendingPathComponent(name)
        let contents = app.appendingPathComponent("Contents")
        try FileManager.default.createDirectory(at: contents, withIntermediateDirectories: true)
        var plist: [String: Any] = ["CFBundlePackageType": "APPL", "CFBundleShortVersionString": "1.0", "CFBundleVersion": "7"]
        if let id { plist["CFBundleIdentifier"] = id }
        for (key, value) in info { plist[key] = value }
        let data = try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
        try data.write(to: contents.appendingPathComponent("Info.plist"))
        return app
    }

    static func app(_ id: String, _ name: String) -> LauncherApp {
        LauncherApp(bundleID: id, name: name, path: "/Applications/\(name).app", version: "1")
    }

    // MARK: Catalogue

    static func scansOnlyTopLevelUserFacingApps(_ base: URL) throws {
        let a = base.appendingPathComponent("scan/Applications")
        let system = base.appendingPathComponent("scan/System")
        let home = base.appendingPathComponent("scan/Home")
        for folder in [a, system, home] { try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true) }
        try bundle(a, "Alpha.app", id: "com.example.alpha")
        try bundle(a, "Agent.app", id: "com.example.agent", info: ["LSUIElement": true])
        try bundle(a, "Daemon.app", id: "com.example.daemon", info: ["LSBackgroundOnly": "1"])
        try bundle(a, "Plugin.app", id: "com.example.plugin", info: ["CFBundlePackageType": "BNDL"])
        try bundle(a, "Broken.app", id: nil)
        try bundle(a, "Bad Id.app", id: "bad id")
        let host = try bundle(a, "Host.app", id: "com.example.host")
        try bundle(host.appendingPathComponent("Contents/Library/LoginItems"), "Helper.app", id: "com.example.helper")
        try bundle(host.appendingPathComponent("Contents/Applications"), "Nested.app", id: "com.example.nested")
        try bundle(a.appendingPathComponent("Utilities"), "Beta.app", id: "com.example.beta")
        try bundle(a.appendingPathComponent("Vendor Suite/Tools"), "Gamma.app", id: "com.example.gamma")
        try bundle(a.appendingPathComponent("one/two/three/four"), "TooDeep.app", id: "com.example.deep")
        try bundle(a.appendingPathComponent(".hidden"), "Hidden.app", id: "com.example.hidden")
        try FileManager.default.createDirectory(at: a.appendingPathComponent("Docs.bundle"), withIntermediateDirectories: true)
        try bundle(system.appendingPathComponent("Utilities"), "Terminal.app", id: "com.example.terminal")
        try bundle(home, "Mine.app", id: "com.example.mine")
        // A symlinked app counts once. /Applications/Safari.app is a symlink
        // with the hidden flag set into the Safari cryptex; it still counts.
        try FileManager.default.createSymbolicLink(at: home.appendingPathComponent("Alpha Link.app"),
                                                   withDestinationURL: a.appendingPathComponent("Alpha.app"))
        let cryptex = try bundle(base.appendingPathComponent("scan/Cryptex"), "Browser.app", id: "com.example.browser")
        var link = a.appendingPathComponent("Browser.app")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: cryptex)
        var hidden = URLResourceValues()
        hidden.isHidden = true
        try link.setResourceValues(hidden)
        let finder = try bundle(base.appendingPathComponent("scan/CoreServices"), "Finder.app", id: "com.example.finder")

        let apps = LauncherCatalog.scan(roots: [a, system, home, base.appendingPathComponent("scan/Missing")], extras: [finder])
        let ids = apps.map(\.bundleID)
        expect(Set(ids) == ["com.example.alpha", "com.example.host", "com.example.beta", "com.example.gamma",
                            "com.example.terminal", "com.example.mine", "com.example.finder", "com.example.browser"],
               "user-facing top-level apps from every root, Utilities and vendor folders, plus extras: \(ids)")
        expect(ids.count == Set(ids).count, "no duplicates")
        expect(!ids.contains("com.example.helper") && !ids.contains("com.example.nested"),
               "apps inside another bundle are helpers, not launcher apps")
        expect(apps.first { $0.bundleID == "com.example.alpha" }?.name == "Alpha", "names drop .app")
        expect(apps.first { $0.bundleID == "com.example.beta" }?.path.hasSuffix("Utilities/Beta.app") == true, "path kept")
    }

    static func prefersLaunchServicesCopyOfDuplicates(_ base: URL) throws {
        let first = base.appendingPathComponent("dup/One"), second = base.appendingPathComponent("dup/Two")
        let older = try bundle(first, "Editor.app", id: "com.example.editor", info: ["CFBundleShortVersionString": "1"])
        let newer = try bundle(second, "Editor.app", id: "com.example.editor", info: ["CFBundleShortVersionString": "2"])
        func real(_ path: String) -> String { URL(fileURLWithPath: path).resolvingSymlinksInPath().path }
        let plain = LauncherCatalog.scan(roots: [first, second])
        expect(plain.count == 1 && real(plain[0].path) == real(older.path),
               "first root wins without a Launch Services answer: \(plain)")
        let resolved = LauncherCatalog.scan(roots: [first, second], preferred: { $0 == "com.example.editor" ? newer : nil })
        expect(resolved.count == 1 && real(resolved[0].path) == real(newer.path), "Launch Services' default copy wins")
    }

    static func versionFollowsTheBundle(_ base: URL) throws {
        let folder = base.appendingPathComponent("version")
        let url = try bundle(folder, "Viewer.app", id: "com.example.viewer")
        let before = LauncherCatalog.read(bundle: url)!.version
        let plist = url.appendingPathComponent("Contents/Info.plist")
        try FileManager.default.setAttributes([.modificationDate: Date(timeIntervalSinceNow: 3600)], ofItemAtPath: plist.path)
        let touched = LauncherCatalog.read(bundle: url)!.version
        try bundle(folder, "Viewer.app", id: "com.example.viewer", info: ["CFBundleVersion": "8"])
        try FileManager.default.setAttributes([.modificationDate: Date(timeIntervalSinceNow: 3600)], ofItemAtPath: plist.path)
        let updated = LauncherCatalog.read(bundle: url)!.version
        expect(before != touched && touched != updated, "icon version changes with the bundle: \(before) \(touched) \(updated)")
        expect(LauncherCatalog.read(bundle: url)!.version == updated, "and is stable while it does not")
    }

    // MARK: Launchpad database

    static func makeLaunchpad(_ url: URL) {
        var db: OpaquePointer?
        expect(sqlite3_open(url.path, &db) == SQLITE_OK, "fixture database opens")
        let sql = """
        PRAGMA journal_mode=WAL;
        CREATE TABLE dbinfo (key VARCHAR, value VARCHAR);
        CREATE TABLE apps (item_id INTEGER PRIMARY KEY, title VARCHAR, bundleid VARCHAR, storeid VARCHAR, category_id INTEGER, moddate REAL, bookmark BLOB);
        CREATE TABLE groups (item_id INTEGER PRIMARY KEY, category_id INTEGER, title VARCHAR);
        CREATE TABLE items (rowid INTEGER PRIMARY KEY ASC, uuid VARCHAR, flags INTEGER, type INTEGER, parent_id INTEGER NOT NULL, ordering INTEGER);
        INSERT INTO dbinfo VALUES ('launchpad_root','1');
        INSERT INTO items VALUES (1,'ROOTPAGE',NULL,1,0,0);
        INSERT INTO items VALUES (2,'HOLDINGPAGE',NULL,3,1,0);
        INSERT INTO items VALUES (30,'page-two',0,3,1,2);
        INSERT INTO items VALUES (20,'page-one',0,3,1,1);
        INSERT INTO items VALUES (21,'a',0,4,20,1);  INSERT INTO apps (item_id,title,bundleid) VALUES (21,'Mail','com.example.mail');
        INSERT INTO items VALUES (22,'b',0,4,20,0);  INSERT INTO apps (item_id,title,bundleid) VALUES (22,'Safari','com.example.safari');
        INSERT INTO items VALUES (23,'f',0,2,20,2);  INSERT INTO groups VALUES (23,NULL,'Games');
        INSERT INTO items VALUES (24,'fp1',0,3,23,0);
        INSERT INTO items VALUES (25,'fp2',0,3,23,1);
        INSERT INTO items VALUES (26,'g1',0,4,24,0); INSERT INTO apps (item_id,title,bundleid) VALUES (26,'Chess','com.example.chess');
        INSERT INTO items VALUES (27,'g2',0,4,25,0); INSERT INTO apps (item_id,title,bundleid) VALUES (27,'Old Game','Old Game');
        INSERT INTO items VALUES (28,'g3',0,4,24,1); INSERT INTO apps (item_id,title,bundleid) VALUES (28,'Gone','com.example.gone');
        INSERT INTO items VALUES (31,'c',0,4,30,0);  INSERT INTO apps (item_id,title,bundleid) VALUES (31,'Notes','com.example.notes');
        INSERT INTO items VALUES (32,'e',0,2,30,1);  INSERT INTO groups VALUES (32,NULL,'Empty');
        INSERT INTO items VALUES (33,'ep',0,3,32,0);
        INSERT INTO items VALUES (34,'x',0,4,33,0);  INSERT INTO apps (item_id,title,bundleid) VALUES (34,'Removed','com.example.removed');
        INSERT INTO items VALUES (40,'orphan',0,4,99,0); INSERT INTO apps (item_id,title,bundleid) VALUES (40,'Orphan','com.example.orphan');
        INSERT INTO items VALUES (41,'held',0,4,2,0); INSERT INTO apps (item_id,title,bundleid) VALUES (41,'Downloading','com.example.downloading');
        """
        expect(sqlite3_exec(db, sql, nil, nil, nil) == SQLITE_OK, "fixture database is written")
        sqlite3_close(db)
    }

    static func importsLaunchpadOrderAndFolders(_ base: URL) throws {
        let folder = base.appendingPathComponent("launchpad")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        let url = folder.appendingPathComponent("db")
        makeLaunchpad(url)
        let before = try FileManager.default.attributesOfItem(atPath: url.path)
        let original = try Data(contentsOf: url)
        let result = LaunchpadDatabase.read(url)
        expect(result.label == "imported", "fixture database imports")
        expect(result.entries == [.app("com.example.safari"), .app("com.example.mail"),
                                  .folder(name: "Games", apps: ["com.example.chess", "com.example.gone", "Old Game"]),
                                  .app("com.example.notes"), .folder(name: "Empty", apps: ["com.example.removed"])],
               "pages in order, folders with all their pages, holding page and orphans left out: \(String(describing: result.entries))")
        let after = try FileManager.default.attributesOfItem(atPath: url.path)
        let current = try Data(contentsOf: url)
        expect(current == original &&
               (before[.modificationDate] as? Date) == (after[.modificationDate] as? Date),
               "the database itself is never written")
        let siblings = try FileManager.default.contentsOfDirectory(atPath: folder.path).sorted()
        expect(siblings.allSatisfy { $0.hasPrefix("db") }, "no files appear beside the database: \(siblings)")
        let leftovers = try FileManager.default.contentsOfDirectory(atPath: FileManager.default.temporaryDirectory.path)
            .filter { $0.hasPrefix("frame-launchpad-") }
        expect(leftovers.isEmpty, "the private copy is deleted")
        expect(LaunchpadDatabase.read(folder.appendingPathComponent("none")) == .missing, "no database is missing")
        let junk = folder.appendingPathComponent("junk")
        try Data("not a database".utf8).write(to: junk)
        expect(LaunchpadDatabase.read(junk) == .unreadable, "a damaged database is unreadable, not a crash")
    }

    static func mergesMissingAndExtraApps() {
        let apps = [app("com.example.safari", "Safari"), app("com.example.mail", "Mail"), app("com.example.chess", "Chess"),
                    app("com.example.notes", "Notes"), app("com.example.zed", "zed"), app("com.example.atlas", "Atlas"),
                    app("com.example.ecran", "Écran")]
        let entries: [LaunchpadEntry] = [.app("com.example.safari"), .app("com.example.mail"),
                                         .folder(name: "Games", apps: ["com.example.chess", "com.example.gone", "Old Game"]),
                                         .app("com.example.notes"), .folder(name: "Empty", apps: ["com.example.removed"]),
                                         .app("com.example.mail"), .folder(name: "  ", apps: ["com.example.atlas"])]
        let merged = LauncherCatalog.merge(launchpad: entries, apps: apps)
        expect(merged == [.app("com.example.safari"), .app("com.example.mail"),
                          .folder(id: "folder-1", name: "Games", apps: ["com.example.chess"]),
                          .app("com.example.notes"),
                          .folder(id: "folder-2", name: "Untitled Folder", apps: ["com.example.atlas"]),
                          .app("com.example.ecran"), .app("com.example.zed")],
               "Launchpad order and folders, gone apps and empty folders dropped, the rest by name: \(merged)")
        let alphabetical = LauncherCatalog.merge(launchpad: nil, apps: apps)
        expect(alphabetical.map { item -> String in if case .app(let id) = item { return id }; return "" } ==
               ["com.example.atlas", "com.example.chess", "com.example.ecran", "com.example.mail",
                "com.example.notes", "com.example.safari", "com.example.zed"],
               "without a database every app is in name order")
        let json = LauncherCatalog.json(apps: apps, items: merged, recent: ["com.example.mail"], launchpad: "imported")
        expect((json["items"] as? [[String: Any]])?[2]["folder"] as? String == "folder-1", "folders serialize")
        expect(JSONSerialization.isValidJSONObject(json), "the catalogue is JSON")
    }

    static func ordersRecentApps() {
        let now = Date()
        let used = (0..<12).map { ("com.example.app\($0)", now.addingTimeInterval(Double(-$0 * 60))) }.shuffled()
        let recent = LauncherCatalog.recent(used)
        expect(recent == (0..<8).map { "com.example.app\($0)" }, "eight most recent first: \(recent)")
        expect(LauncherCatalog.recent([]).isEmpty, "no Spotlight dates, no recent apps")
    }

    // MARK: Key lease

    static func leaseConsumesTypingAndPassesShortcuts() {
        let lease = LauncherKeyLease()
        expect(lease.handle(.down, code: 0x00, command: false, control: false, text: "a") == .pass,
               "without a lease typing stays with the Mac")
        lease.open(at: 0)
        let typed: [(UInt16, String, LauncherKey?)] = [
            (0x23, "p", .text("p")), (0x04, "H", .text("H")), (0x31, " ", .text(" ")), (0x0E, "", nil),
            (0x33, "\u{7f}", .backspace), (0x24, "\r", .submit), (0x4C, "\u{3}", .submit), (0x35, "\u{1b}", .escape),
            (0x7B, "\u{F702}", .left), (0x7C, "\u{F703}", .right), (0x7E, "\u{F700}", .up), (0x7D, "\u{F701}", .down),
            (0x52, "0", .text("0"))]
        for (code, text, _) in typed {
            expect(lease.handle(.down, code: code, command: false, control: false, text: text) == .consume,
                   "key \(code) goes to the search field")
            expect(lease.handle(.up, code: code, command: false, control: false, text: "") == .consume, "and its up")
        }
        let events = lease.snapshot(after: 0).events.map(\.key)
        expect(events == typed.compactMap(\.2), "typed keys are queued in order: \(events)")
        for (code, command, control) in [(UInt16(0x08), true, false), (0x09, true, false), (0x30, true, false),
                                         (0x7B, false, true), (0x31, false, true)] {
            expect(lease.handle(.down, code: code, command: command, control: control, text: "c") == .pass,
                   "Command and Control shortcuts pass through (\(code))")
            expect(lease.handle(.up, code: code, command: command, control: control, text: "") == .pass, "with their ups")
        }
        for code: UInt16 in [0x30, 0x7A, 0x73, 0x77, 0x75, 0x72, 0x47] {
            expect(lease.handle(.down, code: code, command: false, control: false, text: "") == .pass,
                   "Tab, function and navigation keys pass (\(code))")
        }
        expect(lease.handle(.flags, code: 0x37, command: true, control: false, text: "") == .pass, "modifiers pass")
        let last = lease.snapshot(after: 0).last
        expect(lease.snapshot(after: last).events.isEmpty && lease.snapshot(after: last - 2).events.count == 2,
               "polling returns only newer events")
    }

    static func leaseReleasesOnEveryExitPath() {
        let lease = LauncherKeyLease()
        // Close (the launcher closes, Desktop Mode, Stop, the tap going away and helper exit all call close).
        lease.open(at: 10)
        expect(lease.active, "open takes the keyboard")
        lease.close()
        expect(!lease.active && lease.handle(.down, code: 0x00, command: false, control: false, text: "a") == .pass,
               "close gives it back")
        expect(lease.snapshot(after: 0).events.isEmpty, "close drops queued keys")
        // Disconnect or a crashed companion: polling stops, the lease runs out.
        lease.open(at: 100)
        expect(!lease.expire(at: 100 + LauncherKeyLease.lease - 0.1), "not before the lease runs out")
        _ = lease.snapshot(after: 0, renewAt: 102)
        expect(!lease.expire(at: 104) && lease.active, "polling renews it")
        lease.renew(at: 104.5)
        expect(lease.expire(at: 104.5 + LauncherKeyLease.lease), "unrenewed, it expires")
        expect(!lease.active && !lease.expire(at: 1000), "once")
        expect(lease.handle(.down, code: 0x00, command: false, control: false, text: "a") == .pass, "and typing is the Mac's again")
        lease.renew(at: 2000)
        expect(!lease.active, "renewing never reopens a closed lease")
        _ = lease.snapshot(after: 0, renewAt: 2000)
        expect(!lease.active, "nor does polling")
        // Reopening starts clean.
        lease.open(at: 3000)
        _ = lease.handle(.down, code: 0x00, command: false, control: false, text: "a")
        lease.close()
        lease.open(at: 3001)
        expect(lease.snapshot(after: 0).events.isEmpty, "a reopened lease has no stale keys")
    }

    static func leaseKeepsHalfKeystrokesWhole() {
        let lease = LauncherKeyLease()
        lease.open(at: 0)
        expect(lease.handle(.down, code: 0x00, command: false, control: false, text: "a") == .consume, "down consumed")
        lease.close()
        expect(lease.handle(.up, code: 0x00, command: false, control: false, text: "") == .consume,
               "the up of a consumed down is consumed after the lease ends")
        expect(lease.handle(.up, code: 0x00, command: false, control: false, text: "") == .pass, "only once")
        // A held key that starts passing gets its up too.
        lease.open(at: 0)
        _ = lease.handle(.down, code: 0x01, command: false, control: false, text: "s")
        lease.close()
        expect(lease.handle(.down, code: 0x01, command: false, control: false, text: "s") == .pass, "repeat after close passes")
        expect(lease.handle(.up, code: 0x01, command: false, control: false, text: "") == .pass, "so does its up")
        lease.open(at: 0)
        _ = lease.handle(.down, code: 0x02, command: false, control: false, text: "d")
        expect(lease.handle(.down, code: 0x02, command: true, control: false, text: "d") == .pass, "Command added passes")
        expect(lease.handle(.up, code: 0x02, command: true, control: false, text: "") == .pass, "with its up")
    }

    static func leaseBoundsItsQueue() {
        let lease = LauncherKeyLease()
        lease.open(at: 0)
        for _ in 0..<(LauncherKeyLease.maxEvents + 40) {
            _ = lease.handle(.down, code: 0x00, command: false, control: false, text: "aaaaaaaaaaaa")
        }
        let snapshot = lease.snapshot(after: 0)
        expect(snapshot.events.count == LauncherKeyLease.maxEvents, "queue is bounded")
        expect(snapshot.events.allSatisfy { $0.key == .text("aaaaaaaa") }, "text per key is bounded")
        expect(snapshot.last == LauncherKeyLease.maxEvents + 40, "the counter keeps counting")
        expect(JSONSerialization.isValidJSONObject(snapshot.json), "snapshot is JSON")
    }
}
