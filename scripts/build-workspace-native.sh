#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
out="${1:-$repo/native/build}"
mkdir -p "$out"
read -r -a flags <<< "$(pkg-config --cflags --libs sdl2 gl libavcodec libavutil libswscale json-c openssl)"
"${CXX:-c++}" -std=c++17 -O2 -g -Wall -Wextra -Wpedantic -pthread \
  "$repo/native/main.cpp" "$repo/native/gaze_bridge.cpp" -o "$out/frame-workspace-native" "${flags[@]}" -ldl
"$out/frame-workspace-native" --self-test
python3 - "$repo" "$out" <<'PY'
import hashlib,json,pathlib,platform,subprocess,sys
root,out=map(pathlib.Path,sys.argv[1:])
files=[p for p in (root/'native').rglob('*') if p.is_file()
       and not {'build','__pycache__'}.intersection(p.relative_to(root/'native').parts)
       and p.suffix not in {'.pyc','.pyo'}]
meta={'schema':1,'source_revision':subprocess.run(['git','-C',str(root),'rev-parse','HEAD'],capture_output=True,text=True).stdout.strip(),'tree_state':subprocess.run(['git','-C',str(root),'status','--porcelain'],capture_output=True,text=True).stdout.strip(),'arch':platform.machine(),'dependencies':subprocess.check_output(['pkg-config','--modversion','sdl2','gl','libavcodec','libavutil','libswscale','json-c','openssl'],text=True).splitlines(),'source_sha256':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(files)},'binary_sha256':hashlib.sha256((out/'frame-workspace-native').read_bytes()).hexdigest()}
(out/'BUILD-METADATA.json').write_text(json.dumps(meta,indent=2)+'\n')
print('native build verified:',meta['binary_sha256'])
PY
