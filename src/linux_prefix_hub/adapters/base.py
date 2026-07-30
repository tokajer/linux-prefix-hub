"""Common ground for all source adapters (Steam, Lutris, Heroic, generic).

A source adapter does exactly three things:
  1. **Discovery**  -- find games, their prefix and their user dir.
  2. **Hook**       -- wire our launch hook into the launcher's own config.
  3. **Context**    -- tell the core which game is starting right now.

Everything else (snapshots, DB, redirection) is shared and lives in `core/`,
so adding a new source means adding one module here and nothing else.

Adapter module contract -- every adapter provides:

    SOURCE: str
    iter_games() -> Iterator[Game]
    context_from_env() -> Game | None      # who is launching right now?
    connect(app_id) -> HookResult          # install the launch hook
    disconnect(app_id) -> HookResult       # remove it again

`Game` is a plain dict so it serialises straight into the DB:

    source, app_id, game_name, installed, prefix_path, user_dir,
    game_dir, managed, state (free-form, source specific)
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

# Order matters twice: `context_from_env` asks in this order, and the generic
# adapter skips whatever the launchers before it already claim. It stays last.
SOURCES = ("steam", "lutris", "heroic", "generic")

Game = dict[str, Any]


class HookResult(dict):
    """Outcome of connect()/disconnect().

    ok       -- did we change something (or is it already in the wanted state)?
    manual   -- the user has to do one step themselves (Steam, sadly)
    message  -- already-translated text for the user
    detail   -- machine-readable extra (e.g. the launch options string)
    """

    def __init__(self, ok: bool, message: str, manual: bool = False,
                 **detail: Any) -> None:
        super().__init__(ok=ok, message=message, manual=manual, detail=detail)

    @property
    def ok(self) -> bool:
        return bool(self["ok"])

    @property
    def manual(self) -> bool:
        return bool(self["manual"])

    @property
    def message(self) -> str:
        return str(self["message"])


def get_adapter(source: str):
    """Import an adapter by name. Lazy, so `--wrapper` stays fast."""
    if source == "steam":
        from . import steam
        return steam
    if source == "lutris":
        from . import lutris
        return lutris
    if source == "heroic":
        from . import heroic
        return heroic
    if source == "generic":
        from . import generic
        return generic
    raise ValueError(f"unknown source: {source}")


def source_label(source: str) -> str:
    """The name of a source as the user should read it.

    The ids are ours; "generic" in particular is a word for this codebase, not
    for the person who installed a game by hand.
    """
    from ..core.i18n import _
    if source == "generic":
        return _("hand-installed")
    return {"steam": "Steam", "lutris": "Lutris",
            "heroic": "Heroic"}.get(source, source)


def iter_games(sources: tuple[str, ...] | None = None) -> Iterator[Game]:
    """All games from all (or the given) sources.

    A broken launcher config must never take the whole scan down, so each
    adapter is isolated.
    """
    for source in (sources or SOURCES):
        try:
            adapter = get_adapter(source)
            yield from adapter.iter_games()
        except Exception:
            continue


def context_from_env() -> Game | None:
    """Which game is being launched right now? Asks every adapter."""
    for source in SOURCES:
        try:
            ctx = get_adapter(source).context_from_env()
        except Exception:
            continue
        if ctx:
            return ctx
    return None


def context_for(source: str, app_id: str) -> Game | None:
    """Resolve a game by source + id (used by the pre/post hooks)."""
    for game in get_adapter(source).iter_games():
        if str(game.get("app_id")) == str(app_id):
            return game
    return None


# --- Generic prefix helpers ---------------------------------------------
def is_prefix(path: str | Path) -> bool:
    """A Wine prefix is anything with drive_c/ and the two registry hives."""
    p = Path(path)
    return ((p / "drive_c").is_dir()
            and (p / "user.reg").is_file()
            and (p / "system.reg").is_file())


def user_dir_for(prefix_path: str | Path | None) -> str | None:
    """Determine the user folder inside a prefix -- list it, do not guess.

    Proton uses `steamuser`, Lutris/Heroic usually the real login name. We
    list and prefer `steamuser`, ignoring the shared `Public` folder.
    """
    if not prefix_path:
        return None
    users = Path(prefix_path) / "drive_c" / "users"
    if not users.is_dir():
        return None
    try:
        candidates = [d.name for d in users.iterdir()
                      if d.is_dir() and d.name not in ("Public", "crossover")]
    except OSError:
        return None
    if "steamuser" in candidates:
        return "steamuser"
    return candidates[0] if candidates else None
