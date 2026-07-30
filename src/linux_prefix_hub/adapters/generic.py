"""Generic adapter: game folders no launcher knows about.

Hand-rolled setups -- `WINEPREFIX=~/.wine wine setup.exe`, a folder winetricks
made, an old PlayOnLinux install -- have no launcher config to read and none
to write into. What they do have is the only thing the rest of this project
actually works on: a folder with `drive_c` and the two registry hives. That
shape test (`base.is_prefix`) is the entire discovery rule.

Three things follow that no other adapter has to deal with:

  1. **The path is the id.** Nobody assigned this game a number or a slug, and
     the absolute path is unique, stable across scans, and exactly what
     `connect` needs in order to spell out its instructions.
  2. **Other people's folders are not ours.** Steam, Lutris and Heroic keep
     their prefixes in perfectly ordinary directories too (`~/Games/<slug>` is
     the Lutris default), so discovery asks those adapters first and skips
     everything they already claim. Without that, half the library is listed
     twice -- once with a real name and a working hook, once as "generic".
  3. **`connect` cannot connect anything.** There is no config file that
     starts the game; a script or a `.desktop` file of the user's own does. So
     we hand them the one line to put in front of their command and remember
     that they asked for it. For every other source the launcher config *is*
     the record of "connected" -- here our own DB has to be.

Discovery is a shape test, so it stays shallow and bounded on purpose: known
folders, `SCAN_DEPTH` levels down, never descending into something that
already is a game folder. Anything living elsewhere is one
`--add-game-folder` away, and `context_from_env` accepts *any* folder anyway
-- a user who put our wrapper in front of their own command has told us about
that game more clearly than a folder list ever could.

VERIFY-ON-DEVICE:
  - `DEFAULT_ROOTS` is a "where do people keep these" list and cannot be
    complete. Check it against your own setup and extend it.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..core import db, paths
from .base import HookResult, is_prefix, user_dir_for

SOURCE = "generic"

# Where hand-made game folders usually live. Each entry is scanned at most
# SCAN_DEPTH levels deep, because `~/Games/<Game>/pfx` is just as common as
# `~/Games/<Game>`.
DEFAULT_ROOTS = (
    "~/.wine",
    "~/Games",
    "~/Wine",
    "~/wine",
    "~/.local/share/wineprefixes",
    "~/.local/share/wine/prefixes",
    "~/.PlayOnLinux/wineprefix",
)

# The other half of the convention: a second folder next to the default one,
# `~/.wine-osu` and friends.
HOME_GLOB = ".wine*"

SCAN_DEPTH = 2

# Never descend into these. `drive_c`/`dosdevices` are the inside of a game
# folder (only reachable when the folder is half-built and fails the shape
# test), the rest belongs to a launcher that has an adapter of its own.
SKIP_NAMES = frozenset({
    "drive_c", "dosdevices", "compatdata", "steamapps", "shadercache",
    ".git", "node_modules",
})

# A folder below one of these belongs to a launcher even when its adapter did
# not recognise it this second (a manifest being rewritten mid-launch, say).
FOREIGN_PARTS = frozenset({"compatdata", "steamapps"})

# Folder names that describe the container instead of the game.
CONTAINER_NAMES = frozenset({"pfx", "prefix", "wineprefix", "wine", "bottle",
                             "default"})


def _expand(p: str | Path) -> Path:
    return Path(os.path.expanduser(str(p)))


def roots() -> list[Path]:
    """Every folder we look in: the defaults plus the user's own."""
    found: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        if not path.is_dir():
            return
        real = os.path.realpath(path)
        if real not in seen:
            seen.add(real)
            found.append(path)

    for candidate in DEFAULT_ROOTS:
        add(_expand(candidate))
    try:
        for path in sorted(Path.home().glob(HOME_GLOB)):
            add(path)
    except OSError:
        pass
    for extra in db.extra_game_folders():
        add(_expand(extra))
    return found


def _scan(root: Path, depth: int) -> Iterator[Path]:
    """Game folders at or below `root`, at most `depth` levels down."""
    if is_prefix(root):
        yield root
        return                      # the inside of one is not a place to look
    if depth <= 0:
        return
    try:
        children = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return                      # unreadable folder skips, never aborts
    for child in children:
        if child.name in SKIP_NAMES:
            continue
        yield from _scan(child, depth - 1)


def _claimed_prefixes() -> set[str]:
    """Real paths of game folders another adapter already manages.

    Costs one extra discovery pass over the launchers, which is the honest
    price for not listing half the library twice: a Lutris prefix in
    `~/Games/<slug>` has exactly the shape of a hand-made one.
    """
    from . import base
    others = tuple(s for s in base.SOURCES if s != SOURCE)
    claimed: set[str] = set()
    for game in base.iter_games(others):
        prefix = game.get("prefix_path")
        if prefix:
            claimed.add(os.path.realpath(prefix))
    return claimed


def _foreign(path: Path) -> bool:
    return any(part in FOREIGN_PARTS for part in path.parts)


def _pretty(name: str) -> str:
    """A folder name as a game name -- their spelling, minus our vocabulary."""
    text = name.lstrip(".")
    for marker in ("wineprefix-", "wineprefix_", "wine-", "wine_"):
        if text.lower().startswith(marker) and len(text) > len(marker):
            text = text[len(marker):]
            break
    text = text.replace("_", " ").replace("-", " ").strip()
    if not text:
        return name
    # Their capitalisation is a decision ("ELDEN RING"); only fix the folders
    # that are lowercase because file names usually are.
    return text.title() if text.islower() else text


def game_name_for(prefix_path: str | Path) -> str:
    """Name a game folder after itself -- the name the user chose.

    `~/Games/Skyrim/pfx` is "Skyrim", `~/.wine-osu` is "Osu". The classic
    `~/.wine` carries no name at all, and "wine" is a word this app does not
    say to users (CLAUDE.md #6), so it gets a plain description instead.
    """
    from ..core.i18n import _

    path = _expand(prefix_path)
    name = path.name
    if name.lstrip(".").lower() in CONTAINER_NAMES:
        parent = path.parent
        if parent.name and parent != Path.home():
            name = parent.name          # ~/Games/Skyrim/pfx -> "Skyrim"
        else:
            return _("Windows games")   # ~/.wine -- nothing to name it after
    return _pretty(name)


def game_for(prefix_path: str | Path,
             known: dict[str, Any] | None = None) -> dict[str, Any]:
    """The Game dict for one folder. `known` is our DB entry for it, if any."""
    path = str(prefix_path)
    return {
        "source": SOURCE,
        "app_id": path,          # the path is the id -- see the module docs
        "game_name": game_name_for(path),
        "installed": True,       # the folder is there, so the game is
        "game_dir": None,        # hand-made setups install into drive_c
        "config_path": None,
        "prefix_path": path,
        "user_dir": user_dir_for(path),
        "managed": bool((known or {}).get("managed")),
    }


def iter_games() -> Iterator[dict[str, Any]]:
    claimed = _claimed_prefixes()
    known = db.load_prefixes()
    seen: set[str] = set()
    for root in roots():
        for prefix in _scan(root, SCAN_DEPTH):
            real = os.path.realpath(prefix)
            if real in seen or real in claimed:
                continue
            seen.add(real)
            yield game_for(prefix, known.get(db.fingerprint(prefix)))


def context_from_env() -> dict[str, Any] | None:
    """Whatever WINEPREFIX points at, unless somebody else claims it.

    We run last (`base.SOURCES` order), so this only ever sees launches the
    real adapters did not recognise. Deliberately *not* restricted to the scan
    roots: this is the path a user takes after following `connect`, and they
    may well have their game folder somewhere we would never look.
    """
    prefix = os.environ.get("WINEPREFIX")
    if not prefix:
        return None
    path = _expand(prefix)
    if not is_prefix(path) or _foreign(path):
        return None
    if os.path.realpath(path) in _claimed_prefixes():
        return None
    return game_for(path, db.get_prefix(db.fingerprint(path)))


# --- Hook injection ------------------------------------------------------
def launch_command(app_id: str) -> str:
    """What the user puts in front of their own launch command."""
    return f'WINEPREFIX="{app_id}" "{paths.WRAPPER_SHIM}"'


def connect(app_id: str) -> HookResult:
    """No config to write into -- so hand the command to the user.

    We do register the game here, because `managed` has to live somewhere and
    for a hand-made game folder our own DB is the only config that exists.
    """
    from ..core.i18n import _

    if not is_prefix(app_id):
        return HookResult(False, _("{path} is not a game folder we can read.",
                                   path=app_id))
    command = launch_command(app_id)
    db.upsert_prefix({**game_for(app_id), "managed": True})
    return HookResult(
        True,
        _("There is no launcher we can set this up in. Start the game with "
          "this in front of your usual command:\n  {command}",
          command=command),
        manual=True, command=command)


def disconnect(app_id: str) -> HookResult:
    from ..core.i18n import _

    fingerprint = db.fingerprint(app_id)
    if db.get_prefix(fingerprint):
        db.set_managed(fingerprint, False)
    return HookResult(True, _("Disconnected. You can drop the extra part in "
                              "front of your launch command again."))
