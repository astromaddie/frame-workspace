#!/usr/bin/env python3
"""Pressed-title pixel regression through the production scene/material path."""
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from tests_scene import read_png


def main():
    binary = Path(sys.argv[1]).resolve()
    source = Path(__file__).resolve().parent / 'scene'
    with tempfile.TemporaryDirectory(prefix='depth-render-', dir=os.environ.get('FRAME_NATIVE_TEST_TMP')) as tmp:
        root = Path(tmp)
        subprocess.run([sys.executable, str(source / 'generate-depth-scenes.py'), str(root)], check=True)
        spec = importlib.util.spec_from_file_location('drawing', source / 'generate-fixtures.py')
        drawing = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(drawing)
        drawing.ROOT = root
        scene = json.loads((root / 'menubar-pressed.json').read_text())
        # Independent source-ink measurement catches the original fixture bug:
        # [66,0,34,30] began at F instead of centring its padded title slot.
        _, _, source_pixel = read_png(source / 'menubar.png')
        columns = [x for x in range(100, 190)
                   if any(max(source_pixel(x, y)) > 20 for y in range(10, 50))]
        title = scene['menubar']['surface']['selectedRect']
        ink_left, ink_right = min(columns) / 2, (max(columns) + 1) / 2
        assert abs(title[0] + title[2] / 2 - (ink_left + ink_right) / 2) <= 1, 'title slot centred on captured File'
        assert ink_left - title[0] >= 5 and title[0] + title[2] - ink_right >= 5, 'both lips clear the glyph'
        counts = []
        for style in ('transparent', 'accent', 'dark'):
            if style != 'transparent':
                background = (.08, .35, .72, 1) if style == 'accent' else (.08, .09, .12, 1)
                def draw(box, label):
                    box(0, 0, 1200, 60, background)
                    label(22, 41, 'Notes     File     Edit     View     Format     Window     Help', 27, (1, 1, 1, 1))
                drawing.image(style + '.png', 1200, 60, draw)
                scene['menubar']['texture'] = str(root / (style + '.png'))
            manifest = root / (style + '.json')
            manifest.write_text(json.dumps(scene))
            output = root / (style + '-render.png')
            subprocess.run([str(binary), '--render-scene', str(manifest), str(output), '--eye', '0,.65,0',
                            '--look', '0,-10', '--fov', '48', '--size', '1400x700'], check=True)
            _, _, pixel = read_png(output)
            # The File title rectangle, projected by the fixed diagnostic camera.
            colors = [pixel(x, y) for y in range(241, 287) for x in range(178, 229)]
            face = sum(min(c) > 100 for c in colors)
            ink = sum(max(c) < 95 for c in colors)
            glow = sum(c[2] - c[0] > 35 and c[2] > 150 for c in colors)
            assert face > len(colors) // 2, (style, 'pressed face must not be black', face)
            assert ink > 50, (style, 'File ink must remain visible', ink)
            assert glow > 20, (style, 'inward blue bevel reflection', glow)
            # Stay inside the corrected title slot. The old right-shifted ROI
            # included the dark capture background outside the inset.
            glyph = [(x, y) for y in range(256, 278) for x in range(184, 230)
                     if max(pixel(x, y)) < 75]
            assert glyph and min(x for x, _ in glyph) >= 186, (style, 'F clears left lip')
            assert max(x for x, _ in glyph) < 229, (style, 'legend clears right lip')
            counts.append((style, face, ink, glow))
        assert min(c[2] for c in counts) > max(c[2] for c in counts) * .6, ('glyph coverage survives capture style', counts)
        print('Pressed-title image checks passed:', counts)
        # One metre from the window centre, looking along its right wall.
        # This samples the actual full-depth wall, away from the front bezel:
        # a flat slab lacks the broad normal gradient, and a 15 mm wall ends
        # halfway through this region. Abrupt facet bands fail smoothness.
        pitch, yaw = math.radians(7.6), math.radians(75)
        eye = f'{math.cos(pitch)*math.sin(yaw)},{math.sin(pitch)},{-1.65+math.cos(pitch)*math.cos(yaw)}'
        output = root / 'wall-profile.png'
        subprocess.run([str(binary), '--render-scene', str(source / 'example.json'), str(output),
                        '--eye', eye, '--look', '-75,-7.6', '--fov', '60', '--size', '1600x1000'], check=True)
        _, _, pixel = read_png(output)
        profile = [sum(pixel(x, 500)) / 3 for x in range(962, 1066)]
        assert min(profile) > 60, ('full 3 cm wall remains visible', min(profile))
        assert profile[0] - profile[-1] > 45, ('rounded wall has broad lighting gradient', profile[0], profile[-1])
        assert max(abs(a-b) for a, b in zip(profile, profile[1:])) < 8, 'wall profile has no abrupt shading bands'
        print('Rounded-wall production profile passed:', round(profile[0], 2), round(profile[-1], 2))


if __name__ == '__main__':
    main()
