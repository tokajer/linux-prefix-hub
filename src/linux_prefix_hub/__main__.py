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
def _cmd_scan(source: str | None) -> int:
    from .adapters import base
    sources = (source,) if source else None
    groups = base.group_by_source(
        base.iter_games(sources))  # type: ignore[arg-type]
    total = sum(len(games) for _s, games in groups)
    if not total:
        print(_("No games found. Is Steam/Lutris/Heroic installed for this "
                "user?"))
        return 0
    print(_("{n} game(s) found:", n=total))
    for src, games in groups:
        print(f"\n{base.source_label(src)}")
        for g in games:
            state = _("installed") if g.get("installed") else _("downloading")
            prefix = _("ready") if g.get("prefix_path") else _("never started")
            hook = _("connected") if g.get("managed") else _("not connected")
            print(f"  {str(g.get('game_name'))[:34]:<34} "
                  f"[{state}] [{prefix}] [{hook}] id={g['app_id']}")
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


def _cmd_lookup(needle: str, source: str | None) -> int:
    """Ask PCGamingWiki where a game saves, instead of playing it first."""
    from .core import pcgw
    game = _pick_game(needle, source)
    if not game:
        return 1

    result = pcgw.lookup_and_store(game)
    print(result["message"])
    for loc in result["locations"]:
        print(f"    [{loc.get('type', '?'):<7}] {loc.get('win_path')}"
              + (" " + _("(in the game's own folder)")
                 if loc.get("where") == "game_folder" else ""))
    if result["url"]:
        print("    " + str(result["url"]))
    if result["locations"] and not result.get("stored"):
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
    p.add_argument("--target", metavar="PATH",
                   help=_("where --redirect should move the files"))

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
    g.add_argument("--integrate", action="store_true",
                   help=_("(re)create shims, service and menu entry"))
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
                      "redirect", "undo_redirect", "open", "integrate")

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
        return _cmd_scan(parsed.source)
    if parsed.status:
        return _cmd_status()
    if parsed.connect:
        return _cmd_connect(parsed.connect, parsed.source, undo=False)
    if parsed.disconnect:
        return _cmd_connect(parsed.disconnect, parsed.source, undo=True)
    if parsed.lookup:
        return _cmd_lookup(parsed.lookup, parsed.source)
    if parsed.redirect:
        return _cmd_redirect(parsed.redirect, parsed.target, undo=False)
    if parsed.undo_redirect:
        return _cmd_redirect(parsed.undo_redirect, None, undo=True)
    if parsed.open:
        return _cmd_open(parsed.open)
    if parsed.integrate:
        return _cmd_integrate()
    if parsed.check_update:
        return _cmd_update(install=False)
    if parsed.update:
        return _cmd_update(install=True)
    return _cmd_default()


if __name__ == "__main__":
    raise SystemExit(main())
