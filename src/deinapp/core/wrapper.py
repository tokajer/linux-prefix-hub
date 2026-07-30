"""Spielstart-Wrapper. Wird von Steam via  deinapp-wrapper %command%  aufgerufen.

Ablauf:
  1. Kontext ermitteln: SteamAppId aus der Umgebung (Steam setzt sie!),
     daraus Prefix + user_dir.
  2. Snapshot VOR dem Start.
  3. Das eigentliche Spiel starten (die uebergebenen Argumente) und warten.
  4. Snapshot NACH dem Ende -> geaenderte Speicherorte lernen und in DB
     schreiben.

Umleitung ist hier bewusst NICHT aktiv -- die ist optional und kommt als
eigener Schritt. Dieser Wrapper ist read-only fuers Spiel: er beobachtet nur,
wo gespeichert wird. Risikofrei.
"""
from __future__ import annotations

import os
import subprocess
import sys

from ..adapters import steam
from . import db, snapshot


def _steam_context() -> dict | None:
    """Prefix + user_dir aus SteamAppId (von Steam gesetzte Env-Variable)."""
    appid = os.environ.get("SteamAppId") or os.environ.get("STEAM_COMPAT_APP_ID")
    if not appid:
        return None
    # Prefix ueber Discovery finden (deckt Multi-Library ab)
    for game in steam.iter_installed_games():
        if game["app_id"] == appid and game["prefix_path"]:
            udir = steam.user_dir_for(game["prefix_path"])
            if udir:
                return {
                    "app_id": appid,
                    "game_name": game["game_name"],
                    "prefix_path": game["prefix_path"],
                    "user_dir": udir,
                    "source": "steam",
                }
    return None


def main(argv: list[str]) -> int:
    """argv = das echte Spiel-Command (was hinter %command% steht)."""
    if not argv:
        print("wrapper: kein Spiel-Command uebergeben", file=sys.stderr)
        return 2

    ctx = _steam_context()

    before = {}
    if ctx:
        before = snapshot.snapshot(ctx["prefix_path"], ctx["user_dir"])

    # --- Spiel starten und warten ---
    proc = subprocess.run(argv)

    if ctx:
        after = snapshot.snapshot(ctx["prefix_path"], ctx["user_dir"])
        changed = snapshot.diff(before, after)
        locations = snapshot.classify_locations(changed)
        db.upsert_prefix({
            "source": ctx["source"],
            "app_id": ctx["app_id"],
            "game_name": ctx["game_name"],
            "prefix_path": ctx["prefix_path"],
            "user_dir": ctx["user_dir"],
            "storage_locations": locations,
        })

    return proc.returncode
