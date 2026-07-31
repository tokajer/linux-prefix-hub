# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""The launch hook -- the one place where we learn what a game does.

Two shapes, same job:

  * **wrap** (`--wrapper CMD...`): Steam and Heroic hand us the real game
    command. We snapshot, run the game, snapshot again. One process.
  * **hooks** (`--hook pre|post --source S --id ID`): Lutris calls us twice,
    before and after. The "before" snapshot is written to disk so the second
    process can pick it up.

The wrapper is **read-only towards the game**: it observes, it does not change
how the game runs. The only write it performs is re-applying redirections the
user already asked for (self-heal), and that happens before the game starts.
Being invisible includes the environment: see `game_env`, which hands the game
back exactly the environment it would have had without us in the chain.

Failure policy: whatever goes wrong in our code, the game still launches and
its exit code is passed through. A save-game tracker that stops people from
playing has failed at its actual job.
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from typing import Any

from ..adapters import base
from . import db, redirect, snapshot

# --- The environment the game gets --------------------------------------
# The AppImage sets up its own bundled CPython -- for *us*. None of that may
# reach the game. Proton is itself a Python program, started by a different
# interpreter inside the Steam runtime container, so a PYTHONHOME pointing at
# our 3.12 stdlib (under a /tmp mount point the container cannot even see)
# kills the launch before the game ever starts. Same trap `_handover_env` in
# `__main__` documents, one process further down -- and here it is worse,
# because here it looks like "the game is broken since I connected it".
BUNDLE_VARS = ("APPDIR", "APPIMAGE", "ARGV0", "OWD",
               "APPIMAGE_EXTRACT_AND_RUN", "PYTHONHOME",
               "PYTHONDONTWRITEBYTECODE")

# Colon-lists AppRun prepends the bundle to. The user's own entries stay.
BUNDLE_LISTS = ("PYTHONPATH", "LD_LIBRARY_PATH", "PATH", "XDG_DATA_DIRS")


def game_env() -> dict[str, str] | None:
    """The environment the game would have had if we were not in the chain.

    Only what the AppImage added is undone; everything the launcher set
    (SteamAppId, WINEPREFIX, ...) is passed through untouched. Returns None
    when we are not running from an AppImage -- then there is nothing to undo
    and the game simply inherits our environment.
    """
    appdir = os.environ.get("APPDIR")
    if not appdir:
        return None
    env = dict(os.environ)
    for var in BUNDLE_VARS:
        env.pop(var, None)
    for var in BUNDLE_LISTS:
        if var not in env:
            continue
        rest = [p for p in env[var].split(os.pathsep)
                if p and not p.startswith(appdir)]
        if rest:
            env[var] = os.pathsep.join(rest)
        else:
            del env[var]
    return env


def _entry_from_context(ctx: dict[str, Any],
                        locations: list[dict[str, Any]] | None = None
                        ) -> dict[str, Any]:
    entry = {
        "source": ctx.get("source", "unknown"),
        "app_id": ctx.get("app_id", ""),
        "game_name": ctx.get("game_name", ""),
        "prefix_path": ctx["prefix_path"],
        "user_dir": ctx["user_dir"],
        "game_dir": ctx.get("game_dir"),
        "managed": True,
    }
    if locations is not None:
        entry["storage_locations"] = locations
    return entry


def _usable(ctx: dict[str, Any] | None) -> bool:
    return bool(ctx and ctx.get("prefix_path") and ctx.get("user_dir"))


# A "before" state is both spaces at once: the prefix and the install folder.
Snapshots = dict[str, dict[str, float]]


def _snapshot_all(ctx: dict[str, Any]) -> Snapshots:
    states = {snapshot.WHERE_PREFIX: snapshot.snapshot(ctx["prefix_path"],
                                                       ctx["user_dir"])}
    # Absent rather than empty when the install folder is not covered, so the
    # diff below has nothing to compare and simply says nothing about it.
    game = snapshot.snapshot_game_dir(ctx.get("game_dir"))
    if game is not None:
        states[snapshot.WHERE_GAME] = game
    return states


def _before(ctx: dict[str, Any]) -> tuple[str, Snapshots]:
    """Register the game, self-heal what we know about it, snapshot."""
    fingerprint = db.upsert_prefix(_entry_from_context(ctx))
    with contextlib.suppress(Exception):   # never block a launch
        redirect.reapply(fingerprint)
    with contextlib.suppress(Exception):
        # Locations a filter we have today would never have recorded --
        # a shader cache from before that filter existed, or one the user
        # has since added themselves. Separate suppress: a redirection that
        # fails to reapply must not keep the junk around, and vice versa.
        db.prune_locations(fingerprint, snapshot.location_is_noise)
    return fingerprint, _snapshot_all(ctx)


def _known_locations(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """What the user accepted from a lookup -- from the cache only.

    Reading a local JSON is all this does: the wiki is asked when the user
    asks (`--lookup`, the button in the window), never here. A game that has
    just exited is not the moment to wait on someone else's server.

    And only what they said yes to, and only what is on disk right now
    (`pcgw.cached_locations`). A launch is not a moment at which a suggestion
    nobody looked at gets to become a storage location.
    """
    try:
        from . import pcgw
        return pcgw.cached_locations(ctx)
    except Exception:
        return []


def _after(ctx: dict[str, Any], before: Snapshots) -> None:
    """Diff against the pre-launch snapshot and store what we learned.

    Both spaces are diffed. A game like Portal 2 touches nothing but
    AppData/Local/Temp inside the prefix and writes its real saves into
    `<install folder>/portal2/SAVE/` -- prefix-only detection learns nothing
    at all about it.

    Anything already looked up goes in first, so that a location the diff
    also saw keeps the diff's file count while the two entries stay one
    entry (`db.location_key`).
    """
    known = _known_locations(ctx)
    after = _snapshot_all(ctx)
    locations: list[dict[str, Any]] = []
    for where, before_state in before.items():
        changed = snapshot.diff(before_state, after.get(where, {}))
        locations += snapshot.classify_locations(changed, where, known)
    if locations or known:
        db.upsert_prefix(_entry_from_context(ctx, known + locations))


def main(argv: list[str]) -> int:
    """argv = the real game command (whatever stands behind %command%)."""
    if not argv:
        print("wrapper: no game command given", file=sys.stderr)
        return 2

    ctx = None
    before: dict[str, float] = {}
    try:
        ctx = base.context_from_env()
        if _usable(ctx):
            _, before = _before(ctx)
    except Exception as exc:  # observation must never break the launch
        print(f"wrapper: skipping detection ({exc})", file=sys.stderr)
        ctx = None

    proc = subprocess.run(argv, env=game_env())

    try:
        if _usable(ctx):
            _after(ctx, before)
    except Exception as exc:
        print(f"wrapper: could not store results ({exc})", file=sys.stderr)

    return proc.returncode


def hook(phase: str, source: str, app_id: str) -> int:
    """Pre/post hook for launchers that call us twice instead of wrapping."""
    try:
        ctx = base.context_for(source, app_id) or base.context_from_env()
    except Exception:
        ctx = None
    if not _usable(ctx):
        # No prefix yet (first launch ever) is normal, not an error.
        return 0
    assert ctx is not None

    try:
        if phase == "pre":
            fingerprint, before = _before(ctx)
            snapshot.save_pending(fingerprint, before)
        else:
            fingerprint = db.fingerprint(ctx["prefix_path"])
            _after(ctx, snapshot.load_pending(fingerprint))
    except Exception as exc:
        print(f"hook: {exc}", file=sys.stderr)
    return 0
