# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Game folders the user makes themselves, and running things inside one.

Everything else in this project *finds* game folders: a launcher made one and
we read it. This is the one place that makes one, for the games no launcher
has -- an installer downloaded from the publisher, an old disc, a tool that
has to live next to the game.

Two layers can create such a folder, and they are told apart by nothing more
than which program gets started:

  * a compatibility build -- `<build>/proton run wineboot -u`, with
    `STEAM_COMPAT_DATA_PATH` pointing at the folder. The build creates `pfx`
    below it and brings everything else with it;
  * the system's own `wine wineboot -u`, with `WINEPREFIX` pointing at that
    same `pfx`.

Both therefore end at `<folder>/pfx`, which is the shape `adapters/generic`
already discovers -- so a folder made here needs no second code path
anywhere: it is listed, connected, looked up and redirected like any other
hand-installed game. The folder above `pfx` stays empty and is the obvious
place for the installer and the game itself.

Which layer built it is written into `<folder>/.linux-prefix-hub-env` and not
into our config, because the folder is the thing that gets moved, copied and
backed up, and an answer travelling with it is still true afterwards. The name
the user gave it, the short name its folder carries and the program to start
live in that same file.

**Starting the game is where this app learns.** Everywhere else a launcher
starts the game and our hook sits in its config; here there is no launcher, so
nothing observes the game unless we start it ourselves. `launch()` does, and
it goes through `wrapper.observed()` -- the same two snapshots and the same
diff a Steam game gets. Installing something (`install()`) deliberately does
*not*: what an installer writes is the game, not the place the game saves.

What gets started is not always a Windows program. A game can come with a
launcher of its own that is an ordinary Linux binary and runs the game through
a compatibility build it manages itself, pointed at this very folder -- so
what it starts still belongs here. `is_native()` tells the two apart and
`_native_command()` starts such a launcher as it is, naming this folder in
both spellings a launcher might read.

Nothing here runs while a game launches (CLAUDE.md #3), and every process it
starts gets `desktop.child_env()`: a compatibility build is a Python program
itself, and the AppImage's `PYTHONHOME` kills it (CLAUDE.md #4).

One name is not free: see `_numbered`. A build reads the app id it is running
out of the folder's own path, so a path without a digit in it kills a launch
that is not ours before the game starts.

VERIFY-ON-DEVICE:
  - creating a folder with a compatibility build and with the system's Wine,
    and installing a program into each afterwards.
  - a game whose own launcher starts it and then exits: `_wait_until_idle`
    is what keeps the diff from running while the game is still playing.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import db, paths
from .i18n import _

# Written into the folder above the prefix, never into the prefix itself:
# what is below `pfx` belongs to Windows and to whatever gets installed
# there, and a stray file of ours in it would be part of every backup and
# every copy of that game.
MARKER = ".linux-prefix-hub-env"

# The name the folder gets. Not configurable: `adapters/generic` names a game
# after the folder *above* a container it recognises, and `pfx` is one of the
# names it knows.
PREFIX_DIR = "pfx"

WINE = "wine"
ROOT_KEY = "prefix_root"

# How long after the game's own process ended we keep waiting for the prefix
# to go quiet, and how often we look. A game started by its own launcher
# outlives the launcher, and diffing while it is still playing learns the
# half of the save file that had been written by then.
POLL_SECONDS = 3.0
IDLE_LIMIT_SECONDS = 8 * 60 * 60

# How long we keep looking for the game to appear before deciding that it
# never did. A first start with a compatibility build takes a while, so this
# is generous -- it only ends the wait when *nothing at all* is running.
STARTUP_GRACE_SECONDS = 30

# Only the last few lines of a failure are worth showing: Wine writes pages
# of `fixme:` at every start and the reason is at the end.
ERROR_LINES = 6


class PrefixResult(dict):
    """Same shape as `redirect.RedirectResult`: ok, a sentence, and detail."""

    def __init__(self, ok: bool, message: str, **detail: Any) -> None:
        super().__init__(ok=ok, message=message, **detail)

    @property
    def ok(self) -> bool:
        return bool(self["ok"])

    @property
    def message(self) -> str:
        return str(self["message"])


def root() -> Path:
    """Where a new folder is made unless the user names somewhere else.

    `paths.DEFAULT_PREFIX_ROOT` -- `prefix` inside the app's own folder in
    `~/Games`, next to the moved game data and never the same folder as it.
    Below `~/Games` because that is where people already keep games, in a
    folder of our own so `~/Games` stays theirs. It is one of the places
    `adapters/generic` looks (`DEFAULT_ROOTS`), so a folder made here is
    found by the next scan whatever the config says. Removing the app never
    touches it.

    A game does not fit on the disk the home folder is on nearly as often as
    it does fit on another one, which is why this is a setting at all and why
    `create()` takes a target of its own on top of it.
    """
    value = db.load_config().get(ROOT_KEY)
    return Path(os.path.expanduser(str(value))) if value \
        else paths.DEFAULT_PREFIX_ROOT


def set_root(path: str | Path | None) -> Path:
    """Remember where new folders are made. `None` goes back to the default.

    Stored as the absolute path it resolves to now: this ends up next to game
    data on some other disk, and a `~` that means something else after the
    next login is not a place.
    """
    db.set_config(ROOT_KEY,
                  os.path.abspath(os.path.expanduser(str(path)))
                  if path else None)
    return root()


# --- what can run a Windows program on this machine ----------------------
def _proton_builds() -> list[Path]:
    """Every compatibility build we could start, newest of a family last.

    Both places they come from: the ones the user installed themselves
    (`compatibilitytools.d`, which is also where our own per-game copies
    live and those are excluded) and the ones Steam ships in its libraries.
    """
    from ..adapters import steam
    from . import gameopts

    found: list[Path] = []
    tools = gameopts.tools_dir()
    if tools is not None:
        found += [tools / name for name in gameopts.list_bases()]
    for library in steam.find_library_dirs():
        try:
            entries = sorted((library / "common").glob("Proton*"))
        except OSError:
            continue
        found += [e for e in entries if (e / "proton").is_file()]
    return found


def engines() -> list[dict[str, str]]:
    """Everything installed here that can run a Windows program."""
    found = [{"id": path.name, "kind": "proton", "path": str(path)}
             for path in _proton_builds()]
    system = shutil.which(WINE)
    if system:
        found.append({"id": WINE, "kind": WINE, "path": system})
    return found


def engine_label(engine: str) -> str:
    """What an engine is called where the user reads it.

    A compatibility build is called what its folder is called -- that is the
    name the user installed it under and the one the rest of the app already
    shows. Only the system's own has to be described.
    """
    return _("Wine (from your system)") if engine == WINE else engine


def default_engine() -> str:
    """What a new folder gets when nobody chooses.

    The same default the extra options follow, so the app has one answer to
    "which one?" and not two. Falls back to any build, then to the system.
    """
    from . import gameopts
    available = engines()
    known = {e["id"]: e for e in available}
    preferred = gameopts.resolve_base(gameopts.DEFAULT_FAMILY)
    if preferred in known:
        return preferred
    builds = [e["id"] for e in available if e["kind"] == "proton"]
    if builds:
        return builds[-1]
    return available[0]["id"] if available else ""


def find_engine(name: str) -> dict[str, str] | None:
    """The engine of that name, or the one that took its place.

    A build the user replaced with a newer one of the same family would
    otherwise leave a folder nobody can start anything in again, and there is
    no screen anywhere that lets them repair that. A newer build of the same
    family runs the same folder.
    """
    from . import gameopts
    known = {e["id"]: e for e in engines()}
    if name in known:
        return known[name]
    if not name or name == WINE:
        return None
    return known.get(gameopts.detect_base(gameopts.family(name)))


# --- which runtime a build asks for --------------------------------------
# A compatibility build says in its own manifest which Steam Linux Runtime it
# needs. Steam reads that and starts the build inside it; a launcher of the
# game's own may not, and a build that wants a newer runtime than the one it
# is put in dies with a Python traceback that reads like a broken install.
# So we can at least say which one a build asks for.
TOOL_MANIFEST = "toolmanifest.vdf"

RUNTIMES = {
    "1070560": "Steam Linux Runtime 1.0 (scout)",
    "1391110": "Steam Linux Runtime 2.0 (soldier)",
    "1628350": "Steam Linux Runtime 3.0 (sniper)",
    "4183110": "Steam Linux Runtime 4.0",
}

# What launchers that hardcode one runtime hardcode. Naming it is the whole
# point of the warning: "needs 4.0" alone does not tell anybody why their
# game stopped starting.
COMMON_RUNTIME = "1628350"


def required_runtime(engine: str) -> tuple[str, str]:
    """(app id, name) of the runtime this build asks for. ("", "") if none.

    Read line by line rather than through `core/vdf.py`: this is the build
    author's file and we only want one value out of it.
    """
    found = find_engine(str(engine))
    if found is None or found["kind"] != "proton":
        return ("", "")
    try:
        text = (Path(found["path"]) / TOOL_MANIFEST).read_text(
            encoding="utf-8", errors="ignore")
    except OSError:
        return ("", "")
    for line in text.splitlines():
        if "require_tool_appid" not in line:
            continue
        parts = [p for p in line.split('"') if p.strip()]
        if len(parts) >= 2:
            app_id = parts[-1].strip()
            return (app_id, RUNTIMES.get(app_id, app_id))
    return ("", "")


def runtime_warning(engine: str) -> str:
    """What to say when a build needs a runtime a launcher may not give it.

    Only for the builds where it can bite: one that asks for the runtime
    everything uses anyway needs no sentence.
    """
    app_id, name = required_runtime(engine)
    if not app_id or app_id == COMMON_RUNTIME:
        return ""
    return _("{engine} asks for {runtime}. A launcher of the game's own that "
             "always uses {common} cannot start it -- the failure looks like "
             "a broken Proton in that launcher's log.",
             engine=engine, runtime=name,
             common=RUNTIMES[COMMON_RUNTIME])


# --- the marker ----------------------------------------------------------
def read_marker(directory: str | Path) -> dict[str, str]:
    """What we wrote about this folder. Empty means it is not ours."""
    try:
        data = json.loads((Path(directory) / MARKER).read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} \
        if isinstance(data, dict) else {}


def owned(prefix_path: str | Path | None) -> Path | None:
    """The folder a game belongs to, if this is one we made.

    Takes the prefix, because that is what a discovered game carries -- the
    folder we made is the one above it.
    """
    if not prefix_path:
        return None
    path = Path(prefix_path)
    parent = path.parent
    return parent if path.name == PREFIX_DIR \
        and (parent / MARKER).is_file() else None


def engine_of(directory: str | Path) -> str:
    return read_marker(directory).get("engine", "")


def display_name(prefix_path: str | Path) -> str:
    """What to call this game in a list, or "" for a folder we did not make.

    `adapters/generic` names a game after its folder, which is the best it
    can do for a folder somebody else made. For ours the user typed a name,
    and the folder is only the short version of it.
    """
    folder = owned(prefix_path)
    return read_marker(folder).get("name", "") if folder is not None else ""


def find(needle: str) -> Path | None:
    """One of our folders, by its path, its short name or its name.

    Three spellings because all three are what a person has in front of
    them: the path they can see in the window, the folder name they typed
    once, and the name they call the game. A name that fits two of them is
    no answer, so it is not given as one.
    """
    from ..adapters import generic
    text = str(needle).strip()
    if not text:
        return None
    direct = Path(os.path.abspath(os.path.expanduser(text)))
    if read_marker(direct):
        return direct

    named: list[Path] = []
    for game in generic.iter_games():
        folder = owned(game.get("prefix_path"))
        if folder is None:
            continue
        if folder.name == text:
            return folder
        if text.lower() in str(game.get("game_name", "")).lower():
            named.append(folder)
    return named[0] if len(named) == 1 else None


def rename(directory: str | Path, name: str) -> PrefixResult:
    """Change what we call it. The folder keeps its name, deliberately.

    The path is in the DB, in a launch command the user may have written and
    in every folder anything moved so far points at; a display name is in a
    list. Only the second one is free to change.
    """
    folder = Path(directory)
    marker = read_marker(folder)
    if not marker:
        return PrefixResult(False, _("{path} was not made by this app.",
                                     path=str(folder)))
    wanted = str(name).strip()
    if not wanted:
        return PrefixResult(False, _("That name cannot be used."))
    _write_marker(folder, {**marker, "name": wanted})
    return PrefixResult(True, _("Now called {name}.", name=wanted),
                        name=wanted)


def foreign(game: dict[str, Any]) -> bool:
    """A game folder somebody else made, that we can still give a build to.

    Everything below works on a prefix and a name, and neither has to come
    from us: a Lutris prefix, a hand-rolled `~/.wine-osu`, a folder another
    launcher keeps -- all of them can be given a compatibility build of their
    own and be pointed at it. Steam is excluded because it already has that,
    properly: `adapters/steam.set_compat_tool` points Steam at the copy
    itself, and a second way of doing the same thing would be a second
    answer to one question.
    """
    return (str(game.get("source")) not in ("steam", "custom")
            and bool(game.get("prefix_path")))


def engine_for(game: dict[str, Any]) -> str:
    """Which build this game uses, wherever that answer is kept.

    A folder we made carries it in its own marker -- the folder is what gets
    moved and copied, so the answer travels with it. A folder somebody else
    made is not ours to write into, so for those it lives in the profile
    next to everything else the user chose about that game.
    """
    from . import gameopts
    folder = owned(game.get("prefix_path"))
    if folder is not None:
        return engine_of(folder)
    profile = gameopts.read(str(game.get("source")), str(game.get("app_id")))
    stored = str(profile.get("base") or "")
    return stored if stored and stored != gameopts.DEFAULT_FAMILY else ""


def set_engine_for(game: dict[str, Any], engine: str) -> PrefixResult:
    """The same choice, for a folder that may not be ours."""
    from . import gameopts
    folder = owned(game.get("prefix_path"))
    if folder is not None:
        return set_engine(folder, engine)
    if engine not in [e["id"] for e in engines()]:
        return PrefixResult(False, _("{name} is not installed here.",
                                     name=engine))
    source, app_id = str(game.get("source")), str(game.get("app_id"))
    profile = gameopts.read(source, app_id)
    profile["base"] = engine
    gameopts.write(source, app_id, profile)
    warning = runtime_warning(engine)
    message = _("Now using {engine}.", engine=engine_label(engine))
    if gameopts.find_instance(source, app_id) is not None:
        again = make_private_for(game)
        if not again.ok:
            return again
    return PrefixResult(True, message + (" " + warning if warning else ""),
                        engine=engine, warning=warning)


def make_private_for(game: dict[str, Any]) -> PrefixResult:
    """Give any game folder a compatibility build of its own.

    The folder does not have to be one we made. What the copy is for is the
    same either way: it carries this game's options into a launch we do not
    control, and somebody has to point that launcher at it -- so the path is
    in the result.
    """
    from . import gameopts
    folder = owned(game.get("prefix_path"))
    if folder is not None:
        return make_private(folder)

    source, app_id = str(game.get("source")), str(game.get("app_id"))
    profile = gameopts.read(source, app_id)
    base = str(profile.get("base") or "")
    chosen = find_engine(base) if base else find_engine(default_engine())
    if chosen is None or chosen["kind"] != "proton":
        return PrefixResult(False,
                            _("No compatibility build is installed that we "
                              "could use."))
    profile["base"] = str(chosen["id"])
    result = gameopts.build(game, profile)
    if not result.ok:
        return PrefixResult(False, result.message)
    profile["built"] = str(result["base"])
    profile["built_version"] = str(result["version"])
    gameopts.write(source, app_id, profile)
    copy = gameopts.find_instance(source, app_id)
    message = _("{name} now has a version of its own. Point your own "
                "launcher at {path} to use it there too.",
                name=game.get("game_name"), path=str(copy))
    warning = runtime_warning(str(chosen["id"]))
    return PrefixResult(True, message + (" " + warning if warning else ""),
                        path=str(copy), name=str(result["name"]),
                        warning=warning)


def drop_private_for(game: dict[str, Any]) -> PrefixResult:
    """Take that copy away again, wherever the folder came from."""
    from . import gameopts
    folder = owned(game.get("prefix_path"))
    if folder is not None:
        return drop_private(folder)
    source, app_id = str(game.get("source")), str(game.get("app_id"))
    result = gameopts.remove(source, app_id)
    if not result.ok:
        return PrefixResult(False, result.message)
    profile = gameopts.read(source, app_id)
    profile["built"] = ""
    profile["built_version"] = ""
    gameopts.write(source, app_id, profile)
    return PrefixResult(True, _("It uses the installed version again."))


def private_build_for(game: dict[str, Any]) -> Path | None:
    from . import gameopts
    return gameopts.find_instance(str(game.get("source")),
                                  str(game.get("app_id")))


def as_game(directory: str | Path) -> dict[str, Any]:
    """This folder in the shape the rest of the app passes games around in.

    `core/gameopts.py` works on that shape and on `db.game_key`, so a folder
    made here slots into it without a second store -- `generic:<prefix>` sits
    next to `steam:1091500`.
    """
    folder = Path(directory)
    marker = read_marker(folder)
    prefix = folder / PREFIX_DIR
    return {"source": "generic", "app_id": str(prefix),
            "game_name": marker.get("name") or folder.name,
            "alias": marker.get("alias") or folder.name,
            "prefix_path": str(prefix)}


def private_build(directory: str | Path) -> Path | None:
    """The compatibility build that belongs to this folder alone, if any."""
    from . import gameopts
    game = as_game(directory)
    return gameopts.find_instance(str(game["source"]), str(game["app_id"]))


def make_private(directory: str | Path) -> PrefixResult:
    """Give this folder a compatibility build of its own.

    Two things it buys, and the second one is why it exists at all:

      * the version stops moving. A copy is made of hardlinks, so it costs
        no disk space, and it keeps working after the build it came from is
        replaced or deleted;
      * **it carries the extra options into a launch that is not ours.** A
        build reads its own `user_settings.py` from inside the container.
        When we start the game we can simply set the environment, but a
        launcher of the game's own does not ask us -- pointing *it* at this
        copy is what gets an overlay or a log file into that game.
    """
    from . import gameopts
    folder = Path(directory)
    if not read_marker(folder):
        return PrefixResult(False, _("{path} was not made by this app.",
                                     path=str(folder)))
    engine = find_engine(engine_of(folder))
    if engine is None or engine["kind"] != "proton":
        return PrefixResult(False,
                            _("Only a compatibility build can be copied. "
                              "This folder uses {name}.",
                              name=engine_label(engine_of(folder))))

    game = as_game(folder)
    source, app_id = str(game["source"]), str(game["app_id"])
    profile = gameopts.read(source, app_id)
    profile["base"] = str(engine["id"])
    result = gameopts.build(game, profile)
    if not result.ok:
        return PrefixResult(False, result.message)
    profile["built"] = str(result["base"])
    profile["built_version"] = str(result["version"])
    gameopts.write(source, app_id, profile)
    copy = private_build(folder)
    message = _("{name} now has a version of its own. Point your own "
                "launcher at {path} to use it there too.",
                name=str(game["game_name"]), path=str(copy))
    warning = runtime_warning(str(engine["id"]))
    return PrefixResult(True, message + (" " + warning if warning else ""),
                        path=str(copy), name=str(result["name"]),
                        warning=warning)


def drop_private(directory: str | Path) -> PrefixResult:
    """Take that copy away again. The folder and the game stay."""
    from . import gameopts
    game = as_game(directory)
    source, app_id = str(game["source"]), str(game["app_id"])
    result = gameopts.remove(source, app_id)
    if not result.ok:
        return PrefixResult(False, result.message)
    profile = gameopts.read(source, app_id)
    profile["built"] = ""
    profile["built_version"] = ""
    gameopts.write(source, app_id, profile)
    return PrefixResult(True, _("It uses the installed version again."))


def set_engine(directory: str | Path, engine: str) -> PrefixResult:
    """Which Windows version this folder uses from now on.

    Nothing is rebuilt and nothing in the folder is touched: the build is not
    part of the folder, it is what gets pointed at it. A compatibility build
    brings an existing folder up to its own level the next time it starts it,
    which is the same thing that happens when a launcher updates one.
    """
    folder = Path(directory)
    marker = read_marker(folder)
    if not marker:
        return PrefixResult(False, _("{path} was not made by this app.",
                                     path=str(folder)))
    # By its exact name, not through `find_engine`: that one answers "what
    # runs this folder now" for a build that has since been replaced, and
    # accepting a name nobody has installed by quietly storing a different
    # one is not an answer to somebody choosing.
    wanted = str(engine)
    if wanted not in [e["id"] for e in engines()]:
        return PrefixResult(False, _("{name} is not installed here.",
                                     name=wanted))
    _write_marker(folder, {**marker, "engine": wanted})
    warning = runtime_warning(wanted)
    if private_build(folder) is not None:
        # The copy is the thing that actually starts the game, so a version
        # stored next to a copy of the old one is not a version change.
        again = make_private(folder)
        if not again.ok:
            return again
    message = _("Now using {engine}.", engine=engine_label(wanted))
    return PrefixResult(True, message + (" " + warning if warning else ""),
                        engine=wanted, warning=warning)


def watch_dir(directory: str | Path) -> Path | None:
    """A folder outside this one that belongs to the same game, if named.

    The game does not always live where we put its Windows part. A launcher
    of the game's own keeps the install wherever it likes -- another disk,
    usually -- and games write saves next to themselves often enough that
    not looking there is how "it never notices anything" happens. Nobody can
    guess that directory, so the user names it.
    """
    stored = read_marker(directory).get("watch", "")
    return Path(stored) if stored else None


def set_watch_dir(directory: str | Path,
                  path: str | Path | None) -> PrefixResult:
    """Name that folder, or take it back with `None`."""
    folder = Path(directory)
    marker = read_marker(folder)
    if not marker:
        return PrefixResult(False, _("{path} was not made by this app.",
                                     path=str(folder)))
    if not path:
        _write_marker(folder, {**marker, "watch": ""})
        return PrefixResult(True, _("No second folder is watched any more."))

    wanted = Path(os.path.abspath(os.path.expanduser(str(path))))
    if not wanted.is_dir():
        return PrefixResult(False, _("{path} is not a folder.",
                                     path=str(wanted)))
    prefix = folder / PREFIX_DIR
    if wanted == folder or wanted in prefix.parents or prefix == wanted:
        # It holds the prefix, so every change inside would be reported a
        # second time as a change in the game's own folder.
        return PrefixResult(False,
                            _("That is this game folder itself. Name the "
                              "folder the game is installed in instead."))
    _write_marker(folder, {**marker, "watch": str(wanted)})
    return PrefixResult(True, _("{path} is watched as well now.",
                                path=str(wanted)), path=str(wanted))


def program_of(directory: str | Path) -> Path | None:
    """The game this folder starts, if one was chosen.

    Stored relative to the folder whenever it lies inside it, so a folder
    that is copied to another disk still starts the same game.
    """
    stored = read_marker(directory).get("program", "")
    if not stored:
        return None
    path = Path(stored)
    return path if path.is_absolute() else Path(directory) / path


def set_program(directory: str | Path, program: str | Path) -> None:
    folder = Path(directory)
    path = Path(os.path.abspath(os.path.expanduser(str(program))))
    try:
        stored = str(path.relative_to(folder))
    except ValueError:
        stored = str(path)          # somewhere else entirely: keep it whole
    marker = read_marker(folder)
    if marker:
        _write_marker(folder, {**marker, "program": stored})


# --- making one ----------------------------------------------------------
def _findable(directory: Path) -> None:
    """Make sure the next scan looks where this was made.

    The default root is one `adapters/generic` already knows; anywhere else
    the user chose has to be remembered, or the folder they just made is
    invisible to the app that made it.
    """
    from ..adapters import generic
    known = {os.path.realpath(p) for p in generic.roots()}
    if os.path.realpath(directory.parent) not in known:
        db.add_game_folder(directory.parent)


def create(name: str, engine: str = "",
           target: str | Path | None = None,
           alias: str = "") -> PrefixResult:
    """Make a new game folder and set the Windows part of it up.

    Two names, and they are not the same thing -- the same split
    `core/gameopts.py` makes for an environment of the user's own:

      the **alias** is what the folder is called on disk. It is typed once
      and does not move afterwards, because a path is what everything else
      ends up pointing at;
      the **name** is what we call the game in our own lists, and it can say
      "Dark Age of Camelot" while the folder stays "daoc".

    Leaving the alias out makes it the name, which is what most people want.

    `target` is the folder the new one is made *in*, for this one folder
    only -- the remembered default (`root`) stays whatever it was. Somewhere
    else is not an exception here: a game that does not fit on the home disk
    is the ordinary reason to make one of these by hand.
    """
    from ..adapters import base, generic
    from . import gameopts

    label = str(name).strip()
    folder = gameopts.slug(alias or label)
    if not folder:
        return PrefixResult(False, _("That name cannot be used."))

    chosen = find_engine(engine) if engine else find_engine(default_engine())
    if chosen is None:
        if engine:
            return PrefixResult(
                False, _("{name} is not installed here.", name=engine))
        return PrefixResult(
            False, _("Nothing on this system can run Windows games yet. "
                     "Install a compatibility build in Steam, or Wine."))

    where = Path(os.path.expanduser(str(target))) if target else root()
    folder = _numbered(folder)
    directory = where / folder
    prefix = directory / PREFIX_DIR
    if prefix.exists():
        return PrefixResult(False, _("{path} is already there. Pick another "
                                     "name.", path=str(directory)))
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return PrefixResult(False, _("Could not create {path}: {error}",
                                     path=str(directory), error=str(exc)))

    outcome = run(directory, ["wineboot", "-u"], engine=str(chosen["id"]))
    if not base.is_prefix(prefix):
        return PrefixResult(False, _("Setting {name} up did not work: "
                                     "{error}", name=label,
                                     error=outcome.message))

    _write_marker(directory, {"engine": str(chosen["id"]),
                              "name": label or folder, "alias": folder})
    _findable(directory)
    db.upsert_prefix(generic.game_for(prefix))
    return PrefixResult(True,
                        _("{name} is ready. Install your game into it, then "
                          "start it from here.", name=label or folder),
                        path=str(directory), prefix=str(prefix),
                        alias=folder, engine=str(chosen["id"]))


def _numbered(folder: str) -> str:
    """Make sure a number appears somewhere in this folder's path.

    Not cosmetic. A compatibility build's `protonfixes` works out which game
    it is running by reading the app id back out of `STEAM_COMPAT_DATA_PATH`
    -- and when no `SteamAppId` is in the environment it does that literally:

        re.findall(<digits>, os.environ['STEAM_COMPAT_DATA_PATH'])[-1]

    A path without a single digit in it makes that `IndexError`, and the
    launch dies before the game starts. We set `SteamAppId` when we start the
    game ourselves, so this never shows up there; a launcher of the game's
    own does not, and the failure lands in *its* log as a Python traceback
    that reads like a broken Proton.

    So the folder name itself carries one. The name and not the whole path,
    even though a digit anywhere in the path would do: paths move. A folder
    that works in `/mnt/daten2/games` and stops working after being copied to
    `/mnt/spiele` is a worse thing to own than a folder called `Thief-1`, and
    the name in our own lists is unaffected either way.
    """
    from . import gameopts
    if any(char.isdigit() for char in folder):
        return folder
    return f"{folder}-{gameopts.APPID_FALLBACK}"


def _write_marker(directory: Path, data: dict[str, str]) -> None:
    # A folder without the marker is one more hand-made game folder, which
    # the app can already deal with -- so a marker we cannot write costs the
    # two buttons in the window and nothing else.
    with contextlib.suppress(OSError):
        (directory / MARKER).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")


# --- running something in one -------------------------------------------
# What a Windows program is called. Everything else that exists as a file is
# a program of the user's own -- and those are started as they are.
WINDOWS_SUFFIXES = (".exe", ".msi", ".bat", ".cmd", ".com")


def is_native(argv: list[str]) -> bool:
    """Is this a Linux program rather than something for the container?

    A game does not always come with a Windows executable to point at. The
    one that made this necessary is an AppImage launcher that runs the game
    through a compatibility build of its own -- pointed at this very folder,
    which is why what it starts still belongs here. Sending it through
    `proton run` would hand a Linux binary to Windows.

    `winecfg` and `wineboot` are names, not paths, so they never look native.
    """
    if len(argv) != 1:
        return False
    path = Path(argv[0])
    return path.suffix.lower() not in WINDOWS_SUFFIXES and path.is_file()


def _native_command(directory: Path,
                    argv: list[str]) -> tuple[list[str], dict[str, str]]:
    """Start it as it is, and say which folder it is meant to use.

    Both names for the same place, because which one a launcher reads is its
    own business: `WINEPREFIX` is the folder itself, `STEAM_COMPAT_DATA_PATH`
    is the one above it, the way a compatibility build wants it. A launcher
    with its own configuration ignores both, which costs nothing.
    """
    from . import desktop
    env = desktop.child_env()
    env["WINEPREFIX"] = str(directory / PREFIX_DIR)
    env["STEAM_COMPAT_DATA_PATH"] = str(directory)
    return list(argv), env


def _command(engine: dict[str, str], directory: Path,
             argv: list[str]) -> tuple[list[str], dict[str, str]]:
    """The command line and the environment for one of the two layers."""
    from . import desktop, gameopts

    env = desktop.child_env()
    if engine["kind"] == WINE:
        env["WINEPREFIX"] = str(directory / PREFIX_DIR)
        return [engine["path"], *argv], env

    env["STEAM_COMPAT_DATA_PATH"] = str(directory)
    env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(_steam_root())
    # The build reads its own app id back out of that path, finds a folder
    # name where it expects a number, and refuses to start anything at all.
    # Same fallback and same reasoning as `gameopts.env_for`.
    env.setdefault(gameopts.APPID_VAR, gameopts.APPID_FALLBACK)
    return [str(Path(engine["path"]) / "proton"), "run", *argv], env


def _steam_root() -> Path:
    from ..adapters import steam
    roots = steam.find_steam_roots()
    return roots[0] if roots else Path.home() / ".steam" / "steam"


def _tail(text: str) -> str:
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    return "\n".join(lines[-ERROR_LINES:])


def run(directory: str | Path, argv: list[str], engine: str = "",
        cwd: str | Path | None = None, capture: bool = True,
        extra_env: dict[str, str] | None = None) -> PrefixResult:
    """Start a program in this folder and wait for it to finish.

    Waits on purpose: everything this is used for -- setting the folder up,
    an installer, the Windows settings, the game -- is something the user is
    doing right now and wants the result of. The window runs it off its main
    loop (`gui/tasks.py`).

    `capture` is off for a game: a play session writes megabytes of
    `fixme:` lines, and holding all of it in memory to quote six of them
    back is not worth it. What is left is the exit code, which is what the
    message says then.

    `extra_env` is what the user asked this game to run with -- see
    `launch()`. It goes on top, because that is the whole point of it.
    """
    folder = Path(directory)
    if is_native(list(argv)):
        command, env = _native_command(folder, list(argv))
    else:
        chosen = find_engine(engine or engine_of(folder))
        private = private_build(folder) if not engine else None
        if private is not None:
            # Its own copy, once it has one: the same build, plus the
            # settings file that carries this game's options into it.
            chosen = {"id": private.name, "kind": "proton",
                      "path": str(private)}
        if chosen is None:
            return PrefixResult(False, _("The Windows version this folder "
                                         "was set up with is not installed "
                                         "any more."))
        command, env = _command(chosen, folder, list(argv))
    env.update(extra_env or {})
    try:
        done = subprocess.run(command, env=env, cwd=str(cwd or folder),
                              capture_output=capture, text=capture,
                              errors="replace" if capture else None,
                              check=False)
    except OSError as exc:
        return PrefixResult(False, _("Could not start it: {error}",
                                     error=str(exc)))
    if done.returncode != 0:
        return PrefixResult(False, _("That did not work: {error}",
                                     error=_tail(done.stderr) if capture
                                     else _("it ended with {code}",
                                            code=done.returncode)),
                            code=done.returncode)
    return PrefixResult(True, _("Done."), code=0)


# --- a way to start it that is not this app ------------------------------
def shortcut_file(directory: str | Path) -> Path:
    """Where the menu entry for this folder lives."""
    from . import integrate
    marker = read_marker(directory)
    alias = marker.get("alias") or Path(directory).name
    return integrate.APPLICATIONS_DIR / f"{paths.APP_NAME}-{alias}.desktop"


def make_shortcut(directory: str | Path) -> PrefixResult:
    """A menu entry that starts this game -- through the same observation.

    Two things it fixes at once. Starting from our window keeps that window
    busy for the whole session and loses the diff if it is closed; and a game
    people actually play gets started from their desktop, not from a settings
    app. The entry runs `--play`, so what it starts is still watched.
    """
    folder = Path(directory)
    marker = read_marker(folder)
    if not marker:
        return PrefixResult(False, _("{path} was not made by this app.",
                                     path=str(folder)))
    if program_of(folder) is None:
        return PrefixResult(False, _("No game chosen yet."))

    entry = shortcut_file(folder)
    entry.parent.mkdir(parents=True, exist_ok=True)
    name = marker.get("name") or folder.name
    entry.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={name}\n"
        f"Comment={_('Started by {app}', app=paths.APP_TITLE)}\n"
        f"Exec={_exec_line()} --play \"{folder}\"\n"
        f"Icon={paths.APP_ID}\n"
        "Categories=Game;\n"
        "Terminal=false\n", encoding="utf-8")
    return PrefixResult(True, _("{name} is in your application menu now.",
                                name=name), path=str(entry))


def drop_shortcut(directory: str | Path) -> PrefixResult:
    entry = shortcut_file(directory)
    if not entry.exists():
        return PrefixResult(True, _("There is no menu entry for it."))
    try:
        entry.unlink()
    except OSError as exc:
        return PrefixResult(False, _("Could not remove {path}: {error}",
                                     path=str(entry), error=str(exc)))
    return PrefixResult(True, _("The menu entry is gone."))


def _exec_line() -> str:
    """What the entry has to run. The installed app, not this process.

    A checkout has no AppImage, so it falls back to the interpreter that is
    running now -- which is exactly what `integrate.install_desktop_entry`
    does for the app's own entry.
    """
    import sys

    from . import integrate
    appimage = integrate._target_appimage()
    if appimage.exists():
        return f'"{appimage}"'
    return f'"{sys.executable}" -m {paths.PACKAGE}'


# --- taking one away again ----------------------------------------------
def moved_out(prefix_path: str | Path) -> list[str]:
    """Folders of this game's data that live outside it, and would survive.

    Whatever the user moved into their home folder is not inside the folder
    we are about to delete -- only a link to it is. That data is theirs and
    it stays, so `delete()` can say where it went instead of quietly leaving
    it behind.
    """
    entry = db.get_prefix(db.fingerprint(prefix_path))
    if not entry:
        return []
    found = [str(loc.get("redirect_target")) for loc
             in entry.get("storage_locations", [])
             if loc.get("redirected") and loc.get("redirect_target")]
    return sorted(dict.fromkeys(found))


def delete(directory: str | Path) -> PrefixResult:
    """Delete a game folder this app made -- the game and all of it.

    Two gates, and the first one is the important one: nothing without our
    marker is touched, ever. The same reasoning as `gameopts.remove` (rule
    15) and more urgent here, because the folder this would be pointed at by
    mistake is a folder with somebody's games in it.

    The data the user moved into their home folder is deliberately *not*
    fetched back first. Removing the app puts it back because the game
    stays; here the game is what goes, and moving saves into a folder that
    is about to be deleted is how they are lost. `shutil.rmtree` removes the
    links to it and never follows them.
    """
    from ..adapters import generic
    from . import registry
    folder = Path(directory)
    marker = read_marker(folder)
    if not marker:
        return PrefixResult(False,
                            _("{path} was not made by this app, so it is "
                              "not ours to delete.", path=str(folder)))
    prefix = folder / PREFIX_DIR
    if registry.prefix_in_use(prefix):
        return PrefixResult(False, _("It is running. Close it first."))

    kept = moved_out(prefix)
    fingerprint = db.fingerprint(prefix)
    drop_shortcut(folder)               # its name comes from the marker
    try:
        shutil.rmtree(folder)
    except OSError as exc:
        return PrefixResult(False, _("Could not remove {path}: {error}",
                                     path=str(folder), error=str(exc)))

    # Everything keyed by this game, in every store that has an opinion
    # about it. What is left otherwise is a game folder in the lists that
    # nobody can open.
    from . import gameopts
    gameopts.remove(generic.SOURCE, str(prefix))
    gameopts.forget(generic.SOURCE, str(prefix))
    db.forget_prefix(fingerprint)
    db.unhide_game(generic.SOURCE, str(prefix))
    db.drop_pending_redirect(generic.SOURCE, str(prefix))
    db.forget_confirmed(generic.SOURCE, str(prefix))
    _forget_root(folder.parent)

    # The name as it was typed, which is the one the user recognises --
    # the folder is called what a directory may be called.
    message = _("{name} is gone.", name=marker.get("name") or folder.name)
    if kept:
        message += " " + _("Your moved game data stays in {path}.",
                           path=kept[0])
    return PrefixResult(True, message, kept=kept)


def _forget_root(parent: Path) -> None:
    """Stop looking in a folder we only look in because of that game.

    Only when it is empty and only when it is one we added ourselves
    (`_findable`): a folder the user named with `--add-game-folder` is
    theirs, and so is one that still holds another game.
    """
    if os.path.abspath(parent) not in db.extra_game_folders():
        return
    try:
        if any(parent.iterdir()):
            return
    except OSError:
        return
    db.forget_game_folder(parent)


def install(directory: str | Path, program: str | Path) -> PrefixResult:
    """Run an installer (or any program) inside a folder we made.

    From the program's own directory: an installer that came with data files
    next to it looks for them where it lies, not where the game folder is.
    """
    path = Path(os.path.expanduser(str(program)))
    if not path.is_file():
        return PrefixResult(False, _("{path} is not a file.",
                                     path=str(path)))
    result = run(directory, [str(path)], cwd=path.parent)
    if not result.ok:
        return result
    return PrefixResult(True, _("{name} has finished.", name=path.name),
                        code=0)


def launch(directory: str | Path,
           program: str | Path | None = None) -> PrefixResult:
    """Start the game in this folder -- and watch what it does.

    This is the only place a game in one of these folders is ever observed.
    Every other source has a launcher whose config carries our hook; here
    there is none, so a game somebody starts from their own desktop file
    stays invisible however long they play it. Started from here it goes
    through `wrapper.observed()` and gets exactly what a Steam game gets.

    The chosen program is remembered, so the next start is one button.
    """
    from ..adapters import generic
    from . import wrapper
    folder = Path(directory)
    if not read_marker(folder):
        return PrefixResult(False, _("{path} was not made by this app.",
                                     path=str(folder)))
    chosen = Path(os.path.expanduser(str(program))) if program \
        else program_of(folder)
    if chosen is None:
        return PrefixResult(False, _("No game chosen yet."))
    if not chosen.is_file():
        return PrefixResult(False, _("{path} is not a file.",
                                     path=str(chosen)))
    set_program(folder, chosen)

    prefix = folder / PREFIX_DIR
    context = generic.game_for(prefix, db.get_prefix(db.fingerprint(prefix)))
    name = display_name(prefix)
    if name:
        context["game_name"] = name
    # A game that lives next to the Windows part rather than inside it writes
    # its saves there too (the Portal 2 case, one folder over), and naming
    # that directory is what puts it into the diff at all. Two things are
    # never named: the folder we made, because it *holds* the prefix and
    # every change inside would be reported a second time as a change in the
    # game's own folder; and the directory of a launcher of the game's own,
    # which is somebody else's program and not this game's data.
    beside = chosen.parent
    if not is_native([str(chosen)]) and prefix not in chosen.parents \
            and prefix.parent != beside:
        context["game_dir"] = str(beside)
    # A folder the user named wins over anything derived from the program:
    # they know where the game is, and with a launcher of the game's own it
    # is the only way we can be told at all.
    named = watch_dir(folder)
    if named is not None:
        context["game_dir"] = str(named)

    # What the user asked this game to run with. No container in the way
    # here, so the profile is simply the environment of the process we start
    # -- `core/gameopts.py` builds a whole private compatibility build for
    # the same thing, because a Steam game is started by Steam.
    from . import gameopts
    profile = gameopts.read(str(context["source"]), str(context["app_id"]))
    extra = gameopts.env_for(profile) if profile.get("enabled") else {}

    outcome: dict[str, Any] = {}

    def start() -> int:
        outcome["result"] = run(folder, [str(chosen)], cwd=chosen.parent,
                                capture=False, extra_env=extra)
        outcome["ran"] = _wait_until_idle(prefix)
        return int(outcome["result"].get("code") or 0)

    wrapper.observed(context, start)
    result = outcome.get("result")
    if result is not None and not result.ok:
        return result

    if not outcome.get("ran", True):
        # It came back at once and nothing ever ran in this folder. Almost
        # always the same thing: a launcher that was already open took the
        # start and this one exited.
        return PrefixResult(False,
                            _("{name} ended right away and nothing ran in "
                              "this folder. If its launcher is already open, "
                              "close it first -- a second one hands the "
                              "start to the one that is already there.",
                              name=chosen.name), code=0, ran=False)

    entry = db.get_prefix(db.fingerprint(prefix)) or {}
    return PrefixResult(True,
                        _("{name} has finished. We know {n} storage "
                          "location(s) for it now.",
                          name=chosen.name,
                          n=len(entry.get("storage_locations", []))),
                        code=0, ran=True)


def _wait_until_idle(prefix: Path) -> bool:
    """Wait for the last thing using this folder to end. Did anything run?

    A game with its own launcher is why: the launcher starts the game and
    exits, so the process we waited for is gone long before the game is, and
    the snapshot after it would compare a save file that is still being
    written. The prefix itself answers that question -- anything still
    running against it carries it in its environment
    (`registry.prefix_in_use`).

    The answer matters as well as the wait. A launcher that finds an
    instance of itself already running hands the start over to that one and
    exits immediately, so from here it looks exactly like a game that came
    and went in a second -- and the diff then honestly reports that nothing
    changed. Knowing that nothing ever ran is what lets `launch()` say so.
    """
    import time

    from . import registry
    deadline = time.monotonic() + IDLE_LIMIT_SECONDS
    grace = time.monotonic() + STARTUP_GRACE_SECONDS
    seen = False
    while time.monotonic() < deadline:
        busy = registry.prefix_in_use(prefix)
        seen = seen or busy
        if not busy and (seen or time.monotonic() > grace):
            return seen
        time.sleep(POLL_SECONDS)
    return seen


def settings(directory: str | Path) -> PrefixResult:
    """The Windows settings of one folder -- Wine's own configuration."""
    result = run(directory, ["winecfg"])
    return result if not result.ok else PrefixResult(True, _("Saved."),
                                                     code=0)
