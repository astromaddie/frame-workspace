// The Spatial Mode launcher's app catalogue (FW-15, Launcher A): every
// user-facing app in the usual application folders, in the order and folders
// of the user's old Launchpad database where that still exists, then the rest
// alphabetically. Foundation and SQLite only (no AppKit, no event taps), so the
// unit tests run it on fixture folders and a fixture database.
import Foundation
import SQLite3

struct LauncherApp: Equatable {
    let bundleID: String
    let name: String
    let path: String
    /// Bundle version plus Info.plist modification time: the icon cache key, so
    /// an updated app gets a new icon and an unchanged one is never resent.
    let version: String
}

enum LauncherItem: Equatable {
    case app(String)
    case folder(id: String, name: String, apps: [String])
}

/// What the Launchpad database lists, pages flattened in order.
enum LaunchpadEntry: Equatable {
    case app(String)
    case folder(name: String, apps: [String])
}

enum LauncherCatalog {
    static let maxApps = 600
    static let maxDepth = 3

    static var defaultRoots: [URL] {
        ["/Applications", "/System/Applications", NSHomeDirectory() + "/Applications"].map {
            URL(fileURLWithPath: $0, isDirectory: true)
        }
    }

    /// Finder lives outside the application folders but is an app people open.
    static let defaultExtras = [URL(fileURLWithPath: "/System/Library/CoreServices/Finder.app", isDirectory: true)]

    /// Top-level app bundles under `roots` (folders inside them are searched a
    /// few levels down; nothing inside another bundle is), plus `extras`.
    /// Agent and background-only apps are left out. When one bundle ID is
    /// installed more than once, the copy `preferred` names (Launch Services'
    /// default) wins, else the first found in root order.
    static func scan(roots: [URL], extras: [URL] = [], preferred: (String) -> URL? = { _ in nil },
                     fileManager: FileManager = FileManager()) -> [LauncherApp] {
        var candidates: [LauncherApp] = []
        var seenPaths = Set<String>()
        func consider(_ url: URL) {
            guard candidates.count < maxApps * 2 else { return }
            let resolved = url.resolvingSymlinksInPath().standardizedFileURL
            guard seenPaths.insert(resolved.path).inserted, let app = read(bundle: url, resolved: resolved,
                                                                         fileManager: fileManager) else { return }
            candidates.append(app)
        }
        for root in roots {
            // Not .skipsHiddenFiles: /Applications/Safari.app is a symlink with
            // the hidden flag set. Dot files are still skipped.
            guard let walker = fileManager.enumerator(at: root, includingPropertiesForKeys: [.isDirectoryKey],
                                                      options: [.skipsPackageDescendants]) else { continue }
            for case let url as URL in walker {
                // skipDescendants() only for real directories: after a file or
                // symlink it would skip the previous directory's remainder.
                let directory = (try? url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) == true
                let app = url.pathExtension.lowercased() == "app"
                if directory, url.lastPathComponent.hasPrefix(".") || app || walker.level >= maxDepth {
                    walker.skipDescendants()
                }
                if app, !url.lastPathComponent.hasPrefix(".") { consider(url) }
            }
        }
        for url in extras { consider(url) }

        var chosen: [String: Int] = [:]
        var result: [LauncherApp] = []
        for app in candidates {
            if let at = chosen[app.bundleID] {
                let wanted = preferred(app.bundleID)?.resolvingSymlinksInPath().standardizedFileURL.path
                let current = URL(fileURLWithPath: result[at].path).resolvingSymlinksInPath().standardizedFileURL.path
                let this = URL(fileURLWithPath: app.path).resolvingSymlinksInPath().standardizedFileURL.path
                if let wanted, wanted == this, wanted != current { result[at] = app }
                continue
            }
            guard result.count < maxApps else { break }
            chosen[app.bundleID] = result.count
            result.append(app)
        }
        return result
    }

    /// One bundle's identity, or nil when it is not a user-facing app.
    static func read(bundle url: URL, resolved: URL? = nil, fileManager: FileManager = FileManager()) -> LauncherApp? {
        let real = resolved ?? url.resolvingSymlinksInPath()
        var isDirectory: ObjCBool = false
        guard fileManager.fileExists(atPath: real.path, isDirectory: &isDirectory), isDirectory.boolValue else { return nil }
        // macOS bundles keep Info.plist in Contents; wrapped iPhone/iPad apps behind WrappedBundle.
        let plists = [real.appendingPathComponent("Contents/Info.plist"),
                      real.appendingPathComponent("WrappedBundle/Info.plist")]
        guard let plist = plists.first(where: { fileManager.fileExists(atPath: $0.path) }),
              let data = try? Data(contentsOf: plist),
              let info = (try? PropertyListSerialization.propertyList(from: data, format: nil)) as? [String: Any],
              let bundleID = info["CFBundleIdentifier"] as? String,
              SwitcherPanel.validBundleID(bundleID) else { return nil }
        if let type = info["CFBundlePackageType"] as? String, type != "APPL" { return nil }
        if truthy(info["LSUIElement"]) || truthy(info["LSBackgroundOnly"]) { return nil }
        var name = fileManager.displayName(atPath: url.path)
        if name.lowercased().hasSuffix(".app") { name = String(name.dropLast(4)) }
        if name.isEmpty { name = url.deletingPathExtension().lastPathComponent }
        let modified = (try? fileManager.attributesOfItem(atPath: plist.path)[.modificationDate] as? Date)
            .map { Int($0.timeIntervalSince1970) } ?? 0
        let version = [info["CFBundleShortVersionString"], info["CFBundleVersion"]]
            .compactMap { $0 as? String }.joined(separator: "-")
        return LauncherApp(bundleID: bundleID, name: String(name.prefix(128)), path: url.path,
                           version: String("\(version)-\(modified)".prefix(96)))
    }

    private static func truthy(_ value: Any?) -> Bool {
        switch value {
        case let flag as Bool: return flag
        case let number as NSNumber: return number.boolValue
        case let text as String: return ["1", "true", "yes"].contains(text.lowercased())
        default: return false
        }
    }

    /// The launcher's starting layout: the Launchpad order and folders, apps it
    /// lists that no longer exist dropped, then every other app by name.
    static func merge(launchpad: [LaunchpadEntry]?, apps: [LauncherApp]) -> [LauncherItem] {
        let known = Set(apps.map(\.bundleID))
        var used = Set<String>()
        var items: [LauncherItem] = []
        var folders = 0
        for entry in launchpad ?? [] {
            switch entry {
            case .app(let id):
                if known.contains(id), used.insert(id).inserted { items.append(.app(id)) }
            case .folder(let name, let members):
                let kept = members.filter { known.contains($0) && used.insert($0).inserted }
                guard !kept.isEmpty else { continue }
                folders += 1
                let title = name.trimmingCharacters(in: .whitespacesAndNewlines)
                items.append(.folder(id: "folder-\(folders)", name: String((title.isEmpty ? "Untitled Folder" : title).prefix(64)),
                                     apps: kept))
            }
        }
        let rest = apps.filter { !used.contains($0.bundleID) }.sorted {
            let order = $0.name.localizedStandardCompare($1.name)
            return order == .orderedSame ? $0.bundleID < $1.bundleID : order == .orderedAscending
        }
        items.append(contentsOf: rest.map { .app($0.bundleID) })
        return items
    }

    /// Most recently used first, from Spotlight's last-used dates.
    static func recent(_ used: [(String, Date)], limit: Int = 8) -> [String] {
        var seen = Set<String>()
        return used.sorted { $0.1 == $1.1 ? $0.0 < $1.0 : $0.1 > $1.1 }
            .map(\.0).filter { seen.insert($0).inserted }.prefix(limit).map { $0 }
    }

    static func json(apps: [LauncherApp], items: [LauncherItem], recent: [String], launchpad: String) -> [String: Any] {
        ["apps": apps.map { ["bundleID": $0.bundleID, "name": $0.name, "version": $0.version] },
         "items": items.map { item -> [String: Any] in
             switch item {
             case .app(let id): return ["app": id]
             case .folder(let id, let name, let members): return ["folder": id, "name": name, "apps": members]
             }
         },
         "recent": recent, "launchpad": launchpad]
    }
}

/// Reads the old Launchpad database without touching it: a private copy is
/// opened read-only and immutable, then deleted.
enum LaunchpadDatabase {
    static func defaultURL() -> URL? {
        var buffer = [CChar](repeating: 0, count: Int(PATH_MAX))
        guard confstr(_CS_DARWIN_USER_DIR, &buffer, buffer.count) > 0 else { return nil }
        return URL(fileURLWithPath: String(cString: buffer), isDirectory: true)
            .appendingPathComponent("com.apple.dock.launchpad/db/db")
    }

    enum Result: Equatable {
        case missing
        case unreadable
        case entries([LaunchpadEntry])
        var label: String {
            switch self {
            case .missing: return "missing"
            case .unreadable: return "unreadable"
            case .entries: return "imported"
            }
        }
        var entries: [LaunchpadEntry]? {
            if case .entries(let list) = self { return list }
            return nil
        }
    }

    static func read(_ url: URL, fileManager: FileManager = FileManager()) -> Result {
        guard fileManager.fileExists(atPath: url.path) else { return .missing }
        let scratch = fileManager.temporaryDirectory.appendingPathComponent("frame-launchpad-\(UUID().uuidString)")
        defer { try? fileManager.removeItem(at: scratch) }
        do {
            try fileManager.createDirectory(at: scratch, withIntermediateDirectories: false,
                                            attributes: [.posixPermissions: 0o700])
            try fileManager.copyItem(at: url, to: scratch.appendingPathComponent("db"))
        } catch {
            return .unreadable
        }
        guard let entries = entries(copy: scratch.appendingPathComponent("db")) else { return .unreadable }
        return .entries(entries)
    }

    /// `items` holds the tree: the root's children are pages (type 3), a
    /// page holds apps (type 4) and folders (type 2), and a folder holds its
    /// own pages of apps. `ordering` orders siblings.
    static func entries(copy: URL) -> [LaunchpadEntry]? {
        var db: OpaquePointer?
        let uri = "file:" + (copy.path.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? copy.path) + "?immutable=1"
        guard sqlite3_open_v2(uri, &db, SQLITE_OPEN_READONLY | SQLITE_OPEN_URI, nil) == SQLITE_OK, let db else {
            sqlite3_close(db)
            return nil
        }
        defer { sqlite3_close(db) }
        func rows(_ sql: String, _ body: (OpaquePointer) -> Void) -> Bool {
            var statement: OpaquePointer?
            guard sqlite3_prepare_v2(db, sql, -1, &statement, nil) == SQLITE_OK, let statement else { return false }
            defer { sqlite3_finalize(statement) }
            var count = 0
            while sqlite3_step(statement) == SQLITE_ROW, count < 20000 {
                body(statement)
                count += 1
            }
            return true
        }
        func text(_ statement: OpaquePointer, _ column: Int32) -> String {
            sqlite3_column_text(statement, column).map { String(cString: $0) } ?? ""
        }
        var root: Int64 = 1
        _ = rows("SELECT value FROM dbinfo WHERE key='launchpad_root'") { root = Int64(text($0, 0)) ?? 1 }
        var children: [Int64: [(order: Int64, id: Int64, type: Int64)]] = [:]
        var bundles: [Int64: String] = [:]
        var titles: [Int64: String] = [:]
        // Launchpad's holding pages (for apps still downloading) are not pages people see.
        guard rows("SELECT rowid, type, parent_id, ordering FROM items WHERE ifnull(uuid,'') NOT LIKE 'HOLDINGPAGE%'", { s in
            children[sqlite3_column_int64(s, 2), default: []].append(
                (sqlite3_column_int64(s, 3), sqlite3_column_int64(s, 0), sqlite3_column_int64(s, 1)))
        }), rows("SELECT item_id, bundleid FROM apps", { bundles[sqlite3_column_int64($0, 0)] = text($0, 1) }),
              rows("SELECT item_id, title FROM groups", { titles[sqlite3_column_int64($0, 0)] = text($0, 1) }) else {
            return nil
        }
        func ordered(_ parent: Int64, type: Int64? = nil) -> [(order: Int64, id: Int64, type: Int64)] {
            (children[parent] ?? []).filter { type == nil || $0.type == type }
                .sorted { $0.order == $1.order ? $0.id < $1.id : $0.order < $1.order }
        }
        var result: [LaunchpadEntry] = []
        for page in ordered(root, type: 3) {
            for item in ordered(page.id) {
                if item.type == 4, let id = bundles[item.id] {
                    result.append(.app(id))
                } else if item.type == 2 {
                    let members = ordered(item.id, type: 3).flatMap { ordered($0.id, type: 4) }
                        .compactMap { bundles[$0.id] }
                    result.append(.folder(name: titles[item.id] ?? "", apps: members))
                }
            }
        }
        return result
    }
}
