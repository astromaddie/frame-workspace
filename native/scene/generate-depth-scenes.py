#!/usr/bin/env python3
"""Reproduce depth/pressed-title close-ups without capturing or moving Mac windows."""
import copy
import json
from pathlib import Path
import sys
root = Path(__file__).resolve().parent
out = Path(sys.argv[1]).resolve()
out.mkdir(parents=True, exist_ok=True)
scene = json.loads((root / 'example.json').read_text())
for panel in scene['panels']:
    panel['texture'] = str(root / panel['texture'])
for app in scene['apps']:
    app['icon'] = str(root / app['icon'])
for name in ('menubar', 'status', 'dropdown'):
    scene[name]['texture'] = str(root / scene[name]['texture'])
scene['panels'] = [scene['panels'][0]]
scene['panels'][0]['pillLaser'] = False
scene['apps'] = []
scene.pop('status')
scene['menubar']['reveal'] = 1
# Cairo's File ink spans x=132.35..172.37 pixels in this 2x capture.
# Centre the 34-point title slot at 76 points, including its quiet side padding.
scene['menubar']['surface']['selectedRect'] = [59, 0, 34, 30]
(out / 'menubar-pressed.json').write_text(json.dumps(scene, indent=2) + '\n')
scene.pop('menubar')
scene.pop('dropdown')
scene['panels'][0]['placement']['bend'] = {'vertical': False, 'a': .15, 'b': .15}
(out / 'depth-above.json').write_text(json.dumps(scene, indent=2) + '\n')
scene['panels'][0]['placement']['bend'] = {'vertical': False, 'a': 0, 'b': 0}
(out / 'depth-flat-corner.json').write_text(json.dumps(scene, indent=2) + '\n')
