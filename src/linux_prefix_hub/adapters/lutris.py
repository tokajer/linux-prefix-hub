# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Lutris adapter: discovery via pga.db/YAML, hooks via prelaunch/postexit.

Lutris is the friendly source: it has real hooks, so unlike Steam there is no
manual step for the user. And it tells us the prefix outright -- we never have
to guess where it is.

Discovery is two-layered on purpose:
  1. `pga.db` (SQLite, stdlib) gives the real game names, runners and the
     config file name. This is the authoritative list.
  2. `games/<configpath>.yml` gives `game.prefix` -- the actual prefix path.
     That folder lives under the *data* root on current Lutris and under the
     *config* root on older ones, so both are searched (see `games_dirs`).
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
  - Verified against Lutris 0.5.23, which keeps everything under the data
    root. Re-check `games_dirs` if a future release moves the YAMLs again.
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

# The one we share: `system: env:` is where Lutris keeps the environment a
# game runs with, and the user has their own lines in there.
ENV_KEY = "env"


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p))


def config_roots() -> list[tuple[Path, Path]]:
    """Existing (config, data) root pairs.

    Either half is enough. Lutris moved the per-game YAMLs from the config
    root into the data root (0.5.23 keeps pga.db, lutris.conf *and*
    games/*.yml there and may never create ~/.config/lutris at all), so
    gating on the config root alone finds no games on a current install.
    """
    out = []
    for cfg, data in LUTRIS_ROOTS:
        c, d = _expand(cfg), _expand(data)
        if c.is_dir() or d.is_dir():
            out.append((c, d))
    return out


def games_dirs() -> list[Path]:
    """Every directory that may hold per-game YAMLs -- new layout first."""
    dirs: list[Path] = []
    for cfg_root, data_root in config_roots():
        for d in (data_root / "games", cfg_root / "games"):
            if d.is_dir() and d not in dirs:
                dirs.append(d)
    return dirs


def config_file_for(app_id: str) -> Path | None:
    """The game's YAML config (app_id is the Lutris slug/configpath)."""
    for games in games_dirs():
        direct = games / f"{app_id}.yml"
        if direct.is_file():
            return direct
        # configpath usually is "<slug>-<timestamp>"
        matches = sorted(games.glob(f"{app_id}-*.yml"))
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


def _config_path(configpath: str | None) -> Path | None:
    """Locate `<configpath>.yml` in whichever layout this Lutris uses."""
    if not configpath:
        return None
    for games in games_dirs():
        candidate = games / f"{configpath}.yml"
        if candidate.is_file():
            return candidate
    return None


def _is_steam_mirror(app_id: str, runner: Any, prefix: str | None) -> bool:
    """A Steam library entry Lutris only mirrors into its own list.

    Lutris imports the user's Steam library (`runner: steam`, config file
    `steam-<appid>-<timestamp>.yml`). Those entries have no prefix of their
    own -- the Steam adapter lists the same games with the real one -- so
    yielding them here would just offer a second, useless connect. Anything
    that does have a prefix stays, whatever its runner says.
    """
    if prefix:
        return False
    return runner == "steam" or app_id.startswith("steam-")


def iter_games() -> Iterator[dict[str, Any]]:
    seen: set[str] = set()
    # By file, not by slug: Lutris does not require the config file to be
    # named after the slug ("diablo-iv" lives in
    # "diablo-iv-battlenet-<ts>.yml"), so the fallback below would otherwise
    # yield the same game a second time under a name derived from the file.
    used: set[Path] = set()
    for _cfg_root, data_root in config_roots():
        for row in _games_from_pga(data_root):
            app_id = row.get("slug") or row.get("configpath")
            if not app_id or app_id in seen:
                continue
            seen.add(str(app_id))
            cfg_path = _config_path(row.get("configpath"))
            if cfg_path:
                used.add(cfg_path)
            cfg = _read_yaml(cfg_path) if cfg_path else {}
            prefix = _prefix_from_config(cfg)
            if _is_steam_mirror(str(app_id), row.get("runner"), prefix):
                continue
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
    for games in games_dirs():
        for yml in sorted(games.glob("*.yml")):
            app_id = yml.stem
            slug = _slug_from_configpath(app_id)
            if yml in used or slug in seen or app_id in seen:
                continue
            seen.add(slug)
            cfg = _read_yaml(yml)
            prefix = _prefix_from_config(cfg)
            if _is_steam_mirror(slug, cfg.get("runner"), prefix):
                continue
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


def _yaml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _yaml_value(value: str) -> str:
    """Quote a scalar -- except booleans, which Lutris wants as booleans."""
    if value in ("true", "false"):
        return value
    return _yaml_string(value)


def _block_end(lines: list[str], start: int, depth: int = 0) -> int:
    """Where the block opened at `start` ends: the next line beside it."""
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and len(line) - len(line.lstrip()) <= depth:
            return i
    return len(lines)


def _indent_of(block: list[str], fallback: str) -> str:
    for line in block:
        if line.strip():
            return line[:len(line) - len(line.lstrip())]
    return fallback


def _save(path: Path, lines: list[str], new_lines: list[str]) -> bool:
    """Write the file back, with a `.bak`. False if nothing changed."""
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
        end = _block_end(lines, start)
        block = lines[start + 1:end]
        indent = _indent_of(block, "  ")

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

    return _save(path, lines, new_lines)


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


# --- Extra options -------------------------------------------------------
def set_env(app_id: str, env: dict[str, str | None]) -> HookResult:
    """Put variables into the game's own `system: env:` block.

    This is Lutris' answer to what `core/gameopts.py` calls a profile: Lutris
    starts the game itself and hands it this environment, so there is nothing
    to copy and nothing for the user to point anywhere.

    `None` as a value removes the key, which is how a profile that lost a
    switch takes the variable back out. Only the keys named are touched --
    the rest of that block belongs to whoever wrote it.
    """
    from ..core.i18n import _

    cfg_path = config_file_for(app_id)
    if not cfg_path:
        if env and all(value is None for value in env.values()):
            # Taking variables out of a config that is not there any more is
            # already done -- the game left Lutris and took our lines with
            # it. Only a change with something to set has failed here.
            return HookResult(True, _("Nothing to change."))
        return HookResult(False, _("No Lutris config found for '{id}'.",
                                   id=app_id))
    replaced = _edit_env_block(cfg_path, env)
    return HookResult(True, _("Saved."), config=str(cfg_path),
                      replaced=replaced)


def _edit_env_block(path: Path,
                    updates: dict[str, str | None]) -> dict[str, str]:
    """The same line-by-line edit, one level down: `system:` -> `env:`.

    Returns what was standing in the lines it wrote over, so the caller can
    put those values back later (`core/gameopts._turn_on_launcher`). A key
    that was not there before is not in the answer.

    Values are always quoted, unlike `_edit_system_block`: an environment
    variable is text, and `WINEDEBUG: false` read back as a boolean is a
    variable the game never sees.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    wanted = {k: v for k, v in updates.items() if v is not None}

    start = next((i for i, ln in enumerate(lines)
                  if ln.rstrip() == "system:"), None)
    if start is None:
        if wanted:
            _save(path, lines, lines + ["system:", f"  {ENV_KEY}:"]
                  + [f"    {k}: {_yaml_string(v)}"
                     for k, v in wanted.items()])
        return {}

    end = _block_end(lines, start)
    block = lines[start + 1:end]
    indent = _indent_of(block, "  ")
    at = next((i for i, ln in enumerate(block)
               if ln.strip() == f"{ENV_KEY}:"), None)
    replaced: dict[str, str] = {}

    if at is None:
        if not wanted:
            return {}
        block += [f"{indent}{ENV_KEY}:"] + [
            f"{indent * 2}{k}: {_yaml_string(v)}" for k, v in wanted.items()]
    else:
        env_end = _block_end(block, at, len(indent))
        env_block = block[at + 1:env_end]
        env_indent = _indent_of(env_block, indent * 2)
        for key, value in updates.items():
            idx = next((i for i, ln in enumerate(env_block)
                        if ln.strip().split(":")[0].strip() == key), None)
            if idx is not None:
                replaced[key] = _plain_value(env_block[idx])
            if value is None:
                if idx is not None:
                    env_block.pop(idx)
            elif idx is not None:
                env_block[idx] = f"{env_indent}{key}: {_yaml_string(value)}"
            else:
                env_block.append(f"{env_indent}{key}: {_yaml_string(value)}")
        # An `env:` with nothing under it is not the same as no `env:` at
        # all -- Lutris reads it as null -- so the key goes with its last
        # entry.
        block = (block[:at + 1] + env_block + block[env_end:] if env_block
                 else block[:at] + block[env_end:])

    _save(path, lines, lines[:start + 1] + block + lines[end:])
    return replaced


def _plain_value(line: str) -> str:
    """`  DXVK_HUD: "fps"` -> `fps`. A variable is text, whatever it looks
    like, so nothing here turns `1` into a number or `false` into a bool.
    """
    value = line.partition(":")[2].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value
