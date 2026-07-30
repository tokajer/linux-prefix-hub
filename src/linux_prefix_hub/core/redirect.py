"""Hybrid redirection: registry entry + symlink, pointing at the same target.

Why both?
  - The **registry** (Shell Folders / User Shell Folders in user.reg) is the
    official way. Well-behaved games ask Windows where Documents is and follow.
  - The **symlink** at the physical location inside the prefix is the safety
    net: plenty of games ignore the registry and write straight to
    `C:\\users\\steamuser\\AppData\\...`. The symlink catches those.

Both point at the same directory in the user's home, so they cannot disagree.
The whole operation is idempotent and self-healing: if a Proton update wipes
the symlink, the data is still in the home directory and the next run relinks
it. That property is the reason redirection is safe to run before every
launch.

Granularity is the **shell folder**, not the individual save directory --
because that is what the registry can express. One prefix belongs to one game
anyway, so redirecting "Documents" of that prefix is exactly "one folder for
this game".

Locations outside a shell folder (a game writing into its own install folder)
are detected and reported, but not redirected: there is no registry key for
them, and symlinking an install folder fights with the launcher's own updater.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from . import db, paths, registry
from .i18n import _


class RedirectResult(dict):
    def __init__(self, ok: bool, message: str, **detail: Any) -> None:
        super().__init__(ok=ok, message=message, **detail)

    @property
    def ok(self) -> bool:
        return bool(self["ok"])

    @property
    def message(self) -> str:
        return str(self["message"])


def _sanitize(name: str) -> str:
    """A game name that is safe as a directory name."""
    cleaned = "".join(c for c in name if c not in '/\\:*?"<>|').strip()
    return cleaned or "Game"


def default_target(game_name: str, root: str,
                   base: Path | None = None) -> Path:
    """~/Games/<Game>/<Documents|AppData/Roaming|...>: one folder per game."""
    return (base or paths.DEFAULT_REDIRECT_ROOT) / _sanitize(game_name) / root


def physical_path(entry: dict[str, Any], root: str) -> Path:
    """Where the shell folder physically lives inside the prefix."""
    return (Path(entry["prefix_path"]) / "drive_c" / "users"
            / entry["user_dir"] / root)


def windows_default(entry: dict[str, Any], root: str) -> str:
    """The value Wine uses when nothing is redirected."""
    return ("C:\\users\\" + entry["user_dir"] + "\\"
            + root.replace("/", "\\"))


def _merge_move(src: Path, dst: Path) -> tuple[int, list[str]]:
    """Move src into dst without ever overwriting. Returns (moved, skipped).

    Never overwriting is deliberate: if both sides have a save file we cannot
    know which one is newer *and wanted*, so we keep both and tell the user.
    """
    moved = 0
    skipped: list[str] = []
    dst.mkdir(parents=True, exist_ok=True)
    for item in sorted(src.rglob("*")):
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if target.exists():
            skipped.append(str(rel))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(item), str(target))
            moved += 1
        except OSError:
            skipped.append(str(rel))
    return moved, skipped


def _replace_with_symlink(physical: Path, target: Path) -> bool:
    """Make `physical` a symlink to `target`, moving any real data over."""
    target.mkdir(parents=True, exist_ok=True)

    if physical.is_symlink():
        if os.path.realpath(physical) == os.path.realpath(target):
            return True                      # already linked: self-heal no-op
        physical.unlink()
    elif physical.is_dir():
        _merge_move(physical, target)
        try:
            shutil.rmtree(physical)          # only empty dirs are left now
        except OSError:
            return False
    elif physical.exists():
        return False                         # a file where a folder belongs

    physical.parent.mkdir(parents=True, exist_ok=True)
    try:
        physical.symlink_to(target, target_is_directory=True)
    except OSError:
        return False
    return True


def redirect(fingerprint: str, win_path: str,
             target: str | Path | None = None,
             force: bool = False) -> RedirectResult:
    """Redirect the shell folder that `win_path` lives in.

    `force` skips the "prefix is in use" guard -- only for the case where the
    caller knows the game is not running (e.g. our own pre-launch hook).
    """
    entry = db.get_prefix(fingerprint)
    if not entry:
        return RedirectResult(False, _("Unknown game."))
    if not entry.get("prefix_path") or not entry.get("user_dir"):
        return RedirectResult(False, _("This game has no prefix yet. Start it "
                                       "once, then try again."))

    root = registry.shell_folder_root(win_path)
    if not root:
        return RedirectResult(
            False,
            _("'{path}' is not inside a standard Windows folder, so it cannot "
              "be moved safely. It stays where it is.", path=win_path))

    if not force and registry.prefix_in_use(entry["prefix_path"]):
        return RedirectResult(False, _("The game is still running. Close it "
                                       "and try again."))

    dest = Path(target) if target else default_target(
        entry.get("game_name", "Game"), root)
    dest = Path(os.path.expanduser(str(dest)))

    physical = physical_path(entry, root)
    if not _replace_with_symlink(physical, dest):
        return RedirectResult(False, _("Could not move '{path}'.", path=root))

    registry.set_shell_folder(entry["prefix_path"], root, dest)

    for loc in entry.get("storage_locations", []):
        if registry.shell_folder_root(loc.get("win_path", "")) == root:
            db.update_location(fingerprint, loc["win_path"],
                               redirected=True, redirect_target=str(dest))

    return RedirectResult(True,
                          _("Moved to {target}.", target=str(dest)),
                          target=str(dest), root=root)


def undo(fingerprint: str, win_path: str,
         force: bool = False) -> RedirectResult:
    """Put a redirected shell folder back inside the prefix."""
    entry = db.get_prefix(fingerprint)
    if not entry:
        return RedirectResult(False, _("Unknown game."))

    root = registry.shell_folder_root(win_path)
    if not root:
        return RedirectResult(False, _("Nothing to undo."))

    if not force and registry.prefix_in_use(entry["prefix_path"]):
        return RedirectResult(False, _("The game is still running. Close it "
                                       "and try again."))

    physical = physical_path(entry, root)
    source = (Path(os.path.realpath(physical))
              if physical.is_symlink() else None)

    if physical.is_symlink():
        physical.unlink()
    physical.mkdir(parents=True, exist_ok=True)
    if source and source.is_dir():
        _merge_move(source, physical)

    registry.set_values(
        entry["prefix_path"], registry.SHELL_FOLDERS_KEY,
        {n: windows_default(entry, root)
         for n in registry.SHELL_FOLDERS.get(root, ())})
    registry.set_values(
        entry["prefix_path"], registry.USER_SHELL_FOLDERS_KEY,
        {n: windows_default(entry, root)
         for n in registry.SHELL_FOLDERS.get(root, ())})

    for loc in entry.get("storage_locations", []):
        if registry.shell_folder_root(loc.get("win_path", "")) == root:
            db.update_location(fingerprint, loc["win_path"],
                               redirected=False, redirect_target=None)

    return RedirectResult(True, _("Moved back into the game folder."))


def reapply(fingerprint: str) -> list[str]:
    """Re-link every redirected location of one game (self-heal).

    Called before launch: a Proton update that replaced our symlink with a
    fresh empty folder is repaired here, before the game can write into it.
    """
    entry = db.get_prefix(fingerprint)
    if not entry or not entry.get("prefix_path"):
        return []
    healed: list[str] = []
    done: set[str] = set()
    for loc in entry.get("storage_locations", []):
        if not loc.get("redirected") or not loc.get("redirect_target"):
            continue
        root = registry.shell_folder_root(loc.get("win_path", ""))
        if not root or root in done:
            continue
        done.add(root)
        physical = physical_path(entry, root)
        target = Path(loc["redirect_target"])
        if (physical.is_symlink()
                and os.path.realpath(physical) == os.path.realpath(target)):
            continue
        if _replace_with_symlink(physical, target):
            registry.set_shell_folder(entry["prefix_path"], root, target)
            healed.append(root)
    return healed
