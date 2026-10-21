#!/usr/bin/env python3
"""Pressed-title pixel regression through the production scene/material path."""
import importlib.util
import json
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
            colors = [pixel(x, y) for y in range(241, 287) for x in range(190, 241)]
            face = sum(min(c) > 100 for c in colors)
            ink = sum(max(c) < 95 for c in colors)
            glow = sum(c[2] - c[0] > 35 and c[2] > 150 for c in colors)
            assert face > len(colors) // 2, (style, 'pressed face must not be black', face)
            assert ink > 50, (style, 'File ink must remain visible', ink)
            assert glow > 20, (style, 'inward blue bevel reflection', glow)
            counts.append((style, face, ink, glow))
        assert min(c[2] for c in counts) > max(c[2] for c in counts) * .6, ('glyph coverage survives capture style', counts)
        print('Pressed-title image checks passed:', counts)


if __name__ == '__main__':
    main()
