#!/usr/bin/env python3
"""Production decoder/shader/GL readback checks for optional GPU YUV conversion.

Requires the ARM64 native binary, FFmpeg with libx264 and the production
WebSocket integration harness. Uses Xvfb by default; `--use-display` runs on
the existing display. No OpenVR runtime or headset is used.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

binary = Path(sys.argv[1]).resolve()
stream_test = Path(__file__).with_name("tests_stream.py")
use_display = "--use-display" in sys.argv[2:]


def run_stream(*args):
    result = subprocess.run(
        [sys.executable, str(stream_test), str(binary), "--conversion", "gpu",
         *args, *( ["--use-display"] if use_display else [] )],
        capture_output=True,
        text=True,
        timeout=30,
        env=os.environ.copy(),
    )
    if result.returncode:
        raise AssertionError(result.stdout + result.stderr)
    report = json.loads(result.stdout.splitlines()[-1])
    print(json.dumps(report, sort_keys=True))


def yuv_bytes(pixel_format, color_range, pattern):
    width, height = 320, 180
    full = color_range == "full"
    y_plane = bytearray(width * height)
    for y in range(height):
        for x in range(width):
            if pattern == "sharp":
                tile = (x // (width // 2) + 2 * (y // (height // 2))) % 4
                y_plane[y * width + x] = tile * 85 if full else 16 + tile * 73
            else:
                y_plane[y * width + x] = (
                    x * 255 // (width - 1) if full
                    else 16 + x * 219 // (width - 1)
                )

    chroma_width, chroma_height = width // 2, height // 2
    u_plane, v_plane = bytearray(chroma_width * chroma_height), bytearray(chroma_width * chroma_height)
    for y in range(chroma_height):
        for x in range(chroma_width):
            if pattern == "sharp":
                tile = (2 * x // (width // 2) + 2 * (2 * y // (height // 2))) % 4
                u = 24 + tile * 68 if full else 32 + tile * 64
                v = 232 - tile * 68 if full else 224 - tile * 64
            else:
                u = (
                    x * 255 // (chroma_width - 1) if full
                    else 16 + x * 224 // (chroma_width - 1)
                )
                v = (
                    255 - y * 255 // (chroma_height - 1) if full
                    else 240 - y * 224 // (chroma_height - 1)
                )
            i = y * chroma_width + x
            u_plane[i], v_plane[i] = u, v
    if pixel_format == "nv12":
        uv = bytearray(len(u_plane) * 2)
        for i, (u, v) in enumerate(zip(u_plane, v_plane)):
            uv[2 * i], uv[2 * i + 1] = u, v
        return bytes(y_plane + uv)
    return bytes(y_plane + u_plane + v_plane)


def run_shader_layout(pixel_format, color_range, colorspace, pattern):
    width, height = 320, 180
    matrix = "bt709" if colorspace == "bt709" else "bt601"
    raw = yuv_bytes(pixel_format, color_range, pattern)
    with tempfile.TemporaryDirectory(prefix="native-yuv-shader-") as directory:
        root = Path(directory)
        source, dump, cpu_dump = root / "planes.yuv", root / "gpu.ppm", root / "cpu.ppm"
        source.write_bytes(raw)
        expected = subprocess.check_output(
            [
                "ffmpeg", "-v", "error", "-f", "rawvideo", "-pixel_format", pixel_format,
                "-video_size", f"{width}x{height}", "-i", str(source), "-frames:v", "1",
                "-vf", f"scale=in_range={'pc' if color_range == 'full' else 'tv'}:out_range=pc:in_color_matrix={matrix}:out_color_matrix={matrix}",
                "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
            ]
        )
        result = subprocess.run(
            [
                *( [] if use_display else ["xvfb-run", "-a"] ),
                str(binary), "--no-vr", "--test-pattern",
                "--conversion", "gpu", "--test-yuv-format", pixel_format,
                "--test-yuv-range", color_range, "--test-yuv-colorspace", colorspace,
                "--test-yuv-pattern", pattern,
                "--seconds", ".2", "--dump", str(dump),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=(os.environ.copy() if use_display else
                 {**os.environ, "LIBGL_ALWAYS_SOFTWARE": "1"}),
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)
        raw_rgb = dump.read_bytes().split(b"\n", 3)[3]
        cpu_result = subprocess.run(
            [
                *( [] if use_display else ["xvfb-run", "-a"] ),
                str(binary), "--no-vr", "--test-pattern", "--conversion", "cpu",
                "--test-yuv-format", pixel_format, "--test-yuv-range", color_range,
                "--test-yuv-colorspace", colorspace, "--test-yuv-pattern", pattern,
                "--test-yuv-oracle", "--seconds", ".2", "--dump", str(cpu_dump),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            env=(os.environ.copy() if use_display else
                 {**os.environ, "LIBGL_ALWAYS_SOFTWARE": "1"}),
        )
        if cpu_result.returncode:
            raise AssertionError(cpu_result.stdout + cpu_result.stderr)
        cpu_rgb = cpu_dump.read_bytes().split(b"\n", 3)[3]
        assert len(raw_rgb) == len(cpu_rgb) == len(expected), (
            len(raw_rgb), len(cpu_rgb), len(expected)
        )
        deltas = [abs(a - b) for a, b in zip(raw_rgb, cpu_rgb)]
        oracle_delta = max(deltas)
        cli_delta = max(abs(a - b) for a, b in zip(raw_rgb, expected))
        cpu_cli_delta = max(abs(a - b) for a, b in zip(cpu_rgb, expected))
        if oracle_delta > 4:
            worst = max(range(len(raw_rgb)), key=lambda i: abs(raw_rgb[i] - cpu_rgb[i]))
            pixel = worst // 3
            x, y = pixel % width, pixel // width
            neighborhood = []
            for row in range(max(0, y - 1), min(height, y + 2)):
                neighborhood.append([
                     (list(raw_rgb[(row * width + col) * 3:(row * width + col + 1) * 3]),
                     list(cpu_rgb[(row * width + col) * 3:(row * width + col + 1) * 3]))
                    for col in range(max(0, x - 1), min(width, x + 2))
                ])
            raise AssertionError(
                f"{pixel_format} {color_range} {colorspace} {pattern}: "
                f"GPU vs production swscale max RGB delta {oracle_delta}; "
                f"GPU vs CLI FFmpeg={cli_delta}, CPU vs CLI FFmpeg={cpu_cli_delta}; "
                f"pixel={(pixel % width, pixel // width)} channel={worst % 3}; "
                f"neighborhood={neighborhood}; "
                f"actual_head={list(raw_rgb[:24])} expected_head={list(expected[:24])}; "
                f"actual_range=({min(raw_rgb)},{max(raw_rgb)}) "
                f"expected_range=({min(expected)},{max(expected)})"
            )
        print(json.dumps({"test": "production GL shader layout", "format": pixel_format,
                          "range": color_range, "colorspace": colorspace,
                          "pattern": pattern,
                          "full_frame_max_rgb_difference_vs_sws_scale": oracle_delta,
                          "full_frame_gpu_vs_ffmpeg_cli": cli_delta,
                          "full_frame_sws_vs_ffmpeg_cli": cpu_cli_delta,
                          "headset_test": False},
                         sort_keys=True))


for pixel_format in ("yuv420p", "nv12"):
    for color_range in ("limited", "full"):
        for colorspace in ("bt601", "bt709"):
            for pattern in ("sharp", "ramp"):
                run_shader_layout(pixel_format, color_range, colorspace, pattern)

for case in ("bt601-limited", "bt601-full", "bt709-limited", "bt709-full"):
    run_stream("--case", case, "--duration", "1.2")

# Unsupported decoded formats retain explicit software conversion fallback.
run_stream("--case", "unsupported-444", "--duration", "1.2")
# The same production YUV420P decoder path must survive context recovery and a
# resolution switch, rebuilding GL plane/output textures at the new size.
run_stream("--case", "bt709-limited", "--reconnect", "--duration", "2.6")
run_stream("--case", "bt709-limited", "--size-change", "--duration", "1.8")
