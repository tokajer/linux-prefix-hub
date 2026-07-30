"""Steam-Adapter: findet installierte Spiele + Proton-Prefixe.

Deckt ab:
  - Multi-Library (libraryfolders.vdf) -> alle steamapps-Orte
  - appmanifest_*.acf -> appid, name, StateFlags, installdir
  - compatdata/<appid>/pfx -> Proton-Prefix (existiert erst nach 1. Start!)
  - user_dir im Prefix ermitteln (Proton: fast immer 'steamuser')

StateFlags-Bits (die wichtigen):
  4     = fully installed
  1026  = update/download running
Wir behandeln "installiert & spielbereit" als (StateFlags & 4).

VERIFY-ON-DEVICE:
  - Steam-Wurzeln variieren je nach Distro/Flatpak. Die Liste unten deckt die
    haeufigen Faelle ab; auf deinem System pruefen und ggf. ergaenzen.
  - StateFlags-Semantik an echten Manifesten gegenpruefen (Valve dokumentiert
    das nicht offiziell).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

from ..core import vdf

# Haeufige Steam-Wurzeln (nativ + Flatpak). realpath entdoppelt Symlinks.
STEAM_ROOT_CANDIDATES = [
    "~/.steam/steam",
    "~/.local/share/Steam",
    "~/.steam/root",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",  # Flatpak
]


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p))


def find_steam_roots() -> list[Path]:
    seen: set[str] = set()
    roots: list[Path] = []
    for cand in STEAM_ROOT_CANDIDATES:
        path = _expand(cand)
        if path.exists():
            real = os.path.realpath(path)
            if real not in seen:
                seen.add(real)
                roots.append(Path(real))
    return roots


def find_library_dirs() -> list[Path]:
    """Alle steamapps-Verzeichnisse ueber alle Libraries/Platten."""
    libs: list[Path] = []
    seen: set[str] = set()

    for root in find_steam_roots():
        steamapps = root / "steamapps"
        if steamapps.exists():
            real = os.path.realpath(steamapps)
            if real not in seen:
                seen.add(real)
                libs.append(Path(real))

        # Zusaetzliche Libraries aus libraryfolders.vdf
        lf = steamapps / "libraryfolders.vdf"
        if lf.exists():
            try:
                data = vdf.loads(lf.read_text(encoding="utf-8", errors="ignore"))
                folders = data.get("libraryfolders", {})
                for _key, entry in folders.items():
                    if isinstance(entry, dict) and "path" in entry:
                        sa = Path(entry["path"]) / "steamapps"
                        if sa.exists():
                            real = os.path.realpath(sa)
                            if real not in seen:
                                seen.add(real)
                                libs.append(Path(real))
            except Exception:
                # defensiv: kaputte vdf soll Discovery nicht sprengen
                pass
    return libs


def iter_installed_games() -> Iterator[dict[str, Any]]:
    """Yield ein dict pro installiertem Spiel (ueber alle Libraries)."""
    for steamapps in find_library_dirs():
        for acf in steamapps.glob("appmanifest_*.acf"):
            try:
                data = vdf.loads(acf.read_text(encoding="utf-8",
                                               errors="ignore"))
            except Exception:
                continue
            state = data.get("AppState", {})
            appid = state.get("appid")
            if not appid:
                continue
            try:
                flags = int(state.get("StateFlags", "0"))
            except ValueError:
                flags = 0

            installdir = state.get("installdir", "")
            game_dir = steamapps / "common" / installdir if installdir else None

            yield {
                "source": "steam",
                "app_id": appid,
                "game_name": state.get("name", f"App {appid}"),
                "installed": bool(flags & 4),
                "state_flags": flags,
                "steamapps": str(steamapps),
                "game_dir": str(game_dir) if game_dir else None,
                "prefix_path": _prefix_for(steamapps, appid),
            }


def _prefix_for(steamapps: Path, appid: str) -> str | None:
    """Proton-Prefix-Pfad; None wenn noch nie gestartet (pfx fehlt)."""
    pfx = steamapps / "compatdata" / appid / "pfx"
    return str(pfx) if pfx.exists() else None


def user_dir_for(prefix_path: str) -> str | None:
    """Ermittelt den user-Ordner im Prefix (nicht raten -- auflisten).

    Proton nutzt fast immer 'steamuser'. Wir listen dennoch auf, um robust
    gegen Abweichungen zu sein, und ignorieren 'Public'.
    """
    users = Path(prefix_path) / "drive_c" / "users"
    if not users.exists():
        return None
    candidates = [d.name for d in users.iterdir()
                  if d.is_dir() and d.name != "Public"]
    if "steamuser" in candidates:
        return "steamuser"
    return candidates[0] if candidates else None
