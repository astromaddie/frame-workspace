"""Private, versioned workspace profiles and native spatial layouts for MacView.

Profiles contain only durable display preferences and stable window matching
rules. Current Mac window and display IDs, stream tickets, and reconnect keys
are resolved or minted by MacView at restore time and are never stored here.
"""
import json
import os
import re
import stat
import tempfile
import threading
from pathlib import Path

import frame_macview


SCHEMA_VERSION = 2
MAX_STORE_BYTES = 64 * 1024
MAX_PROFILES = 64
MAX_RULES = 24
MAX_NAME = 48
MAX_TITLE_HINT = 200
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,47}\Z")
_BUNDLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,254}\Z")


class ProfileError(ValueError):
    """A profile request or stored profile is invalid."""


class ProfileStoreError(ProfileError):
    """The local profile file could not be safely read or written."""


class _DuplicateJsonKey(ValueError):
    pass


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(key)
        result[key] = value
    return result


def _clean_text(value, field, limit):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ProfileError(f"{field} must be a non-empty string of at most {limit} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ProfileError(f"{field} cannot contain control characters")
    return value


def _pose(value):
    try:
        return frame_macview.validate_pose(value)
    except frame_macview.MacViewError as exc:
        raise ProfileError(str(exc)) from exc


def validate_profile(value, *, allow_poses=True):
    """Validate and return a canonical profile; unknown fields fail closed."""
    if not isinstance(value, dict):
        raise ProfileError("profile must be an object")
    expected = {"name", "workspace", "windows"}
    if set(value) != expected:
        unknown = sorted(set(value) - expected)
        missing = sorted(expected - set(value))
        details = []
        if unknown:
            details.append("unknown fields: " + ", ".join(unknown))
        if missing:
            details.append("missing fields: " + ", ".join(missing))
        raise ProfileError("invalid profile (" + "; ".join(details) + ")")

    name = _clean_text(value["name"], "profile name", MAX_NAME)
    if not _NAME.fullmatch(name) or name.strip() != name:
        raise ProfileError("profile name may contain letters, numbers, spaces, dots, underscores and hyphens")

    workspace = value["workspace"]
    settings = {"preset", "scale", "quality"}
    if (not isinstance(workspace, dict) or not settings <= set(workspace) or
            set(workspace) - settings - ({"pose"} if allow_poses else set())):
        raise ProfileError("workspace must contain exactly preset, scale and quality, with an optional pose")
    preset, scale, quality = workspace["preset"], workspace["scale"], workspace["quality"]
    if not isinstance(preset, str) or preset not in frame_macview.WorkspaceDisplayManager.PRESETS:
        raise ProfileError("workspace preset is unsupported")
    if type(scale) is not int or scale not in (1, 2):
        raise ProfileError("workspace scale must be 1 or 2")
    if not isinstance(quality, str) or quality not in frame_macview.QUALITY:
        raise ProfileError("workspace quality is unsupported")
    # Stored definitions must remain readable when the viewer backend changes.
    # Runtime codec/decoder compatibility is checked by start_workspace instead.
    width, height = frame_macview.WorkspaceDisplayManager.PRESETS[preset]
    if width * height * scale * scale > 67_108_864:
        raise ProfileError("workspace buffer exceeds the safe size limit")

    windows = value["windows"]
    if not isinstance(windows, list) or len(windows) > MAX_RULES:
        raise ProfileError(f"windows must be a list of at most {MAX_RULES} matching rules")
    canonical_rules = []
    seen = set()
    for index, rule in enumerate(windows, 1):
        if (not isinstance(rule, dict) or "bundleID" not in rule or
                set(rule) - {"bundleID", "titleHint"} - ({"pose"} if allow_poses else set())):
            raise ProfileError(f"window rule {index} must contain bundleID, optional titleHint and optional pose only")
        bundle_id = _clean_text(rule["bundleID"], f"window rule {index} bundleID", 255)
        if not _BUNDLE_ID.fullmatch(bundle_id) or "." not in bundle_id:
            raise ProfileError(f"window rule {index} bundleID is not a valid stable bundle identifier")
        title_hint = rule.get("titleHint")
        if title_hint is not None:
            title_hint = _clean_text(title_hint, f"window rule {index} titleHint", MAX_TITLE_HINT)
            if not title_hint.strip():
                raise ProfileError(f"window rule {index} titleHint cannot be blank")
        identity = (bundle_id.casefold(), (title_hint or "").casefold())
        if identity in seen:
            raise ProfileError(f"window rule {index} duplicates an earlier matching rule")
        seen.add(identity)
        clean_rule = {"bundleID": bundle_id}
        if title_hint is not None:
            clean_rule["titleHint"] = title_hint
        if "pose" in rule:
            clean_rule["pose"] = _pose(rule["pose"])
        canonical_rules.append(clean_rule)

    clean_workspace = {"preset": preset, "scale": scale, "quality": quality}
    if "pose" in workspace:
        clean_workspace["pose"] = _pose(workspace["pose"])
    return {"name": name, "workspace": clean_workspace,
            "windows": canonical_rules}


def validate_document(value):
    if not isinstance(value, dict) or set(value) != {"schemaVersion", "profiles"}:
        raise ProfileStoreError("workspace profile file must contain only schemaVersion and profiles")
    version = value["schemaVersion"]
    if type(version) is not int or version not in (1, SCHEMA_VERSION):
        raise ProfileStoreError(f"unsupported workspace profile schema version: {version!r}")
    profiles = value["profiles"]
    if not isinstance(profiles, list) or len(profiles) > MAX_PROFILES:
        raise ProfileStoreError(f"workspace profile file must contain at most {MAX_PROFILES} profiles")
    clean = []
    names = set()
    for profile in profiles:
        item = validate_profile(profile, allow_poses=version >= 2)
        key = item["name"].casefold()
        if key in names:
            raise ProfileStoreError("workspace profile names must be unique, ignoring case")
        names.add(key)
        clean.append(item)
    return {"schemaVersion": SCHEMA_VERSION, "profiles": clean}


class ProfileStore:
    """Atomic owner-only JSON store; callers can supply a temporary path in tests."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self):
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return {"schemaVersion": SCHEMA_VERSION, "profiles": []}
        except OSError as exc:
            raise ProfileStoreError(f"could not inspect workspace profile file: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ProfileStoreError("workspace profile path must be a regular file, not a link or special file")
        if info.st_size > MAX_STORE_BYTES:
            raise ProfileStoreError("workspace profile file is too large; it was left unchanged")
        if os.name != "nt" and stat.S_IMODE(info.st_mode) != 0o600:
            raise ProfileStoreError("workspace profile file permissions must be 0600; it was left unchanged")
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.path, flags)
            with os.fdopen(fd, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode):
                    raise ProfileStoreError("workspace profile path changed to a special file; it was left unchanged")
                if os.name != "nt" and stat.S_IMODE(opened.st_mode) != 0o600:
                    raise ProfileStoreError("workspace profile file permissions must be 0600; it was left unchanged")
                raw = stream.read(MAX_STORE_BYTES + 1)
            if len(raw) > MAX_STORE_BYTES:
                raise ProfileStoreError("workspace profile file is too large; it was left unchanged")
            decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_object)
        except _DuplicateJsonKey as exc:
            raise ProfileStoreError("workspace profile file contains duplicate JSON keys; it was left unchanged") from exc
        except ProfileStoreError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            if isinstance(exc, json.JSONDecodeError):
                reason = f"invalid JSON at line {exc.lineno}, column {exc.colno}"
            else:
                reason = str(exc)
            raise ProfileStoreError(f"workspace profile file is unreadable or corrupted ({reason}); it was left unchanged") from exc
        return validate_document(decoded)

    def _write(self, document):
        clean = validate_document(document)
        encoded = (json.dumps(clean, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        if len(encoded) > MAX_STORE_BYTES:
            raise ProfileStoreError("workspace profile data exceeds the local size limit")
        parent = self.path.parent
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            parent_info = parent.lstat()
            if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
                raise ProfileStoreError("workspace profile directory must be a real directory")
            if os.name != "nt":
                os.chmod(parent, 0o700)
            fd, temp_name = tempfile.mkstemp(prefix=".workspace-profiles-", dir=str(parent))
            try:
                if os.name != "nt":
                    os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, self.path)
                if os.name != "nt":
                    dir_fd = os.open(parent, os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
            finally:
                try:
                    os.unlink(temp_name)
                except FileNotFoundError:
                    pass
        except ProfileStoreError:
            raise
        except OSError as exc:
            raise ProfileStoreError(f"could not save workspace profiles: {exc}") from exc

    def list(self):
        with self._lock:
            return self._load()["profiles"]

    def get(self, name):
        name = _clean_text(name, "profile name", MAX_NAME)
        with self._lock:
            for profile in self._load()["profiles"]:
                if profile["name"].casefold() == name.casefold():
                    return profile
        raise ProfileError(f"workspace profile {name!r} was not found")

    def save(self, profile):
        clean = validate_profile(profile)
        with self._lock:
            document = self._load()
            profiles = [p for p in document["profiles"] if p["name"].casefold() != clean["name"].casefold()]
            if len(profiles) >= MAX_PROFILES:
                raise ProfileError(f"at most {MAX_PROFILES} workspace profiles can be saved")
            profiles.append(clean)
            profiles.sort(key=lambda p: p["name"].casefold())
            self._write({"schemaVersion": SCHEMA_VERSION, "profiles": profiles})
        return clean

    def delete(self, name):
        name = _clean_text(name, "profile name", MAX_NAME)
        with self._lock:
            document = self._load()
            profiles = [p for p in document["profiles"] if p["name"].casefold() != name.casefold()]
            if len(profiles) == len(document["profiles"]):
                raise ProfileError(f"workspace profile {name!r} was not found")
            self._write({"schemaVersion": SCHEMA_VERSION, "profiles": profiles})
        return name

    def reset(self):
        """Clear saved profiles only; this does not stop or reposition Mac windows."""
        with self._lock:
            self._write({"schemaVersion": SCHEMA_VERSION, "profiles": []})
        return {"schemaVersion": SCHEMA_VERSION, "profiles": []}


def match_windows(profile, windows):
    """Resolve durable rules against one fresh Mac enumeration, failing closed."""
    profile = validate_profile(profile)
    results = []
    used_ids = set()
    for index, rule in enumerate(profile["windows"], 1):
        bundle_id = rule["bundleID"]
        hint = rule.get("titleHint")
        app_matches = [window for window in windows
                       if isinstance(window, dict) and window.get("bundleID") == bundle_id]
        candidates = app_matches
        if hint is not None:
            candidates = [window for window in app_matches
                          if isinstance(window.get("title"), str) and hint.casefold() in window["title"].casefold()]
        ref = {"rule": index, "bundleID": bundle_id, "titleHint": hint}
        if not app_matches:
            results.append({**ref, "status": "missing",
                            "message": f"No visible window for {bundle_id}; open the app and make its window visible."})
        elif hint is not None and not candidates:
            results.append({**ref, "status": "retitled",
                            "message": f"Windows for {bundle_id} no longer contain title hint {hint!r}; update or remove the hint."})
        elif len(candidates) != 1:
            titles = [str(window.get("title") or "(untitled)")[:100] for window in candidates[:5]]
            message = (f"More than one window matches {bundle_id}; add a title hint to select one."
                       if candidates else f"No window matches {bundle_id}.")
            results.append({**ref, "status": "ambiguous", "candidates": titles, "message": message})
        else:
            window = candidates[0]
            window_id = window.get("id")
            if (type(window_id) is not int or window_id <= 0
                    or window.get("src") != f"window:{window_id}"):
                results.append({**ref, "status": "unavailable",
                                "message": f"Mac enumeration returned an invalid current window for {bundle_id}."})
            elif window_id in used_ids:
                results.append({**ref, "status": "duplicate",
                                "message": f"This rule resolved to a window already selected by an earlier rule."})
            else:
                used_ids.add(window_id)
                results.append({**ref, "status": "matched", "source": window["src"],
                                "title": str(window.get("title") or ""), "app": str(window.get("app") or "")})
    return results


def save(store, macview, value):
    """Snapshot native poses against stable matching rules before atomic save."""
    profile = validate_profile(value)
    if frame_macview.workspace_viewer_backend() == "native":
        state = macview.state()
        if not state.get("available"):
            raise frame_macview.MacViewError(state.get("reason") or "MacView is unavailable for pose capture.")
        matches = match_windows(profile, state.get("windows", [])) if macview._native_windows() else []
        matched = [item for item in matches if item["status"] == "matched"]
        poses = macview.read_panel_poses([frame_macview.WorkspaceDisplayManager.KEY] +
                                        [item["source"] for item in matched])
        # This save is a fresh snapshot: absent files and unmatched rules must
        # not carry over a supplied pose from an older session.
        profile["workspace"].pop("pose", None)
        for rule in profile["windows"]:
            rule.pop("pose", None)
        if "workspace" in poses:
            profile["workspace"]["pose"] = poses["workspace"]
        for item in matched:
            if item["source"] in poses:
                profile["windows"][item["rule"] - 1]["pose"] = poses[item["source"]]
    return store.save(profile)


def restore(store, macview, name):
    """Run the production Workspace and MacView paths using fresh window IDs."""
    profile = store.get(name)
    workspace_pose = profile["workspace"].get("pose")
    window_poses = any("pose" in rule for rule in profile["windows"])
    native = frame_macview.workspace_viewer_backend() == "native"
    if (workspace_pose is not None or window_poses) and not native:
        raise frame_macview.MacViewError("Saved poses require the native viewer; select it before restoring this profile.")
    if window_poses and not macview._native_windows():
        raise frame_macview.MacViewError("Saved window poses require native window panels.")
    before_start = macview.state()
    if not before_start.get("available"):
        raise frame_macview.MacViewError(before_start.get("reason") or "MacView is unavailable on this computer.")
    if not before_start.get("screen"):
        raise frame_macview.MacViewError("Screen Recording permission is required to restore Mac windows; allow it in MacView first.")

    # start_workspace creates a fresh owned display and opens its normal viewer,
    # minting a one-use ticket through MacView. Window viewers use that same
    # path below, so no ticket or reconnect credential enters the profile store.
    workspace_result = macview.start_workspace(**profile["workspace"])
    # A changed preset can replace the virtual display and macOS can rehome,
    # close, or retitle windows during that lifecycle. Resolve durable rules only
    # against this second enumeration, then pass its current IDs to show().
    after_start = macview.state()
    if not after_start.get("available"):
        raise frame_macview.MacViewError(after_start.get("reason") or "MacView became unavailable after Workspace start.")
    if not after_start.get("screen"):
        raise frame_macview.MacViewError("Screen Recording permission became unavailable after Workspace start.")
    resolutions = match_windows(profile, after_start.get("windows", []))
    shown_results = []
    restored_poses = int(workspace_pose is not None)
    for result in resolutions:
        if result["status"] != "matched":
            shown_results.append({key: value for key, value in result.items() if key != "source"})
            continue
        try:
            pose = profile["windows"][result["rule"] - 1].get("pose")
            shown = macview.show(result["source"], quality=profile["workspace"]["quality"],
                                 **({"pose": pose} if pose is not None else {}))
            restored_poses += int(pose is not None)
            shown_results.append({"rule": result["rule"], "bundleID": result["bundleID"],
                                  "titleHint": result.get("titleHint"), "status": "shown",
                                  "title": result["title"], "app": result["app"], "panel": shown.get("panel"),
                                  "poseRestored": pose is not None})
        except frame_macview.MacViewError as exc:
            shown_results.append({"rule": result["rule"], "bundleID": result["bundleID"],
                                  "titleHint": result.get("titleHint"), "status": "error", "message": str(exc)})

    workspace = workspace_result.get("workspace", {})
    return {"name": profile["name"], "placement": "saved" if restored_poses else "manual",
            "spatialRestore": {"available": native, "restoredPoses": restored_poses,
                               "message": "Native poses use the current SteamVR room origin; cross-room alignment is not verified."
                               if native else "Panel placement is manual with the browser viewer."},
            "workspace": {key: workspace[key] for key in ("preset", "scale", "quality", "display", "stream")
                          if key in workspace},
            "windows": shown_results}
