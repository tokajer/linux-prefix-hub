# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Single-binary entry point with several modes.

Matches the AppImage concept: ONE artifact, several modes.

  (no args)          the window -- always, unless there is no display
  --gui              the window, explicitly
  --terminal         the overview in the terminal instead
  --wrapper CMD...   wrap a game launch     (Steam/Heroic call this via shim)
  --hook pre|post    pre/post launch hook   (Lutris calls this via shim)
  --daemon           watcher                (systemd calls this via shim)
  --integrate        force self-setup       (AppRun calls this to self-heal)
  --scan             list games from all sources
  --status           show what we learned
  --connect GAME     install the launch hook for one game
  --lookup GAME      ask PCGamingWiki where it stores things, unplayed
  --redirect GAME    move its storage locations into your home
  --open GAME        show its data folder in the file manager
  --hide GAME        leave it out of the lists (--unhide puts it back)
  --options GAME     extra options for one game (--options-on/-off/-edit)
  --new-options NAME an environment of your own, belonging to no game
  --uninstall        move all game data back, then remove the app

The three shim modes are dispatched before argparse and with lazy imports, so
a game launch pays for nothing it does not use.
"""
from __future__ import annotations

import contextlib
import sys
from typing import Any

from . import __version__
from .core.i18n import _


# --- helpers -------------------------------------------------------------
def _match_games(needle: str, source: str | None = None) -> list[dict]:
    """Find games by id or (partial, case-insensitive) name."""
    from .adapters import base
    sources = (source,) if source else None
    games = list(base.iter_games(sources))  # type: ignore[arg-type]
    exact = [g for g in games if str(g.get("app_id")) == needle]
    if exact:
        return exact
    low = needle.lower()
    return [g for g in games if low in str(g.get("game_name", "")).lower()]


def _pick_game(needle: str, source: str | None) -> dict | None:
    matches = _match_games(needle, source)
    if not matches:
        print(_("No game found for '{needle}'.", needle=needle))
        return None
    if len(matches) > 1:
        print(_("'{needle}' matches several games:", needle=needle))
        for g in matches:
            print(f"  {g['source']:<7} {g['app_id']:<24} {g['game_name']}")
        print(_("Use the id, or add --source."))
        return None
    return matches[0]


# --- commands ------------------------------------------------------------
def _cmd_scan(source: str | None, show_hidden: bool = False) -> int:
    from .adapters import base
    from .core import db
    sources = (source,) if source else None
    found = list(base.iter_games(sources))  # type: ignore[arg-type]
    listed = found if show_hidden else list(base.visible_games(found))
    left_out = len(found) - len(listed)

    if not listed:
        print(_("No games found. Is Steam/Lutris/Heroic installed for this "
                "user?") if not left_out
              else _("All {n} game(s) found are hidden. Add --show-hidden to "
                     "list them.", n=left_out))
        return 0

    hidden_keys = set(db.hidden_games()) if show_hidden else set()
    print(_("{n} game(s) found:", n=len(listed)))
    for src, games in base.group_by_source(listed):
        print(f"\n{base.source_label(src)}")
        for g in games:
            state = _("installed") if g.get("installed") else _("downloading")
            prefix = _("ready") if g.get("prefix_path") else _("never started")
            hook = _("connected") if g.get("managed") else _("not connected")
            flags = f"[{state}] [{prefix}] [{hook}]"
            if base.game_key(g) in hidden_keys:
                flags += f" [{_('hidden')}]"
            print(f"  {str(g.get('game_name'))[:34]:<34} "
                  f"{flags} id={g['app_id']}")
    if left_out:
        print()
        print(_("{n} hidden game(s) not listed. Add --show-hidden to see "
                "them.", n=left_out))
    return 0


def _cmd_hide(needle: str, source: str | None, undo: bool) -> int:
    """Leave a game out of the lists -- or put it back.

    Nothing else changes: the launch hook stays installed, what we learned
    stays learned and a move that was asked for still happens. This is about
    a list being too long, not about a game being none of our business.
    """
    from .core import db
    game = _pick_game(needle, source)
    if not game:
        return 1
    src, app_id = str(game["source"]), str(game["app_id"])
    name = game.get("game_name")
    if undo:
        print(_("{game} is back in the list.", game=name)
              if db.unhide_game(src, app_id)
              else _("{game} was not hidden.", game=name))
        return 0
    print(_("{game} is hidden. --show-hidden lists it again.", game=name)
          if db.hide_game(src, app_id)
          else _("{game} is already hidden.", game=name))
    return 0


def _cmd_status() -> int:
    from .core import db, redirect, registry
    prefixes = db.load_prefixes()
    if not prefixes:
        print(_("Nothing learned yet. Connect a game and play it once, then "
                "its storage locations show up here."))
        return 0
    for _fp, entry in prefixes.items():
        print(f"\n{entry.get('game_name')} "
              f"({entry.get('source')}/{entry.get('app_id')})")
        print("  " + _("game folder: {path}", path=entry.get("prefix_path")))
        noted: set[str] = set()
        for loc in entry.get("storage_locations", []):
            if loc.get("redirected"):
                where = _("moved to {target}",
                          target=loc.get("redirect_target"))
            elif loc.get("where") == "game_folder":
                # Cannot be moved -- say so here rather than let the user
                # discover it by trying.
                where = _("in the game's own folder, stays there")
            else:
                where = _("in place")
            print(f"    [{loc.get('type', '?'):<7}] {loc.get('win_path')}  "
                  f"({where})")
            # Only for a folder we actually moved: everywhere else the second
            # writer is Steam's business and none of ours.
            root = registry.shell_folder_root(str(loc.get("win_path", "")))
            if not loc.get("redirected") or not root or root in noted:
                continue
            noted.add(root)
            warning = redirect.cloud_warning(entry, root)
            if warning:
                print(f"             {warning[0]}")
    return 0


def _game_folder(needle: str, entry: dict | None) -> str | None:
    """Where the game itself lives -- from the DB, else from the launcher.

    The DB only knows a game once something has been learned about it, but
    the folder exists as soon as the game has been started once. Asking the
    adapters covers exactly that gap.
    """
    if entry and entry.get("prefix_path"):
        return str(entry["prefix_path"])
    folders = {str(g["prefix_path"]) for g in _match_games(needle)
               if g.get("prefix_path")}
    return folders.pop() if len(folders) == 1 else None


def _cmd_open(needle: str) -> int:
    """Show a game's data folder in the file manager.

    Falls back to the game folder itself. "Where does this game keep its
    things?" has an answer before we have learned anything -- and that answer
    is the folder the game lives in.
    """
    from .core import db, desktop, redirect
    found = db.resolve(needle)
    entry = found[1] if found else None

    opened = 0
    for loc in (entry or {}).get("storage_locations", []):
        path = redirect.location_path(entry or {}, loc)
        if path and desktop.open_folder(path):
            print(_("Opening {path}", path=str(path)))
            opened += 1
    if opened:
        return 0

    folder = _game_folder(needle, entry)
    if folder and desktop.open_folder(folder):
        print(_("Opening the game folder {path}", path=folder))
        return 0
    if entry:
        print(_("No folder to open for {game} -- nothing is stored there "
                "(yet).", game=entry.get("game_name")))
    else:
        print(_("'{needle}' is not in the list yet. Connect the game and play "
                "it once so we know where it stores its data.", needle=needle))
    return 1


def _cmd_connect(needle: str, source: str | None, undo: bool) -> int:
    from .adapters import base
    from .core import db
    game = _pick_game(needle, source)
    if not game:
        return 1
    adapter = base.get_adapter(game["source"])
    result = (adapter.disconnect if undo else adapter.connect)(game["app_id"])
    print(f"{game['game_name']}: {result.message}")
    if game.get("prefix_path"):
        found = db.find_prefix(game["source"], game["app_id"])
        if found:
            db.set_managed(found[0], result.ok and not undo)
    return 0 if result.ok else 1


def _ask(question: str) -> bool:
    """A yes/no question in the terminal. Anything that is not yes is no.

    EOF is not an answer either: a pipe or a service unit has nobody to ask,
    and silence must not count as agreement. `--yes` is how those say yes.
    """
    try:
        answer = input(question).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("y", "yes", "j", "ja")


def _location_line(loc: dict[str, Any], here: bool = True) -> str:
    """One suggested storage location, as a line in the terminal."""
    line = f"    [{loc.get('type', '?'):<7}] {loc.get('win_path')}"
    if loc.get("where") == "game_folder":
        line += " " + _("(in the game's own folder)")
    return line if here else line + "  " + _("(not there yet)")


def _cmd_lookup(needle: str, source: str | None, assume_yes: bool) -> int:
    """Ask PCGamingWiki where a game saves, instead of playing it first.

    The answer is a suggestion and is shown as one: nothing is written until
    the user says yes, and even then only the folders that are actually
    there. See rule 4 in `core/pcgw.py`.
    """
    from .core import pcgw
    game = _pick_game(needle, source)
    if not game:
        return 1

    result = pcgw.lookup(game)
    print(result["message"])
    here, not_here = pcgw.on_disk(game, result["locations"])
    for loc in here:
        print(_location_line(loc))
    for loc in not_here:
        print(_location_line(loc, here=False))
    if result["url"]:
        print("    " + str(result["url"]))
    if not result["locations"]:
        return 0 if result["ok"] else 1

    if not assume_yes and not _ask(_("Use these storage locations? [y/N] ")):
        print(_("Nothing was added."))
        return 0

    outcome = pcgw.confirm(game, result["locations"])
    if outcome["stored"]:
        print(_("Added {n} storage location(s).", n=len(outcome["added"])))
    if outcome["waiting"]:
        # Confirmed, but there is nothing to point at yet. Not an error and
        # not a promise -- if the game never writes it, we never write it.
        print(_("{n} of them do not exist yet. They are added the first time "
                "the game creates them.", n=len(outcome["waiting"])))
    elif not outcome["stored"]:
        print(_("Connect the game and start it once -- then these show up "
                "with it."))
    return 0 if result["ok"] else 1


def _redirect_later(needle: str, target: str | None, undo: bool) -> int:
    """Nothing learned about this game yet -- the wish can still be kept.

    A game nobody has played has no folder to move, but "put this one in my
    home folder" is a perfectly sensible thing to say about it. So it is
    remembered and acted on at the first moment it can be
    (`core/redirect.apply_pending`), which is usually the watcher's job --
    unless the game already has a folder, in which case it is this call's.
    """
    from .core import redirect
    game = _pick_game(needle, None)
    if game is None:
        return 1

    if undo:
        if redirect.cancel_request(game):
            print(_("{game} will be left where it is.",
                    game=game.get("game_name")))
            return 0
        print(_("'{needle}' is not in the list yet. Connect the game and play "
                "it once so we know where it stores its data.", needle=needle))
        return 1

    result = redirect.request(game, target=target)
    moved = redirect.apply_pending(game)
    if moved:
        print(_("Moved: {roots}", roots=", ".join(moved)))
        return 0
    print(result.message)
    return 0


def _cmd_redirect(needle: str, target: str | None, undo: bool) -> int:
    from .core import db, redirect
    found = db.resolve(needle)
    if not found:
        return _redirect_later(needle, target, undo)
    fingerprint, entry = found

    roots = redirect.movable_roots(entry)
    if not roots:
        if not entry.get("storage_locations"):
            # Known as a game, but nothing learned about it yet. That is the
            # same situation as an unknown game, so it gets the same answer.
            return _redirect_later(needle, target, undo)
        print(_("No movable storage location known for {game}.",
                game=entry.get("game_name")))
        if any(loc.get("where") == "game_folder"
               for loc in entry.get("storage_locations", [])):
            print(_("This game keeps its data in its own folder. That "
                    "cannot be moved safely -- use --open to look at it."))
        return 1
    if target and len(roots) > 1:
        print(_("This game has several storage locations; --target only works "
                "with one. Locations: {roots}", roots=", ".join(roots)))
        return 1

    failed = False
    for root in roots:
        # Before, not after: "your launcher also writes here" is something to
        # know while the folder is still where it was.
        warning = None if undo else redirect.cloud_warning(entry, root)
        if warning:
            print(f"{root}: {warning[0]}")
            print(f"  {warning[1]}")
        action = redirect.undo if undo else redirect.redirect
        result = (action(fingerprint, root) if undo
                  else action(fingerprint, root, target))
        print(f"{root}: {result.message}")
        failed = failed or not result.ok
    return 1 if failed else 0


# --- extra options -------------------------------------------------------
def _pick_target(needle: str, source: str | None) -> dict | None:
    """A game, or one of the user's own environments.

    Theirs are checked first and by id: they are few, they are named by the
    user, and a name they chose should not be beaten by a partial match in a
    library of four hundred games.
    """
    from .core import gameopts, newprefix
    wanted = gameopts.slug(needle)
    for game in gameopts.custom_environments():
        if str(game["app_id"]) == wanted:
            return game
    # A folder this app made answers to its short name too, which is what
    # the folder is called and therefore what people type.
    folder = newprefix.find(needle)
    if folder is not None:
        return newprefix.as_game(folder)
    return _pick_game(needle, source)


def _cmd_options(needle: str, source: str | None,
                 name: str | None = None) -> int:
    """Show what a game's extra options currently are -- or rename it."""
    from .adapters import steam
    from .core import gameopts
    game = _pick_target(needle, source)
    if not game:
        return 1
    src, app_id = str(game["source"]), str(game["app_id"])

    if name:
        from .core import newprefix
        folder = newprefix.owned(game.get("prefix_path"))
        # A folder we made keeps its name in its own marker, where the scan
        # reads it -- the profile's title is for an environment with no
        # folder behind it.
        result = (newprefix.rename(folder, name) if folder is not None
                  else gameopts.rename(src, app_id, name))
        print(result.message)
        if not result.ok:
            return 1
        game["game_name"] = str(result.get("title") or result.get("name")
                                or name)
    profile = gameopts.read(src, app_id)

    print(f"{game['game_name']} ({src}/{app_id})")
    if not profile["enabled"]:
        print("  " + _("Extra options are off."))
        print("  " + _("Turn them on with: {cmd}",
                       cmd=f"{_app_name()} --options-on {app_id}"))
        return 0

    print("  " + _("Extra options are on."))
    for switch in profile["switches"]:
        print(f"    [x] {gameopts.switch_label(switch)}")
    # Every line the user wrote, including one that turns a switch above back
    # off again -- hiding it would draw a switch as on that is not.
    own = gameopts.parse_custom(profile["custom"])
    for key, value in own.items():
        print(f"    {key}={value}")
    if gameopts.APPID_VAR in gameopts.env_for(profile, app_id) \
            and gameopts.APPID_VAR not in own:
        print(f"    {gameopts.APPID_VAR}={gameopts.APPID_FALLBACK}  "
              + _("(this game needs one)"))
    built = gameopts.find_instance(src, app_id)
    if src == "steam" and (built is None
                           or steam.compat_tool(app_id) != built.name):
        print("  " + _("Steam is not set to use them. Turn them on again."))
    if gameopts.outdated(profile):
        print("  " + _("A newer version is available. Refresh with: {cmd}",
                       cmd=f"{_app_name()} --rebuild-options"))
    return 0


def _cmd_options_switch(needle: str, source: str | None, on: bool) -> int:
    from .core import gameopts
    game = _pick_target(needle, source)
    if not game:
        return 1
    result = gameopts.turn_on(game) if on else gameopts.turn_off(game)
    print(f"{game['game_name']}: {result.message}")
    return 0 if result.ok else 1


def _cmd_options_edit(needle: str, source: str | None) -> int:
    """Edit the game's own KEY=value lines in $EDITOR, then apply them.

    Through a temporary file rather than a file of our own on disk: the
    profile lives in `config.json` with everything else a game owns, and a
    second copy of it that only the editor knows about is a second answer to
    the same question.
    """
    import os
    import subprocess
    import tempfile
    from pathlib import Path

    from .core import gameopts
    game = _pick_target(needle, source)
    if not game:
        return 1
    src, app_id = str(game["source"]), str(game["app_id"])
    profile = gameopts.read(src, app_id)

    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(profile["custom"] or _CUSTOM_TEMPLATE)
        temp = handle.name
    try:
        subprocess.run([os.environ.get("EDITOR", "nano"), temp], check=False)
        profile["custom"] = Path(temp).read_text(encoding="utf-8")
    finally:
        os.unlink(temp)

    gameopts.write(src, app_id, profile)
    if not profile["enabled"]:
        print(_("Saved. Turn the options on to use them: {cmd}",
                cmd=f"{_app_name()} --options-on {app_id}"))
        return 0
    result = gameopts.turn_on(game, profile)
    print(f"{game['game_name']}: {result.message}")
    return 0 if result.ok else 1


_CUSTOM_TEMPLATE = (
    "# One NAME=value per line. Lines starting with # are ignored.\n"
    "# These are handed to the game exactly as written.\n")


def _cmd_new_options(name: str) -> int:
    """An environment of your own: a name, and nothing behind it.

    No game, no launcher -- so nothing is pointed at it either. It shows up
    in Steam's compatibility list and the user picks it wherever they want.
    """
    from .core import gameopts
    if not gameopts.slug(name):
        print(_("That name cannot be used."))
        return 1
    game = gameopts.as_game(name)
    result = gameopts.turn_on(game)
    print(result.message)
    if result.ok:
        print(_("Add your own settings with: {cmd}",
                cmd=f"{_app_name()} --options-edit {game['app_id']}"))
    return 0 if result.ok else 1


def _cmd_list_options() -> int:
    from .core import gameopts
    own = gameopts.custom_environments()
    waiting = gameopts.importable()
    if own:
        for game in own:
            profile = gameopts.read(game["source"], str(game["app_id"]))
            state = _("on") if profile["enabled"] else _("off")
            built = gameopts.find_instance(game["source"],
                                           str(game["app_id"]))
            short = built.name if built is not None else str(game["app_id"])
            print(f"  {str(game['game_name'])[:28]:<28} [{state}]  {short}")
    else:
        print(_("You have no environments of your own yet."))
        print(_("Make one with: {cmd}",
                cmd=f"{_app_name()} --new-options NAME"))
    if waiting:
        print()
        print(_("{n} profile(s) from the proton-instance script can be taken "
                "over: {cmd}", n=len(waiting),
                cmd=f"{_app_name()} --import-options"))
    return 0


def _cmd_import_options(assume_yes: bool) -> int:
    """Take the shell script's profiles over. Its own folders stay put."""
    from .core import gameopts
    waiting = gameopts.importable()
    if not waiting:
        print(_("Nothing to take over."))
        return 0
    for entry in waiting:
        print(f"  {entry['name']}  ({entry['base']})")
    if not assume_yes and not _ask(_("Take these over? [y/N] ")):
        print(_("Nothing was changed."))
        return 0
    for entry in waiting:
        print(gameopts.import_legacy(entry).message)
    print(_("Their own folders were left alone. Turn one on with: {cmd}",
            cmd=f"{_app_name()} --options-on NAME"))
    return 0


def _cmd_rebuild_options() -> int:
    from .core import gameopts
    results = gameopts.rebuild_all()
    if not results:
        print(_("No game uses extra options."))
        return 0
    for result in results:
        print(result.message)
    return 0 if all(r.ok for r in results) else 1


def _cmd_new_prefix(name: str, engine: str | None, target: str | None,
                    alias: str | None) -> int:
    """A game folder of the user's own: make it, then say what is in it."""
    from .core import newprefix
    available = newprefix.engines()
    if engine and engine not in [e["id"] for e in available]:
        print(_("'{name}' is not installed here.", name=engine))
        _print_engines(available)
        return 1
    result = newprefix.create(name, engine or "", target, alias or "")
    print(result.message)
    if not result.ok:
        return 1
    print(_("Folder: {path}", path=str(result["path"])))
    print(_("Windows version: {name}",
            name=newprefix.engine_label(str(result["engine"]))))
    print(_("Install something into it with: {cmd}",
            cmd=f"{_app_name()} --run-in {result['path']} "
                f"--program SETUP.EXE"))
    return 0


def _print_engines(available: list[dict[str, str]]) -> None:
    from .core import newprefix
    print(_("Available:"))
    for engine in available:
        print(f"  {engine['id']:<24} {newprefix.engine_label(engine['id'])}")


def _cmd_run_in(needle: str, program: str | None) -> int:
    """Run a program inside a game folder we made -- or its settings."""
    from .core import newprefix
    directory = _own_folder(needle)
    if directory is None:
        return 1
    result = newprefix.install(directory, program) if program \
        else newprefix.settings(directory)
    print(result.message)
    return 0 if result.ok else 1


def _cmd_delete_prefix(needle: str, assume_yes: bool) -> int:
    """Delete a game folder this app made, with everything in it."""
    from .core import newprefix
    directory = _own_folder(needle)
    if directory is None:
        return 1

    kept = newprefix.moved_out(f"{directory}/{newprefix.PREFIX_DIR}")
    print(_("This deletes {path} and everything in it: the game, its "
            "settings and anything saved inside it.", path=directory))
    for path in kept:
        print(_("Your moved game data stays in {path}.", path=path))
    if not assume_yes and not _ask(_("Delete it? [y/N] ")):
        print(_("Nothing was changed."))
        return 0

    result = newprefix.delete(directory)
    print(result.message)
    return 0 if result.ok else 1


def _cmd_play(needle: str, program: str | None) -> int:
    """Start the game in one of our folders -- and learn what it touches."""
    from .core import newprefix
    directory = _own_folder(needle)
    if directory is None:
        return 1
    if not program and newprefix.program_of(directory) is None:
        print(_("No game chosen yet."))
        print(_("Name it once with: {cmd}",
                cmd=f"{_app_name()} --play {directory} --program GAME.EXE"))
        return 1
    print(_("Starting it. This window stays busy until the game ends."))
    result = newprefix.launch(directory, program)
    print(result.message)
    return 0 if result.ok else 1


def _cmd_watch_folder(needle: str, target: str | None) -> int:
    """A second folder that belongs to the same game."""
    from .core import newprefix
    directory = _own_folder(needle)
    if directory is None:
        return 1
    if target is None:
        named = newprefix.watch_dir(directory)
        print(_("Also watched: {path}", path=str(named)) if named
              else _("No second folder is watched."))
        print(_("Name one with: {cmd}",
                cmd=f"{_app_name()} --watch-folder {needle} --target PATH"))
        return 0
    result = newprefix.set_watch_dir(directory, target or None)
    print(result.message)
    return 0 if result.ok else 1


def _cmd_shortcut(needle: str, undo: bool) -> int:
    """A menu entry that starts the game -- and still lets us watch it."""
    from .core import newprefix
    directory = _own_folder(needle)
    if directory is None:
        return 1
    result = (newprefix.drop_shortcut(directory) if undo
              else newprefix.make_shortcut(directory))
    print(result.message)
    return 0 if result.ok else 1


def _cmd_own_version(needle: str, undo: bool) -> int:
    """A compatibility build that belongs to one folder alone."""
    from .core import newprefix
    directory = _own_folder(needle)
    if directory is None:
        return 1
    result = (newprefix.drop_private(directory) if undo
              else newprefix.make_private(directory))
    print(result.message)
    return 0 if result.ok else 1


def _cmd_set_engine(needle: str, engine: str | None) -> int:
    """Which Windows version one of our folders uses from now on."""
    from .core import newprefix
    directory = _own_folder(needle)
    if directory is None:
        return 1
    available = newprefix.engines()
    if not engine:
        current = newprefix.engine_of(directory)
        print(_("Windows version: {name}",
                name=newprefix.engine_label(current) or "?"))
        warning = newprefix.runtime_warning(current)
        if warning:
            print("  " + warning)
        _print_engines_with_runtime(available)
        print(_("Choose one with: {cmd}",
                cmd=f"{_app_name()} --set-engine {directory} --engine NAME"))
        return 0
    result = newprefix.set_engine(directory, engine)
    print(result.message)
    if not result.ok:
        _print_engines(available)
    return 0 if result.ok else 1


def _print_engines_with_runtime(available: list[dict[str, str]]) -> None:
    from .core import newprefix
    print(_("Available:"))
    for engine in available:
        _appid, runtime = newprefix.required_runtime(str(engine["id"]))
        label = newprefix.engine_label(str(engine["id"]))
        print(f"  {engine['id']:<24} {label}"
              + (f"  [{runtime}]" if runtime else ""))


def _own_folder(needle: str) -> str | None:
    """The folder behind a path, a short name or a name -- only one of ours.

    `newprefix.find` answers all three, and a path works before a scan has
    ever seen the folder. Anything it cannot settle falls through to the
    ordinary game matching, which has the messages for "no match" and "that
    matches several" already.
    """
    from .core import newprefix
    folder = newprefix.find(needle)
    if folder is not None:
        return str(folder)
    game = _pick_game(needle, "generic")
    if not game:
        return None
    directory = newprefix.owned(game.get("prefix_path"))
    if directory is None:
        print(_("{name} is not a game folder this app made.",
                name=str(game.get("game_name"))))
        return None
    return str(directory)


def _cmd_move_old_data(assume_yes: bool) -> int:
    """Bring data an earlier version moved into the current data folder."""
    from .core import redirect
    waiting = redirect.stale_targets()
    if not waiting:
        print(_("Nothing to move."))
        return 0
    for item in waiting:
        print(f"  {item['game_name']}")
        print(f"    {item['source']}")
        print(f"    -> {item['target']}")
    if not assume_yes and not _ask(_("Move these? [y/N] ")):
        print(_("Nothing was changed."))
        return 0
    results = redirect.move_stale()
    for result in results:
        print(result.message)
    return 0 if all(r.ok for r in results) else 1


def _cmd_game_root(path: str) -> None:
    """Where new game folders are made from now on."""
    from .core import newprefix
    where = newprefix.set_root(path)
    print(_("New game folders are made in {path}.", path=str(where)))
    print(_("Folders you already made stay where they are."))


def _cmd_game_folder(path: str, forget: bool) -> None:
    """Remember (or drop) a folder we look in for hand-installed games."""
    import os

    from .core import db
    full = os.path.abspath(os.path.expanduser(path))
    if forget:
        print(_("No longer looking in {path}.", path=full)
              if db.forget_game_folder(full)
              else _("{path} was not in the list.", path=full))
        return
    print(_("Also looking for games in {path}.", path=full)
          if db.add_game_folder(full)
          else _("{path} is already in the list.", path=full))


def _cmd_ignore_path(fragment: str, forget: bool) -> None:
    """Add (or drop) a filter for paths that are never a storage location.

    Adding one cleans up straight away instead of only from the next launch:
    the point of typing this is usually a folder the user is looking at right
    now. Anything already moved into the home folder stays -- see
    `db.prune_locations`.
    """
    from .core import db, snapshot
    if forget:
        print(_("'{path}' counts as a storage location again.", path=fragment)
              if db.forget_ignore_path(fragment)
              else _("'{path}' was not on the ignore list.", path=fragment))
        return
    if not db.add_ignore_path(fragment):
        print(_("'{path}' is already ignored.", path=fragment))
        return
    print(_("Ignoring '{path}' from now on.", path=fragment))
    dropped = db.prune_locations(None, snapshot.location_is_noise)
    if dropped:
        print(_("{n} known location(s) matched and were forgotten.",
                n=dropped))


def _cmd_update(install: bool) -> int:
    from .core import updater
    if install:
        result = updater.update()
        print(result["message"])
        return 0 if result["ok"] else 1
    state = updater.check(force=True)
    # "Up to date" is only honest when we actually got an answer. Saying it
    # after a check that never happened is how a build with no updater in it
    # ends up claiming to be current while a newer release sits on GitHub.
    if state.get("reason") == "unavailable":
        print(_("This build cannot update itself. Download the latest "
                "version from {url}.", url=updater.repo_url()))
        return 1
    if state.get("reason") == "unreachable":
        print(_("Could not reach GitHub."))
        return 1
    if state.get("available"):
        print(_("Update available: {version} (you have {current}).",
                version=state.get("version"), current=updater.__version__))
        print(_("Install it with: {cmd}",
                cmd=f"{_app_name()} --update"))
    else:
        print(_("You are up to date ({version}).",
                version=updater.__version__))
    return 0


def _app_name() -> str:
    from .core import paths
    return paths.APP_NAME


def _print_list(heading: str, items: list[str], limit: int = 8) -> None:
    """A heading and its lines, with a long tail folded into a count."""
    if not items:
        return
    print(heading)
    for item in items[:limit]:
        print(f"  {item}")
    if len(items) > limit:
        print("  " + _("...and {n} more", n=len(items) - limit))


def _cmd_uninstall(assume_yes: bool, keep_settings: bool) -> int:
    """Remove the app -- but only once the games are back to normal.

    The plan is printed before anything happens, because the interesting
    part of an uninstall is not what gets deleted: it is which game folders
    move back and which launcher configs are edited. Both are undone here
    whether the user asked for that or not, so both are shown first.
    """
    from .core import paths, uninstall

    print(_("Removing {app} will:", app=paths.APP_TITLE))
    preview = uninstall.plan()
    if preview["games"]:
        _print_list(_("  move game data back into these games:"),
                    list(preview["games"]))
    if preview["connected"]:
        _print_list(_("  disconnect these games again:"),
                    [str(g.get("game_name") or g.get("app_id"))
                     for g in preview["connected"]])
    if preview["options"]:
        _print_list(_("  hand these games back to Steam's own settings:"),
                    [str(g.get("game_name")) for g in preview["options"]])
    _print_list(_("  delete:"), [str(p) for p in preview["files"]],
                limit=20)
    if keep_settings:
        print("  " + _("Settings and what was learned stay in {path}.",
                       path=preview["config_dir"]))
    else:
        print("  " + _("delete {path} (add --keep-settings to keep it)",
                       path=preview["config_dir"]))
    if preview["gearlever"]:
        print("  " + _("GearLever placed the app file, so it stays -- remove "
                       "it in GearLever."))

    if preview["blockers"]:
        print()
        print(_("Not yet:"))
        for line in preview["blockers"]:
            print(f"  {line}")
        print(_("Nothing was changed."))
        return 1

    if not assume_yes and not _ask(_("Remove {app} now? [y/N] ",
                                     app=paths.APP_TITLE)):
        print(_("Nothing was changed."))
        return 0

    result = uninstall.run(keep_settings=keep_settings)
    if not result["ok"]:
        print(result["message"])
        _print_list("", list(result.get("failed", [])), limit=20)
        return 1

    print(result["message"])
    if result["reverted"]:
        print(_("{n} folder(s) are back in their game.",
                n=len(result["reverted"])))
    _print_list(_("Both copies were kept here:"),
                list(result.get("notes", [])), limit=20)
    if result.get("manual"):
        print(_("You started these yourself -- take '{shim}' back out of "
                "your own launch command: {games}",
                shim=str(paths.WRAPPER_SHIM),
                games=", ".join(result["manual"])))
    if result["kept_settings"]:
        print(_("Settings and what was learned stay in {path}.",
                path=str(paths.CONFIG_DIR)))
    return 0


def _cmd_integrate() -> int:
    from .core import integrate
    for key, value in integrate.full_setup(enable_watcher=True).items():
        print(f"{key:14s} {value}")
    return 0


def _has_display() -> bool:
    """Is there anything to draw a window on?"""
    import os
    return bool(os.environ.get("WAYLAND_DISPLAY")
                or os.environ.get("DISPLAY"))


def _has_gtk() -> bool:
    """Can *this* interpreter draw the window itself?"""
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        return True
    except (ImportError, ValueError):
        return False


def _gui_available() -> bool:
    """A display to draw on and the GTK bindings to draw with.

    Note this is deliberately *not* the condition for starting the GUI -- see
    `_cmd_gui`, which can still get there by handing over to another
    interpreter. Inside the AppImage this is always False.
    """
    return _has_display() and _has_gtk()


GUI_PROBE = ("import gi; gi.require_version('Gtk', '4.0'); "
             "gi.require_version('Adw', '1')")
REEXEC_FLAG = "LPH_GUI_REEXEC"


def _handover_env() -> dict[str, str]:
    """Environment for a *different* interpreter than the current one.

    The AppImage's AppRun exports PYTHONHOME (and a PYTHONPATH) for its
    bundled CPython. Inheriting those would make any other interpreter load
    the wrong standard library, which is exactly why the first attempt at
    this failed. So they are dropped and PYTHONPATH is rebuilt to point at
    our package alone -- it is pure Python and imports fine anywhere.
    """
    import os
    from pathlib import Path
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP",
                        "PYTHONEXECUTABLE")}
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    env[REEXEC_FLAG] = str(os.getpid())      # see _reexec_gui
    return env


def _system_python_with_gtk(env: dict[str, str]) -> str | None:
    """A system interpreter that has the GTK bindings this one lacks.

    The AppImage bundles its own CPython, and PyGObject cannot sensibly be
    bundled with it -- it wants the host's GTK 4, libadwaita and typelibs. Our
    package is pure Python, so any system interpreter >= 3.10 can run it: we
    hand the window over to that one instead of shipping a second GTK stack.
    """
    import shutil
    import subprocess
    for name in ("python3", "python3.14", "python3.13", "python3.12",
                 "python3.11", "python3.10"):
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            probe = subprocess.run([exe, "-c", GUI_PROBE], check=False,
                                   timeout=20, capture_output=True, env=env)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return exe
    return None


def _reexec_gui() -> int:
    """Restart --gui under a system interpreter that has GTK. Never returns
    on success.

    The loop guard is **our own pid**, not a plain "1". `execve` keeps the
    pid, so the interpreter we hand over to still recognises the flag -- but a
    process that merely *inherited* our environment does not, and that
    distinction is the whole point. We start the user's file manager
    (`core/desktop.py`), KDE keeps that one Dolphin alive for the rest of the
    session and hands new windows to it, so a flag it kept would make every
    later start of this app skip the handover and fall through to the terminal
    branch -- with no window and the explanation printed into the journal
    where nobody reads it. Symptom: "the app does not start any more".
    """
    import os
    if os.environ.get(REEXEC_FLAG) == str(os.getpid()):
        return 1                       # already tried; do not loop
    env = _handover_env()
    exe = _system_python_with_gtk(env)
    if not exe:
        return 1
    try:
        os.execve(exe, [exe, "-m", "linux_prefix_hub", "--gui"], env)
    except OSError:
        return 1
    return 1                           # unreachable unless execve failed


def _cmd_gui() -> int:
    """Start the window. This is what a bare start does.

    Three steps, in order:
      1. this interpreter has GTK -> just draw the window
      2. there is a display but no GTK here (the AppImage case) -> hand over
         to a system interpreter that has it; `execve` does not come back
      3. no display at all (ssh, a TTY) -> say so once, then the terminal flow

    Only step 3 is a fallback. Steps 1 and 2 both open the window, which is
    why the bare command must land here and not in a `_gui_available()` check:
    inside the AppImage that check is always False.
    """
    if _gui_available():
        from .gui import app
        return app.main()

    if _has_display():
        _reexec_gui()                  # returns only when it did not work
        print(_("The graphical interface needs GTK 4 and libadwaita "
                "(package python3-gobject). Falling back to the terminal."))
    return _cmd_terminal()


def _cmd_terminal() -> int:
    from .core import db, paths
    from .gui import welcome
    if not db.load_config().get("setup_done"):
        welcome.run(interactive=sys.stdin.isatty())
        return 0
    print(_("{app} is set up.", app=paths.APP_TITLE))
    print("  --gui      " + _("open the window (this is the default)"))
    print("  --scan     " + _("list your games"))
    print("  --connect  " + _("connect a game so we can learn from it"))
    print("  --lookup   " + _("look up online where a game stores its data"))
    print("  --status   " + _("show learned storage locations"))
    print("  --redirect " + _("move a game's storage into your home folder"))
    print("  --open     " + _("show a game's data folder in the file "
                              "manager"))
    print("  --options  " + _("extra options for one game"))
    print("  " + _("Game data is kept in {path}.",
                   path=str(db.redirect_root())))
    return 0


def _cmd_default() -> int:
    """No arguments: the window. Always.

    `_cmd_gui` decides how to get there and drops to the terminal flow only
    when there is no display at all.
    """
    return _cmd_gui()


# --- entry point ---------------------------------------------------------
COPYRIGHT = "Copyright (C) 2026 tokajer"
LICENSE_NOTICE = (
    "License GPL-3.0-or-later: GNU GPL version 3 or later "
    "<https://gnu.org/licenses/gpl.html>\n"
    "This is free software: you are free to change and redistribute it.\n"
    "There is NO WARRANTY, to the extent permitted by law.")


def version_text() -> str:
    """`--version`: the short notice the GPL asks an interactive program to
    show (section 5d). The window says the same in its About dialog.

    English on purpose -- a licence notice is not UI text, and translating a
    legal statement is how you end up saying something you did not mean.
    """
    from .core import paths
    return f"{paths.APP_TITLE} {__version__}\n{COPYRIGHT}\n{LICENSE_NOTICE}"


def _version_action() -> Any:
    """argparse's own `version` action re-wraps the text it is given, which
    folds the four-line notice into one paragraph. So we print it ourselves.
    """
    import argparse

    class VersionAction(argparse.Action):
        def __call__(self, parser: Any, namespace: Any, values: Any,
                     option_string: str | None = None) -> None:
            print(version_text())
            parser.exit()

    return VersionAction


def _build_parser() -> Any:
    import argparse

    from .adapters.base import SOURCES
    from .core import paths

    p = argparse.ArgumentParser(
        prog=paths.APP_NAME,
        description=_("Find out where your games store their data -- and "
                      "optionally move that data into your home folder."))
    p.add_argument("--lang", metavar="CODE",
                   help=_("language for this run (en, de, auto)"))
    p.add_argument("--set-language", metavar="CODE",
                   help=_("remember a language (en, de, auto)"))
    # --set-save-folder is the name this shipped under. It stays, silently:
    # a flag in somebody's script must not stop working because we found a
    # better word for it.
    p.add_argument("--set-data-folder", "--set-save-folder", metavar="PATH",
                   dest="set_data_folder",
                   help=_("remember where moved game data should be kept"))
    p.add_argument("--add-game-folder", metavar="PATH",
                   help=_("also look for games in this folder"))
    p.add_argument("--forget-game-folder", metavar="PATH",
                   help=_("stop looking for games in that folder"))
    p.add_argument("--ignore-path", metavar="PATH",
                   help=_("never report this path as a storage location"))
    p.add_argument("--unignore-path", metavar="PATH",
                   help=_("report that path again"))
    p.add_argument("--source", choices=SOURCES,
                   help=_("limit to one launcher"))
    p.add_argument("--show-hidden", action="store_true",
                   help=_("list hidden games too"))
    p.add_argument("--yes", "-y", action="store_true",
                   help=_("accept what --lookup suggests without asking"))
    p.add_argument("--target", metavar="PATH",
                   help=_("where --redirect should move the files, or where "
                          "--new-game-folder should make the folder"))
    p.add_argument("--set-game-root", metavar="PATH", dest="set_game_root",
                   help=_("remember where new game folders are made"))
    p.add_argument("--name", metavar="NAME",
                   help=_("with --options: what to call it here"))
    p.add_argument("--engine", metavar="NAME",
                   help=_("with --new-game-folder: which Windows version to "
                          "set it up with"))
    p.add_argument("--alias", metavar="NAME",
                   help=_("with --new-game-folder: the short name its folder "
                          "gets (default: from the name)"))
    p.add_argument("--program", metavar="PATH",
                   help=_("with --run-in: the program to start in it"))
    p.add_argument("--keep-settings", action="store_true",
                   help=_("with --uninstall: keep what was learned"))

    g = p.add_mutually_exclusive_group()
    g.add_argument("--gui", action="store_true",
                   help=_("open the window (this is the default)"))
    g.add_argument("--terminal", action="store_true",
                   help=_("the overview in the terminal, no window"))
    g.add_argument("--scan", action="store_true",
                   help=_("list your games"))
    g.add_argument("--status", action="store_true",
                   help=_("show learned storage locations"))
    g.add_argument("--connect", metavar="GAME",
                   help=_("install the launch hook for a game"))
    g.add_argument("--disconnect", metavar="GAME",
                   help=_("remove the launch hook again"))
    g.add_argument("--lookup", metavar="GAME",
                   help=_("look up online where a game stores its data"))
    g.add_argument("--redirect", metavar="GAME",
                   help=_("move a game's storage into your home folder"))
    g.add_argument("--undo-redirect", metavar="GAME",
                   help=_("move it back into the game folder"))
    g.add_argument("--open", metavar="GAME",
                   help=_("show a game's data folder in the file manager"))
    g.add_argument("--hide", metavar="GAME",
                   help=_("leave a game out of the lists"))
    g.add_argument("--unhide", metavar="GAME",
                   help=_("put a hidden game back in the lists"))
    g.add_argument("--options", metavar="GAME",
                   help=_("show a game's extra options"))
    g.add_argument("--options-on", metavar="GAME", dest="options_on",
                   help=_("turn a game's extra options on"))
    g.add_argument("--options-off", metavar="GAME", dest="options_off",
                   help=_("turn them off again"))
    g.add_argument("--options-edit", metavar="GAME", dest="options_edit",
                   help=_("edit a game's own settings in your editor"))
    g.add_argument("--rebuild-options", action="store_true",
                   dest="rebuild_options",
                   help=_("refresh every game that uses extra options"))
    g.add_argument("--new-options", metavar="NAME", dest="new_options",
                   help=_("make an environment of your own, without a game"))
    g.add_argument("--list-options", action="store_true", dest="list_options",
                   help=_("list your own environments"))
    g.add_argument("--import-options", action="store_true",
                   dest="import_options",
                   help=_("take over profiles from the proton-instance "
                          "script"))
    g.add_argument("--new-game-folder", metavar="NAME",
                   dest="new_game_folder",
                   help=_("set up a new game folder of your own"))
    g.add_argument("--run-in", metavar="FOLDER", dest="run_in",
                   help=_("run a program in one of those folders (without "
                          "--program: its Windows settings)"))
    g.add_argument("--play", metavar="FOLDER",
                   help=_("start the game in one of those folders, and learn "
                          "where it stores its data"))
    g.add_argument("--set-engine", metavar="FOLDER", dest="set_engine",
                   help=_("which Windows version one of those folders uses "
                          "(with --engine; without it, what is available)"))
    g.add_argument("--watch-folder", metavar="FOLDER", dest="watch_folder",
                   help=_("also watch the folder the game is installed in "
                          "(with --target; empty --target forgets it)"))
    g.add_argument("--shortcut", metavar="FOLDER",
                   help=_("put a game folder in your application menu"))
    g.add_argument("--remove-shortcut", metavar="FOLDER",
                   dest="remove_shortcut",
                   help=_("take that menu entry away again"))
    g.add_argument("--own-version", metavar="FOLDER", dest="own_version",
                   help=_("give a folder its own copy of the Windows version "
                          "-- your own launcher can use it too"))
    g.add_argument("--shared-version", metavar="FOLDER",
                   dest="shared_version",
                   help=_("take that copy away again"))
    g.add_argument("--delete-game-folder", metavar="FOLDER",
                   dest="delete_game_folder",
                   help=_("delete a game folder you made here, and the game "
                          "in it"))
    g.add_argument("--move-old-data", action="store_true",
                   dest="move_old_data",
                   help=_("move data an earlier version left in the old "
                          "folder into the current one"))
    g.add_argument("--integrate", action="store_true",
                   help=_("(re)create shims, service and menu entry"))
    g.add_argument("--uninstall", action="store_true",
                   help=_("move all game data back, then remove the app"))
    g.add_argument("--check-update", action="store_true",
                   help=_("ask GitHub whether a newer version exists"))
    g.add_argument("--update", action="store_true",
                   help=_("download and install the newest version"))
    p.add_argument("--version", action=_version_action(), nargs=0,
                   help=_("show the version and the licence"))
    return p


def main() -> int:
    args = sys.argv[1:]

    # Fast paths: these run on every game launch, keep them cheap.
    if args and args[0] == "--wrapper":
        from .core import wrapper
        return wrapper.main(args[1:])

    if args and args[0] == "--hook":
        from .core import wrapper
        phase = args[1] if len(args) > 1 else "pre"
        source = args[args.index("--source") + 1] if "--source" in args else ""
        app_id = args[args.index("--id") + 1] if "--id" in args else ""
        return wrapper.hook(phase, source, app_id)

    if args and args[0] == "--daemon":
        from .daemon import watcher
        watcher.run()
        return 0

    # Velopack's startup hook: it finishes a pending update and fires the
    # first-run/restart callbacks. Deliberately *after* the three shim modes
    # -- a game launch must not pay for importing a compiled SDK.
    from .core import updater
    updater.app_hook()

    # Resolve the language before the parser is built, so --help is
    # translated too.
    from .core import db, i18n
    if "--lang" in args:
        with contextlib.suppress(IndexError):
            i18n.set_language(args[args.index("--lang") + 1])

    parsed = _build_parser().parse_args(args)

    other_commands = ("scan", "status", "connect", "disconnect", "lookup",
                      "redirect", "undo_redirect", "open", "hide", "unhide",
                      "options", "options_on", "options_off", "options_edit",
                      "rebuild_options", "new_options", "list_options",
                      "import_options", "new_game_folder", "run_in",
                      "delete_game_folder", "play", "set_engine",
                      "own_version", "shared_version", "watch_folder",
                      "shortcut", "remove_shortcut", "move_old_data",
                      "integrate", "uninstall")

    if parsed.set_language:
        db.set_config("language", parsed.set_language)
        i18n.set_language(None)
        if not any(getattr(parsed, name) for name in other_commands):
            print(_("Language set to '{lang}'.", lang=parsed.set_language))
            return 0

    if parsed.set_data_folder:
        import os
        root = os.path.abspath(os.path.expanduser(parsed.set_data_folder))
        db.set_config("redirect_root", root)
        if not any(getattr(parsed, name) for name in other_commands):
            print(_("Game data will be kept in {path}.", path=root))
            print(_("Already moved folders stay where they are."))
            return 0

    if parsed.set_game_root:
        _cmd_game_root(parsed.set_game_root)
        if not any(getattr(parsed, name) for name in other_commands):
            return 0

    if parsed.add_game_folder or parsed.forget_game_folder:
        if parsed.add_game_folder:
            _cmd_game_folder(parsed.add_game_folder, forget=False)
        if parsed.forget_game_folder:
            _cmd_game_folder(parsed.forget_game_folder, forget=True)
        if not any(getattr(parsed, name) for name in other_commands):
            return 0

    if parsed.ignore_path or parsed.unignore_path:
        if parsed.ignore_path:
            _cmd_ignore_path(parsed.ignore_path, forget=False)
        if parsed.unignore_path:
            _cmd_ignore_path(parsed.unignore_path, forget=True)
        if not any(getattr(parsed, name) for name in other_commands):
            return 0

    if parsed.gui:
        return _cmd_gui()
    if parsed.terminal:
        return _cmd_terminal()
    if parsed.scan:
        return _cmd_scan(parsed.source, parsed.show_hidden)
    if parsed.status:
        return _cmd_status()
    if parsed.connect:
        return _cmd_connect(parsed.connect, parsed.source, undo=False)
    if parsed.disconnect:
        return _cmd_connect(parsed.disconnect, parsed.source, undo=True)
    if parsed.lookup:
        return _cmd_lookup(parsed.lookup, parsed.source, parsed.yes)
    if parsed.redirect:
        return _cmd_redirect(parsed.redirect, parsed.target, undo=False)
    if parsed.undo_redirect:
        return _cmd_redirect(parsed.undo_redirect, None, undo=True)
    if parsed.open:
        return _cmd_open(parsed.open)
    if parsed.hide:
        return _cmd_hide(parsed.hide, parsed.source, undo=False)
    if parsed.unhide:
        return _cmd_hide(parsed.unhide, parsed.source, undo=True)
    if parsed.options:
        return _cmd_options(parsed.options, parsed.source, parsed.name)
    if parsed.options_on:
        return _cmd_options_switch(parsed.options_on, parsed.source, on=True)
    if parsed.options_off:
        return _cmd_options_switch(parsed.options_off, parsed.source, on=False)
    if parsed.options_edit:
        return _cmd_options_edit(parsed.options_edit, parsed.source)
    if parsed.rebuild_options:
        return _cmd_rebuild_options()
    if parsed.new_options:
        return _cmd_new_options(parsed.new_options)
    if parsed.list_options:
        return _cmd_list_options()
    if parsed.import_options:
        return _cmd_import_options(parsed.yes)
    if parsed.new_game_folder:
        return _cmd_new_prefix(parsed.new_game_folder, parsed.engine,
                               parsed.target, parsed.alias)
    if parsed.run_in:
        return _cmd_run_in(parsed.run_in, parsed.program)
    if parsed.play:
        return _cmd_play(parsed.play, parsed.program)
    if parsed.set_engine:
        return _cmd_set_engine(parsed.set_engine, parsed.engine)
    if parsed.watch_folder:
        return _cmd_watch_folder(parsed.watch_folder, parsed.target)
    if parsed.shortcut:
        return _cmd_shortcut(parsed.shortcut, undo=False)
    if parsed.remove_shortcut:
        return _cmd_shortcut(parsed.remove_shortcut, undo=True)
    if parsed.own_version:
        return _cmd_own_version(parsed.own_version, undo=False)
    if parsed.shared_version:
        return _cmd_own_version(parsed.shared_version, undo=True)
    if parsed.delete_game_folder:
        return _cmd_delete_prefix(parsed.delete_game_folder, parsed.yes)
    if parsed.move_old_data:
        return _cmd_move_old_data(parsed.yes)
    if parsed.integrate:
        return _cmd_integrate()
    if parsed.uninstall:
        return _cmd_uninstall(parsed.yes, parsed.keep_settings)
    if parsed.check_update:
        return _cmd_update(install=False)
    if parsed.update:
        return _cmd_update(install=True)
    return _cmd_default()


if __name__ == "__main__":
    raise SystemExit(main())
