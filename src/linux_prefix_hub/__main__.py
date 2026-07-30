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
  --redirect GAME    move its storage locations into your home

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
    games = sorted(base.iter_games(sources),  # type: ignore[arg-type]
                   key=lambda g: str(g.get("game_name", "")).lower())
    if not games:
        print(_("No games found. Is Steam/Lutris/Heroic installed for this "
                "user?"))
        return 0
    print(_("{n} game(s) found:", n=len(games)) + "\n")
    for g in games:
        state = _("installed") if g.get("installed") else _("downloading")
        prefix = _("ready") if g.get("prefix_path") else _("never started")
        hook = _("connected") if g.get("managed") else _("not connected")
        print(f"  {str(g.get('game_name'))[:34]:<34} "
              f"[{g['source']}] [{state}] [{prefix}] [{hook}] "
              f"id={g['app_id']}")
    return 0


def _cmd_status() -> int:
    from .core import db
    prefixes = db.load_prefixes()
    if not prefixes:
        print(_("Nothing learned yet. Connect a game and play it once, then "
                "its storage locations show up here."))
        return 0
    for _fp, entry in prefixes.items():
        print(f"\n{entry.get('game_name')} "
              f"({entry.get('source')}/{entry.get('app_id')})")
        print("  " + _("game folder: {path}", path=entry.get("prefix_path")))
        for loc in entry.get("storage_locations", []):
            where = (_("moved to {target}", target=loc.get("redirect_target"))
                     if loc.get("redirected") else _("in place"))
            print(f"    [{loc.get('type', '?'):<7}] {loc.get('win_path')}  "
                  f"({where})")
    return 0


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


def _cmd_redirect(needle: str, target: str | None, undo: bool) -> int:
    from .core import db, redirect, registry
    found = db.resolve(needle)
    if not found:
        print(_("'{needle}' is not in the list yet. Connect the game and play "
                "it once so we know where it stores things.", needle=needle))
        return 1
    fingerprint, entry = found

    roots: list[str] = []
    for loc in entry.get("storage_locations", []):
        root = registry.shell_folder_root(loc.get("win_path", ""))
        if root and root not in roots:
            roots.append(root)
    if not roots:
        print(_("No movable storage location known for {game}.",
                game=entry.get("game_name")))
        return 1
    if target and len(roots) > 1:
        print(_("This game has several storage locations; --target only works "
                "with one. Locations: {roots}", roots=", ".join(roots)))
        return 1

    failed = False
    for root in roots:
        action = redirect.undo if undo else redirect.redirect
        result = (action(fingerprint, root) if undo
                  else action(fingerprint, root, target))
        print(f"{root}: {result.message}")
        failed = failed or not result.ok
    return 1 if failed else 0


def _cmd_update(install: bool) -> int:
    from .core import updater
    if install:
        result = updater.update()
        print(result["message"])
        return 0 if result["ok"] else 1
    state = updater.check(force=True)
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
    env[REEXEC_FLAG] = "1"
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
    on success."""
    import os
    if os.environ.get(REEXEC_FLAG):
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
    print("  --status   " + _("show learned storage locations"))
    print("  --redirect " + _("move a game's storage into your home folder"))
    return 0


def _cmd_default() -> int:
    """No arguments: the window. Always.

    `_cmd_gui` decides how to get there and drops to the terminal flow only
    when there is no display at all.
    """
    return _cmd_gui()


# --- entry point ---------------------------------------------------------
def _build_parser() -> Any:
    import argparse

    from .adapters.base import SOURCES
    from .core import paths

    p = argparse.ArgumentParser(
        prog=paths.APP_NAME,
        description=_("Find out where your games store their saves -- and "
                      "optionally move those saves into your home folder."))
    p.add_argument("--lang", metavar="CODE",
                   help=_("language for this run (en, de, auto)"))
    p.add_argument("--set-language", metavar="CODE",
                   help=_("remember a language (en, de, auto)"))
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
    g.add_argument("--redirect", metavar="GAME",
                   help=_("move a game's storage into your home folder"))
    g.add_argument("--undo-redirect", metavar="GAME",
                   help=_("move it back into the game folder"))
    g.add_argument("--integrate", action="store_true",
                   help=_("(re)create shims, service and menu entry"))
    g.add_argument("--check-update", action="store_true",
                   help=_("ask GitHub whether a newer version exists"))
    g.add_argument("--update", action="store_true",
                   help=_("download and install the newest version"))
    p.add_argument("--version", action="version",
                   version=f"{paths.APP_TITLE} {__version__}")
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

    if parsed.set_language:
        db.set_config("language", parsed.set_language)
        i18n.set_language(None)
        if not any(getattr(parsed, name) for name in
                   ("scan", "status", "connect", "disconnect", "redirect",
                    "undo_redirect", "integrate")):
            print(_("Language set to '{lang}'.", lang=parsed.set_language))
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
    if parsed.redirect:
        return _cmd_redirect(parsed.redirect, parsed.target, undo=False)
    if parsed.undo_redirect:
        return _cmd_redirect(parsed.undo_redirect, None, undo=True)
    if parsed.integrate:
        return _cmd_integrate()
    if parsed.check_update:
        return _cmd_update(install=False)
    if parsed.update:
        return _cmd_update(install=True)
    return _cmd_default()


if __name__ == "__main__":
    raise SystemExit(main())
