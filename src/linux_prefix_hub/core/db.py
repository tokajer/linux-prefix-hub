# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Prefix DB (SQLite) and config (JSON) in ~/.config/linux-prefix-hub.

The two are stored differently because they are used differently. `config.json`
is a handful of settings one person changes by hand now and then, and a file
somebody can open in an editor is worth keeping. The prefix DB is written by
three processes that do not know about each other -- the launch wrapper files
what a session changed, the watcher files a game it has just seen, the window
files what the user just decided -- and read-whole-file/write-whole-file means
whoever saves last quietly wins. That is what the `.db` is for; see the
"Prefix DB" section below.

Data model per game/prefix:
{
  "<fingerprint>": {
    "source": "steam" | "lutris" | "heroic" | "generic",
    "app_id": "1091500",            # source-specific id (appid, slug, path)
    "game_name": "Cyberpunk 2077",
    "prefix_path": "/.../compatdata/1091500/pfx",
    "user_dir": "steamuser",        # folder inside drive_c/users
    "managed": false,               # launch hook installed?
    "storage_locations": [ {...}, ... ],
    "last_seen": "ISO-8601"
  }
}

storage_location:
{
  "type": "saves" | "config" | "unknown",
  "win_path": "Documents/CD Projekt Red/Cyberpunk 2077",
  "file_count": 12,
  "detected_by": "diff" | "heuristic" | "pcgamingwiki",
  "redirected": false,
  "redirect_target": "/home/you/Games/Cyberpunk 2077/Documents"
}

Invariant: a rescan must never overwrite a user decision. Fields the user
controls are listed in USER_FIELDS / LOCATION_USER_FIELDS and are preserved by
`upsert_prefix`. If you add such a field, add it there too.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths

# Fields owned by the user -- a discovery scan must never reset these.
USER_FIELDS = ("managed",)
LOCATION_USER_FIELDS = ("redirected", "redirect_target")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(prefix_path: str | Path) -> str:
    """Stable identifier of a prefix, derived from its real path.

    Deliberately source-agnostic: it does not matter whether Steam, Lutris,
    Heroic or a hand-rolled Wine setup created the prefix.
    """
    real = os.path.realpath(str(prefix_path))
    return hashlib.sha256(real.encode()).hexdigest()[:16]


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(path)  # atomic


# --- Config (chosen install_dir, language, ...) --------------------------
def load_config() -> dict[str, Any]:
    return _read_json(paths.CONFIG_FILE, {})


def save_config(cfg: dict[str, Any]) -> None:
    _write_json(paths.CONFIG_FILE, cfg)


def set_config(key: str, value: Any) -> None:
    cfg = load_config()
    cfg[key] = value
    save_config(cfg)


def install_dir() -> Path:
    d = load_config().get("install_dir")
    return Path(d) if d else paths.DEFAULT_INSTALL_DIR


def redirect_root() -> Path:
    """Where moved save folders end up: one directory per game below this."""
    d = load_config().get("redirect_root")
    return Path(os.path.expanduser(d)) if d else paths.DEFAULT_REDIRECT_ROOT


def extra_game_folders() -> list[str]:
    """Folders the user told us to look in for hand-installed games.

    The generic adapter knows the usual places; this is for everything else,
    and a hand-rolled setup can live anywhere.
    """
    value = load_config().get("game_folders")
    return [str(v) for v in value] if isinstance(value, list) else []


def add_game_folder(path: str | Path) -> bool:
    """Remember a folder to look in. False if it was already remembered."""
    folder = os.path.abspath(os.path.expanduser(str(path)))
    folders = extra_game_folders()
    if folder in folders:
        return False
    set_config("game_folders", folders + [folder])
    return True


def forget_game_folder(path: str | Path) -> bool:
    """Drop a folder again. False if it was not in the list."""
    folder = os.path.abspath(os.path.expanduser(str(path)))
    folders = extra_game_folders()
    if folder not in folders:
        return False
    set_config("game_folders", [f for f in folders if f != folder])
    return True


def extra_ignore_paths() -> list[str]:
    """Path fragments that must never count as a storage location.

    Kept as the user typed them; `snapshot.user_ignores` normalises. The
    built-in list in `core/snapshot.py` covers the churn we have seen, this
    is for the rest -- every engine invents its own cache folder.
    """
    value = load_config().get("ignore_paths")
    return [str(v) for v in value] if isinstance(value, list) else []


def add_ignore_path(fragment: str) -> bool:
    """Remember a fragment to ignore. False if it was already remembered."""
    frag = str(fragment).strip()
    fragments = extra_ignore_paths()
    if not frag or frag in fragments:
        return False
    set_config("ignore_paths", fragments + [frag])
    return True


def forget_ignore_path(fragment: str) -> bool:
    """Drop a fragment again. False if it was not in the list."""
    frag = str(fragment).strip()
    fragments = extra_ignore_paths()
    if frag not in fragments:
        return False
    set_config("ignore_paths", [f for f in fragments if f != frag])
    return True


def background_tray() -> bool:
    """Should the app stay alive in the tray when its window is closed?

    Default on: the window is not the app. An update check, a new-game
    notification and a move waiting for a game's first launch all outlive it,
    and an icon is the only thing that says so. Off puts the old behaviour
    back, where closing the window ends the process.
    """
    value = load_config().get("background_tray")
    return True if value is None else bool(value)


def game_key(source: str, app_id: str) -> str:
    """The identity a game has before it has a folder.

    The prefix DB is keyed by the prefix, which does not exist until the game
    has run once -- so everything we want to remember about a game *before*
    that (a wished-for move, a game the user does not want to see) is keyed by
    this instead, and lives in `config.json`.
    """
    return f"{source}:{app_id}"


# --- Redirections asked for before there was anything to redirect --------
def pending_key(source: str, app_id: str) -> str:
    """`game_key` under the name the pending redirects shipped with."""
    return game_key(source, app_id)


def pending_redirects() -> dict[str, Any]:
    """Every stored wish, keyed by `pending_key`."""
    value = load_config().get("pending_redirects")
    return dict(value) if isinstance(value, dict) else {}


def add_pending_redirect(source: str, app_id: str, game_name: str = "",
                         roots: list[str] | None = None,
                         target: str | None = None) -> str:
    """Remember that this game's data should be moved once it can be.

    `roots` empty means "whatever turns out to be movable" -- before the first
    launch we usually do not know yet, and guessing here would be worse than
    resolving it at the moment we act (`redirect.apply_pending`).
    """
    key = pending_key(source, app_id)
    pending = pending_redirects()
    pending[key] = {"source": source, "app_id": app_id,
                    "game_name": game_name, "roots": list(roots or []),
                    "target": target, "asked_at": _now()}
    set_config("pending_redirects", pending)
    return key


def drop_pending_redirect(source: str, app_id: str) -> bool:
    """Forget a wish. False if there was none."""
    key = pending_key(source, app_id)
    pending = pending_redirects()
    if key not in pending:
        return False
    del pending[key]
    set_config("pending_redirects", pending)
    return True


# --- Games the user does not want to look at -----------------------------
# Hiding is a statement about the *list*, not about the game: a hidden game
# keeps its launch hook, its learned storage locations and any move that was
# asked for, and the wrapper still files what a launch changed. Nothing here
# is allowed to become "stop managing this" -- a filter that quietly turns
# features off is a filter nobody dares to use.
def hidden_games() -> list[str]:
    """Every hidden game, as `game_key` strings."""
    value = load_config().get("hidden_games")
    return [str(v) for v in value] if isinstance(value, list) else []


def is_hidden(source: str, app_id: str) -> bool:
    return game_key(source, app_id) in hidden_games()


def hide_game(source: str, app_id: str) -> bool:
    """Leave a game out of the lists. False if it already was."""
    key = game_key(source, app_id)
    hidden = hidden_games()
    if key in hidden:
        return False
    set_config("hidden_games", hidden + [key])
    return True


def unhide_game(source: str, app_id: str) -> bool:
    """Put it back. False if it was not hidden."""
    key = game_key(source, app_id)
    hidden = hidden_games()
    if key not in hidden:
        return False
    set_config("hidden_games", [k for k in hidden if k != key])
    return True


def location_key(loc: dict[str, Any]) -> tuple[str, str]:
    """Identity of a storage location: its space plus its path in it.

    Entries written before the install folder was tracked have no `where`;
    they are prefix locations.
    """
    return (str(loc.get("where") or "prefix"), str(loc.get("win_path", "")))


# --- Suggestions the user has said yes to --------------------------------
# A PCGamingWiki lookup proposes storage locations; it never decides. What
# the user accepted is remembered here and not in that lookup's own cache,
# because the cache expires after a month and the next refresh overwrites it
# -- a decision no rescan may undo cannot live in a file that is rewritten by
# one. Keyed by `game_key` like everything else a game can own before it has
# a prefix.
def confirm_key(loc: dict[str, Any]) -> tuple[str, str]:
    """`location_key`, case-folded and with one kind of slash.

    The wiki respells its paths -- "Documents" today, "documents" after
    somebody tidied the article. That is not a different folder and must not
    read as a suggestion the user has not seen yet.
    """
    where, win_path = location_key(loc)
    return where, win_path.replace("\\", "/").strip("/").lower()


def confirmed_lookups() -> dict[str, list[list[str]]]:
    """Every accepted suggestion, keyed by `game_key`."""
    value = load_config().get("confirmed_lookups")
    if not isinstance(value, dict):
        return {}
    return {str(key): [[str(part) for part in pair]
                       for pair in pairs if isinstance(pair, list)]
            for key, pairs in value.items() if isinstance(pairs, list)}


def confirmed_locations(source: str, app_id: str) -> set[tuple[str, str]]:
    """The `confirm_key`s the user accepted for one game."""
    stored = confirmed_lookups().get(game_key(source, app_id), [])
    return {(pair[0], pair[1]) for pair in stored if len(pair) >= 2}


def confirm_locations(source: str, app_id: str,
                      locations: list[dict[str, Any]]) -> int:
    """Remember that the user accepted these. Returns how many are on file.

    Additive: a second lookup that finds one more location does not withdraw
    the yes given to the first.
    """
    accepted = confirmed_locations(source, app_id)
    accepted |= {confirm_key(loc) for loc in locations}
    everything = confirmed_lookups()
    everything[game_key(source, app_id)] = [list(key)
                                            for key in sorted(accepted)]
    set_config("confirmed_lookups", everything)
    return len(accepted)


def forget_confirmed(source: str, app_id: str) -> bool:
    """Withdraw the yes for one game. False if there was none."""
    everything = confirmed_lookups()
    if everything.pop(game_key(source, app_id), None) is None:
        return False
    set_config("confirmed_lookups", everything)
    return True


# --- Prefix DB -----------------------------------------------------------
# SQLite, because this file has three writers. The launch wrapper, the
# watcher and the window all reach for it, two of them can land in the same
# second, and "read the whole file, change one field, write the whole file
# back" resolves that by letting whoever saves last win -- the other decision
# is simply gone, with nothing to show that it ever happened. Every function
# below touches the rows it means, inside one transaction, and lets SQLite
# hold the lock.
#
# What a caller gets back is unchanged: the same nested dicts, the same
# signatures, `where` and `win_path` still the identity of a location. The
# columns are an index over an entry, not a replacement for it -- `extra`
# carries every key we have no column for, so an adapter can put a new field
# into an entry without a schema change here.
SCHEMA_VERSION = 1
MIGRATED_KEY = "migrated_from_json"

# Entry fields with a column of their own. Everything else goes to `extra`.
_ENTRY_COLUMNS = ("source", "app_id", "game_name", "prefix_path",
                  "user_dir", "game_dir", "managed", "last_seen")
# Same for a storage location. `where` is stored as `where_space` -- WHERE is
# a SQL keyword and quoting it everywhere is not worth the cleverness.
_LOCATION_COLUMNS = ("type", "where", "win_path", "file_count",
                     "detected_by", "redirected", "redirect_target")

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta ("
    " key TEXT PRIMARY KEY, value TEXT NOT NULL)",

    "CREATE TABLE IF NOT EXISTS prefixes ("
    " fingerprint TEXT PRIMARY KEY,"
    " source TEXT, app_id TEXT, game_name TEXT, prefix_path TEXT,"
    " user_dir TEXT, game_dir TEXT,"
    " managed INTEGER NOT NULL DEFAULT 0,"
    " last_seen TEXT NOT NULL DEFAULT '',"
    " extra TEXT NOT NULL DEFAULT '{}')",

    "CREATE TABLE IF NOT EXISTS locations ("
    " fingerprint TEXT NOT NULL,"
    " where_space TEXT NOT NULL DEFAULT 'prefix',"
    " win_path TEXT NOT NULL DEFAULT '',"
    " type TEXT, file_count INTEGER, detected_by TEXT,"
    " redirected INTEGER NOT NULL DEFAULT 0,"
    " redirect_target TEXT,"
    " position INTEGER NOT NULL DEFAULT 0,"
    " extra TEXT NOT NULL DEFAULT '{}',"
    " PRIMARY KEY (fingerprint, where_space, win_path))",

    "CREATE INDEX IF NOT EXISTS locations_by_prefix"
    " ON locations (fingerprint)",
    "CREATE INDEX IF NOT EXISTS prefixes_by_game ON prefixes (source, app_id)",
)


@contextlib.contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """A connection for the length of one call, and no longer.

    Deliberately not cached. `paths` resolves its constants at import time and
    the tests reload it, so a connection kept across that would go on writing
    into the previous run's directory -- and the AppImage's three modes are
    separate processes anyway, so there is nothing to keep it open for.
    Opening a SQLite file is cheap; being wrong about which file is not.
    """
    paths.PREFIX_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(paths.PREFIX_DB), timeout=15.0,
                           isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 15000")
        # WAL lets the watcher read while the wrapper writes. It needs shared
        # memory, which a network home directory may not have -- then the
        # default journal is used and everything still works, just serialised.
        with contextlib.suppress(sqlite3.Error):
            conn.execute("PRAGMA journal_mode = WAL")
        _ensure_schema(conn)
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def _write(conn: sqlite3.Connection) -> Iterator[None]:
    """One write transaction. IMMEDIATE: take the lock before reading.

    Read-then-write is exactly the pattern we moved here to fix, so the read
    half has to be inside the same lock as the write half.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    for statement in _SCHEMA:
        conn.execute(statement)
    if _meta(conn, MIGRATED_KEY) is None:
        _migrate_json(conn)


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?",
                       (key,)).fetchone()
    return str(row["value"]) if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                 (key, value))


def _migrate_json(conn: sqlite3.Connection) -> None:
    """Fold a pre-SQLite prefixes.json in, exactly once.

    The file stays on disk afterwards. It is small, it is the only backup of
    a database that takes months of playing to fill, and what says the import
    happened is the flag in `meta` -- not the file being gone. So deleting
    the database re-imports it, and deleting the file loses nothing.
    """
    legacy = _read_json(paths.LEGACY_PREFIX_DB, None)
    with _write(conn):
        if isinstance(legacy, dict):
            for fp, entry in legacy.items():
                if isinstance(entry, dict):
                    _put_entry(conn, str(fp), entry)
        _set_meta(conn, MIGRATED_KEY, _now())
        _set_meta(conn, "schema_version", str(SCHEMA_VERSION))


# --- row <-> dict --------------------------------------------------------
def _entry_from_row(row: sqlite3.Row) -> dict[str, Any]:
    """A prefixes row as the entry the rest of the app knows.

    A NULL column is left out rather than handed over as None: it was not in
    the dict when it went in, and an entry that grows keys on a round-trip is
    a surprise waiting to happen.
    """
    entry: dict[str, Any] = {}
    for field in _ENTRY_COLUMNS:
        value = row[field]
        if field == "managed":
            entry[field] = bool(value)
        elif value is not None:
            entry[field] = value
    extra = _loads(row["extra"])
    entry.update(extra)
    return entry


def _location_from_row(row: sqlite3.Row) -> dict[str, Any]:
    loc: dict[str, Any] = {}
    for field, value in (("type", row["type"]),
                         ("where", row["where_space"]),
                         ("win_path", row["win_path"]),
                         ("file_count", row["file_count"]),
                         ("detected_by", row["detected_by"])):
        if value is not None:
            loc[field] = value
    loc["redirected"] = bool(row["redirected"])
    if row["redirect_target"] is not None:
        loc["redirect_target"] = row["redirect_target"]
    loc.update(_loads(row["extra"]))
    return loc


def _loads(text: Any) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _dumps(data: dict[str, Any]) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        # An adapter put something unserialisable in an entry. Losing that one
        # field is not a reason to lose the game it belongs to.
        return "{}"


def _put_entry(conn: sqlite3.Connection, fp: str,
               entry: dict[str, Any]) -> None:
    """Write one whole entry: its row, and all of its locations."""
    extra = {k: v for k, v in entry.items()
             if k not in _ENTRY_COLUMNS and k != "storage_locations"}
    conn.execute(
        "INSERT INTO prefixes (fingerprint, source, app_id, game_name,"
        " prefix_path, user_dir, game_dir, managed, last_seen, extra)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(fingerprint) DO UPDATE SET"
        " source = excluded.source, app_id = excluded.app_id,"
        " game_name = excluded.game_name,"
        " prefix_path = excluded.prefix_path,"
        " user_dir = excluded.user_dir, game_dir = excluded.game_dir,"
        " managed = excluded.managed, last_seen = excluded.last_seen,"
        " extra = excluded.extra",
        (fp, entry.get("source"), entry.get("app_id"),
         entry.get("game_name"), entry.get("prefix_path"),
         entry.get("user_dir"), entry.get("game_dir"),
         1 if entry.get("managed") else 0,
         str(entry.get("last_seen") or ""), _dumps(extra)))

    conn.execute("DELETE FROM locations WHERE fingerprint = ?", (fp,))
    for position, loc in enumerate(entry.get("storage_locations", [])):
        if isinstance(loc, dict):
            _put_location(conn, fp, position, loc)


def _put_location(conn: sqlite3.Connection, fp: str, position: int,
                  loc: dict[str, Any]) -> None:
    where, win_path = location_key(loc)
    extra = {k: v for k, v in loc.items() if k not in _LOCATION_COLUMNS}
    conn.execute(
        "INSERT OR REPLACE INTO locations (fingerprint, where_space,"
        " win_path, type, file_count, detected_by, redirected,"
        " redirect_target, position, extra)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (fp, where, win_path, loc.get("type"), loc.get("file_count"),
         loc.get("detected_by"), 1 if loc.get("redirected") else 0,
         loc.get("redirect_target"), position, _dumps(extra)))


def _read_one(conn: sqlite3.Connection, fp: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM prefixes WHERE fingerprint = ?",
                       (fp,)).fetchone()
    if row is None:
        return None
    entry = _entry_from_row(row)
    entry["storage_locations"] = [
        _location_from_row(r) for r in conn.execute(
            "SELECT * FROM locations WHERE fingerprint = ?"
            " ORDER BY position, rowid", (fp,))]
    return entry


# --- the API the rest of the app uses ------------------------------------
def load_prefixes() -> dict[str, Any]:
    """Every entry, in the order they were first seen."""
    entries: dict[str, Any] = {}
    with _connect() as conn:
        for row in conn.execute("SELECT * FROM prefixes ORDER BY rowid"):
            entry = _entry_from_row(row)
            entry["storage_locations"] = []
            entries[str(row["fingerprint"])] = entry
        for row in conn.execute("SELECT * FROM locations"
                                " ORDER BY fingerprint, position, rowid"):
            entry = entries.get(str(row["fingerprint"]))
            if entry is not None:
                entry["storage_locations"].append(_location_from_row(row))
    return entries


def save_prefixes(db: dict[str, Any]) -> None:
    """Replace the whole database with `db`.

    The counterpart to `load_prefixes`, kept because handing back what you
    were given has to keep working. Nothing in here uses it: writing all of
    it to change one field is the pattern this module moved away from.
    """
    with _connect() as conn, _write(conn):
        conn.execute("DELETE FROM locations")
        conn.execute("DELETE FROM prefixes")
        for fp, entry in db.items():
            if isinstance(entry, dict):
                _put_entry(conn, str(fp), entry)


def upsert_prefix(entry: dict[str, Any]) -> str:
    """Insert or update a detected prefix; returns its fingerprint.

    Merges storage_locations and preserves the user-owned flags, so that a
    rescan never overwrites what the user decided. Read and write share one
    transaction -- a rescan and a click on the same game are two processes,
    and the whole point of the merge is lost if the read half sees a state
    the write half then overwrites.
    """
    fp = fingerprint(entry["prefix_path"])
    with _connect() as conn, _write(conn):
        existing = _read_one(conn, fp) or {}
        _put_entry(conn, fp, _merged(existing, entry))
    return fp


def _merged(existing: dict[str, Any],
            entry: dict[str, Any]) -> dict[str, Any]:
    """The invariant at the top of this file, in one place."""
    merged = {**existing, **entry, "last_seen": _now()}

    for field in USER_FIELDS:
        if field in existing and field not in entry:
            merged[field] = existing[field]
    merged.setdefault("managed", False)

    # storage_locations: merge by (space, win_path). The two spaces are
    # separate namespaces -- "cfg" in the install folder is not "cfg" in the
    # prefix -- so the key has to carry `where`.
    old_locs = {location_key(loc): loc
                for loc in existing.get("storage_locations", [])}
    for loc in entry.get("storage_locations", []):
        key = location_key(loc)
        old = old_locs.get(key)
        if old:
            for field in LOCATION_USER_FIELDS:
                if field in old:
                    loc[field] = old[field]
        loc.setdefault("redirected", False)
        old_locs[key] = loc
    merged["storage_locations"] = list(old_locs.values())
    return merged


def get_prefix(fp: str) -> dict[str, Any] | None:
    with _connect() as conn:
        return _read_one(conn, fp)


def find_prefix(source: str, app_id: str) -> tuple[str, dict[str, Any]] | None:
    """Look up a known prefix by source + app id."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT fingerprint FROM prefixes"
            " WHERE source = ? AND app_id = ? ORDER BY rowid LIMIT 1",
            (source, app_id)).fetchone()
        if row is None:
            return None
        fp = str(row["fingerprint"])
        entry = _read_one(conn, fp)
        return (fp, entry) if entry is not None else None


def resolve(needle: str) -> tuple[str, dict[str, Any]] | None:
    """Resolve a fingerprint, an app id or a (partial) game name to an entry.

    Convenience for the CLI so users never have to type a fingerprint. The
    name match stays in Python rather than becoming a LIKE: SQLite's `lower`
    only folds ASCII, and a library with a German or Japanese title in it
    would quietly stop matching what the user typed.
    """
    with _connect() as conn:
        found = conn.execute(
            "SELECT fingerprint FROM prefixes WHERE fingerprint = ?",
            (needle,)).fetchone()
        if found is None:
            found = conn.execute(
                "SELECT fingerprint FROM prefixes WHERE app_id = ?"
                " ORDER BY rowid LIMIT 1", (needle,)).fetchone()
        if found is None:
            low = needle.lower()
            for row in conn.execute("SELECT fingerprint, game_name"
                                    " FROM prefixes ORDER BY rowid"):
                if low in str(row["game_name"] or "").lower():
                    found = row
                    break
        if found is None:
            return None
        fp = str(found["fingerprint"])
        entry = _read_one(conn, fp)
        return (fp, entry) if entry is not None else None


def update_location(fp: str, win_path: str, where: str = "prefix",
                    **fields: Any) -> bool:
    """Patch one storage location of one prefix. Returns True if it existed.

    `where` defaults to the prefix because that is the only space anything
    is ever redirected in.
    """
    with _connect() as conn, _write(conn):
        row = conn.execute(
            "SELECT * FROM locations WHERE fingerprint = ?"
            " AND where_space = ? AND win_path = ?",
            (fp, where, win_path)).fetchone()
        if row is None:
            return False
        loc = _location_from_row(row)
        loc.update(fields)
        _put_location(conn, fp, int(row["position"]), loc)
        return True


def prune_locations(fp: str | None,
                    is_noise: Callable[[dict[str, Any]], bool]) -> int:
    """Forget storage locations that should never have been recorded.

    `fp` names one game, `None` means all of them. Returns how many were
    dropped. The rule itself lives in `core/snapshot.py`
    (`location_is_noise`) -- a filter added today has to be able to clean up
    after itself, or the shader cache somebody recorded last month stays a
    "config" location forever.

    What the user acted on is never offered to `is_noise`: a moved folder is
    a decision, and undoing it silently would break the invariant at the top
    of this file (and leave a symlink pointing at a folder nobody tracks).
    """
    query = "SELECT * FROM locations"
    args: tuple[Any, ...] = ()
    if fp is not None:
        query += " WHERE fingerprint = ?"
        args = (fp,)
    dropped = 0
    with _connect() as conn, _write(conn):
        for row in conn.execute(query, args).fetchall():
            loc = _location_from_row(row)
            if _user_owned(loc) or not is_noise(loc):
                continue
            conn.execute(
                "DELETE FROM locations WHERE fingerprint = ?"
                " AND where_space = ? AND win_path = ?",
                (row["fingerprint"], row["where_space"], row["win_path"]))
            dropped += 1
    return dropped


def _user_owned(loc: dict[str, Any]) -> bool:
    """Has the user acted on this location? Then it is not ours to remove."""
    return any(loc.get(field) for field in LOCATION_USER_FIELDS)


def forget_prefix(fp: str) -> bool:
    """Drop everything we learned about one game folder. False if unknown.

    Only ever for a folder that is *gone* (`newprefix.delete`): what we know
    about a game that still exists is worth keeping even while it is hidden,
    and re-learning it costs the user another playthrough.
    """
    with _connect() as conn, _write(conn):
        conn.execute("DELETE FROM locations WHERE fingerprint = ?", (fp,))
        changed = conn.execute("DELETE FROM prefixes WHERE fingerprint = ?",
                               (fp,)).rowcount
    return bool(changed)


def set_managed(fp: str, managed: bool) -> bool:
    """Record whether the launch hook is installed for this prefix."""
    with _connect() as conn, _write(conn):
        changed = conn.execute(
            "UPDATE prefixes SET managed = ? WHERE fingerprint = ?",
            (1 if managed else 0, fp)).rowcount
    return bool(changed)
