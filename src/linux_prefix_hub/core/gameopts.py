# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Extra options for one game: environment that reaches the game itself.

The problem this solves is narrow and the solution is odd, so it is worth
saying both plainly.

**The problem.** Steam does not start the game directly. It starts a container
(the Steam Linux Runtime), and the container decides which environment
variables get through to what runs inside it. Launch options and our own
launch hook both sit *outside* that container, so a variable set there is not
reliably the variable the game sees. That makes "let this one game run with a
performance overlay" impossible from where the rest of this app stands.

**The solution.** The compatibility build Steam uses reads a `user_settings.py`
next to itself, from inside the container, and puts what it finds into the
game's environment. That file belongs to the *build*, though, not to a game --
writing into it would change every game that uses that build. So we give the
game a build of its own: a private copy of an installed one, with its own
`user_settings.py`. The copy is made of hardlinks, so it costs no disk space,
and Steam is pointed at it (`adapters/steam.set_compat_tool`).

Two halves live here. The first is the profile -- what the user chose -- and
knows nothing about any of the above; when Lutris and Heroic get this, they
will set the same variables their own way and read the same profile. The
second is the private build, which is Steam's mechanism and only Steam's.

Nothing in this module runs while a game launches (CLAUDE.md #3). A build is
made when the user turns the switch on and at no other time.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from . import db, paths, registry
from .i18n import _


class OptionsResult(dict):
    """Same shape as `redirect.RedirectResult`: ok, a sentence, and detail."""

    def __init__(self, ok: bool, message: str, **detail: Any) -> None:
        super().__init__(ok=ok, message=message, **detail)
        # `manual` and `detail` are what the window looks for when a result
        # needs a step only the user can take, so every result carries them.
        self.setdefault("manual", False)
        self.setdefault("detail", {})

    @property
    def ok(self) -> bool:
        return bool(self["ok"])

    @property
    def message(self) -> str:
        return str(self["message"])


# =========================================================================
# The profile -- what the user chose. No launcher in here.
# =========================================================================

# The named switches, in the order they are drawn. Each is a small set of
# variables the user should not have to know the names of; anyone who does
# know them has the free-text field instead.
SWITCHES: dict[str, dict[str, str]] = {
    # Turning it on and nothing else, deliberately. The obvious next step is
    # to set MANGOHUD_CONFIG with a sensible layout -- but that variable
    # *replaces* the user's own ~/.config/MangoHud/MangoHud.conf rather than
    # adding to it, so a helpful default here silently throws away whatever
    # they set up in Goverlay. Switching the overlay on is ours to do; what
    # it shows is theirs.
    "overlay": {"MANGOHUD": "1"},
    # Lives in the graphics layer itself, so it still shows up when the
    # overlay above is not installed for both 32- and 64-bit.
    "fps": {"DXVK_HUD": "fps,frametime"},
    "log": {"PROTON_LOG": "1"},
}

# Default family for a game that has never picked a build. A family prefix,
# not a fixed version, so `--rebuild-options` keeps following it.
DEFAULT_FAMILY = "GE-Proton"

CONFIG_KEY = "game_options"

# An environment the user made themselves belongs to no launcher and no game,
# so it gets a source of its own and slots into everything keyed by
# `db.game_key` without a second store: `custom:daoc` sits next to
# `steam:1091500` and is read, written, listed and removed by the same code.
SOURCE_CUSTOM = "custom"

# Where the standalone script this grew out of keeps its profiles. Read only,
# and only when the user asks for an import -- see `importable`.
LEGACY_PROFILE_DIR = "proton-instances"


def switch_label(switch: str) -> str:
    """What a switch is called, in the user's words."""
    return {
        "overlay": _("Show the MangoHud overlay"),
        "fps": _("Show the frame rate"),
        "log": _("Write a log file while playing"),
    }.get(switch, switch)


def switch_hint(switch: str) -> str:
    return {
        "overlay": _("Turns it on and leaves what it shows to your own "
                     "MangoHud settings."),
        "fps": _("Just the frame rate. Works where the overlay above does "
                 "not."),
        "log": _("For when something goes wrong and you want to say what."),
    }.get(switch, "")


def profiles() -> dict[str, Any]:
    """Every stored profile, keyed by `db.game_key`."""
    value = db.load_config().get(CONFIG_KEY)
    return dict(value) if isinstance(value, dict) else {}


def read(source: str, app_id: str) -> dict[str, Any]:
    """One game's profile. An empty one is a full answer, not a miss."""
    stored = profiles().get(db.game_key(source, app_id))
    profile = dict(stored) if isinstance(stored, dict) else {}
    profile.setdefault("enabled", False)
    profile.setdefault("base", DEFAULT_FAMILY)
    profile.setdefault("switches", [])
    profile.setdefault("custom", "")
    profile.setdefault("built", "")
    profile.setdefault("built_version", "")
    profile.setdefault("title", "")
    return profile


def write(source: str, app_id: str, profile: dict[str, Any]) -> None:
    stored = profiles()
    stored[db.game_key(source, app_id)] = {
        "enabled": bool(profile.get("enabled")),
        "base": str(profile.get("base") or DEFAULT_FAMILY),
        "switches": [str(s) for s in profile.get("switches", [])
                     if s in SWITCHES],
        "custom": str(profile.get("custom") or ""),
        "built": str(profile.get("built") or ""),
        # The version file of the build we copied, as it was at the time.
        # `outdated` needs it: a "latest" folder keeps its name while what is
        # behind it is replaced, and then the name says nothing.
        "built_version": str(profile.get("built_version") or ""),
        # Only ever set for a custom environment: the name as it was typed.
        # The slug is the id and cannot be turned back into it ("Old Game"
        # and "Old-Game" are one directory but two different words).
        "title": str(profile.get("title") or ""),
    }
    db.set_config(CONFIG_KEY, stored)


def forget(source: str, app_id: str) -> bool:
    """Drop a profile entirely. False if there was none."""
    stored = profiles()
    if stored.pop(db.game_key(source, app_id), None) is None:
        return False
    db.set_config(CONFIG_KEY, stored)
    return True


def enabled_games() -> list[tuple[str, str]]:
    """(source, app_id) of every game that has a build right now."""
    found: list[tuple[str, str]] = []
    for key, profile in profiles().items():
        source, _sep, app_id = str(key).partition(":")
        if app_id and isinstance(profile, dict) and profile.get("enabled"):
            found.append((source, app_id))
    return found


def slug(name: str) -> str:
    """A typed name as something safe to call a directory.

    Kept readable rather than escaped: this ends up in Steam's own list, and
    a name nobody recognises there is worse than one that lost a comma.
    """
    kept = [c if (c.isalnum() or c in "-_") else "-"
            for c in str(name).strip()]
    cleaned = "".join(kept).strip("-")
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned


# An environment has two names, and they are not the same thing.
#
#   the **alias** is what it is called on disk and in Steam's own list --
#   short, typed once, and never changed again, because a folder name that
#   moves takes a launcher's configuration with it (`protonSteamPath` in
#   somebody else's config points at this exact directory);
#   the **title** is what *we* call it in our own window, and it can say
#   "Dark Age of Camelot" while the alias stays "daoc".
#
# The alias is the id, so it is what `db.game_key` is built from.
def as_game(alias: str, title: str = "") -> dict[str, Any]:
    """A custom environment in the shape everything else here already takes.

    Deliberately not a second code path: `build`, `turn_on`, `turn_off` and
    the window's row all work on a dict with a source, an id and a name, and
    a standalone environment is exactly that with no launcher behind it.
    """
    key = slug(alias)
    return {"source": SOURCE_CUSTOM, "app_id": key,
            "game_name": str(title or alias), "alias": key,
            "prefix_path": None}


def custom_environments() -> list[dict[str, Any]]:
    """Every environment the user made, by the name they read."""
    found: list[dict[str, Any]] = []
    for key, profile in profiles().items():
        source, _sep, alias = str(key).partition(":")
        if source == SOURCE_CUSTOM and alias:
            found.append(as_game(alias, str((profile or {}).get("title") or
                                            alias)))
    return sorted(found, key=lambda g: str(g["game_name"]).lower())


def rename(source: str, app_id: str, title: str) -> OptionsResult:
    """Change what we call it. The alias and the folder do not move.

    On purpose: the folder name is in somebody else's configuration by now
    -- the launcher this environment was made for points straight at it --
    and renaming it out from under them is how a working setup stops working.
    """
    wanted = str(title).strip()
    if not wanted:
        return OptionsResult(False, _("That name cannot be used."))
    profile = read(source, app_id)
    profile["title"] = wanted
    write(source, app_id, profile)
    return OptionsResult(True, _("Now called {name}.", name=wanted),
                         title=wanted)


def parse_custom(text: str) -> dict[str, str]:
    """The user's own lines: one `KEY=value` per line, `#` starts a comment.

    Deliberately forgiving about whitespace and unforgiving about everything
    else: a line we cannot read is skipped, never guessed at. Quotes are not
    stripped -- a value with quotes in it is a value with quotes in it, and
    the shell that would have removed them is not involved anywhere here.
    """
    env: dict[str, str] = {}
    for raw in str(text or "").splitlines():
        line = raw.rstrip("\r").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _sep, value = line.partition("=")
        key = key.strip()
        if key:
            env[key] = value.strip()
    return env


# A game whose id is not a number has a game folder whose path is not a
# number either, and the compatibility build reads its own app id back out of
# that path. It finds nothing, and refuses to start the game at all -- which
# looks exactly like "turning the extra options on broke my game". Naming any
# number puts it back, so we name one for the games that need it and stay out
# of the way of anyone who names their own.
APPID_VAR = "SteamAppId"
APPID_FALLBACK = "1"


def env_for(profile: dict[str, Any], app_id: str = "") -> dict[str, str]:
    """The whole environment this profile asks for.

    The user's own lines come last and therefore win: they typed a variable
    the switch above also sets, and the one they typed is the answer. That
    holds for the fallback above too -- it is only ever a default.
    """
    env: dict[str, str] = {}
    for switch in profile.get("switches", []):
        env.update(SWITCHES.get(str(switch), {}))
    env.update(parse_custom(str(profile.get("custom") or "")))
    if app_id and not str(app_id).isdigit():
        env.setdefault(APPID_VAR, APPID_FALLBACK)
    return env


# =========================================================================
# The private build -- Steam's mechanism, and only Steam's.
# =========================================================================

# Written into every directory we create. Two jobs, and the second one is the
# important one: it says which build the copy was made from, and it is the
# only thing that ever makes a directory ours to delete. Without that gate a
# wrong app id turns `remove()` into `rm -rf` on the user's real Proton
# install -- so nothing here touches a directory that does not have it.
MARKER = ".linux-prefix-hub-instance"

# What that file holds. JSON since the directory name stopped being the
# identity: the name is for the person reading the folder list, `key` is what
# says which game a copy belongs to, and `version` is the build's own version
# file as it was when we copied -- see `outdated`. The very first release
# wrote the bare base name into this file, so reading still accepts that.
MARKER_FIELDS = ("base", "key", "version", "name")

# Every compatibility build carries this; a directory without it is not one.
BUILD_MARKER = "proton"

# The folder every game folder is stamped out of. A build without it cannot
# start anything -- and the failure lands in Steam's log at the moment the
# game starts, a long way from the switch that caused it. It goes missing
# for real: a "latest" directory that some other tool keeps up to date is
# one interrupted update away from being a build with no contents, and
# copying that faithfully gets you a faithful copy of something broken.
DEFAULT_PFX = "files/share/default_pfx"

MANIFEST = "compatibilitytool.vdf"
SETTINGS = "user_settings.py"


def tools_dir() -> Path | None:
    """Where Steam looks for compatibility builds, ours included."""
    from ..adapters import steam
    roots = steam.find_steam_roots()
    return roots[0] / "compatibilitytools.d" if roots else None


def is_ours(path: Path) -> bool:
    return (path / MARKER).is_file()


def read_marker(path: Path) -> dict[str, str]:
    """What we wrote about this copy. Empty means it is not ours."""
    try:
        text = (path / MARKER).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        # The plain-text form the first release wrote: just the base name.
        return {"base": text.strip()}
    return {str(k): str(v) for k, v in data.items()} \
        if isinstance(data, dict) else {}


def built_from(path: Path) -> str:
    """Which build this copy was made from, or "" if it is not ours."""
    return read_marker(path).get("base", "")


def base_version(name: str) -> str:
    """A build's own version file -- the one thing that moves under a name.

    `Proton-GE Latest` is a name that never changes while what is behind it
    is replaced. Comparing names says such a copy is current forever; this
    is what actually differs. Every build carries the file, because the build
    reads it itself.
    """
    directory = tools_dir()
    if directory is None:
        return ""
    try:
        return (directory / name / "version").read_text(
            encoding="utf-8").strip()
    except OSError:
        return ""


def list_bases() -> list[str]:
    """Every installed build we could copy, ours excluded, newest last.

    Sorted per family and never across families. Version-sorting the whole
    list compares `Proton-Tkg` against `GE-Proton` on the leading letter, so
    "the newest one" would answer with whichever vendor sorts late. A family
    is the part before the first digit, which is how these builds are named.
    """
    directory = tools_dir()
    if directory is None or not directory.is_dir():
        return []
    found: list[str] = []
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir() or is_ours(entry):
            continue
        if (entry / BUILD_MARKER).exists():
            found.append(entry.name)
    return sorted(found, key=_version_key)


def family(name: str) -> str:
    """The part of a build name before its version -- `GE-Proton10-34` is
    the `GE-Proton` family. What "the newest one" is only ever true within.
    """
    for index, char in enumerate(name):
        if char.isdigit():
            return name[:index]
    return name


def _version_key(name: str) -> tuple[str, list[Any]]:
    """Family first, then the numbers in it, so 10-34 beats 9-27."""
    parts: list[Any] = []
    number = ""
    for char in name[len(family(name)):]:
        if char.isdigit():
            number += char
            continue
        if number:
            parts.append(int(number))
            number = ""
        parts.append(char)
    if number:
        parts.append(int(number))
    return (family(name), parts)


def detect_base(pattern: str = DEFAULT_FAMILY) -> str:
    """The newest installed build whose name starts with `pattern`."""
    matching = [name for name in list_bases() if name.startswith(pattern)]
    return matching[-1] if matching else ""


def resolve_base(want: str) -> str:
    """A stored choice is either an exact build or a family to follow."""
    directory = tools_dir()
    if directory is not None and (directory / want / BUILD_MARKER).exists():
        return want
    return detect_base(want or DEFAULT_FAMILY)


PREFIX = "LinuxPrefixHub"


def wanted_name(game: dict[str, Any]) -> str:
    """What a copy for this game should be called.

    The game's name, not its id: this is a folder people scroll past in
    Steam's own list and in `compatibilitytools.d`, and a row of numbers
    there tells nobody which game is which. The id only comes back when two
    games really do share a name, which is the one case the name cannot
    settle on its own.
    """
    # The alias where there is one (a custom environment keeps its folder
    # name when its title changes), the game's name otherwise.
    label = str(game.get("alias") or game.get("game_name") or "")
    base = f"{PREFIX}-{slug(label)}".rstrip("-")
    # Through `slug` both times: a game folder this app made has its own
    # *path* as its id (`adapters/generic`), and a path is not something a
    # directory can be called.
    if base == PREFIX:
        base = f"{PREFIX}-{slug(str(game.get('app_id')))}"
    directory = tools_dir()
    if directory is None:
        return base
    key = db.game_key(str(game.get("source")), str(game.get("app_id")))
    taken = directory / base
    if taken.exists() and read_marker(taken).get("key") not in ("", key):
        return f"{base}-{slug(str(game.get('app_id')))}"
    return base


def find_instance(source: str, app_id: str) -> Path | None:
    """The copy that belongs to this game, whatever it ended up called.

    The marker is the identity, not the folder name -- that is the whole
    point of the name being free to be readable. Falls back to the name the
    first release used, so a copy built before this still answers.
    """
    directory = tools_dir()
    if directory is None:
        return None
    key = db.game_key(source, app_id)
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return None
    for entry in entries:
        if entry.is_dir() and read_marker(entry).get("key") == key:
            return entry
    legacy = directory / f"{PREFIX}-{app_id}"
    return legacy if is_ours(legacy) else None


def display_name(name: str) -> str:
    """What the copy calls itself, everywhere it says a name.

    One string for the internal name and the shown name, deliberately: they
    are two keys in the same manifest, and a launcher that reads both ends up
    listing one copy twice. The shell script this grew out of used one name
    for both and did not have that problem.
    """
    return name


def outdated(profile: dict[str, Any]) -> str:
    """A newer build this profile could follow, or "" if it is current.

    Two ways a copy falls behind, and only the first one shows in a name:

      * the profile follows a family and a later release turned up, so
        `GE-Proton10-34` became `GE-Proton11-5`;
      * the profile follows a *name that never changes* -- a "latest" folder
        some other tool keeps up to date -- and what is behind that name was
        swapped out underneath it. Nothing about the name moved, so this was
        invisible until the build's own version file was compared as well.

    The copy keeps working either way: the hardlinks hold the files it was
    made from alive even after the folder they came from is overwritten. It
    is frozen, not broken -- which is exactly why somebody has to be told.
    """
    if not profile.get("enabled") or not profile.get("built"):
        return ""
    newest = resolve_base(str(profile.get("base") or DEFAULT_FAMILY))
    if not newest:
        return ""
    if newest != str(profile["built"]):
        return newest
    stored = str(profile.get("built_version") or "")
    current = base_version(newest)
    return newest if stored and current and current != stored else ""


def render_settings(env: dict[str, str]) -> str:
    """The generated `user_settings.py` for one environment.

    Backslashes are escaped before quotes, and that order is not a style
    choice: a Windows path like `C:\\Users\\...` written out unescaped is
    `\\U` to Python, which is the start of a character escape, and the build
    fails to import its own settings file.
    """
    lines = ["# Written by Linux Prefix Hub. Changes here are overwritten.",
             "user_settings = {"]
    for key, value in env.items():
        safe = str(value).replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'    "{key}": "{safe}",')
    lines.append("}")
    return "\n".join(lines) + "\n"


def _replace(path: Path, text: str) -> None:
    """Write a file inside a copy, without writing into the original.

    The copy is made of hardlinks, so before this runs the file here and the
    file in the installed build are one file with two names. Opening it for
    writing truncates both. Unlinking first breaks the link and leaves the
    original alone -- and it is the whole reason this is a function rather
    than a `write_text` call at each site.
    """
    path.unlink(missing_ok=True)
    path.write_text(text, encoding="utf-8")


def write_user_settings(directory: Path, env: dict[str, str]) -> None:
    _replace(directory / SETTINGS, render_settings(env))


def rewrite_manifest(directory: Path, name: str, display: str) -> None:
    """Give the copy an identity of its own, editing the file line by line.

    Not through `core/vdf.py` (CLAUDE.md #2): this manifest is the build
    author's file and most of it is comments explaining the format, which our
    tokeniser drops. Reformatting somebody else's config is a bug even when
    the result parses.

    The internal name is matched by shape rather than by value: it is the one
    lone quoted token inside the `compat_tools` block, and in GE builds it
    carries a trailing `// Internal name of this tool` comment that is kept.
    """
    manifest = directory / MANIFEST
    try:
        original = manifest.read_text(encoding="utf-8")
    except OSError:
        return

    out: list[str] = []
    in_block = False
    renamed = False
    for line in original.splitlines():
        stripped = line.strip()
        if not in_block and '"compat_tools"' in stripped:
            in_block = True
            out.append(line)
            continue
        indent = line[:len(line) - len(line.lstrip())]
        if in_block and not renamed and _is_lone_token(stripped):
            _head, sep, tail = stripped.partition("//")
            out.append(f'{indent}"{name}"' + (f" //{tail}" if sep else ""))
            renamed = True
            continue
        if stripped.startswith('"display_name"'):
            out.append(f'{indent}"display_name" "{display}"')
            continue
        out.append(line)
    _replace(manifest, "\n".join(out) + "\n")


def _is_lone_token(stripped: str) -> bool:
    """`"GE-Proton10-34"` or `"GE-Proton10-34" // comment` and nothing else."""
    head = stripped.partition("//")[0].strip()
    return (len(head) > 2 and head.startswith('"') and head.endswith('"')
            and '"' not in head[1:-1])


def _clone(base: Path, target: Path) -> bool:
    """Copy a build. True if it cost disk space.

    Hardlinks first: a build is several hundred megabytes and a copy per game
    would add up fast. They only work within one filesystem, so a Steam
    library on another disk falls back to a real copy -- which works, costs
    the space, and is worth saying out loud rather than discovering later.
    """
    try:
        shutil.copytree(base, target, copy_function=os.link, symlinks=True)
        return False
    except OSError:
        shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(base, target, symlinks=True)
        return True


def build(game: dict[str, Any], profile: dict[str, Any]) -> OptionsResult:
    """Give this game its own build, carrying the profile's environment.

    Refuses while the game is running, because the first thing this does is
    delete the previous copy -- and the running game is executing out of it.
    """
    source, app_id = str(game.get("source")), str(game.get("app_id"))
    tools = tools_dir()
    if tools is None:
        return OptionsResult(False, _("No Steam installation found."))
    directory = tools / wanted_name(game)

    prefix = game.get("prefix_path")
    if prefix and registry.prefix_in_use(str(prefix)):
        return OptionsResult(False, _("{game} is running. Close it first.",
                                      game=game.get("game_name")))

    base = resolve_base(str(profile.get("base") or DEFAULT_FAMILY))
    if not base:
        return OptionsResult(False,
                             _("No compatibility build is installed that we "
                               "could use."))
    origin = tools / base
    if not (origin / DEFAULT_PFX).is_dir():
        return OptionsResult(False,
                             _("{base} is not complete -- no game could "
                               "start with it. Install that version again, "
                               "or choose another one.", base=base))

    # A copy from before this game was renamed, or from when copies were
    # named after the id. It is ours by its marker, so it goes.
    previous = find_instance(source, app_id)
    if previous is not None and previous != directory:
        shutil.rmtree(previous, ignore_errors=True)

    if directory.exists():
        if not is_ours(directory):
            # Something else is sitting on the name we use. Refusing is the
            # only safe answer: the alternative is deleting a build the user
            # installed themselves.
            return OptionsResult(False,
                                 _("{path} exists and is not ours. Remove it "
                                   "yourself and try again.",
                                   path=str(directory)))
        shutil.rmtree(directory)

    try:
        copied = _clone(origin, directory)
    except OSError as exc:
        shutil.rmtree(directory, ignore_errors=True)
        return OptionsResult(False, _("Could not prepare the options: "
                                      "{error}", error=str(exc)))

    name = directory.name
    _replace(directory / MARKER, json.dumps(
        {"base": base, "key": db.game_key(source, app_id),
         "version": base_version(base), "name": name}, ensure_ascii=False))
    rewrite_manifest(directory, name, display_name(name))
    write_user_settings(directory, env_for(profile, app_id))

    message = _("Extra options are ready for {game}.",
                game=game.get("game_name"))
    if copied:
        message += " " + _("This game is on another disk, so a full copy was "
                           "made instead of a shortcut.")
    return OptionsResult(True, message, base=base, name=name, copied=copied,
                         version=base_version(base))


def remove(source: str, app_id: str) -> OptionsResult:
    """Take the private build away again. Nothing else is touched."""
    directory = find_instance(source, app_id)
    if directory is None or not directory.exists():
        return OptionsResult(True, _("Nothing to remove."))
    if not is_ours(directory):
        return OptionsResult(False,
                             _("{path} is not ours -- left alone.",
                               path=str(directory)))
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        return OptionsResult(False, _("Could not remove {path}: {error}",
                                      path=str(directory), error=str(exc)))
    return OptionsResult(True, _("Extra options removed."))


# --- the two whole operations, as the CLI and the window use them --------
def own_folder(game: dict[str, Any]) -> bool:
    """Is this a game folder this app made itself?

    Then the whole mechanism below is unnecessary. The container is Steam's
    problem: it starts the game and decides what reaches it. A folder we made
    is started by *us* (`newprefix.launch`), so the profile can simply be put
    into the environment of that process and there is nothing to build,
    nothing to copy and nothing to point at anything.
    """
    from . import newprefix
    if str(game.get("source")) != "generic":
        return False
    # The id *is* the prefix path for a hand-installed game, and a caller
    # that only knows the profile (`uninstall.games_with_options`) has
    # nothing else to hand us.
    where = game.get("prefix_path") or game.get("app_id")
    return newprefix.owned(where) is not None


def turn_on(game: dict[str, Any],
            profile: dict[str, Any] | None = None) -> OptionsResult:
    """Build it, then point Steam at it. Both, or neither is much use.

    Steam being open is not a failure: the build is made and ready, and the
    only thing missing is a choice in a list the user can make themselves.
    That is the same `manual` answer `adapters/steam.connect` gives, and the
    window already knows how to show it.

    For a folder this app made there is no build and no pointing: storing the
    profile *is* turning it on, because the launch reads it.
    """
    from ..adapters import steam
    source, app_id = str(game.get("source")), str(game.get("app_id"))
    if own_folder(game):
        profile = dict(profile or read(source, app_id))
        profile["enabled"] = True
        write(source, app_id, profile)
        # A folder with a build of its own carries them into a launch that
        # is not ours as well (`newprefix.make_private`), and that file is
        # the only place such a launch can read them from.
        instance = find_instance(source, app_id)
        if instance is not None:
            write_user_settings(instance, env_for(profile, app_id))
            return OptionsResult(True,
                                 _("Extra options are on, in this game's own "
                                   "version too."))
        return OptionsResult(True,
                             _("Extra options are on. They apply the next "
                               "time you start {game} from here.",
                               game=game.get("game_name")))
    if source not in ("steam", SOURCE_CUSTOM):
        return OptionsResult(False,
                             _("Extra options only work for Steam games so "
                               "far."))

    profile = dict(profile or read(source, app_id))
    profile["enabled"] = True
    if source == SOURCE_CUSTOM and not profile.get("title"):
        profile["title"] = str(game.get("game_name") or app_id)
    result = build(game, profile)
    if not result.ok:
        return result

    profile["built"] = str(result["base"])
    profile["built_version"] = str(result["version"])
    write(source, app_id, profile)

    if source == SOURCE_CUSTOM:
        # Nothing to point at it, on purpose: an environment that belongs to
        # no game is one the user picks themselves, wherever they want it.
        return OptionsResult(True,
                             _("{name} is ready. Choose it under the game's "
                               "compatibility setting.",
                               name=str(result["name"])),
                             name=str(result["name"]),
                             base=str(result["base"]))

    hook = steam.set_compat_tool(app_id, str(result["name"]))
    if hook.ok:
        return OptionsResult(True, result.message, name=str(result["name"]),
                             base=str(result["base"]))
    return OptionsResult(False, hook.message, manual=hook.manual,
                         detail=dict(hook["detail"]))


def turn_off(game: dict[str, Any]) -> OptionsResult:
    """The reverse, in the reverse order.

    The mapping goes first. A build Steam still points at but that is no
    longer there is a game Steam quietly starts with a different one, and a
    different compatibility build against the same game folder is exactly the
    kind of surprise this app exists to prevent.
    """
    from ..adapters import steam
    source, app_id = str(game.get("source")), str(game.get("app_id"))
    if own_folder(game):
        profile = read(source, app_id)
        profile["enabled"] = False
        write(source, app_id, profile)
        # The copy stays: it is this folder's version, not its options.
        instance = find_instance(source, app_id)
        if instance is not None:
            write_user_settings(instance, {})
        return OptionsResult(True, _("{game} runs normally again.",
                                     game=game.get("game_name")))

    directory = find_instance(source, app_id)
    if source != SOURCE_CUSTOM:
        # Only what we actually put there: a name we guessed could be a
        # choice the user has since made themselves.
        expect = directory.name if directory is not None \
            else f"{PREFIX}-{app_id}"
        hook = steam.clear_compat_tool(app_id, expect=expect)
        if not hook.ok:
            return OptionsResult(False, hook.message, manual=hook.manual,
                                 detail=dict(hook["detail"]))

    result = remove(source, app_id)
    if not result.ok:
        return result

    profile = read(source, app_id)
    profile["enabled"] = False
    profile["built"] = ""
    profile["built_version"] = ""
    write(source, app_id, profile)
    return OptionsResult(True, _("{game} runs normally again.",
                                 game=game.get("game_name")))


# --- taking over what the standalone script set up -----------------------
# This module grew out of a shell script that kept its profiles in
# ~/.config/proton-instances/<name>.env, with the chosen base next to it.
# Reading those is a one-way import the user asks for: we copy the profile
# and leave the script's own instance directory exactly where it is. Two
# tools writing into one directory is how you lose a setup that works.
def importable() -> list[dict[str, str]]:
    """Profiles the script left behind that we do not have yet."""
    folder = paths.XDG_CONFIG_HOME / LEGACY_PROFILE_DIR
    if not folder.is_dir():
        return []
    have = {str(g["app_id"]) for g in custom_environments()}
    found: list[dict[str, str]] = []
    try:
        entries = sorted(folder.glob("*.env"))
    except OSError:
        return []
    for env_file in entries:
        name = env_file.stem
        if slug(name) in have:
            continue
        try:
            text = env_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        found.append({"name": name, "custom": text,
                      "base": _legacy_base(folder, name)})
    return found


def _legacy_base(folder: Path, name: str) -> str:
    """The build that profile followed, as a name we can resolve.

    The script stored whatever was typed, which can be an absolute path into
    somebody else's runner folder. Only the last part of it can mean anything
    here, and only if a build of that name is actually installed -- otherwise
    the import falls back to the default family rather than carrying a broken
    choice across.
    """
    try:
        stored = (folder / f"{name}.base").read_text(
            encoding="utf-8", errors="ignore").strip()
    except OSError:
        return DEFAULT_FAMILY
    candidate = stored.rstrip("/").rpartition("/")[2] or stored
    return candidate if candidate and resolve_base(candidate) \
        else DEFAULT_FAMILY


def import_legacy(entry: dict[str, str]) -> OptionsResult:
    """Keep one of those profiles as an environment of our own.

    Nothing is built and nothing of the script's is touched -- the profile
    lands in our config and the user turns it on when they want to.
    """
    name = str(entry.get("name") or "")
    if not slug(name):
        return OptionsResult(False, _("That name cannot be used."))
    write(SOURCE_CUSTOM, slug(name),
          {"enabled": False, "base": str(entry.get("base") or DEFAULT_FAMILY),
           "switches": [], "custom": str(entry.get("custom") or ""),
           "built": "", "title": name})
    return OptionsResult(True, _("{name} was taken over.", name=name),
                         name=name)


def rebuild_all() -> list[OptionsResult]:
    """Put every game that has options onto the newest build it follows.

    A copy keeps working after the build it came from is deleted -- the
    hardlinks hold the files alive -- so this is never urgent. It is how a
    game picks up a newer build, not how it keeps running.
    """
    from ..adapters import base as adapters
    enabled = enabled_games()
    results: list[OptionsResult] = []

    # An environment of the user's own needs no library walk -- it *is* its
    # own entry, so it is rebuilt straight from what is stored.
    for game in custom_environments():
        if (SOURCE_CUSTOM, str(game["app_id"])) not in enabled:
            continue
        results.append(_rebuild_one(game))

    # Folders this app made, but only the ones that have a copy: a folder
    # whose options are simply its environment has nothing to rebuild, and
    # the copy is the thing that ages (`newprefix.make_private`).
    from . import newprefix
    for game in adapters.get_adapter("generic").iter_games():
        folder = newprefix.owned(game.get("prefix_path"))
        if folder is None or newprefix.private_build(folder) is None:
            continue
        outcome = newprefix.make_private(folder)
        results.append(OptionsResult(outcome.ok, outcome.message))

    wanted = dict.fromkeys(app_id for source, app_id in enabled
                           if source == "steam")
    for game in adapters.get_adapter("steam").iter_games():
        app_id = str(game.get("app_id"))
        if app_id not in wanted:
            continue
        del wanted[app_id]
        results.append(_rebuild_one(game))
    return results


def _rebuild_one(game: dict[str, Any]) -> OptionsResult:
    source, app_id = str(game["source"]), str(game["app_id"])
    profile = read(source, app_id)
    result = build(game, profile)
    if result.ok:
        profile["built"] = str(result["base"])
        profile["built_version"] = str(result["version"])
        write(source, app_id, profile)
    return result
