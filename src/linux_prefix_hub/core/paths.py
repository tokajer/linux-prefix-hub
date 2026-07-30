"""Central paths. Everything persistent lives at these fixed locations.

Layout philosophy:
  - AppImage + app data:     ~/.local/share/linux-prefix-hub/  (XDG_DATA_HOME)
  - Config / DB / state:     ~/.config/linux-prefix-hub/       (XDG_CONFIG)
  - Shims (Steam/systemd):   ~/.local/bin/                     (fixed entry)

Reason for the split: the location that Steam launch options and the systemd
unit point at must NEVER change. So the shims live at a fixed, "boring" place
and the AppImage at a fixed data location.
"""
from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "linux-prefix-hub"      # on-disk name (dirs, shims, unit)
APP_TITLE = "Linux Prefix Hub"     # user-visible name
PACKAGE = "linux_prefix_hub"       # importable Python package


def _xdg(env_var: str, default_rel: str) -> Path:
    val = os.environ.get(env_var)
    return Path(val) if val else Path.home() / default_rel


# --- Base directories ----------------------------------------------------
XDG_DATA_HOME = _xdg("XDG_DATA_HOME", ".local/share")
XDG_CONFIG_HOME = _xdg("XDG_CONFIG_HOME", ".config")

# Fixed install location of the AppImage (default; can be changed during the
# welcome flow, but is then written to config once and for all).
DEFAULT_INSTALL_DIR = XDG_DATA_HOME / APP_NAME
CONFIG_DIR = XDG_CONFIG_HOME / APP_NAME

# Fixed location for the shims. Referenced absolutely (not via PATH) so that
# Steam launch options work even when ~/.local/bin is not in PATH.
LOCAL_BIN = Path.home() / ".local" / "bin"
WRAPPER_SHIM = LOCAL_BIN / f"{APP_NAME}-wrapper"   # Steam: %command% wrapping
HOOK_SHIM = LOCAL_BIN / f"{APP_NAME}-hook"         # Lutris/Heroic: pre/post
DAEMON_SHIM = LOCAL_BIN / f"{APP_NAME}-daemon"     # systemd: watcher

# systemd user unit
SYSTEMD_USER_DIR = XDG_CONFIG_HOME / "systemd" / "user"
WATCHER_UNIT = SYSTEMD_USER_DIR / f"{APP_NAME}-watcher.service"

# --- Files inside CONFIG_DIR --------------------------------------------
CONFIG_FILE = CONFIG_DIR / "config.json"       # install_dir, language, ...
PREFIX_DB = CONFIG_DIR / "prefixes.json"       # detected prefixes + storage
KNOWN_GAMES = CONFIG_DIR / "known_games.json"  # for new-game detection
SNAPSHOT_DIR = CONFIG_DIR / "snapshots"        # pending pre-launch snapshots

# Where redirected storage locations end up by default: one folder per game
# below this. Our own subfolder, so ~/Games stays the user's -- they very
# likely already keep game installs there. Overridable via config
# ("redirect_root", see db.redirect_root); already-moved folders keep the
# absolute target stored with them, so changing it never strands data.
DEFAULT_REDIRECT_ROOT = Path.home() / "Games" / APP_NAME


def installed_appimage_path(install_dir: Path | None = None) -> Path:
    """Where the AppImage copies itself to (fixed location)."""
    d = install_dir or DEFAULT_INSTALL_DIR
    return d / "LinuxPrefixHub.AppImage"


def snapshot_file(fingerprint: str) -> Path:
    """Pre-launch snapshot handed from the `pre` hook to the `post` hook."""
    return SNAPSHOT_DIR / f"{fingerprint}.json"


def ensure_dirs() -> None:
    for d in (DEFAULT_INSTALL_DIR, CONFIG_DIR, LOCAL_BIN,
              SYSTEMD_USER_DIR, SNAPSHOT_DIR):
        d.mkdir(parents=True, exist_ok=True)
