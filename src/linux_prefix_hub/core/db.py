# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Prefix DB and config persistence (JSON in ~/.config/linux-prefix-hub).

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

import hashlib
import json
import os
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


def location_key(loc: dict[str, Any]) -> tuple[str, str]:
    """Identity of a storage location: its space plus its path in it.

    Entries written before the install folder was tracked have no `where`;
    they are prefix locations.
    """
    return (str(loc.get("where") or "prefix"), str(loc.get("win_path", "")))


# --- Prefix DB -----------------------------------------------------------
def load_prefixes() -> dict[str, Any]:
    return _read_json(paths.PREFIX_DB, {})


def save_prefixes(db: dict[str, Any]) -> None:
    _write_json(paths.PREFIX_DB, db)


def upsert_prefix(entry: dict[str, Any]) -> str:
    """Insert or update a detected prefix; returns its fingerprint.

    Merges storage_locations and preserves the user-owned flags, so that a
    rescan never overwrites what the user decided.
    """
    db = load_prefixes()
    fp = fingerprint(entry["prefix_path"])
    existing = db.get(fp, {})

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

    db[fp] = merged
    save_prefixes(db)
    return fp


def get_prefix(fp: str) -> dict[str, Any] | None:
    return load_prefixes().get(fp)


def find_prefix(source: str, app_id: str) -> tuple[str, dict[str, Any]] | None:
    """Look up a known prefix by source + app id."""
    for fp, entry in load_prefixes().items():
        if entry.get("source") == source and entry.get("app_id") == app_id:
            return fp, entry
    return None


def resolve(needle: str) -> tuple[str, dict[str, Any]] | None:
    """Resolve a fingerprint, an app id or a (partial) game name to an entry.

    Convenience for the CLI so users never have to type a fingerprint.
    """
    db = load_prefixes()
    if needle in db:
        return needle, db[needle]
    low = needle.lower()
    for fp, entry in db.items():
        if entry.get("app_id") == needle:
            return fp, entry
    for fp, entry in db.items():
        if low in str(entry.get("game_name", "")).lower():
            return fp, entry
    return None


def update_location(fp: str, win_path: str, where: str = "prefix",
                    **fields: Any) -> bool:
    """Patch one storage location of one prefix. Returns True if it existed.

    `where` defaults to the prefix because that is the only space anything
    is ever redirected in.
    """
    db = load_prefixes()
    entry = db.get(fp)
    if not entry:
        return False
    for loc in entry.get("storage_locations", []):
        if location_key(loc) == (where, win_path):
            loc.update(fields)
            save_prefixes(db)
            return True
    return False


def set_managed(fp: str, managed: bool) -> bool:
    """Record whether the launch hook is installed for this prefix."""
    db = load_prefixes()
    entry = db.get(fp)
    if not entry:
        return False
    entry["managed"] = managed
    save_prefixes(db)
    return True
