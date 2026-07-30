"""Zentrale Pfade. Alles Persistente hängt an diesen festen Orten.

Layout-Philosophie (mit dir abgestimmt):
  - AppImage + App-Daten:  ~/.local/share/deinapp/   (XDG_DATA_HOME)
  - Config / DB / State:    ~/.config/deinapp/         (XDG_CONFIG_HOME)
  - Shims (Steam/systemd):  ~/.local/bin/              (fester Einstiegspunkt)

Der Grund für die Trennung: der Ort, auf den Steam-Launch-Options und die
systemd-Unit zeigen, darf sich NIE ändern. Deshalb leben die Shims an einem
festen, "langweiligen" Ort und das AppImage an einem festen Data-Ort.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "deinapp"


def _xdg(env_var: str, default_rel: str) -> Path:
    val = os.environ.get(env_var)
    base = Path(val) if val else Path.home() / default_rel
    return base


# --- Basis-Verzeichnisse -------------------------------------------------
XDG_DATA_HOME = _xdg("XDG_DATA_HOME", ".local/share")
XDG_CONFIG_HOME = _xdg("XDG_CONFIG_HOME", ".config")

# Fester Installationsort des AppImage (Default; kann beim Welcome geaendert
# werden, wird dann aber EINMALIG in config festgeschrieben).
DEFAULT_INSTALL_DIR = XDG_DATA_HOME / APP_NAME
CONFIG_DIR = XDG_CONFIG_HOME / APP_NAME

# Fester Ort fuer die Shims. Absolut referenzieren (nicht via PATH), damit
# Steam-Launch-Options auch dann funktionieren, wenn ~/.local/bin nicht im
# PATH liegt.
LOCAL_BIN = Path.home() / ".local" / "bin"
WRAPPER_SHIM = LOCAL_BIN / f"{APP_NAME}-wrapper"
DAEMON_SHIM = LOCAL_BIN / f"{APP_NAME}-daemon"

# systemd user unit
SYSTEMD_USER_DIR = XDG_CONFIG_HOME / "systemd" / "user"
WATCHER_UNIT = SYSTEMD_USER_DIR / f"{APP_NAME}-watcher.service"

# --- Dateien innerhalb CONFIG_DIR ---------------------------------------
CONFIG_FILE = CONFIG_DIR / "config.json"      # gewaehlter install_dir etc.
PREFIX_DB = CONFIG_DIR / "prefixes.json"       # erkannte Prefixe + Storage
KNOWN_GAMES = CONFIG_DIR / "known_games.json"  # fuer Neu-Spiel-Erkennung
SNAPSHOT_DIR = CONFIG_DIR / "snapshots"        # Diff-Snapshots pro Spiel

# Wo das AppImage sich selbst hinkopiert. Wird nach dem Welcome aus der
# config gelesen; bis dahin der Default.
def installed_appimage_path(install_dir: Path | None = None) -> Path:
    d = install_dir or DEFAULT_INSTALL_DIR
    return d / "DeineApp.AppImage"


def ensure_dirs() -> None:
    for d in (DEFAULT_INSTALL_DIR, CONFIG_DIR, LOCAL_BIN,
              SYSTEMD_USER_DIR, SNAPSHOT_DIR):
        d.mkdir(parents=True, exist_ok=True)
