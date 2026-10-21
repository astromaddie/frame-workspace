#!/usr/bin/env python3
"""Write reproducible mixed-aspect L2 scene inputs into the supplied scratch folder."""
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
for count in (6, 9):
    scene = json.loads((root / "example-slim.json").read_text())
    for name in ("menubar", "status", "dropdown"):
        scene[name]["texture"] = str(root / scene[name]["texture"])
    for app in scene["apps"]:
        app["icon"] = str(root / app["icon"])
    textures = [root / "document.png", out / "portrait.png", root / "code.png"]
    scene["panels"] = [dict(id=f"wall-{i}", role="window", slot=i,
        texture=str(textures[i % len(textures)]), focused=i == 0) for i in range(count)]
    (out / f"wall-{count}.json").write_text(json.dumps(scene, indent=2) + "\n")
