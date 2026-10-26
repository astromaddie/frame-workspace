#!/usr/bin/env python3
"""Write reproducible Launcher A scene inputs into the supplied scratch folder:
generic 128 px app icons (no real app artwork), a catalogue in launcher.json's
format, and scenes for --render-scene: the launcher open with Recent Apps and
the grid, filtered by a search, a folder open, and an icon being moved."""
import ctypes as C
import ctypes.util
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
OUT = Path(sys.argv[1]).resolve()
OUT.mkdir(parents=True, exist_ok=True)
lib = C.CDLL(ctypes.util.find_library("cairo") or "/opt/homebrew/lib/libcairo.2.dylib")


def api(name, result, *args):
    fn = getattr(lib, "cairo_" + name)
    fn.restype, fn.argtypes = result, list(args)
    return fn


P, D, I = C.c_void_p, C.c_double, C.c_int
surface = api("image_surface_create", P, I, I, I)
create = api("create", P, P)
rgba = api("set_source_rgba", None, P, D, D, D, D)
arc = api("arc", None, P, D, D, D, D, D)
sub_path = api("new_sub_path", None, P)
close_path = api("close_path", None, P)
fill = api("fill", None, P)
source = api("set_source", None, P, P)
linear = api("pattern_create_linear", P, D, D, D, D)
stop = api("pattern_add_color_stop_rgb", None, P, D, D, D, D)
pattern_destroy = api("pattern_destroy", None, P)
face = api("select_font_face", None, P, C.c_char_p, I, I)
font = api("set_font_size", None, P, D)


class Extents(C.Structure):
    _fields_ = [(n, D) for n in ("xb", "yb", "w", "h", "xa", "ya")]


extents = api("text_extents", None, P, C.c_char_p, C.POINTER(Extents))
move = api("move_to", None, P, D, D)
text = api("show_text", None, P, C.c_char_p)
write = api("surface_write_to_png", I, P, C.c_char_p)
destroy = api("destroy", None, P)
free = api("surface_destroy", None, P)

# Generic apps: neutral names, flat two-tone squircles with a letter. The
# accented name exercises the label font; none of these are real app icons.
APPS = [
    ("Mail", "#62adff", "#1f60d6"), ("Notes", "#fff2a8", "#f1c232"), ("Calendar", "#ffffff", "#e6e6ec"),
    ("Photos", "#ffd08a", "#ff7a59"), ("Music", "#ff7a8a", "#e8344a"), ("Maps", "#bdf0b0", "#5fc46a"),
    ("Weather", "#5dbaff", "#2274e0"), ("Calculator", "#5a5a60", "#232326"), ("Files", "#7ec8fb", "#3a97e8"),
    ("Browser", "#7fd4ff", "#2f86f0"), ("Terminal", "#3c3c42", "#141418"), ("Editor", "#6fc6ff", "#1a5fd0"),
    ("Chat", "#7fe08f", "#2fb34a"), ("Video", "#ff9b6a", "#e5532d"), ("Books", "#ffb850", "#f07a0a"),
    ("Podcasts", "#d390ff", "#8a3cf0"), ("Reminders", "#ffffff", "#dfe3ea"), ("Clock", "#4b4b52", "#1b1b1f"),
    ("Camera", "#9aa3ae", "#56606c"), ("Voice Memos", "#ff6a5c", "#c8202a"), ("Contacts", "#d8c3a5", "#a8875f"),
    ("Stocks", "#2f2f35", "#0f0f12"), ("News", "#ff8a8a", "#e0303a"), ("Translate", "#6fb0ff", "#2c62d8"),
    ("Paint", "#ffa3d1", "#e04c98"), ("Studio", "#9d8cff", "#5a3fe0"), ("Sketch", "#ffd166", "#f4a300"),
    ("Slides", "#62adff", "#1f60d6"), ("Sheets", "#66d982", "#22a246"), ("Docs", "#ffb36b", "#f07a1a"),
    ("Mixer", "#c79bff", "#7d3ce8"), ("Scanner", "#b8c4d0", "#76869a"), ("Écran Partagé", "#7ad1ff", "#2a8fe0"),
    ("Monitor", "#5e6b7a", "#2c3440"), ("Archive", "#d9c08a", "#a8843a"), ("Settings", "#c0c4cb", "#6b7079"),
    ("Preview", "#f2f6fb", "#c9d9ee"), ("Store", "#62c2ff", "#1570ea"),
    ("Chess", "#d8c49b", "#8a6a3a"), ("Puzzles", "#ff9fb0", "#e0506a"), ("Racer", "#ffcc66", "#e8892a"),
    ("Console", "#3a3a40", "#121216"), ("Disk Tool", "#c8ccd4", "#80889a"), ("Keychain", "#e0d0a0", "#a89050"),
    ("Network", "#8fd0ff", "#3a8ee0"),
]


def hex_rgb(value):
    return tuple(int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))


def bundle(name):
    return "com.example." + "".join(c for c in name.lower() if c.isalnum() and c.isascii()) or "app"


def icon(name, top, bottom):
    s = surface(0, 128, 128)
    cr = create(s)
    # macOS icon grid: a 100 px squircle-ish rounded square in a 128 px canvas.
    x, y, w, r = 14, 12, 100, 23
    rgba(cr, 0, 0, 0, .18)
    for dx, dy, dr in ((0, 3, 1.5),):
        sub_path(cr)
        arc(cr, x + w - r + dx, y + r + dy, r + dr, -math.pi / 2, 0)
        arc(cr, x + w - r + dx, y + w - r + dy, r + dr, 0, math.pi / 2)
        arc(cr, x + r + dx, y + w - r + dy, r + dr, math.pi / 2, math.pi)
        arc(cr, x + r + dx, y + r + dy, r + dr, math.pi, 3 * math.pi / 2)
        close_path(cr)
        fill(cr)
    g = linear(0, y, 0, y + w)
    stop(g, 0, *hex_rgb(top))
    stop(g, 1, *hex_rgb(bottom))
    sub_path(cr)
    arc(cr, x + w - r, y + r, r, -math.pi / 2, 0)
    arc(cr, x + w - r, y + w - r, r, 0, math.pi / 2)
    arc(cr, x + r, y + w - r, r, math.pi / 2, math.pi)
    arc(cr, x + r, y + r, r, math.pi, 3 * math.pi / 2)
    close_path(cr)
    source(cr, g)
    fill(cr)
    pattern_destroy(g)
    light = sum(hex_rgb(bottom)) / 3 > .7
    rgba(cr, *((.25, .22, .3) if light else (1, 1, 1)), 1)
    face(cr, b"sans-serif", 0, 1)
    font(cr, 54)
    letter = name[0].encode()
    e = Extents()
    extents(cr, letter, C.byref(e))
    move(cr, 64 - e.xa / 2, 62 + e.h / 2)
    text(cr, letter)
    assert write(s, str(OUT / (bundle(name) + ".png")).encode()) == 0
    destroy(cr)
    free(s)


for name, top, bottom in APPS:
    icon(name, top, bottom)
ids = {name: bundle(name) for name, _, _ in APPS}
games = [ids[n] for n in ("Chess", "Puzzles", "Racer")]
utilities = [ids[n] for n in ("Console", "Disk Tool", "Keychain", "Network", "Monitor")]
foldered = set(games + utilities)
top = [ids[name] for name, _, _ in APPS[:38] if ids[name] not in foldered]
items = [{"app": b} for b in top[:10]] + [{"folder": "folder-1", "name": "Games", "apps": games}] + \
    [{"app": b} for b in top[10:20]] + [{"folder": "folder-2", "name": "Utilities", "apps": utilities}] + \
    [{"app": b} for b in top[20:]]
catalog = {"catalog": 1, "apps": [{"id": ids[n], "name": n, "v": "1"} for n, _, _ in APPS], "items": items,
           "recent": [ids[n] for n in ("Terminal", "Editor", "Browser", "Chat", "Photos", "Docs", "Sheets", "Music")]}
(OUT / "launcher.json").write_text(json.dumps(catalog, indent=1, ensure_ascii=False) + "\n")

# The room: two windows on the wall behind, and the shelf with its own apps.
base = json.loads((ROOT / "example-slim.json").read_text())
for name in ("status",):
    base[name]["texture"] = str(ROOT / base[name]["texture"])
base.pop("menubar", None)
base.pop("dropdown", None)
base["panels"] = [dict(id="wall-0", role="window", slot=0, texture=str(ROOT / "document.png")),
                  dict(id="wall-1", role="window", slot=1, texture=str(ROOT / "code.png")),
                  dict(id="wall-2", role="window", slot=2, texture=str(ROOT / "document.png"))]
base["apps"] = [{"id": ids[n], "name": n, "icon": ids[n] + ".png"}
                for n in ("Files", "Browser", "Terminal", "Editor", "Mail", "Chat", "Music", "Notes")]
base["shelf"] = {"scale": 1, "magnify": 0}


def scene(name, **launcher):
    s = json.loads(json.dumps(base))
    s["launcher"] = {"catalog": catalog, "keyboard": True, **launcher}
    (OUT / f"{name}.json").write_text(json.dumps(s, indent=1, ensure_ascii=False) + "\n")


# Search results come from the companion's own search (ui/frame_spatial_launcher.py).
sys.path.insert(0, str(ROOT.parent.parent / "ui"))
from frame_spatial_launcher import search  # noqa: E402

scene("launcher-open", hover={"kind": "cell", "index": 3})
for name, query in (("launcher-search", "pa"), ("launcher-search-wide", "e")):
    scene(name, query=query, selected=0,
          results=search([(ids[n], n) for n, _, _ in APPS], query, catalog["recent"]))
scene("launcher-folder", folder="folder-2", hover={"kind": "back"})
scene("launcher-carry", carry={"id": ids["Weather"], "x": 300 - 1.5 * 66 + 33, "y": 168 + 60 + 17})
print("wrote", len(APPS), "icons and launcher scenes to", OUT)
