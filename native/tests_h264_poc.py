#!/usr/bin/env python3
"""The POC type 0 -> 2 rewriter keeps every picture bit-identical.

Encodes short VideoToolbox clips (the Mac's encoder, POC type 0), rewrites
them with `frame-workspace-native --rewrite-poc`, and checks with FFmpeg that
the rewrite declares pic_order_cnt_type 2 and decodes to the same frames.
macOS only (needs h264_videotoolbox). Usage: tests_h264_poc.py BINARY
"""
import subprocess
import sys
import tempfile
from pathlib import Path


def frame_hashes(path):
    out = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-f", "framemd5", "-"],
                         check=True, capture_output=True, text=True).stdout
    return [line.rsplit(",", 1)[-1].strip() for line in out.splitlines() if line and not line.startswith("#")]


def poc_types(path):
    out = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(path), "-c", "copy", "-bsf:v", "trace_headers",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    return {line.split()[-1] for line in out.splitlines() if "pic_order_cnt_type" in line}


def main():
    binary = Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix="fw-poc-") as tmp:
        for size, extra in (("320x180", []), ("1280x720", ["-g", "30"]), ("644x484", ["-b:v", "2M"])):
            src, dst = Path(tmp) / f"{size}.h264", Path(tmp) / f"{size}-poc2.h264"
            subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            f"testsrc2=size={size}:rate=60", "-t", "2", "-c:v", "h264_videotoolbox",
                            "-realtime", "1", "-bf", "0", "-profile:v", "high", *extra, str(src)], check=True)
            assert poc_types(src) == {"0"}, (size, poc_types(src))
            subprocess.run([str(binary), "--rewrite-poc", str(src), str(dst)], check=True)
            before, after = frame_hashes(src), frame_hashes(dst)
            assert poc_types(dst) == {"2"}, (size, poc_types(dst))
            assert len(before) == len(after) == 120 and before == after, (size, len(before), len(after))
            print(f"PASS {size}: {len(after)} frames identical after POC 0 -> 2")


if __name__ == "__main__":
    main()
