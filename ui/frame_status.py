"""Runs ON the Steam Frame (piped over SSH as `python3 -`); prints one JSON object.

Read-only. Every probe is best effort: a missing tool or file gives null, not an
error. Sensor paths verified on SteamOS 0.3.0; OpenVR timing on 0.4.1
(build 20260925.6191901). See docs/vr-utilities.md.
"""
import ctypes as C
import glob
import hashlib
import math
import json
import os
import re
import socket
import subprocess
import time

HOME = os.path.expanduser("~")
STEAM = os.path.join(HOME, ".local/share/Steam")
# Runtimes and compatibility tools that show up as "apps" in steamapps/.
TOOL_NAME = re.compile(r"^(Steam Linux Runtime|Proton|Steamworks Common|FEX$|Lepton Development$)")


def read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def run(*cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def num(path, scale=1.0):
    v = read(path)
    try:
        return int(v) * scale
    except (TypeError, ValueError):
        return None


def battery():
    for d in glob.glob("/sys/class/power_supply/*"):
        if read(d + "/type") != "Battery":
            continue
        cap = read(d + "/capacity")
        # max1720x reports microvolts/microamps; current is positive while charging.
        volts, amps = num(d + "/voltage_now", 1e-6), num(d + "/current_now", 1e-6)
        return {"percent": int(cap) if cap and cap.isdigit() else None,
                "status": read(d + "/status"),
                "watts": round(volts * amps, 2) if volts is not None and amps is not None else None,
                "timeToFull": num(d + "/time_to_full_now"),
                "timeToEmpty": num(d + "/time_to_empty_now"),
                "tempC": num(d + "/temp", 0.1),
                "health": read(d + "/health")}
    return None


def power_source():
    """The plugged-in charger, if any: {'type': 'C PD [PD_PPS]', 'watts': 20.0}."""
    for d in glob.glob("/sys/class/power_supply/*"):
        if read(d + "/type") == "USB" and read(d + "/online") == "1":
            volts, amps = num(d + "/voltage_now", 1e-6), num(d + "/current_now", 1e-6)
            return {"type": read(d + "/usb_type"),
                    "watts": round(volts * amps, 1) if volts and amps else None}
    for d in glob.glob("/sys/class/power_supply/*"):
        if read(d + "/type") != "Battery" and read(d + "/online") == "1":
            return {"type": None, "watts": None}
    return None


def disk(path):
    try:
        s = os.statvfs(path)
    except OSError:
        return None
    return {"total": s.f_blocks * s.f_frsize, "free": s.f_bavail * s.f_frsize}


def memory():
    info = {}
    for line in (read("/proc/meminfo") or "").splitlines():
        k, _, v = line.partition(":")
        info[k] = int(v.split()[0]) * 1024 if v.split() else 0
    if "MemTotal" not in info:
        return None
    return {"total": info["MemTotal"], "available": info.get("MemAvailable", 0)}


def max_temp():
    temps = []
    for z in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        t = read(z)
        if t and t.lstrip("-").isdigit():
            temps.append(int(t) / 1000)
    return max(temps) if temps else None


def wifi():
    for line in run("nmcli", "-t", "-f", "active,ssid,signal", "dev", "wifi").splitlines():
        # nmcli escapes ':' inside fields as '\:'.
        parts = re.split(r"(?<!\\):", line)
        if len(parts) >= 3 and parts[0] == "yes":
            return {"ssid": parts[1].replace("\\:", ":"),
                    "signal": int(parts[2]) if parts[2].isdigit() else None}
    return None


def ip_addr():
    m = re.search(r"\s(\d+\.\d+\.\d+\.\d+)/", run("ip", "-4", "-brief", "addr", "show", "scope", "global"))
    return m.group(1) if m else None


def os_release():
    out = {}
    for line in (read("/etc/os-release") or "").splitlines():
        k, _, v = line.partition("=")
        out[k] = v.strip('"')
    return {"version": out.get("VERSION_ID"), "build": out.get("BUILD_ID"),
            "variant": out.get("VARIANT_ID"), "headset": headset_id()}


def headset_id():
    """A one-way hash of the headset's /etc/machine-id: the same for this headset on every
    computer, and the machine-id itself never leaves the headset. None without one."""
    mid = read("/etc/machine-id")
    if not mid:
        return None
    return hashlib.sha256(("frame-control-headset:" + mid).encode()).hexdigest()[:16]


def volume():
    m = re.search(r"Volume:\s*([\d.]+)(.*)", run("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"))
    if not m:
        return None
    return {"level": float(m.group(1)), "muted": "MUTED" in m.group(2)}


def process_names():
    return set(run("ps", "-e", "-o", "comm=").split())  # xrdp runs as root


def port_listening(port):
    return f":{port} " in run("ss", "-ltn")


def games():
    out = []
    for f in glob.glob(os.path.join(STEAM, "steamapps/appmanifest_*.acf")):
        text = read(f) or ""
        fields = dict(re.findall(r'^\s*"(appid|name|SizeOnDisk)"\s+"([^"]*)"', text, re.M))
        if fields.get("appid", "").isdigit() and not TOOL_NAME.match(fields.get("name", "")):
            out.append({"appid": fields["appid"], "name": fields.get("name", fields["appid"]),
                        "size": int(fields.get("SizeOnDisk", "0")) if fields.get("SizeOnDisk", "").isdigit() else 0})
    return sorted(out, key=lambda g: g["name"].lower())


def flatpaks():
    out = []
    for line in run("flatpak", "list", "--app", "--columns=application,name,version,installation").splitlines():
        p = line.split("\t")
        if len(p) == 4:
            out.append({"id": p[0], "name": p[1], "version": p[2], "installation": p[3]})
    return out


def thermal_alerts():
    """Use the kernel's per-zone hot/critical trips, never a guessed chip limit."""
    alerts, known = [], False
    for z in glob.glob("/sys/class/thermal/thermal_zone*"):
        t = num(z + "/temp", 0.001)
        for trip in glob.glob(z + "/trip_point_*_type"):
            if read(trip) not in ("hot", "critical"):
                continue
            limit = num(trip[:-4] + "temp", 0.001)
            if t is not None and limit is not None and limit > 0:
                known = True
                if t >= limit:
                    alerts.append({"zone": read(z + "/type"), "tempC": t, "limitC": limit})
    return alerts if known else None


def activity_level():
    try:
        rows = json.loads(run("/opt/steamvr/bin/linuxarm64/vrcmd", "--stats"))
        if not isinstance(rows, list):
            return None
        return next((r.get("activity_level") for r in rows
                     if isinstance(r, dict) and r.get("operation") == "status"), None)
    except (ValueError, TypeError):
        return None


# OpenVR C ABI, ValveSoftware/openvr headers/openvr_capi.h (IVRCompositor_029).
# Exact interface version: never guess an index against a different table.
class Pose(C.Structure):
    _fields_ = [('matrix', C.c_float * 12), ('velocity', C.c_float * 3),
                ('angular', C.c_float * 3), ('result', C.c_int),
                ('valid', C.c_bool), ('connected', C.c_bool)]


class Timing(C.Structure):
    _fields_ = [(n, C.c_uint32) for n in ('size', 'index', 'presents', 'mis', 'dropped', 'flags')] + [
        ('time', C.c_double)] + [(n, C.c_float) for n in (
        'gpuPre', 'gpuPost', 'gpu', 'compositorGpu', 'compositorCpu', 'idleCpu', 'interval',
        'presentCpu', 'waitCpu', 'submit', 'posesCalled', 'posesReady', 'frameReady',
        'updateStart', 'updateEnd', 'renderStart')] + [
        ('pose', Pose), ('ready', C.c_uint32), ('first', C.c_uint32), ('transfer', C.c_float)]


class OpenVR:
    def __enter__(self):
        self.lib = C.CDLL('/opt/steamvr/bin/linuxarm64/libopenvr_api.so')
        self.lib.VR_InitInternal2.argtypes = [C.POINTER(C.c_int), C.c_int, C.c_char_p]
        self.lib.VR_GetGenericInterface.argtypes = [C.c_char_p, C.POINTER(C.c_int)]
        self.lib.VR_GetGenericInterface.restype = C.c_void_p
        err = C.c_int()
        self.lib.VR_InitInternal2(C.byref(err), 3, None)  # background: never start or keep SteamVR running
        if err.value:
            raise RuntimeError(f'SteamVR unavailable (init {err.value})')
        return self

    def __exit__(self, *args):
        self.lib.VR_ShutdownInternal()

    def function(self, interface, index, result, *args):
        err = C.c_int()
        ptr = self.lib.VR_GetGenericInterface(('FnTable:' + interface).encode(), C.byref(err))
        if err.value or not ptr:
            raise RuntimeError(f'{interface} unavailable ({err.value})')
        return C.CFUNCTYPE(result, *args)(C.cast(ptr, C.POINTER(C.c_void_p))[index])


def cpu_ticks():
    line = (read('/proc/stat') or '').splitlines()
    if not line or not line[0].startswith('cpu '):
        return None
    try:
        # guest/guest_nice are already included in user/nice.
        ticks = [int(x) for x in line[0].split()[1:9]]
        return sum(ticks), ticks[3] + ticks[4]
    except (ValueError, IndexError):
        return None


def cpu_percent(before, after):
    if before is None or after is None or after[0] <= before[0]:
        return None
    return round(max(0, min(100, 100 * (1 - (after[1] - before[1]) / (after[0] - before[0])))), 1)


def positive(value):
    return round(value, 2) if math.isfinite(value) and value > 0 else None


def timing_values(before, after):
    dt, frames = after.time - before.time, after.index - before.index
    if dt <= 0 or frames <= 0 or frames / dt > 1000:
        return {}  # standby or old data; never present stale timings as live
    return {'compositorFps': positive(frames / dt),
            'frameMs': positive(1000 * dt / frames),
            'appFps': positive(1000 / after.interval) if after.interval > 0 else None,
            'gpuMs': positive(after.gpu), 'compositorCpuMs': positive(after.compositorCpu)}


def performance():
    out = {'compositorFps': None, 'frameMs': None, 'appFps': None,
           'gpuMs': None, 'compositorCpuMs': None, 'cpuPercent': None,
           'gpuMHz': num('/sys/class/devfreq/3d00000.gpu/cur_freq', 1e-6)}
    before = cpu_ticks()
    try:
        with OpenVR() as vr:
            get = vr.function('IVRCompositor_029', 10, C.c_bool, C.POINTER(Timing), C.c_uint32)
            a, b = Timing(), Timing()
            a.size = b.size = C.sizeof(Timing)
            first = get(C.byref(a), 0)
            time.sleep(0.2)
            if first and get(C.byref(b), 0):
                out.update(timing_values(a, b))
    except (OSError, RuntimeError, AttributeError):  # AttributeError: a SteamVR build without these exports
        time.sleep(0.2)
    out['cpuPercent'] = cpu_percent(before, cpu_ticks())
    return out


def status():
    uptime = read("/proc/uptime")
    procs = process_names()
    return {
        "time": time.time(),
        "performance": performance(),
        "hostname": socket.gethostname(),
        "os": os_release(),
        "uptime": float(uptime.split()[0]) if uptime else None,
        "battery": battery(),
        "power": power_source(),
        "disk": {"root": disk("/"), "home": disk("/home")},
        "memory": memory(),
        "temp": max_temp(),
        "thermalAlerts": thermal_alerts(),
        "wifi": wifi(),
        "ip": ip_addr(),
        "volume": volume(),
        "services": {
            "steamvr": "vrserver" in procs,
            "desktop": "plasmashell" in procs,
            "lepton": port_listening(5555),
            "rdp": "xrdp" in procs,
        },
        "games": games(),
        "flatpaks": flatpaks(),
    }


def main():
    print(json.dumps(status()))


if __name__ == "__main__":
    main()
