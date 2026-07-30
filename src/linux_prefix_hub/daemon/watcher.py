# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Watcher: reports newly installed games.

Steam is watched with inotify on every library's steamapps directory (an
install writes appmanifest_<appid>.acf), which makes detection instant. Lutris
and Heroic are picked up by the same loop's periodic re-scan -- their configs
change rarely enough that a minute of latency is fine.

On the very first run every installed game is marked as known instead of
reported, so the user does not get their whole library as "new".

Later this same watcher can also react to `compatdata/<appid>/pfx` appearing
(= started for the first time), which is the safe moment to apply a pending
redirection.

VERIFY-ON-DEVICE:
  - Needs the PyPI package `inotify_simple` for instant Steam detection. This
    module degrades to polling when it is missing, so the core stays
    dependency-free.
  - Desktop notifications from a systemd user service need a reachable D-Bus
    (DBUS_SESSION_BUS_ADDRESS). We shell out to notify-send; test it on your
    desktop.
"""
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from ..adapters import base, steam
from ..core import paths
from ..core.i18n import _

POLL_INTERVAL = 60.0


def _load_known() -> set[str]:
    try:
        return set(json.loads(paths.KNOWN_GAMES.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return set()


def _save_known(known: set[str]) -> None:
    paths.KNOWN_GAMES.parent.mkdir(parents=True, exist_ok=True)
    paths.KNOWN_GAMES.write_text(json.dumps(sorted(known)), encoding="utf-8")


def _key(game: dict) -> str:
    return f"{game.get('source')}:{game.get('app_id')}"


def _installed_keys() -> set[str]:
    return {_key(g) for g in base.iter_games() if g.get("installed")}


def _notify(title: str, body: str) -> None:
    """Desktop notification via notify-send (best effort)."""
    try:
        subprocess.run(["notify-send", "-a", paths.APP_TITLE, title, body],
                       check=False, timeout=5)
    except (FileNotFoundError, subprocess.SubprocessError):
        print(f"[notify] {title}: {body}")


def _scan_once(known: set[str]) -> set[str]:
    """One scan pass: report games that finished installing."""
    newly = set()
    for game in base.iter_games():
        if not game.get("installed"):
            continue
        key = _key(game)
        if key in known:
            continue
        _notify(_("New game detected"),
                _("{game} is installed. Open {app} to manage its data.",
                  game=game.get("game_name", key), app=paths.APP_TITLE))
        newly.add(key)
    return newly


def _initial_known() -> set[str]:
    known = _load_known()
    if not known:
        known = _installed_keys()
        _save_known(known)
    return known


def _maybe_notify_update() -> None:
    """Once a day, tell the user about a new release -- at most once per
    version, because a notification you have already dismissed is spam."""
    try:
        from ..core import db, updater
        state = updater.check()
        if not state.get("available"):
            return
        version = str(state.get("version"))
        if db.load_config().get("update_notified") == version:
            return
        db.set_config("update_notified", version)
        _notify(_("Update available"),
                _("{app} {version} is available.",
                  app=paths.APP_TITLE, version=version))
    except Exception:
        pass  # never let the updater take the watcher down


def _refresh(known: set[str]) -> set[str]:
    newly = _scan_once(known)
    if newly:
        known |= newly
        _save_known(known)
    _maybe_notify_update()
    return known


def run_poll(interval: float = POLL_INTERVAL) -> None:
    """Polling fallback (no inotify). Simple and robust."""
    known = _initial_known()
    while True:
        known = _refresh(known)
        time.sleep(interval)


def run() -> None:
    """Prefers inotify for Steam, falls back to polling."""
    try:
        from inotify_simple import INotify, flags  # type: ignore
    except ImportError:
        print("[watcher] inotify_simple missing -> poll mode")
        run_poll()
        return

    known = _initial_known()

    inotify = INotify()
    watch_flags = flags.CLOSE_WRITE | flags.MOVED_TO | flags.CREATE
    watched: dict[int, Path] = {}
    for steamapps in steam.find_library_dirs():
        try:
            watched[inotify.add_watch(str(steamapps), watch_flags)] = steamapps
        except OSError:
            continue

    print(f"[watcher] inotify active on {len(watched)} Steam librar"
          f"{'y' if len(watched) == 1 else 'ies'}; "
          f"other sources every {int(POLL_INTERVAL)}s")

    while True:
        # A timeout turns the same loop into the poll cycle for Lutris/Heroic.
        events = inotify.read(timeout=int(POLL_INTERVAL * 1000))
        relevant = any(
            (e.name or "").startswith("appmanifest_")
            and (e.name or "").endswith(".acf") for e in events)
        if relevant or not events:
            known = _refresh(known)
