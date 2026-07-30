"""Welcome / setup flow.

Terminal version for now. The graphical version (GTK4/libadwaita) will sit on
exactly this logic, which is why the decisions live here and the presentation
does not: `choose_install_dir` returns a path, `run` is the controller.

Design:
  - GearLever detected? Ask nothing, just confirm.
  - Otherwise propose ~/.local/share/linux-prefix-hub, "change" is optional.
  - Unstable locations (Downloads/Desktop/tmp/removable media) get a warning,
    not a ban. It is the user's machine.
"""
from __future__ import annotations

import os
from pathlib import Path

from ..core import db, integrate, paths
from ..core.i18n import _

# Locations that get cleaned up or are not always mounted.
UNSTABLE_ROOTS = ("/tmp", "/var/tmp", "/media", "/run/media", "/mnt")
# Inside the home folder only these two are volatile.
UNSTABLE_HOME_DIRS = ("downloads", "desktop")


def is_unstable(path: Path) -> bool:
    """Is `path` a place an installed app tends to disappear from?

    Matches path components, never substrings: a home folder that lives on a
    mounted disk (`/mnt/...`) or a user directory that happens to contain the
    word "desktop" must not trigger a warning for the default location.
    """
    p = Path(os.path.expanduser(str(path)))
    try:
        rel = p.resolve().relative_to(Path.home().resolve())
    except (OSError, ValueError):
        rel = None

    if rel is not None:
        # Inside the home folder: the home folder itself may sit anywhere.
        first = rel.parts[0].lower() if rel.parts else ""
        return first in UNSTABLE_HOME_DIRS

    for root in UNSTABLE_ROOTS:
        base = Path(root)
        if p == base or base in p.parents:
            return True
    return False


def choose_install_dir(interactive: bool = True) -> Path:
    """Determine the install location. Enter accepts the default."""
    default = paths.DEFAULT_INSTALL_DIR
    if not interactive:
        return default

    print("\n" + _("Install location [{path}]", path=str(default)))
    print("  " + _("Enter = keep the default, or type your own path:"))
    raw = input("> ").strip()
    if not raw:
        return default
    chosen = Path(os.path.expanduser(raw))
    if is_unstable(chosen):
        print("\n  " + _("Note: '{path}' is often cleaned up or not always "
                         "mounted. The default is recommended so your games "
                         "keep finding this app.", path=str(chosen)))
        answer = input("  " + _("Use it anyway? [y/N] ")).strip().lower()
        if answer not in ("y", "j"):
            return choose_install_dir(interactive=True)
    return chosen


def run(interactive: bool = True) -> dict[str, str]:
    print("=" * 52)
    print("  " + _("{app} - setup", app=paths.APP_TITLE))
    print("=" * 52)

    gl = integrate.detect_gearlever()
    if gl:
        print("\n" + _("GearLever management detected ({path}).",
                       path=str(gl)))
        print(_("GearLever handles placement and updates -- I only set up the "
                "launcher and background parts."))
        cfg = db.load_config()
        cfg["setup_done"] = True
        cfg["managed_by"] = "gearlever"
        db.save_config(cfg)
        result = integrate.full_setup(enable_watcher=True)
        _print_result(result)
        return result

    install_dir = choose_install_dir(interactive=interactive)
    cfg = db.load_config()
    cfg["install_dir"] = str(install_dir)
    cfg["setup_done"] = True
    cfg["managed_by"] = "self"
    db.save_config(cfg)

    print("\n" + _("Setting up in: {path}", path=str(install_dir)))
    result = integrate.full_setup(enable_watcher=True)
    _print_result(result)
    return result


def _print_result(result: dict[str, str]) -> None:
    print("\n" + _("Set up:"))
    for k, v in result.items():
        print(f"  {k:14s} {v}")
    print("\n" + _("Next step: connect your games."))
    print("  " + _("Lutris and Heroic games connect themselves:"))
    print(f"    {paths.APP_NAME} --connect lutris <game>")
    print("  " + _("For Steam games, put this into the game's launch options "
                   "(or let --connect do it while Steam is closed):"))
    print(f'    "{paths.WRAPPER_SHIM}" %command%')
