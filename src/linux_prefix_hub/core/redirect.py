# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

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

Everything above needs a prefix, and a prefix only exists after the first
launch -- while the most natural moment to ask is *before* it. `request()`
stores that wish and `apply_pending()` acts on it later; see `request` for why
the two are apart.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from . import db, registry
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
    """`<root>/<Game>/<Documents|AppData/Roaming|...>`: one folder per game.

    The root is `db.redirect_root()` -- configurable, because "where my games
    keep their saves" is exactly the kind of thing people have an opinion
    about.
    """
    return (base or db.redirect_root()) / _sanitize(game_name) / root


def physical_path(entry: dict[str, Any], root: str) -> Path:
    """Where the shell folder physically lives inside the prefix."""
    return (Path(entry["prefix_path"]) / "drive_c" / "users"
            / entry["user_dir"] / root)


def location_path(entry: dict[str, Any], loc: dict[str, Any]) -> Path | None:
    """Where a storage location's files are *right now*, on disk.

    That is what "open this folder" needs: the redirect target once it has
    been moved, the install folder for game-folder locations, and the path
    inside the prefix otherwise. Returns None when it cannot be resolved.
    """
    if loc.get("redirected") and loc.get("redirect_target"):
        return Path(str(loc["redirect_target"]))

    win_path = str(loc.get("win_path", ""))
    if str(loc.get("where")) == "game_folder":
        game_dir = entry.get("game_dir")
        return Path(str(game_dir)) / win_path if game_dir else None

    if not entry.get("prefix_path") or not entry.get("user_dir"):
        return None
    return (Path(entry["prefix_path"]) / "drive_c" / "users"
            / entry["user_dir"] / win_path)


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


def movable_roots(entry: dict[str, Any]) -> list[str]:
    """The shell folders of one game that redirection can actually express.

    A location in the game's own install folder is skipped rather than
    reported as a failure: it is not movable by design (see the module
    docstring), and it is not what the caller asked about.
    """
    roots: list[str] = []
    for loc in entry.get("storage_locations", []):
        if loc.get("where") == "game_folder":
            continue
        root = registry.shell_folder_root(str(loc.get("win_path", "")))
        if root and root not in roots:
            roots.append(root)
    return roots


# --- Asked for before the game ever ran ---------------------------------
def request(game: dict[str, Any], roots: list[str] | None = None,
            target: str | Path | None = None) -> RedirectResult:
    """Remember that this game's data should live in the home folder.

    Redirection needs a prefix: a registry to point somewhere else and a
    directory to replace with a symlink. Neither exists until the game has
    run once -- but "before I ever start it" is exactly when someone decides
    where its data should go, and right after a PCGamingWiki lookup told them
    what it will write. So the wish is stored under the game's own identity
    (`db.pending_key`, not a prefix fingerprint, because that is the thing
    missing) and `apply_pending` acts on it.

    Storing a wish is not doing the thing, and the message says so.
    """
    db.add_pending_redirect(str(game.get("source", "")),
                            str(game.get("app_id", "")),
                            str(game.get("game_name", "")),
                            roots, str(target) if target else None)
    return RedirectResult(
        True,
        _("{game} has not been started yet. Its data will be moved into your "
          "home folder the first time you play it.",
          game=game.get("game_name") or game.get("app_id")))


def cancel_request(game: dict[str, Any]) -> bool:
    """Drop a stored wish again. False if there was none."""
    return db.drop_pending_redirect(str(game.get("source", "")),
                                    str(game.get("app_id", "")))


def is_requested(game: dict[str, Any]) -> bool:
    """Is a move waiting for this game's first launch?"""
    key = db.pending_key(str(game.get("source", "")),
                         str(game.get("app_id", "")))
    return key in db.pending_redirects()


def _cached_locations(game: dict[str, Any]) -> list[dict[str, Any]]:
    """What a PCGamingWiki lookup already found, from its cache alone.

    That answer may have been sitting there for weeks: a lookup before the
    first launch has no prefix to be keyed by, so it waits in the cache until
    something can file it. This is that something.
    """
    try:
        from . import pcgw
        return pcgw.cached_locations(str(game.get("source", "")),
                                     str(game.get("app_id", "")))
    except Exception:
        return []


def _register(game: dict[str, Any]) -> str:
    """File a freshly created prefix in the DB, with what we already know.

    Normally the launch hook does this -- but a pending wish exists precisely
    for a game nobody connected, so nothing else would.
    """
    entry = {key: game.get(key)
             for key in ("source", "app_id", "game_name", "prefix_path",
                         "user_dir", "game_dir")}
    locations = _cached_locations(game)
    if locations:
        entry["storage_locations"] = locations
    return db.upsert_prefix(entry)


def apply_pending(game: dict[str, Any]) -> list[str]:
    """Act on a stored wish now that the game has a folder. Roots moved.

    Empty means "not this time", never "give up": the wish stays until it has
    been carried out in full. There are three honest reasons to come back
    later, and the caller (`daemon/watcher.py`) simply retries every pass.

      1. **Still never started.** No prefix, nothing to do.
      2. **Started right now.** A prefix appearing means the game is booting,
         not that it is idle -- and Wine writes its in-memory registry over
         `user.reg` when it shuts down, so an edit made now is gone by the
         time the player quits (CLAUDE.md rule 7). The game is filed in the
         DB on this pass and moved on a later one.
      3. **Nothing movable known yet.** A game we never learned anything
         about has no storage location to move. It gets one from a lookup or
         from its first session with the hook installed.
    """
    source = str(game.get("source", ""))
    app_id = str(game.get("app_id", ""))
    wish = db.pending_redirects().get(db.pending_key(source, app_id))
    if wish is None:
        return []
    if not game.get("prefix_path") or not game.get("user_dir"):
        return []                                            # (1)

    fingerprint = _register(game)
    if registry.prefix_in_use(str(game["prefix_path"])):
        return []                                            # (2)

    entry = db.get_prefix(fingerprint) or {}
    roots = [str(r) for r in (wish.get("roots") or [])]
    roots = roots or movable_roots(entry)
    if not roots:
        return []                                            # (3)

    # One named target cannot mean two folders, and which one it would mean
    # was not knowable when the wish was made. Several roots therefore fall
    # back to the default layout, which gives each of them its own place.
    target = wish.get("target") if len(roots) == 1 else None
    moved = [root for root in roots
             if redirect(fingerprint, root, target).ok]
    if len(moved) == len(roots):
        db.drop_pending_redirect(source, app_id)
    return moved


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
