"""Prefix-DB und Config-Persistenz (JSON in ~/.config/deinapp).

Datenmodell pro Spiel/Prefix:
{
  "<fingerprint>": {
    "source": "steam" | "lutris" | "heroic",
    "app_id": "1091500",           # quellenspezifische ID (Steam appid etc.)
    "game_name": "Cyberpunk 2077",
    "prefix_path": "/.../compatdata/1091500/pfx",
    "user_dir": "steamuser",        # Ordner in drive_c/users
    "managed": false,               # Wrapper-Hook gesetzt?
    "storage_locations": [ {...}, ... ],
    "last_seen": "ISO-8601"
  }
}

storage_location:
{
  "type": "saves" | "config" | "unknown",
  "win_path": "Documents/CD Projekt Red/Cyberpunk 2077",
  "phys_path": "/.../users/steamuser/Documents/...",
  "detected_by": "diff" | "heuristic" | "pcgamingwiki",
  "redirected": false
}
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(prefix_path: str | Path) -> str:
    """Stabiler Fingerprint eines Prefix ueber realen Pfad."""
    real = os.path.realpath(str(prefix_path))
    return hashlib.sha256(real.encode()).hexdigest()[:16]


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(path)  # atomar


# --- Config (gewaehlter install_dir etc.) --------------------------------
def load_config() -> dict[str, Any]:
    return _read_json(paths.CONFIG_FILE, {})


def save_config(cfg: dict[str, Any]) -> None:
    _write_json(paths.CONFIG_FILE, cfg)


def install_dir() -> Path:
    cfg = load_config()
    d = cfg.get("install_dir")
    return Path(d) if d else paths.DEFAULT_INSTALL_DIR


# --- Prefix-DB -----------------------------------------------------------
def load_prefixes() -> dict[str, Any]:
    return _read_json(paths.PREFIX_DB, {})


def save_prefixes(db: dict[str, Any]) -> None:
    _write_json(paths.PREFIX_DB, db)


def upsert_prefix(entry: dict[str, Any]) -> str:
    """Fuegt einen erkannten Prefix ein oder aktualisiert ihn.

    Merged storage_locations & bewahrt 'redirected'/'managed'-Flags, damit
    ein erneuter Scan die Nutzer-Entscheidungen nicht ueberschreibt.
    """
    db = load_prefixes()
    fp = fingerprint(entry["prefix_path"])
    existing = db.get(fp, {})

    merged = {**existing, **entry, "last_seen": _now()}

    # Flags aus Bestand bewahren (Scan darf Nutzerentscheidung nicht kippen)
    if "managed" in existing and "managed" not in entry:
        merged["managed"] = existing["managed"]
    merged.setdefault("managed", False)

    # storage_locations: nach win_path mergen
    old_locs = {l["win_path"]: l for l in existing.get("storage_locations", [])}
    for loc in entry.get("storage_locations", []):
        wp = loc["win_path"]
        if wp in old_locs:
            # 'redirected' aus Bestand bewahren
            loc["redirected"] = old_locs[wp].get("redirected",
                                                 loc.get("redirected", False))
        old_locs[wp] = loc
    merged["storage_locations"] = list(old_locs.values())

    db[fp] = merged
    save_prefixes(db)
    return fp


def get_prefix(fp: str) -> dict[str, Any] | None:
    return load_prefixes().get(fp)
