#!/usr/bin/env python3
"""Render the current Mac desktop's window pictures without changing any windows.

Requires pre-existing Screen Recording permission. Never requests permission.
All source captures and the scene manifest live in a private temporary directory
and are deleted even on failure. Only the two final PNGs are retained.
"""
import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = Path('/private/tmp/claude-501/-Users-mads-code-frame-workspace/'
                   'd6cfb87f-3bf9-45b7-bf70-3ae621c211ed/scratchpad/real-spatial-scene.png')

# Only authorized content: window metadata needed to select their pictures,
# running-app icons, and the built-in screen's menu-bar rectangle. No AX calls,
# text extraction, window activation, workspace changes, or permission requests.
SWIFT = r'''
import AppKit
import CoreGraphics
import Foundation

guard CGPreflightScreenCaptureAccess() else {
    FileHandle.standardError.write(Data("Screen Recording permission is missing; no capture or permission request was made.\n".utf8))
    exit(3)
}
let destination = URL(fileURLWithPath: CommandLine.arguments[1])
let apps = NSWorkspace.shared.runningApplications.filter { $0.activationPolicy == .regular }
let pids = Set(apps.map { $0.processIdentifier })
let allWindows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] ?? []
let windows = allWindows.filter {
    guard let layer = $0[kCGWindowLayer as String] as? Int,
          let pid = $0[kCGWindowOwnerPID as String] as? Int32,
          let alpha = $0[kCGWindowAlpha as String] as? Double,
          let bounds = $0[kCGWindowBounds as String] as? [String: Double] else { return false }
    return layer == 0 && pids.contains(pid) && alpha > 0 &&
           (bounds["Width"] ?? 0) >= 160 && (bounds["Height"] ?? 0) >= 100
}.prefix(6).compactMap { window -> [String: Any]? in
    guard let id = window[kCGWindowNumber as String] as? Int else { return nil }
    return ["id": id]
}
var icons: [[String: Any]] = []
for (index, app) in apps.enumerated() {
    guard let image = app.icon,
          let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 128, pixelsHigh: 128,
                      bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                      colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0),
          let context = NSGraphicsContext(bitmapImageRep: bitmap) else { continue }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    image.draw(in: NSRect(x: 0, y: 0, width: 128, height: 128), from: .zero,
               operation: .sourceOver, fraction: 1)
    NSGraphicsContext.restoreGraphicsState()
    let filename = "icon-\(index).png"
    guard let png = bitmap.representation(using: .png, properties: [:]) else { continue }
    try png.write(to: destination.appendingPathComponent(filename))
    icons.append(["id": app.bundleIdentifier ?? "app.\(index)",
                  "name": app.localizedName ?? "App", "icon": filename])
}
guard let screen = NSScreen.screens.first(where: {
    guard let id = $0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? CGDirectDisplayID else { return false }
    return CGDisplayIsBuiltin(id) != 0
}), let id = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? CGDirectDisplayID else {
    FileHandle.standardError.write(Data("Built-in display unavailable; no menu capture was made.\n".utf8)); exit(4)
}
let bounds = CGDisplayBounds(id)
let height = max(24, screen.frame.maxY - screen.visibleFrame.maxY)
let bar = CGRect(x: bounds.minX, y: bounds.minY, width: bounds.width, height: height)
// Same layer-25 menu/status boundary as SpatialShared.refresh(). No AX reads.
let statusFrames = allWindows.compactMap { window -> CGRect? in
    guard window[kCGWindowLayer as String] as? Int == 25,
          let raw = window[kCGWindowBounds as String] as? [String: Any],
          let rect = CGRect(dictionaryRepresentation: raw as CFDictionary), rect.intersects(bar) else { return nil }
    return rect
}
let split = min(bar.maxX, max(bar.minX, SpatialSharedPolicy.menuBarSplit(statusItemFrames: statusFrames) ?? bar.maxX))
let menus = CGRect(x: bar.minX, y: bar.minY, width: max(1, split-bar.minX), height: height)
let status = CGRect(x: split, y: bar.minY, width: max(1, bar.maxX-split), height: height)
let result: [String: Any] = ["windows": Array(windows), "apps": icons,
    "menubar": SpatialSharedPolicy.rectJSON(menus), "status": SpatialSharedPolicy.rectJSON(status)]
FileHandle.standardOutput.write(try JSONSerialization.data(withJSONObject: result))
'''


def png_size(path):
    with path.open('rb') as stream:
        header = stream.read(24)
    if len(header) != 24 or header[:8] != b'\x89PNG\r\n\x1a\n':
        raise RuntimeError('Capture did not produce a PNG')
    return struct.unpack('>II', header[16:24])


def capture(path, *options):
    subprocess.run(['/usr/sbin/screencapture', '-x', '-o', '-t', 'png', *options, str(path)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    png_size(path)


def surface(texture, rect, **fields):
    return dict(texture=texture, surface=dict(t='surface', v=1, seq=1, visible=True,
                anchor='none', rect=rect, anchorRect=rect, **fields))


@contextmanager
def private_work(scratch):
    with tempfile.TemporaryDirectory(prefix='mac-scene-', dir=scratch) as temporary:
        work = Path(temporary)
        try:
            yield work
        finally:
            reclaimed = sum(p.stat().st_size for p in work.rglob('*') if p.is_file())
            print(f'Deleted {reclaimed} Bytes Of Intermediate Capture And Compiler Data')


def run(args):
    if sys.platform != 'darwin':
        raise RuntimeError('Real Desktop Capture Requires macOS')
    binary = args.binary.resolve()
    if not binary.is_file():
        raise RuntimeError('Build The Native Renderer First')
    output = args.output.resolve()
    slim = output.with_name(output.stem + '-slim.png')
    if output.exists() or slim.exists():
        raise RuntimeError('Output Already Exists; Choose A New Output Path')
    output.parent.mkdir(parents=True, exist_ok=True)
    scratch = args.scratch.resolve() if args.scratch else output.parent / 'render'
    scratch.mkdir(parents=True, exist_ok=True)
    os.umask(0o077)
    with private_work(scratch) as work:
        probe = work / 'capture.swift'
        policy = ROOT/'mac/frame-mac-view/Sources/SpatialSharedPolicy.swift'
        probe.write_text(policy.read_text() + "\n" + SWIFT)
        env = dict(os.environ, CLANG_MODULE_CACHE_PATH=str(work / 'clang-cache'),
                   SWIFT_MODULECACHE_PATH=str(work / 'swift-cache'))
        while True:
            pressure = subprocess.check_output(['memory_pressure'], text=True).splitlines()[-1]
            if int(pressure.rsplit(' ', 1)[-1].rstrip('%')) >= 15:
                break
            print('Waiting For At Least 15% Free Memory Before Swift Compilation', flush=True)
            time.sleep(30)
        result = subprocess.run(['/usr/bin/swift', str(probe), str(work)], env=env,
                                capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or 'Mac Capture Preflight Failed')
        inventory = json.loads(result.stdout)
        if not inventory['windows']:
            raise RuntimeError('No Eligible On-Screen Windows')
        panels = []
        focused_pixels = 0
        for index, window in enumerate(inventory['windows']):
            path = work / f'window-{index}.png'
            capture(path, '-l', str(window['id']))
            w, h = png_size(path)
            if index == 0:
                focused_pixels = w
            # Actual Spatial Mode has no automatic arc: _show_native_window
            # only calls beside_workspace_pose in Desktop Mode. New Spatial
            # panels use Placement defaults + Overlay::recenter (identity HMD).
            panels.append(dict(id=f'window-{index}', role='window', texture=path.name,
                               focused=index == 0, height=2.4*h/w,
                               placement=dict(version=1, width=2.4, distance=2,
                                              curvature=0, matrix=[1,0,0,0,0,1,0,0,0,0,1,-2])))
        menu_rect = [round(v) for v in inventory['menubar']]
        status_rect = [round(v) for v in inventory['status']]
        capture(work/'menubar.png', '-R', ','.join(map(str,menu_rect)))
        capture(work/'status.png', '-R', ','.join(map(str,status_rect)))
        scene = dict(panels=panels, apps=inventory['apps'],
                     menubar=surface('menubar.png',menu_rect,hot=True,menusWidth=menu_rect[2]),
                     status=surface('status.png',status_rect))
        # Frame the existing surfaces by turning the diagnostic camera only.
        # These bounds follow the flat recentered panels and the host's menu
        # scale; they never change panel placement, width, or capture content.
        mpp = 2 * 2.4 / focused_pixels
        angles = [math.atan2(-1.35, 1.97), math.atan2(1.35, 1.97),
                  math.atan2(-1.2, 1.984), math.atan2(-1.2+menu_rect[2]*mpp, 1.984)]
        count = max(1,len(inventory['apps']))
        shelf_width = min(.105,1.35/count)*count+.022
        angles.append(math.atan2(shelf_width/2+.006+status_rect[2]*.001,1.286))
        low, high = map(math.degrees,(min(angles),max(angles)))
        if high-low > 96:
            raise RuntimeError('Scene Exceeds The 100-Degree View; Use An Explicit Scene Camera')
        yaw = (low+high)/2 if low < -48 or high > 48 else 0
        manifest = work/'scene.json'
        for hot, destination in [(True,output),(False,slim)]:
            scene['menubar']['surface']['hot'] = hot
            manifest.write_text(json.dumps(scene))
            rendered = work/('wide.png' if hot else 'slim.png')
            subprocess.run([str(binary),'--render-scene',str(manifest),str(rendered),'--look',f'{yaw},0'],check=True)
        # Publish only after both renders succeeded, with private permissions.
        os.replace(work/'wide.png',output)
        os.replace(work/'slim.png',slim)
        print(f'Rendered {len(panels)} Windows Using Production Spatial Defaults; Camera Yaw {yaw:.2f} Degrees')
        print(output)
        print(slim)
    print('Deleted All Intermediate Captures, Icons, Scene Data, And Compiler Cache')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary',type=Path,default=ROOT/'native/build/frame-workspace-native')
    parser.add_argument('--output',type=Path,default=DEFAULT_OUT)
    parser.add_argument('--scratch',type=Path)
    args = parser.parse_args()
    try:
        run(args)
    except (RuntimeError,OSError,ValueError,subprocess.CalledProcessError) as exc:
        print(str(exc),file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
