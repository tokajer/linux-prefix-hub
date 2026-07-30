"""Diff-basierte Speicherort-Erkennung.

Prinzip: Snapshot des Prefix VOR Spielstart, Snapshot NACH Spielende.
Was sich geaendert/neu ist, ist ein Speicherort. Findet auch exotische
Orte ohne Vorwissen ("Learn"-Modus).

Wir scannen nur relevante Zweige unter drive_c/users, um IO zu sparen --
das ist die typische Heimat von AppData/Documents/Saved Games/Downloads.
Der Install-Ordner-Sonderfall (Spiel schreibt in steamapps/common/...)
wird hier NICHT erfasst; den behandelt der Steam-Adapter separat, weil er
ausserhalb des Prefix liegt.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# Relative Zweige unter <prefix>/drive_c/users/<user_dir>/ die interessieren
INTERESTING_SUBTREES = [
    "AppData/Roaming",
    "AppData/Local",
    "AppData/LocalLow",
    "Documents",
    "Saved Games",
    "Downloads",
]


def snapshot(prefix_path: Path, user_dir: str) -> dict[str, float]:
    """mtime-Snapshot relevanter Dateien. Pfad relativ zum user-Verzeichnis."""
    base = Path(prefix_path) / "drive_c" / "users" / user_dir
    result: dict[str, float] = {}
    for sub in INTERESTING_SUBTREES:
        root = base / sub
        if not root.exists():
            continue
        for p in root.rglob("*"):
            try:
                if p.is_file():
                    rel = str(p.relative_to(base))
                    result[rel] = p.stat().st_mtime
            except OSError:
                continue
    return result


def diff(before: dict[str, float], after: dict[str, float]) -> list[str]:
    """Liefert relative Pfade, die neu oder geaendert sind."""
    changed = []
    for path, mtime in after.items():
        if path not in before or before[path] != mtime:
            changed.append(path)
    return changed


def classify_locations(changed_paths: list[str]) -> list[dict[str, Any]]:
    """Gruppiert geaenderte Dateien zu Speicherorten (Verzeichnis-Ebene).

    Heuristik fuer 'type': Documents/Saved Games -> saves,
    AppData/Local -> config (grob). Wird spaeter durch PCGamingWiki-Daten
    verfeinerbar.
    """
    # Auf sinnvolle Verzeichnis-Ebene aggregieren: die ersten 3 Segmente
    dirs: dict[str, int] = {}
    for p in changed_paths:
        parts = p.split("/")
        depth = min(3, len(parts) - 1) if len(parts) > 1 else 1
        d = "/".join(parts[:depth]) if depth > 0 else parts[0]
        dirs[d] = dirs.get(d, 0) + 1

    locations = []
    for win_path, count in sorted(dirs.items(), key=lambda x: -x[1]):
        locations.append({
            "type": _guess_type(win_path),
            "win_path": win_path,
            "file_count": count,
            "detected_by": "diff",
            "redirected": False,
        })
    return locations


def _guess_type(win_path: str) -> str:
    low = win_path.lower()
    if "saved games" in low or "documents" in low or "my games" in low:
        return "saves"
    if "appdata/local" in low or "appdata\\local" in low:
        return "config"
    if "appdata/roaming" in low:
        return "config"
    return "unknown"
