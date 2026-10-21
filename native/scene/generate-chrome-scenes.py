#!/usr/bin/env python3
"""Prepare focused shelf and menu-transition scenes; outputs stay in task scratch."""
import copy
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parent
out = Path(sys.argv[1]).resolve()
out.mkdir(parents=True, exist_ok=True)
scene = json.loads((root / "example-slim.json").read_text())
for panel in scene["panels"]:
    panel["texture"] = str(root / panel["texture"])
for app in scene["apps"]:
    app["icon"] = str(root / app["icon"])
for name in ("menubar", "status", "dropdown"):
    scene[name]["texture"] = str(root / scene[name]["texture"])
(out / "shelf-status.json").write_text(json.dumps(scene, indent=2) + "\n")
for label, reveal in (("slim", 0), ("halfway", .5), ("hot", 1)):
    frame = copy.deepcopy(scene)
    frame["panels"] = [frame["panels"][0]]
    frame["panels"][0]["pillLaser"] = False
    frame["apps"] = []
    frame.pop("status")
    frame.pop("dropdown")
    frame["menubar"]["reveal"] = reveal
    (out / f"menubar-{label}.json").write_text(json.dumps(frame, indent=2) + "\n")

# A stronger curve and an off-centre dropdown make the concentric strip and
# the menu's local tangent visible from an elevated diagnostic camera.
curved = copy.deepcopy(scene)
curved["panels"] = [curved["panels"][0]]
curved["panels"][0]["pillLaser"] = False
curved["panels"][0]["placement"]["bend"] = {"vertical": False, "a": .15, "b": .15}
curved["panels"][0]["focusSnap"] = 1
curved["apps"] = []
curved.pop("status")
curved["menubar"]["reveal"] = 1
curved["dropdown"]["surface"]["visible"] = True
(out / "menubar-curved.json").write_text(json.dumps(curved, indent=2) + "\n")

# Optional: combine already-rendered 900 x 380 transition frames with Cairo.
# generate-chrome-scenes.py SCRATCH FINAL.png
if len(sys.argv) > 2:
    import importlib.util
    spec = importlib.util.spec_from_file_location("fixtures", root / "generate-fixtures.py")
    drawing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drawing)
    load = drawing.api("image_surface_create_from_png", drawing.P, drawing.C.c_char_p)
    source = drawing.api("set_source_surface", None, drawing.P, drawing.P, drawing.D, drawing.D)
    paint = drawing.api("paint", None, drawing.P)
    canvas = drawing.surface(0, 2700, 420)
    cr = drawing.create(canvas)
    drawing.rgba(cr, .09, .10, .13, 1); paint(cr)
    for i, label in enumerate(("Slim", "Halfway", "Hot")):
        frame = load(str(out / ("menubar-" + label.lower() + ".png")).encode())
        source(cr, frame, i * 900, 40); paint(cr); drawing.free(frame)
        drawing.rgba(cr, .94, .94, .97, 1); drawing.font(cr, 22)
        drawing.move(cr, i * 900 + 20, 28); drawing.text(cr, label.encode())
    assert drawing.write(canvas, str(Path(sys.argv[2]).resolve()).encode()) == 0
    drawing.destroy(cr); drawing.free(canvas)
