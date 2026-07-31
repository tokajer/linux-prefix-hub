# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Self-integration: the one-time setup step.

Solves the wandering-path problem: the AppImage copies itself to a fixed
location, and Steam/Lutris/systemd only ever point at fixed shims -- never
directly at the (possibly wandering) AppImage.

Sequence (idempotent -- safe to run on every start):
  1. Detect GearLever. If it already integrated the app at a fixed location we
     respect that and do NOT relocate ourselves.
  2. Otherwise copy the AppImage into install_dir (unless it is already there).
  3. Create the shims in ~/.local/bin (fixed entry points).
  4. Create + enable the systemd user unit for the watcher.
  5. Create a desktop entry so the app shows up in the application menu.

We may well NOT be running as an AppImage (pipx during development). Then we
skip the relocation and write shims that call the dev entry point instead.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from . import db, paths

APPLICATIONS_DIR = paths.XDG_DATA_HOME / "applications"
# Named after the app id, because that is the name the window carries on the
# desktop (`paths.APP_ID`): the task bar goes from the open window's app_id to
# the entry of the same name, and takes the icon from there.
DESKTOP_FILE = APPLICATIONS_DIR / f"{paths.APP_ID}.desktop"
# Where it lived while it was named after the on-disk name. Deleted when the
# current one is written -- two entries would mean two menu items for one app.
LEGACY_DESKTOP_FILE = APPLICATIONS_DIR / f"{paths.APP_NAME}.desktop"


def running_as_appimage() -> str | None:
    """The AppImage path, if we are running as one."""
    return os.environ.get("APPIMAGE")  # set by AppRun


# Flatpak GearLever stores its GSettings in a plain keyfile, so we can read
# the folder the user actually configured instead of guessing it.
GEARLEVER_KEYFILE = Path.home() / (
    ".var/app/it.mijorus.gearlever/config/glib-2.0/settings/keyfile")
GEARLEVER_SETTING = "appimages-default-folder"


def gearlever_folders() -> list[Path]:
    """Folders GearLever may keep managed AppImages in, best first.

    The configured folder wins; the static candidates stay as a fallback for
    a non-Flatpak GearLever whose settings live in dconf.
    """
    folders: list[Path] = []
    try:
        text = GEARLEVER_KEYFILE.read_text(encoding="utf-8")
    except OSError:
        text = ""
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == GEARLEVER_SETTING:
            configured = value.strip().strip("'\"")
            if configured:
                folders.append(Path(os.path.expanduser(configured)))
            break
    folders += [Path.home() / ".local" / "share" / "AppImages",
                Path.home() / "AppImages",
                Path.home() / "Applications"]
    unique: list[Path] = []
    for folder in folders:
        if folder not in unique:
            unique.append(folder)
    return unique


def detect_gearlever() -> Path | None:
    """Has GearLever already integrated this app?

    If we run from a folder GearLever manages, it owns placement and updates
    and we must not relocate ourselves.
    """
    appimg = running_as_appimage()
    if not appimg:
        return None
    real = Path(os.path.realpath(appimg))
    for gd in gearlever_folders():
        try:
            if gd in real.parents:
                return real
        except (OSError, ValueError):
            continue
    return None


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    st = path.stat().st_mode
    path.chmod(st | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _target_appimage() -> Path:
    """Where the authoritative AppImage is (or should be)."""
    return (detect_gearlever()
            or paths.installed_appimage_path(db.install_dir()))


def relocate_appimage() -> Path | None:
    """Copy the running AppImage to the fixed install_dir location.

    Returns the fixed target path, or None if we are not running as an
    AppImage (dev mode) or GearLever handles it.
    """
    if detect_gearlever():
        return _target_appimage()   # GearLever owns placement and updates

    src = running_as_appimage()
    if not src:
        return None                 # dev mode (pipx): nothing to relocate

    target = paths.installed_appimage_path(db.install_dir())
    target.parent.mkdir(parents=True, exist_ok=True)

    src_real = os.path.realpath(src)
    if os.path.realpath(target) == src_real:
        return target               # already running from the fixed location

    shutil.copy2(src_real, target)
    st = target.stat().st_mode
    target.chmod(st | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def _shim_body(mode: str) -> str:
    """Shim content: call the AppImage at its fixed location in `mode`, or in
    dev mode the Python entry point."""
    appimg = _target_appimage()
    if running_as_appimage() or appimg.exists():
        return f'#!/usr/bin/env bash\nexec "{appimg}" --{mode} "$@"\n'
    return (f'#!/usr/bin/env bash\n'
            f'exec "{sys.executable}" -m {paths.PACKAGE} --{mode} "$@"\n')


def install_shims() -> dict[str, Path]:
    """The three fixed entry points other programs may know about."""
    shims = {
        "wrapper": paths.WRAPPER_SHIM,   # Steam/Heroic: wraps %command%
        "hook": paths.HOOK_SHIM,         # Lutris: pre/post
        "daemon": paths.DAEMON_SHIM,     # systemd: watcher
    }
    for mode, path in shims.items():
        _write_executable(path, _shim_body(mode))
    return shims


def install_systemd_unit(enable: bool = True) -> Path:
    unit = (
        "[Unit]\n"
        f"Description={paths.APP_TITLE} watcher (detect newly installed games)"
        "\nAfter=graphical-session.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={paths.DAEMON_SHIM}\n"
        "Restart=on-failure\n"
        "RestartSec=10\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    paths.WATCHER_UNIT.parent.mkdir(parents=True, exist_ok=True)
    paths.WATCHER_UNIT.write_text(unit, encoding="utf-8")

    if enable:
        # Quiet on purpose: this runs on every normal start (self-heal), and
        # systemd's complaints when there is no user session are noise the
        # user can do nothing about.
        try:
            subprocess.run(["systemctl", "--user", "daemon-reload"],
                           check=False, timeout=10, capture_output=True)
            subprocess.run(["systemctl", "--user", "enable", "--now",
                            paths.WATCHER_UNIT.name],
                           check=False, timeout=10, capture_output=True)
        except (FileNotFoundError, subprocess.SubprocessError):
            # No systemd --user available -> the unit file is there, the user
            # can enable it manually. VERIFY-ON-DEVICE.
            pass
    return paths.WATCHER_UNIT


def install_icon() -> Path | None:
    """Put the icon where the desktop looks for it, under both its names.

    Nothing that shows the icon carries the image itself, they all name it:
    the About dialog and the tray ask for `linux-prefix-hub`, while the
    desktop entry -- and with it the open window's place in the task bar --
    goes by the app id. Without a file in the icon theme each of them falls
    back to a blank placeholder. hicolor/256x256/apps is the per-user search
    path every desktop reads, no cache refresh needed.
    """
    if not paths.ICON_SOURCE.exists():
        return None                    # nothing to install (source layout)
    try:
        paths.ICON_DIR.mkdir(parents=True, exist_ok=True)
        for target in (paths.ICON_FILE, paths.ICON_FILE_APP_ID):
            shutil.copyfile(paths.ICON_SOURCE, target)
    except OSError:
        return None                    # a missing icon must not fail setup
    return paths.ICON_FILE


def install_desktop_entry() -> Path | None:
    """Application-menu entry. Skipped when GearLever manages the app.

    It is also what an *open* window is matched against: the shell reads the
    window's app id and looks for the entry of that name, which is why the
    file is called `paths.APP_ID`.desktop and says `StartupWMClass` on top --
    the second one is how the X11 side (and GNOME's Wayland matching) gets
    there. Without the match the task bar has no entry to take an icon from
    and falls back to the interpreter's.
    """
    if detect_gearlever():
        return None
    appimg = _target_appimage()
    # --gui explicitly: the entry has Terminal=false, so the terminal flow
    # would be invisible here.
    exec_line = (f'"{appimg}" --gui' if appimg.exists()
                 else f'"{sys.executable}" -m {paths.PACKAGE} --gui')
    DESKTOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    DESKTOP_FILE.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={paths.APP_TITLE}\n"
        "Comment=Manage where your games store their data\n"
        f"Exec={exec_line}\n"
        f"Icon={paths.APP_ID}\n"
        f"StartupWMClass={paths.APP_ID}\n"
        "Categories=Game;Utility;\n"
        "Terminal=false\n",
        encoding="utf-8")
    if LEGACY_DESKTOP_FILE != DESKTOP_FILE:
        # The entry this one replaces. Left behind it is a second menu item
        # for the same app, pointing at the same file.
        LEGACY_DESKTOP_FILE.unlink(missing_ok=True)
    return DESKTOP_FILE


def full_setup(enable_watcher: bool = True) -> dict[str, str]:
    """Complete setup run. Idempotent."""
    paths.ensure_dirs()
    appimg = relocate_appimage()
    shims = install_shims()
    unit = install_systemd_unit(enable=enable_watcher)
    icon = install_icon()
    desktop = install_desktop_entry()
    return {
        "appimage": str(appimg) if appimg else "(dev mode, no relocation)",
        "gearlever": str(detect_gearlever() or "not detected"),
        "wrapper_shim": str(shims["wrapper"]),
        "hook_shim": str(shims["hook"]),
        "daemon_shim": str(shims["daemon"]),
        "systemd_unit": str(unit),
        "desktop_entry": str(desktop) if desktop else "(managed by GearLever)",
        "icon": str(icon) if icon else "(not shipped with this build)",
    }
