"""Lutris adapter: discovery via pga.db/YAML, hooks via prelaunch/postexit.

Lutris is the friendly source: it has real hooks, so unlike Steam there is no
manual step for the user. And it tells us the prefix outright -- we never have
to guess where it is.

Discovery is two-layered on purpose:
  1. `pga.db` (SQLite, stdlib) gives the real game names, runners and the
     config file name. This is the authoritative list.
  2. `<config>/games/<configpath>.yml` gives `game.prefix` -- the actual
     prefix path.
If pga.db is missing or its schema changed, we fall back to scanning the YAML
files directly, so discovery degrades instead of dying.

Writing is deliberately **not** a YAML round-trip: we edit the affected lines
in place so the user's comments, ordering and formatting survive, and we keep
a `.bak` next to the file. A config parser that reformats someone's launcher
config is a bug, not a feature.

VERIFY-ON-DEVICE:
  - `prelaunch_wait` exists to make Lutris wait for our pre-hook before the
    game starts. Confirm your Lutris version still honours it (it has moved
    around between releases); without it the "before" snapshot may race.
  - Flatpak Lutris uses a different config root -- both are handled below,
    check which one your installation actually uses.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..core import paths, yamlite
from .base import HookResult, user_dir_for

SOURCE = "lutris"

# (config root, data root) per installation flavour.
LUTRIS_ROOTS = [
    ("~/.config/lutris", "~/.local/share/lutris"),
    ("~/.var/app/net.lutris.Lutris/config/lutris",
     "~/.var/app/net.lutris.Lutris/data/lutris"),
]

# Keys we own inside the game's `system:` block.
PRE_KEY = "prelaunch_command"
POST_KEY = "postexit_command"
WAIT_KEY = "prelaunch_wait"


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p))


def config_roots() -> list[tuple[Path, Path]]:
    """Existing (config, data) root pairs."""
    out = []
    for cfg, data in LUTRIS_ROOTS:
        c, d = _expand(cfg), _expand(data)
        if c.is_dir():
            out.append((c, d))
    return out


def config_file_for(app_id: str) -> Path | None:
    """The game's YAML config (app_id is the Lutris slug/configpath)."""
    for cfg_root, _data in config_roots():
        direct = cfg_root / "games" / f"{app_id}.yml"
        if direct.is_file():
            return direct
        # configpath usually is "<slug>-<timestamp>"
        matches = sorted((cfg_root / "games").glob(f"{app_id}-*.yml"))
        if matches:
            return matches[0]
    return None


def _games_from_pga(data_root: Path) -> list[dict[str, Any]]:
    """Authoritative game list from Lutris' SQLite DB (read-only)."""
    pga = data_root / "pga.db"
    if not pga.is_file():
        return []
    try:
        con = sqlite3.connect(f"file:{pga}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return []
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT name, slug, runner, directory, configpath, installed "
            "FROM games").fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()
    return [dict(r) for r in rows]


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        return yamlite.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return {}


def _prefix_from_config(cfg: dict[str, Any]) -> str | None:
    for section in ("game", "wine"):
        block = cfg.get(section)
        if isinstance(block, dict) and block.get("prefix"):
            return str(_expand(str(block["prefix"])))
    return None


def _slug_from_configpath(configpath: str) -> str:
    """"half-life-2-1690000000" -> "half-life-2".

    Lutris appends a unix timestamp to the config file name. Only strip a
    trailing all-digit segment that is long enough to be one -- otherwise we
    would eat the "2" in "half-life-2".
    """
    head, _, tail = configpath.rpartition("-")
    if head and tail.isdigit() and len(tail) >= 8:
        return head
    return configpath


def _is_hooked(cfg: dict[str, Any]) -> bool:
    system = cfg.get("system")
    if not isinstance(system, dict):
        return False
    return str(paths.HOOK_SHIM) in str(system.get(PRE_KEY, ""))


def iter_games() -> Iterator[dict[str, Any]]:
    seen: set[str] = set()
    for cfg_root, data_root in config_roots():
        rows = _games_from_pga(data_root)
        for row in rows:
            app_id = row.get("slug") or row.get("configpath")
            if not app_id or app_id in seen:
                continue
            seen.add(app_id)
            cfg_path = (cfg_root / "games" / f"{row.get('configpath')}.yml"
                        if row.get("configpath") else None)
            has_cfg = bool(cfg_path and cfg_path.is_file())
            cfg = _read_yaml(cfg_path) if has_cfg else {}
            prefix = _prefix_from_config(cfg)
            yield {
                "source": SOURCE,
                "app_id": str(app_id),
                "game_name": row.get("name") or str(app_id),
                "installed": bool(row.get("installed", 1)),
                "runner": row.get("runner"),
                "game_dir": row.get("directory"),
                "config_path": str(cfg_path) if cfg_path else None,
                "prefix_path": prefix,
                "user_dir": user_dir_for(prefix),
                "managed": _is_hooked(cfg),
            }

        # Fallback / extra: YAML files without a pga.db row.
        for yml in sorted((cfg_root / "games").glob("*.yml")):
            app_id = yml.stem
            slug = _slug_from_configpath(app_id)
            if slug in seen or app_id in seen:
                continue
            seen.add(slug)
            cfg = _read_yaml(yml)
            prefix = _prefix_from_config(cfg)
            yield {
                "source": SOURCE,
                "app_id": slug,
                "game_name": slug.replace("-", " ").title(),
                "installed": True,
                "runner": cfg.get("runner"),
                "game_dir": None,
                "config_path": str(yml),
                "prefix_path": prefix,
                "user_dir": user_dir_for(prefix),
                "managed": _is_hooked(cfg),
            }


def context_from_env() -> dict[str, Any] | None:
    """Lutris exports WINEPREFIX for the game -- match it against discovery.

    The normal path is the pre/post hook, which passes --source/--id
    explicitly. This is the safety net for wrapper-style setups.
    """
    prefix = os.environ.get("WINEPREFIX")
    if not prefix:
        return None
    real = os.path.realpath(prefix)
    for game in iter_games():
        known = game["prefix_path"]
        if known and os.path.realpath(known) == real:
            return game
    return None


# --- Hook injection ------------------------------------------------------
def hook_command(app_id: str, phase: str) -> str:
    return f'{paths.HOOK_SHIM} {phase} --source {SOURCE} --id {app_id}'


def _yaml_value(value: str) -> str:
    """Quote a scalar -- except booleans, which Lutris wants as booleans."""
    if value in ("true", "false"):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _edit_system_block(path: Path, updates: dict[str, str | None]) -> bool:
    """Set/remove keys inside the top-level `system:` block, line by line.

    `None` as a value removes the key. Returns True if the file changed.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False

    start = next((i for i, ln in enumerate(lines)
                  if ln.rstrip() == "system:"), None)

    if start is None:
        wanted = {k: v for k, v in updates.items() if v is not None}
        if not wanted:
            return False
        block = ["system:"] + [f"  {k}: {_yaml_value(v)}"
                               for k, v in wanted.items()]
        new_lines = lines + block
    else:
        end = len(lines)
        for i in range(start + 1, len(lines)):
            ln = lines[i]
            if ln.strip() and not ln[:1].isspace():
                end = i
                break
        block = lines[start + 1:end]
        indent = "  "
        for ln in block:
            if ln.strip():
                indent = ln[:len(ln) - len(ln.lstrip())]
                break

        for key, value in updates.items():
            idx = next((i for i, ln in enumerate(block)
                        if ln.strip().split(":")[0].strip() == key), None)
            if value is None:
                if idx is not None:
                    block.pop(idx)
            elif idx is not None:
                block[idx] = f"{indent}{key}: {_yaml_value(value)}"
            else:
                block.append(f"{indent}{key}: {_yaml_value(value)}")
        new_lines = lines[:start + 1] + block + lines[end:]

    text = "\n".join(new_lines) + "\n"
    if text == "\n".join(lines) + "\n":
        return False
    try:
        if path.exists():
            shutil.copy2(path, path.with_suffix(".yml.bak"))
        path.write_text(text, encoding="utf-8")
    except OSError:
        return False
    return True


def connect(app_id: str) -> HookResult:
    from ..core.i18n import _

    cfg_path = config_file_for(app_id)
    if not cfg_path:
        return HookResult(False, _("No Lutris config found for '{id}'.",
                                   id=app_id))
    _edit_system_block(cfg_path, {
        PRE_KEY: hook_command(app_id, "pre"),
        POST_KEY: hook_command(app_id, "post"),
        WAIT_KEY: "true",
    })
    return HookResult(True, _("Connected. Start the game as usual."),
                      config=str(cfg_path))


def disconnect(app_id: str) -> HookResult:
    from ..core.i18n import _

    cfg_path = config_file_for(app_id)
    if not cfg_path:
        return HookResult(False, _("No Lutris config found for '{id}'.",
                                   id=app_id))
    _edit_system_block(cfg_path, {PRE_KEY: None, POST_KEY: None,
                                  WAIT_KEY: None})
    return HookResult(True, _("Disconnected."), config=str(cfg_path))
