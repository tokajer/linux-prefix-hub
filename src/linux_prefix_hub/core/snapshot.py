# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Diff-based detection of where a game stores its data.

Principle: snapshot the prefix before the game starts, snapshot it after the
game exits, diff. Whatever changed is a storage location. This finds exotic
locations with no prior knowledge ("learn" mode).

Inside the prefix we only scan the relevant subtrees below drive_c/users to
save IO -- that is where AppData/Documents/Saved Games/Downloads live.

The **install folder** is scanned as a second, separate location space
(`snapshot_game_dir`). Source-engine games are the classic case: Portal 2
writes its saves to `steamapps/common/Portal 2/portal2/SAVE/<steamid>/` and
touches nothing but `AppData/Local/Temp` inside the prefix, so a prefix-only
diff learns exactly nothing about it. Those locations are reported but never
redirected -- there is no registry key for them and symlinking an install
folder fights the launcher's updater (see `core/redirect.py`). Each location
carries `where`: "prefix" or "game_folder".
"""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

from . import paths

# Relative subtrees below <prefix>/drive_c/users/<user_dir>/ we care about.
INTERESTING_SUBTREES = [
    "AppData/Roaming",
    "AppData/Local",
    "AppData/LocalLow",
    "Documents",
    "Saved Games",
    "Downloads",
]

# Churn that is never a save game: Wine/Windows scratch space. Matched as a
# case-insensitive substring against the path relative to the user folder.
IGNORE_FRAGMENTS = (
    "appdata/local/temp/",
    "appdata/local/crashdumps/",
    "appdata/local/microsoft/windows/inetcache/",
    "appdata/local/microsoft/windows/explorer/",
    "appdata/roaming/microsoft/windows/recent/",
    "appdata/local/d3dscache/",
    "appdata/local/nvidia/",
    "appdata/local/amd/",
)


# The same idea for the install folder, where the churn looks different:
# logs, shader caches and the launcher's own bookkeeping.
IGNORE_GAME_FRAGMENTS = (
    "/shadercache/", "/shader_cache/", "/steamapps/downloading/",
    "/crashes/", "/logs/",
)
IGNORE_GAME_SUFFIXES = (".log", ".tmp", ".dmp", ".pyc")
IGNORE_GAME_NAMES = ("steam_appid.txt", "steam_autocloud.vdf",
                     ".steam_shortcut", "workshop_log.txt")

# An install folder is not a prefix: it can be hundreds of thousands of files.
# Past this many we give up rather than delay a launch -- the prefix diff still
# runs, so we lose a nice-to-have, not the feature.
MAX_GAME_DIR_FILES = 60_000

WHERE_PREFIX = "prefix"
WHERE_GAME = "game_folder"


def _ignored(rel: str) -> bool:
    low = rel.replace("\\", "/").lower()
    return any(frag in low for frag in IGNORE_FRAGMENTS)


def _ignored_in_game_dir(rel: str) -> bool:
    low = "/" + rel.replace("\\", "/").lower()
    if any(frag in low for frag in IGNORE_GAME_FRAGMENTS):
        return True
    name = low.rpartition("/")[2]
    return (name in IGNORE_GAME_NAMES
            or name.endswith(IGNORE_GAME_SUFFIXES))


def snapshot(prefix_path: str | Path, user_dir: str) -> dict[str, float]:
    """mtime snapshot of relevant files, keyed by path relative to user dir."""
    base = Path(prefix_path) / "drive_c" / "users" / user_dir
    result: dict[str, float] = {}
    for sub in INTERESTING_SUBTREES:
        root = base / sub
        if not root.exists():
            continue
        for p in root.rglob("*"):
            try:
                if not p.is_file():
                    continue
                rel = str(p.relative_to(base))
                if _ignored(rel):
                    continue
                result[rel] = p.stat().st_mtime
            except OSError:
                continue
    return result


def snapshot_game_dir(game_dir: str | Path | None) -> dict[str, float] | None:
    """mtime snapshot of the install folder, keyed by path relative to it.

    `None` means "not covered": no folder, or one too big to walk cheaply.
    That is deliberately not the same as `{}`, which means "covered, and
    currently empty" -- a fresh install really can be almost empty, and
    treating that as "not covered" would lose the first launch, the one that
    has the most to teach us.
    """
    if not game_dir:
        return None
    base = Path(game_dir)
    if not base.is_dir():
        return None
    result: dict[str, float] = {}
    visited = 0
    for p in base.rglob("*"):
        visited += 1
        if visited > MAX_GAME_DIR_FILES:
            return None        # too big: skip rather than stall the launch
        try:
            if p.is_symlink() or not p.is_file():
                continue
            rel = str(p.relative_to(base))
            if _ignored_in_game_dir(rel):
                continue
            result[rel] = p.stat().st_mtime
        except OSError:
            continue
    return result


def diff(before: dict[str, float], after: dict[str, float]) -> list[str]:
    """Relative paths that are new or were modified."""
    return [path for path, mtime in after.items()
            if path not in before or before[path] != mtime]


def classify_locations(changed_paths: list[str],
                       where: str = WHERE_PREFIX,
                       known: list[dict[str, Any]] | None = None
                       ) -> list[dict[str, Any]]:
    """Group changed files into storage locations (directory level).

    Heuristic for `type`: Documents/Saved Games -> saves, AppData -> config.
    `known` are locations somebody else already typed for us -- PCGamingWiki
    (`core/pcgw.py`), read from its cache. Those win over the heuristic,
    which is the whole point of looking them up.
    """
    # Aggregate at a sensible directory level: the first 3 segments.
    dirs: dict[str, int] = {}
    for p in changed_paths:
        parts = p.split("/")
        depth = min(3, len(parts) - 1) if len(parts) > 1 else 1
        d = "/".join(parts[:depth]) if depth > 0 else parts[0]
        dirs[d] = dirs.get(d, 0) + 1

    guess = _guess_type_in_game_dir if where == WHERE_GAME else _guess_type
    return [
        {
            "type": known_type(win_path, where, known) or guess(win_path),
            "win_path": win_path,
            "where": where,
            "file_count": count,
            "detected_by": "diff",
            "redirected": False,
        }
        for win_path, count in sorted(dirs.items(), key=lambda x: -x[1])
    ]


def _norm_path(win_path: str) -> str:
    """Windows paths are case-insensitive; the wiki spells them freely."""
    return win_path.replace("\\", "/").strip("/").lower()


def _contains(outer: str, inner: str) -> bool:
    return bool(outer) and bool(inner) and inner.startswith(outer + "/")


def known_type(win_path: str, where: str,
               known: list[dict[str, Any]] | None) -> str:
    """The type somebody already knows for this path, or "".

    The diff aggregates to three path segments, a wiki entry names the exact
    folder, so the two rarely spell the same string: `Documents/My Games/
    Skyrim` against `Documents/My Games/Skyrim/Saves`. Containment in either
    direction counts, but an exact match wins -- a game with a saves folder
    *inside* its config folder must not turn the config folder into saves.
    """
    exact = nested = ""
    for loc in known or ():
        if str(loc.get("where") or WHERE_PREFIX) != where:
            continue
        kind = str(loc.get("type") or "")
        if kind in ("", "unknown"):
            continue
        mine, theirs = _norm_path(win_path), _norm_path(
            str(loc.get("win_path", "")))
        if mine and mine == theirs:
            exact = exact or kind
        elif _contains(mine, theirs) or _contains(theirs, mine):
            nested = nested or kind
    return exact or nested


def _guess_type(win_path: str) -> str:
    low = win_path.lower()
    if "saved games" in low or "documents" in low or "my games" in low:
        return "saves"
    if low.startswith("appdata/"):
        return "config"
    return "unknown"


def _guess_type_in_game_dir(rel_path: str) -> str:
    """Same idea one namespace over: install folders spell it differently."""
    low = rel_path.lower()
    if "save" in low or "profile" in low:
        return "saves"
    if "cfg" in low or "config" in low or "settings" in low:
        return "config"
    return "unknown"


# --- Pending snapshots (pre/post hook flow) ------------------------------
# Steam gets wrapped (one process, both snapshots in memory). Lutris and
# Heroic call us twice -- prelaunch and postexit -- so the "before" snapshot
# has to survive between two processes. What travels is one state per space:
# {"prefix": {...}, "game_folder": {...}}.

def save_pending(fingerprint: str, data: dict[str, dict[str, float]]) -> Path:
    path = paths.snapshot_file(fingerprint)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)
    return path


def load_pending(fingerprint: str,
                 consume: bool = True) -> dict[str, dict[str, float]]:
    path = paths.snapshot_file(fingerprint)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if consume:
        with contextlib.suppress(OSError):
            path.unlink()
    if not isinstance(data, dict):
        return {}
    # A file written before the install folder was a thing is one flat state.
    if not all(isinstance(v, dict) for v in data.values()):
        data = {WHERE_PREFIX: data}
    return {space: {k: float(v) for k, v in state.items()}
            for space, state in data.items()}
