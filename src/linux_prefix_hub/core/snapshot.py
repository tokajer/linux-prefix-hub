"""Diff-based detection of where a game stores its data.

Principle: snapshot the prefix before the game starts, snapshot it after the
game exits, diff. Whatever changed is a storage location. This finds exotic
locations with no prior knowledge ("learn" mode).

We only scan the relevant subtrees below drive_c/users to save IO -- that is
where AppData/Documents/Saved Games/Downloads live. The install-folder special
case (a game writing into steamapps/common/...) is NOT covered here; it lives
outside the prefix and is handled by the source adapter.
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


def _ignored(rel: str) -> bool:
    low = rel.replace("\\", "/").lower()
    return any(frag in low for frag in IGNORE_FRAGMENTS)


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


def diff(before: dict[str, float], after: dict[str, float]) -> list[str]:
    """Relative paths that are new or were modified."""
    return [path for path, mtime in after.items()
            if path not in before or before[path] != mtime]


def classify_locations(changed_paths: list[str]) -> list[dict[str, Any]]:
    """Group changed files into storage locations (directory level).

    Heuristic for `type`: Documents/Saved Games -> saves, AppData -> config.
    Can be refined later with PCGamingWiki data.
    """
    # Aggregate at a sensible directory level: the first 3 segments.
    dirs: dict[str, int] = {}
    for p in changed_paths:
        parts = p.split("/")
        depth = min(3, len(parts) - 1) if len(parts) > 1 else 1
        d = "/".join(parts[:depth]) if depth > 0 else parts[0]
        dirs[d] = dirs.get(d, 0) + 1

    return [
        {
            "type": _guess_type(win_path),
            "win_path": win_path,
            "file_count": count,
            "detected_by": "diff",
            "redirected": False,
        }
        for win_path, count in sorted(dirs.items(), key=lambda x: -x[1])
    ]


def _guess_type(win_path: str) -> str:
    low = win_path.lower()
    if "saved games" in low or "documents" in low or "my games" in low:
        return "saves"
    if low.startswith("appdata/"):
        return "config"
    return "unknown"


# --- Pending snapshots (pre/post hook flow) ------------------------------
# Steam gets wrapped (one process, both snapshots in memory). Lutris and
# Heroic call us twice -- prelaunch and postexit -- so the "before" snapshot
# has to survive between two processes.

def save_pending(fingerprint: str, data: dict[str, float]) -> Path:
    path = paths.snapshot_file(fingerprint)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)
    return path


def load_pending(fingerprint: str, consume: bool = True) -> dict[str, float]:
    path = paths.snapshot_file(fingerprint)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if consume:
        with contextlib.suppress(OSError):
            path.unlink()
    return {k: float(v) for k, v in data.items()}
