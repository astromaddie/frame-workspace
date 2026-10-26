#!/usr/bin/env python3
"""Write reproducible L2 wall scene inputs (6, 9 and 12 windows, opening placeholders,
and cells around a placed panel) into the supplied scratch folder."""
import importlib.util
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parent
out = Path(sys.argv[1]).resolve()
out.mkdir(parents=True, exist_ok=True)
spec = importlib.util.spec_from_file_location("fixtures", root / "generate-fixtures.py")
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
fixtures.ROOT = out

def portrait(box, label):
    box(0, 0, 720, 1200, (.96, .97, .99, 1))
    box(0, 0, 720, 70, (.87, .89, .93, 1))
    label(26, 46, "Field Notes", 30)
    label(32, 140, "Review Checklist", 38)
    for i, text in enumerate(["Window Arrangement", "Focus Confirmation", "Menu Placement", "Shelf Clearance", "Portrait Content", "Saved Positions"]):
        box(32, 200 + i * 130, 22, 22, (.32, .54, .72, 1))
        label(76, 220 + i * 130, text, 26)
        box(76, 248 + i * 130, 530, 3, (.80, .84, .89, 1))
    label(32, 1140, "Draft 04 / Saved Locally", 24)

fixtures.image("portrait.png", 720, 1200, portrait)

def base():
    scene = json.loads((root / "example-slim.json").read_text())
    for name in ("menubar", "status", "dropdown"):
        scene[name]["texture"] = str(root / scene[name]["texture"])
    for app in scene["apps"]:
        app["icon"] = str(root / app["icon"])
    # Cells are handed out as the host does: free cells only, around placed panels.
    scene["allocate"] = True
    return scene

# Mixed windows with their Mac sizes in points: panels share one text scale,
# a 1280-point window fills its cell, smaller ones are smaller.
textures = [(root / "document.png", [1280, 800]), (out / "portrait.png", [540, 900]), (root / "code.png", [1100, 764]),
            (root / "metrics.png", [900, 625]), (root / "document.png", [760, 475]), (root / "code.png", [1440, 1000])]
for count in (6, 9, 12):
    scene = base()
    scene["panels"] = [dict(id=f"wall-{i}", role="window", slot=i, texture=str(textures[i % 6][0]),
                            points=textures[i % 6][1], focused=i == 0) for i in range(count)]
    (out / f"wall-{count}.json").write_text(json.dumps(scene, indent=2) + "\n")

# Opening: three windows already live, the others still placeholders (the
# app's icon and the window title), one of which failed to open.
scene = base()
icons = {"Notes": "notes-icon.png", "Workbench": "workbench-icon.png", "Metrics": "metrics-icon.png",
         "Files": "files-icon.png"}
opening = [("Notes", "Field Notes — Project Atlas", "", [1280, 800]),
           ("Workbench", "Workbench — Scene.cpp", "", [1100, 764]),
           ("Metrics", "Weekly Metrics", "", [900, 625]),
           ("Files", "Downloads", "", [880, 560]),
           ("Notes", "Meeting Notes", "Couldn't open this window. Retrying…", [760, 475])]
scene["panels"] = [dict(id="live-0", role="window", slot=0, texture=str(root / "document.png"), points=[1280, 800],
                        focused=True),
                   dict(id="live-1", role="window", slot=1, texture=str(root / "code.png"), points=[1100, 764]),
                   dict(id="live-2", role="window", slot=2, texture=str(root / "metrics.png"), points=[900, 625])]
for i, (app, title, error, points) in enumerate(opening[3:]):
    scene["panels"].append(dict(id=f"opening-{i}", role="window", slot=3 + i, points=points,
                                placeholder=dict(title=title, app=app, icon=str(root / icons[app]), error=error)))
scene["panels"].append(dict(id="opening-2", role="window", slot=5, points=[540, 900],
                            placeholder=dict(title="Field Notes", app="Notes", icon=str(root / icons["Notes"]))))
(out / "wall-opening.json").write_text(json.dumps(scene, indent=2) + "\n")

# A panel the user placed in the middle of the wall: four new windows all ask
# for the centre slot and are given the free cells around it.
scene = base()
scene["panels"] = [dict(id="user", role="window", texture=str(root / "document.png"), focused=True,
                        placement={"version": 1, "width": .9, "distance": 1.6, "curvature": 0,
                                   "matrix": [1, 0, 0, .12, 0, 1, 0, .2, 0, 0, 1, -1.6]})]
scene["panels"] += [dict(id=f"new-{i}", role="window", slot=0, texture=str(textures[(i + 1) % 6][0]),
                         points=textures[(i + 1) % 6][1]) for i in range(4)]
(out / "wall-occupied.json").write_text(json.dumps(scene, indent=2) + "\n")
