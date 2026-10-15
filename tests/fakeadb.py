#!/usr/bin/env python3
"""A stand-in for Android's adb, for tests/test_quest.py: just enough of `devices -l`,
`shell`, and `pull` for frame_quest.py, run as a real subprocess.

$FAKEADB_STATE is a JSON file:
  devices   [{"serial", "state", "extra" (the rest of the `devices -l` line), "props" {getprop name: value}}]
  root      a folder standing in for each device's file system (remote /x/y is root/x/y)
  packages  {package: [APK paths, as `pm path` prints them]}
  fail_pull a remote path whose pull fails
Every call is appended to $FAKEADB_LOG as a JSON line. Shell commands are the simple
`; `-separated ones frame_quest sends, each answered here: no real shell runs.
"""
import json
import os
import shutil
import sys

args = sys.argv[1:]
with open(os.environ['FAKEADB_LOG'], 'a') as f:
    f.write(json.dumps(args) + '\n')
with open(os.environ['FAKEADB_STATE']) as f:
    state = json.load(f)
root = state.get('root', '')
serial = None
if args[:1] == ['-s']:
    serial, args = args[1], args[2:]


def fail(message, code=1):
    sys.stderr.write(message + '\n')
    sys.exit(code)


def local(remote):
    return os.path.join(root, remote.lstrip('/'))


def size_kb(path):
    total = 0
    for dirpath, _, files in os.walk(path):
        total += sum(os.path.getsize(os.path.join(dirpath, n)) for n in files)
    return max(4, total // 1024)


if args == ['devices', '-l']:
    print('List of devices attached')
    for d in state.get('devices', []):
        print(f"{d['serial']:<22} {d['state']} {d.get('extra', '')}".rstrip())
    sys.exit(0)

device = next((d for d in state.get('devices', []) if d['serial'] == serial), None)
if serial is None or device is None:
    fail(f"adb: device '{serial}' not found")
if device['state'] == 'unauthorized':
    fail('adb: device unauthorized.\nThis adb server\'s $ADB_VENDOR_KEYS is not set')
if device['state'] != 'device':
    fail(f"adb: device {device['state']}")

if args[:1] == ['shell']:
    out = []
    for command in ' '.join(args[1:]).split('; '):
        command = command.replace(' 2>/dev/null', '').strip()
        words = command.split()
        if not words or command == 'true':
            continue
        if words[0] == 'getprop':
            out.append(device.get('props', {}).get(words[1], ''))
        elif words[0] == 'echo':
            out.append(' '.join(words[1:]))
        elif command == 'pm list packages -3':
            out += ['package:' + p for p in state.get('packages', {})]
        elif words[:2] == ['pm', 'path']:
            out += ['package:' + p for p in state.get('packages', {}).get(words[2], [])]
        elif words[:2] == ['du', '-sk']:
            if os.path.isdir(local(words[2])):
                out.append(f'{size_kb(local(words[2]))}\t{words[2]}')
        elif words[:3] == ['stat', '-c', '%s']:
            out += [str(os.path.getsize(local(p.strip("'")))) for p in words[3:]]
        else:
            fail(f'/system/bin/sh: {words[0]}: inaccessible or not found', 127)
    print('\n'.join(out))
    sys.exit(0)

if args[:1] == ['pull'] and len(args) == 3:
    remote, dest = args[1], args[2]
    if remote == state.get('fail_pull'):
        fail(f"adb: error: failed to copy '{remote}' to '{dest}': remote Connection reset")
    src = local(remote)
    if not os.path.exists(src):
        fail(f"adb: error: failed to stat remote object '{remote}': No such file or directory")
    if os.path.isdir(dest):
        dest = os.path.join(dest, os.path.basename(remote.rstrip('/')))
    if os.path.isdir(src):
        shutil.copytree(src, dest)
    else:
        shutil.copyfile(src, dest)
    print(f'{remote}: 1 file pulled.')
    sys.exit(0)

fail('adb: unknown command ' + ' '.join(args))
