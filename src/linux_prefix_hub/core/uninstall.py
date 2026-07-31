# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Take the app back off the machine, in the one order that cannot lose data.

Uninstalling this app is not "delete some files". Two things it did are
inside *other* people's configuration and outlive us:

  1. **Moved game data.** A storage location we redirected lives in the home
     folder, and the game only finds it through a symlink and a registry
     entry inside its own folder. Delete the app and leave that in place and
     the arrangement still works -- until Proton recreates the folder, the
     link goes, and the game starts a new save next to one nobody knows
     about any more. So every moved folder goes back where the game expects
     it *first*.
  2. **Launch hooks.** Steam launch options, Lutris `prelaunch_command`,
     Heroic `wrapperOptions` all name a shim in `~/.local/bin`. Remove the
     shim while the option still points at it and the game does not start.
     That is not data loss, but it is the user's library broken by our
     cleanup, so it is treated exactly as seriously.

Hence the order in `run()`, and hence the rule that gives this module its
shape: **a step that fails stops the uninstall where it is.** Each stage
leaves the machine in a state that works -- data in the game folder is the
default one, and a hook whose shim still exists is a hook that still runs --
so stopping is always safe and never half-done.

The last stage is the only one that deletes, and by then there is nothing
left that another program points at.
"""
from __future__ import annotations

import contextlib
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import db, integrate, paths, redirect, registry
from .i18n import _


# --- What is out there ---------------------------------------------------
def moved_folders() -> list[dict[str, Any]]:
    """Every shell folder we moved out of a game, one entry each.

    Keyed by the *root* rather than the storage location, because that is
    what redirection can express and what `redirect.undo` takes -- several
    locations inside `Documents` are one moved folder, not three.
    """
    found: list[dict[str, Any]] = []
    for fingerprint, entry in db.load_prefixes().items():
        seen: set[str] = set()
        for loc in entry.get("storage_locations", []):
            if not loc.get("redirected"):
                continue
            root = registry.shell_folder_root(str(loc.get("win_path", "")))
            if not root or root in seen:
                continue
            seen.add(root)
            found.append({
                "fingerprint": fingerprint,
                "root": root,
                "game_name": str(entry.get("game_name") or fingerprint),
                "prefix_path": str(entry.get("prefix_path") or ""),
                "target": str(loc.get("redirect_target") or ""),
            })
    return found


def connected_games() -> list[dict[str, Any]]:
    """Games whose launcher config still names one of our shims.

    Asked of the adapters, not of our own DB: the launcher's config is the
    thing that has to be clean when we are gone, and it can name a shim for
    a game we never got to learn anything about.
    """
    from ..adapters import base
    return [game for game in base.iter_games() if game.get("managed")]


def blockers() -> list[str]:
    """Reasons to say "not now" before touching anything.

    `run()` is safe without this -- it stops on the first failure with
    everything before it in a working state. This is so the user hears
    "close Steam" before forty folders move rather than after.
    """
    found: list[str] = []
    named: set[str] = set()
    for folder in moved_folders():
        prefix = folder["prefix_path"]
        name = folder["game_name"]
        if not prefix or name in named:
            continue
        if registry.prefix_in_use(prefix):
            named.add(name)
            found.append(_("{game} is running. Close it first.", game=name))

    if any(str(g.get("source")) == "steam" for g in connected_games()):
        try:
            from ..adapters import steam
            if steam.steam_is_running():
                found.append(_("Steam is running. Close Steam and try "
                               "again."))
        except Exception:
            pass                    # same rule as discovery: stay quiet
    return found


def removable_files() -> list[Path]:
    """Everything `integrate.full_setup` put on the machine.

    The AppImage GearLever manages is deliberately not in here -- it placed
    that file and it is the one that removes it (see `plan`).
    """
    files = [paths.WRAPPER_SHIM, paths.HOOK_SHIM, paths.DAEMON_SHIM,
             paths.WATCHER_UNIT, integrate.DESKTOP_FILE,
             # An older setup wrote the entry under the on-disk name and only
             # one icon; both are ours to take with us either way.
             integrate.LEGACY_DESKTOP_FILE,
             paths.ICON_FILE, paths.ICON_FILE_APP_ID]
    if not integrate.detect_gearlever():
        files.append(paths.installed_appimage_path(db.install_dir()))
    return [f for f in files if f.exists() or f.is_symlink()]


def plan() -> dict[str, Any]:
    """What `run()` would do, without doing any of it."""
    folders = moved_folders()
    return {
        "folders": folders,
        "games": sorted({f["game_name"] for f in folders}),
        "connected": connected_games(),
        "pending": len(db.pending_redirects()),
        "files": removable_files(),
        "gearlever": str(integrate.detect_gearlever() or ""),
        "config_dir": str(paths.CONFIG_DIR),
        "blockers": blockers(),
    }


# --- The steps -----------------------------------------------------------
def _prune_empty(path: str | Path) -> None:
    """Drop a folder we just emptied, and its parents while they stay empty.

    `rmdir` and nothing else, ever: it refuses on a directory that still has
    something in it, which is exactly the guarantee this needs. A file the
    merge could not move back is a file that keeps its folder alive, all the
    way up.

    Inside first, deepest last-in-first: `redirect._merge_move` moves files
    and leaves the directories that held them, so the top of the tree is
    never empty until the bottom of it is.

    Only below the configured root. A target the user named with `--target`
    is theirs, and tidying up in it is not part of the deal.
    """
    if not path:
        return
    root = db.redirect_root()
    start = Path(str(path))
    if root not in start.parents:
        return
    for child in sorted(start.rglob("*"), key=lambda p: -len(p.parts)):
        if child.is_dir() and not child.is_symlink():
            try:
                child.rmdir()
            except OSError:
                continue            # something is still in there
    current = start
    while root in current.parents:
        try:
            current.rmdir()
        except OSError:
            return                  # not empty, or not ours to remove
        current = current.parent
    # And the root itself, but only the one we invented. `~/Games` is very
    # likely older than this app and full of the user's own things, which
    # is exactly why the default puts our folder one level below it.
    if current == root == paths.DEFAULT_REDIRECT_ROOT:
        with contextlib.suppress(OSError):
            current.rmdir()


def revert_all() -> dict[str, Any]:
    """Put every moved folder back inside its game.

    Returns {ok, reverted, failed, notes}. `redirect.undo` merges without
    ever overwriting and deletes nothing, so a folder that already has a
    file of the same name keeps both copies -- that is a `note`, not a
    failure: the game reads its own folder again either way, and the second
    copy is named so the user can compare the two.
    """
    reverted: list[str] = []
    failed: list[str] = []
    notes: list[str] = []
    for folder in moved_folders():
        result = redirect.undo(folder["fingerprint"], folder["root"])
        label = f"{folder['game_name']}: {folder['root']}"
        if not result.ok:
            failed.append(f"{label} -- {result.message}")
            continue
        reverted.append(label)
        if result.get("kept"):
            notes.append(f"{label} -- {result.message}")
        _prune_empty(folder["target"])
    return {"ok": not failed, "reverted": reverted, "failed": failed,
            "notes": notes}


def disconnect_all() -> dict[str, Any]:
    """Take our shims back out of every launcher config we wrote into.

    Returns {ok, disconnected, failed, manual}. `manual` is the
    hand-installed case and the honest limit of this whole module: there is
    no config to edit, the wrapper sits in a launch command the user wrote,
    and only they can take it out again (`adapters/generic.py`).
    """
    from ..adapters import base
    done: list[str] = []
    failed: list[str] = []
    manual: list[str] = []
    for game in connected_games():
        source, app_id = str(game.get("source")), str(game.get("app_id"))
        name = str(game.get("game_name") or app_id)
        try:
            result = base.get_adapter(source).disconnect(app_id)
        except Exception as exc:               # noqa: BLE001 -- reported
            failed.append(f"{name} -- {exc}")
            continue
        if not result.ok:
            failed.append(f"{name} -- {result.message}")
            continue
        done.append(name)
        if source == "generic":
            manual.append(name)
        found = db.find_prefix(source, app_id)
        if found:
            db.set_managed(found[0], False)
    return {"ok": not failed, "disconnected": done, "failed": failed,
            "manual": manual}


def stop_watcher() -> None:
    """Stop and forget the systemd user unit. Quiet, like `install_*`.

    A machine with no `systemd --user` never had the unit running, and its
    complaints are noise the user can do nothing about.
    """
    with contextlib.suppress(FileNotFoundError, subprocess.SubprocessError):
        subprocess.run(["systemctl", "--user", "disable", "--now",
                        paths.WATCHER_UNIT.name],
                       check=False, timeout=10, capture_output=True)


def _reload_systemd() -> None:
    with contextlib.suppress(FileNotFoundError, subprocess.SubprocessError):
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       check=False, timeout=10, capture_output=True)


def remove_files(keep_settings: bool = False) -> list[str]:
    """Delete what we installed. Returns what really went.

    The AppImage may well be the file this process is executing. Unlinking
    it is fine -- the mount is held by a file descriptor and lives until
    this process ends, which is the same lifetime the code needs.
    """
    removed: list[str] = []
    for path in removable_files():
        try:
            path.unlink()
            removed.append(str(path))
        except OSError:
            continue

    # The install dir is Velopack's `packages/` and the AppImage, both ours.
    # Only rmdir, so a folder the user chose and put something else in stays.
    install = db.install_dir()
    packages = install / "packages"
    if packages.is_dir():
        shutil.rmtree(packages, ignore_errors=True)
    try:
        install.rmdir()
        removed.append(str(install))
    except OSError:
        pass

    if not keep_settings and paths.CONFIG_DIR.is_dir():
        shutil.rmtree(paths.CONFIG_DIR, ignore_errors=True)
        removed.append(str(paths.CONFIG_DIR))
    _reload_systemd()
    return removed


def run(keep_settings: bool = False) -> dict[str, Any]:
    """Uninstall. Returns {ok, stage, ...} and stops at the first failure.

    `stage` names where it stopped, and everything up to that point is a
    state the machine works in -- see the module docstring for why that is
    true of each step and not a hope.
    """
    reverted = revert_all()
    if not reverted["ok"]:
        return {"ok": False, "stage": "revert", **reverted,
                "message": _("Some game data could not be moved back, so "
                             "nothing was removed. Look at the folders "
                             "below and try again.")}

    hooks = disconnect_all()
    if not hooks["ok"]:
        return {"ok": False, "stage": "disconnect",
                "reverted": reverted["reverted"], **hooks,
                "message": _("Your game data is back in the game folders, "
                             "but some games are still connected. Nothing "
                             "was removed -- disconnect them and try "
                             "again.")}

    pending = list(db.pending_redirects())
    for key in pending:
        source, _sep, app_id = key.partition(":")
        db.drop_pending_redirect(source, app_id)

    stop_watcher()
    gearlever = str(integrate.detect_gearlever() or "")
    removed = remove_files(keep_settings=keep_settings)
    return {"ok": True, "stage": "done",
            "reverted": reverted["reverted"],
            "notes": reverted["notes"],
            "disconnected": hooks["disconnected"],
            "manual": hooks["manual"],
            "pending": len(pending),
            "removed": removed,
            "kept_settings": keep_settings,
            "gearlever": gearlever,
            "message": _("{app} has been removed.", app=paths.APP_TITLE)}
