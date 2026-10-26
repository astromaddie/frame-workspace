"""FW-15 Launcher A, the Mac side's pure parts: the Applications grid's layout
(the helper's starting layout, the user's saved rearrangement over it), app
name search and the search field's key events. The only I/O is the saved
layout file, which never touches macOS data."""
import json
import os
from pathlib import Path
import re
import tempfile
import unicodedata

BUNDLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
FOLDER = re.compile(r"[A-Za-z0-9_-]{1,40}\Z")
MAX_APPS = 600
MAX_QUERY = 64
COLUMNS = 8          # the grid's columns (native/launcher.hpp), for Up and Down
RECENT = 8


def clean(value, limit):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))[:limit]


def fold(text):
    """Case- and diacritic-insensitive form: "Écran" and "ecran" match."""
    decomposed = unicodedata.normalize("NFKD", str(text))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def words(text):
    return [w for w in re.split(r"[^\w]+", fold(text)) if w]


def search(apps, query, recent=()):
    """Bundle IDs of `apps` ((bundleID, name) pairs) whose name matches.
    Names starting with the query come first, then names with a word starting
    with it, then names containing it; recently used apps first within each,
    then by name."""
    needle = fold(query).strip()
    if not needle:
        return []
    order = {b: i for i, b in enumerate(recent)}
    ranked = []
    for bundle, name in apps:
        folded = fold(name)
        if folded.startswith(needle):
            rank = 0
        elif any(w.startswith(needle) for w in words(name)) or (" " in needle and f" {needle}" in f" {folded}"):
            rank = 1
        elif needle in folded:
            rank = 2
        else:
            continue
        ranked.append((rank, order.get(bundle, len(order)), folded, bundle))
    return [r[3] for r in sorted(ranked)]


def sanitize_catalog(raw):
    """The helper's GET /launcher, checked: apps (bundleID, name, version),
    items (apps and folders of known apps, each app once), recent."""
    if not isinstance(raw, dict):
        raise ValueError("Invalid launcher catalogue")
    apps, seen = [], set()
    for app in raw.get("apps") or []:
        if not isinstance(app, dict):
            continue
        bundle = app.get("bundleID")
        if isinstance(bundle, str) and BUNDLE.fullmatch(bundle) and bundle not in seen and len(apps) < MAX_APPS:
            seen.add(bundle)
            apps.append({"bundleID": bundle, "name": clean(app.get("name") or bundle, 128),
                         "version": clean(app.get("version"), 96)})
    items = normalize_items(raw.get("items"), seen)
    recent = [b for b in dict.fromkeys(raw.get("recent") or []) if isinstance(b, str) and b in seen][:RECENT]
    launchpad = raw.get("launchpad") if raw.get("launchpad") in ("imported", "missing", "unreadable") else "missing"
    return {"apps": apps, "items": items, "recent": recent, "launchpad": launchpad}


def normalize_items(raw, known):
    """Layout items (`{"app": id}` or `{"folder": id, "name": n, "apps": [...]}`)
    limited to `known` apps, each app once, folders unique and never empty."""
    items, used, folders = [], set(), set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        if "folder" in item:
            folder = item.get("folder")
            members = [b for b in item.get("apps") or [] if isinstance(b, str) and b in known and b not in used]
            members = list(dict.fromkeys(members))
            if not (isinstance(folder, str) and FOLDER.fullmatch(folder)) or folder in folders or not members:
                continue
            folders.add(folder)
            used.update(members)
            items.append({"folder": folder, "name": clean(item.get("name") or "Untitled Folder", 64), "apps": members})
        else:
            bundle = item.get("app")
            if isinstance(bundle, str) and bundle in known and bundle not in used:
                used.add(bundle)
                items.append({"app": bundle})
    return items


def flatten(items):
    for item in items:
        if "folder" in item:
            yield from item["apps"]
        else:
            yield item["app"]


def merge_layout(base, saved, known):
    """The user's saved arrangement, minus apps that are gone, plus apps it
    has not seen (in the helper's order) at the end."""
    if saved is None:
        return normalize_items(base, known)
    items = normalize_items(saved, known)
    used = set(flatten(items))
    for bundle in flatten(normalize_items(base, known)):
        if bundle not in used:
            used.add(bundle)
            items.append({"app": bundle})
    for bundle in sorted(known - used):  # not in either: still listed
        items.append({"app": bundle})
    return items


def item_id(item):
    return "folder:" + item["folder"] if "folder" in item else item["app"]


def move(items, moved, before=None, folder=None):
    """Drop `moved` before `before` (None: at the end), in the top level or in
    `folder`. Dropping onto an app makes no folder; moving between levels is
    not a launcher gesture. Returns the new items, or None when nothing moves."""
    items = [dict(i, apps=list(i["apps"])) if "folder" in i else dict(i) for i in items]
    if folder is None:
        ids = [item_id(i) for i in items]
        if moved not in ids or moved == before or (before is not None and before not in ids):
            return None
        item = items.pop(ids.index(moved))
        ids.remove(moved)
        items.insert(ids.index(before) if before is not None else len(items), item)
    else:
        target = next((i for i in items if i.get("folder") == folder), None)
        if (target is None or moved not in target["apps"] or moved == before or
                (before is not None and before not in target["apps"])):
            return None
        apps = target["apps"]
        apps.remove(moved)
        apps.insert(apps.index(before) if before is not None else len(apps), moved)
    return items


def load_layout(path):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw.get("items") if isinstance(raw, dict) and raw.get("version") == 1 else None


def save_layout(path, items):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="." + path.name + "-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump({"version": 1, "items": items}, out, separators=(",", ":"))
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class SearchField:
    """The search field's state, driven by the Mac keyboard's key events
    (GET /launcher/keys). `apply` returns "close" for Esc and "submit" for
    Return; the caller opens the selected match."""

    def __init__(self):
        self.query, self.selected, self.page_step = "", 0, 0

    def reset(self):
        self.query, self.selected, self.page_step = "", 0, 0

    def apply(self, event, count):
        kind = event.get("k")
        if kind == "text" and isinstance(event.get("s"), str):
            self.query = clean(self.query + event["s"], MAX_QUERY)
            self.selected = 0
        elif kind == "back":
            self.query = self.query[:-1]
            self.selected = 0
        elif kind == "esc":
            return "close"
        elif kind == "return":
            return "submit"
        elif kind in ("left", "right", "up", "down"):
            step = {"left": -1, "right": 1, "up": -COLUMNS, "down": COLUMNS}[kind]
            if self.query.strip():
                self.selected = min(max(self.selected + step, 0), max(count - 1, 0))
            elif kind in ("left", "right"):
                self.page_step += step  # the headset turns the page
        return None
