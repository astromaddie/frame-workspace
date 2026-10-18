#!/usr/bin/env python3
"""Selection/exit diagnostics through the real executable. No OpenVR runtime.
Pass --expect-no-v4l2 only in a host without a compatible V4L2 decoder device.
Pass --use-display to use the existing GL display without forcing software GL.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

binary = str(Path(sys.argv[1]).resolve())
use_display = "--use-display" in sys.argv[2:]
with tempfile.TemporaryDirectory(prefix="native-decoder-") as directory:
    root = Path(directory)
    invalid = subprocess.run([binary, "--decoder", "auto"], capture_output=True, text=True)
    assert invalid.returncode == 1
    assert "decoder must be software or v4l2" in invalid.stdout + invalid.stderr
    state = root / "state.json"
    pattern = subprocess.run(
        [*([] if use_display else ["xvfb-run", "-a"]),
         binary, "--no-vr", "--test-pattern", "--seconds", ".2",
         "--state-file", str(state)], capture_output=True, text=True, timeout=10,
        env=(os.environ.copy() if use_display else
             {**os.environ, "LIBGL_ALWAYS_SOFTWARE": "1"}))
    assert pattern.returncode == 0, pattern.stderr
    value = json.loads(state.read_text())
    assert value["decoderRequested"] == "software" and value["decoderActive"] == ""
    assert value["exitReason"] == "timer" and value["uploaded"] > 0, value
    assert "exit reason=timer" in pattern.stdout + pattern.stderr
    # Exercise the exact parser used before V4L2 open, including cropped coded
    # dimensions and consecutive resolution changes in one parser instance.
    paths=[]
    for width,height in [(320,180),(2880,2560),(640,360)]:
        target=root / f"{width}x{height}.h264"
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i',f'color=c=red:s={width}x{height}',
                        '-frames:v','1','-c:v','libx264','-preset','ultrafast','-tune','zerolatency',str(target)],check=True)
        paths += ['--inspect-h264',str(target)]
    inspected=subprocess.run([binary,*paths],capture_output=True,text=True,check=True)
    dimensions=[json.loads(line) for line in inspected.stdout.splitlines()]
    assert dimensions==[
        {'width':320,'height':180,'codedWidth':320,'codedHeight':192,'keyFrame':1},
        {'width':2880,'height':2560,'codedWidth':2880,'codedHeight':2560,'keyFrame':1},
        {'width':640,'height':360,'codedWidth':640,'codedHeight':368,'keyFrame':1}],dimensions
    if "--expect-no-v4l2" in sys.argv[2:]:
        subprocess.run([sys.executable,str(Path(__file__).with_name('tests_stream.py')),binary,
                        '--expect-no-v4l2', *(["--use-display"] if use_display else [])],check=True)
    print(json.dumps({"selection_and_exit": "passed", "v4l2_unavailable_fail_closed":
                      "passed" if "--expect-no-v4l2" in sys.argv[2:] else "not requested"}))
