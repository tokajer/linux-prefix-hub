# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

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

# What the *window* is called on the desktop. GTK hands the program name to
# the compositor (Wayland `app_id`, X11 `WM_CLASS`), and the task bar looks
# for a desktop entry of exactly that name to find out which icon an open
# window gets. Left alone the program name is the interpreter's -- "python3",
# or "python3.12" out of the AppImage -- which is the icon the task bar drew.
# So `gui.app.main` sets this as the program name and the desktop entry is
# named after it; the two must stay the same string.
APP_ID = "io.github.tokajer.LinuxPrefixHub"


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

# The icon. The menu entry, the About dialog and the tray all reference it by
# *name*, which only resolves once a file sits in the icon theme -- otherwise
# they show a blank placeholder. It ships inside the package so this works
# from a pip install, the AppImage and a checkout alike;
# `integrate.install_icon` copies it into place, under both names.
_PACKAGE_DIR = Path(__file__).resolve().parent.parent
ICON_SOURCE = _PACKAGE_DIR / "data" / f"{APP_NAME}.png"
ICON_DIR = XDG_DATA_HOME / "icons" / "hicolor" / "256x256" / "apps"
ICON_FILE = ICON_DIR / f"{APP_NAME}.png"          # tray + About ask for this
ICON_FILE_APP_ID = ICON_DIR / f"{APP_ID}.png"     # the open window's own name

# --- Files inside CONFIG_DIR --------------------------------------------
CONFIG_FILE = CONFIG_DIR / "config.json"       # install_dir, language, ...
PREFIX_DB = CONFIG_DIR / "prefixes.db"         # detected prefixes + storage
# What PREFIX_DB was until the wrapper, the watcher and the window started
# writing it at the same time. `core/db.py` folds it in once and then leaves
# it alone -- it costs nothing and it is the only backup of a file that takes
# months of playing to fill.
LEGACY_PREFIX_DB = CONFIG_DIR / "prefixes.json"
KNOWN_GAMES = CONFIG_DIR / "known_games.json"  # for new-game detection
SNAPSHOT_DIR = CONFIG_DIR / "snapshots"        # pending pre-launch snapshots
PCGW_DIR = CONFIG_DIR / "pcgamingwiki"         # cached answers, one per game

# The app's own corner of ~/Games. One folder with everything this app puts
# on disk for the user below it, so ~/Games stays theirs -- they very likely
# already keep game installs there.
APP_GAMES_DIR = Path.home() / "Games" / APP_NAME

# Where redirected storage locations end up by default: one folder per game
# below this. Overridable via config ("redirect_root", see db.redirect_root);
# already-moved folders keep the absolute target stored with them, so
# changing it never strands data.
DEFAULT_REDIRECT_ROOT = APP_GAMES_DIR / "Games"

# Where a game folder the user sets up themselves is made by default: one
# folder per game below this. A sibling of the redirect root and not the same
# folder, because the two hold different things -- moved game data there,
# whole game installs here. Overridable via config ("prefix_root", see
# newprefix.root); folders that already exist keep their absolute path, so
# changing it strands nothing.
DEFAULT_PREFIX_ROOT = APP_GAMES_DIR / "prefix"


def installed_appimage_path(install_dir: Path | None = None) -> Path:
    """Where the AppImage copies itself to (fixed location)."""
    d = install_dir or DEFAULT_INSTALL_DIR
    return d / "LinuxPrefixHub.AppImage"


def snapshot_file(fingerprint: str) -> Path:
    """Pre-launch snapshot handed from the `pre` hook to the `post` hook."""
    return SNAPSHOT_DIR / f"{fingerprint}.json"


def pcgw_cache_file(key: str) -> Path:
    """One cached PCGamingWiki answer, keyed by source + game id."""
    return PCGW_DIR / f"{key}.json"


def ensure_dirs() -> None:
    for d in (DEFAULT_INSTALL_DIR, CONFIG_DIR, LOCAL_BIN,
              SYSTEMD_USER_DIR, SNAPSHOT_DIR, PCGW_DIR):
        d.mkdir(parents=True, exist_ok=True)
