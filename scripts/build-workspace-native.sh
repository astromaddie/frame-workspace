#!/usr/bin/env bash
# Builds the native Workspace viewer and dashboard, runs the viewer's self-test
# and writes BUILD-METADATA.json. Usage: build-workspace-native.sh [-j N] [OUT]
# -j 2 compiles the viewer and the dashboard at the same time.
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
jobs=1
if [ "${1:-}" = -j ]; then
  [ "$#" -ge 2 ] || { echo 'usage: build-workspace-native.sh [-j N] [OUT]' >&2; exit 2; }
  jobs=$2; shift 2
elif [ "${1:-}" != "${1#-j}" ]; then
  jobs=${1#-j}; shift
fi
case "$jobs" in ''|*[!0-9]*) echo '-j needs a number' >&2; exit 2 ;; esac
out="${1:-$repo/native/build}"
mkdir -p "$out"
identity=$(python3 "$repo/scripts/native_source_identity.py" "$repo")
read -r -a flags <<< "$(pkg-config --cflags --libs sdl2 gl libavcodec libavutil libswscale json-c openssl)"
# The SteamVR bar panel (FW-08): cairo/Pango drawing, no GL.
read -r -a dash_flags <<< "$(pkg-config --cflags --libs cairo pangocairo json-c)"
build_viewer() {
  "${CXX:-c++}" -std=c++17 -O2 -g -Wall -Wextra -Wpedantic -pthread \
    -DFRAME_WORKSPACE_SOURCE_ID="\"$identity\"" \
    "$repo/native/main.cpp" "$repo/native/gaze_bridge.cpp" -o "$out/frame-workspace-native" "${flags[@]}" -ldl
}
build_dash() {
  "${CXX:-c++}" -std=c++17 -O2 -g -Wall -Wextra -Wpedantic \
    "$repo/native/dash.cpp" -o "$out/frame-workspace-dash" "${dash_flags[@]}" -ldl
}
if [ "$jobs" -ge 2 ]; then
  build_dash & dash_pid=$!
  viewer_status=0; build_viewer || viewer_status=$?
  dash_status=0; wait "$dash_pid" || dash_status=$?
  [ "$viewer_status" -eq 0 ] && [ "$dash_status" -eq 0 ] || exit 1
else
  build_viewer
  build_dash
fi
"$out/frame-workspace-native" --self-test
python3 - "$repo" "$out" "$identity" <<'PY'
import hashlib,json,os,pathlib,platform,subprocess,sys
root,out=map(pathlib.Path,sys.argv[1:3])
identity=sys.argv[3]
files=[p for p in (root/'native').rglob('*') if p.is_file()
       and not {'build','__pycache__'}.intersection(p.relative_to(root/'native').parts)
       and p.suffix not in {'.pyc','.pyo'}]
reported=json.loads(subprocess.check_output([str(out/'frame-workspace-native'),'--capabilities'],text=True))
if reported.get('sourceIdentity')!=identity:
    raise SystemExit('the built viewer does not report the source identity it was built from')
def git(*args):
    try: return subprocess.run(['git','-C',str(root),*args],capture_output=True,text=True).stdout.strip()
    except OSError: return ''  # a source copy without Git, such as the headset update
revision=os.environ.get('FRAME_NATIVE_SOURCE_REVISION') or git('rev-parse','HEAD')
meta={'schema':1,'source_identity':identity,'source_revision':revision,'tree_state':'' if os.environ.get('FRAME_NATIVE_SOURCE_REVISION') else git('status','--porcelain'),'arch':platform.machine(),'dependencies':subprocess.check_output(['pkg-config','--modversion','sdl2','gl','libavcodec','libavutil','libswscale','json-c','openssl'],text=True).splitlines(),'source_sha256':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)},'binary_sha256':hashlib.sha256((out/'frame-workspace-native').read_bytes()).hexdigest(),'dash_sha256':hashlib.sha256((out/'frame-workspace-dash').read_bytes()).hexdigest(),'capabilities':reported}
(out/'BUILD-METADATA.json').write_text(json.dumps(meta,indent=2)+'\n')
print('native build verified:',meta['binary_sha256'])
print('native source identity:',identity)
PY
