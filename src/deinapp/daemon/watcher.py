"""inotify-Watcher: meldet neu installierte Steam-Spiele.

Lauscht auf Aenderungen an appmanifest_*.acf in ALLEN Library-steamapps.
Wenn ein Spiel den Zustand "fully installed" (StateFlags & 4) erreicht und
noch nicht bekannt ist -> Desktop-Notification "neues Spiel".

Derselbe Watcher kann spaeter auch das Auftauchen von compatdata/<appid>/pfx
erkennen (= zum ersten Mal gestartet), der Moment fuer optionale Umleitung.

VERIFY-ON-DEVICE:
  - Braucht das PyPI-Paket 'inotify_simple' (in pyproject als Dependency).
    Dieser Modul-Code degradiert auf einen Poll-Fallback, wenn es fehlt, damit
    das Fundament auch ohne die Dependency laeuft.
  - Desktop-Notifications aus einem systemd-user-service brauchen einen
    erreichbaren D-Bus (DBUS_SESSION_BUS_ADDRESS). notify-send ist der
    pragmatische Weg; wir rufen es via subprocess. Auf deinem Desktop testen.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from ..adapters import steam
from ..core import paths, vdf


def _load_known() -> set[str]:
    try:
        return set(json.loads(paths.KNOWN_GAMES.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_known(known: set[str]) -> None:
    paths.KNOWN_GAMES.parent.mkdir(parents=True, exist_ok=True)
    paths.KNOWN_GAMES.write_text(json.dumps(sorted(known)), encoding="utf-8")


def _notify(title: str, body: str) -> None:
    """Desktop-Notification via notify-send (best effort)."""
    try:
        subprocess.run(["notify-send", "-a", "DeinApp", title, body],
                       check=False, timeout=5)
    except (FileNotFoundError, subprocess.SubprocessError):
        # Kein notify-send / kein D-Bus -> still. Auf dem Zielsystem pruefen.
        print(f"[notify] {title}: {body}")


def _scan_once(known: set[str]) -> set[str]:
    """Ein Scan-Durchlauf: neue, fertig installierte Spiele melden."""
    newly = set()
    for game in steam.iter_installed_games():
        appid = game["app_id"]
        if game["installed"] and appid not in known:
            _notify(
                "Neues Spiel erkannt",
                f"{game['game_name']} ist installiert. "
                f"Klicke in DeinApp, um Speicherstaende zu verwalten.",
            )
            newly.add(appid)
    return newly


def run_poll(interval: float = 15.0) -> None:
    """Poll-Fallback (kein inotify). Simpel & robust."""
    known = _load_known()
    # Beim ersten Lauf: bestehende Spiele als bekannt markieren, NICHT melden
    if not known:
        known = {g["app_id"] for g in steam.iter_installed_games()
                 if g["installed"]}
        _save_known(known)
    while True:
        newly = _scan_once(known)
        if newly:
            known |= newly
            _save_known(known)
        time.sleep(interval)


def run() -> None:
    """Bevorzugt inotify, faellt auf Poll zurueck."""
    try:
        from inotify_simple import INotify, flags  # type: ignore
    except ImportError:
        print("[watcher] inotify_simple fehlt -> Poll-Modus")
        run_poll()
        return

    known = _load_known()
    if not known:
        known = {g["app_id"] for g in steam.iter_installed_games()
                 if g["installed"]}
        _save_known(known)

    inotify = INotify()
    watch_flags = flags.CLOSE_WRITE | flags.MOVED_TO | flags.CREATE
    watched: dict[int, Path] = {}
    for steamapps in steam.find_library_dirs():
        try:
            wd = inotify.add_watch(str(steamapps), watch_flags)
            watched[wd] = steamapps
        except OSError:
            continue

    print(f"[watcher] inotify aktiv auf {len(watched)} Library/-s")
    while True:
        for event in inotify.read(timeout=None):
            name = event.name or ""
            if name.startswith("appmanifest_") and name.endswith(".acf"):
                newly = _scan_once(known)
                if newly:
                    known |= newly
                    _save_known(known)
