"""Heroic adapter: discovery from GamesConfig JSON, hook via wrapperOptions.

Heroic stores one JSON file per game under `GamesConfig/<appName>.json` and
keeps the human-readable titles in its store caches. The per-game file is the
authority for the prefix; the caches are only there so the user sees "ELDEN
RING" instead of an Epic app name like `9a1b2c3d...`.

Hooking is the nice case: Heroic supports a wrapper around the game command,
exactly like Steam's `%command%`. So we reuse the very same wrapper entry
point -- no pre/post hook pair needed.

Because Heroic reshuffles its config layout between major versions, discovery
here is written to be *shape-tolerant*: instead of hard-coding cache paths we
walk the JSON and pick up anything that looks like {app_name, title}. A layout
change then costs us a nicer title, not the whole adapter.

VERIFY-ON-DEVICE:
  - The wrapper key is `wrapperOptions: [{exe, args}]` in Heroic 2.x. Check it
    against your installed version (Settings -> Advanced -> Wrapper command)
    and adjust WRAPPER_KEY if your version differs.
  - Heroic must be closed while we write GamesConfig JSON, otherwise it may
    overwrite the file from memory. We keep a `.bak`.
"""
from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from ..core import paths
from .base import HookResult, is_prefix, user_dir_for

SOURCE = "heroic"

HEROIC_ROOTS = [
    "~/.config/heroic",
    "~/.var/app/com.heroicgameslauncher.hgl/config/heroic",
]

WRAPPER_KEY = "wrapperOptions"
PREFIX_KEYS = ("winePrefix", "prefixInstallPath", "wine_prefix")


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p))


def config_roots() -> list[Path]:
    return [p for p in (_expand(r) for r in HEROIC_ROOTS) if p.is_dir()]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return None


def games_config_dir(root: Path) -> Path:
    return root / "GamesConfig"


def config_file_for(app_id: str) -> Path | None:
    for root in config_roots():
        path = games_config_dir(root) / f"{app_id}.json"
        if path.is_file():
            return path
    return None


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    """Yield every dict inside a JSON structure."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _library_index(root: Path) -> dict[str, dict[str, Any]]:
    """appName -> {title, installed, install_path} from any store cache.

    Shape-tolerant on purpose: we scan the known cache directories and accept
    every object that carries an app name and a title.
    """
    index: dict[str, dict[str, Any]] = {}
    candidates: list[Path] = []
    for sub in ("store_cache", "store", "gog_store", "sideload_apps", "."):
        d = root / sub
        if d.is_dir():
            candidates += sorted(d.glob("*.json"))
    for path in candidates:
        for obj in _walk(_read_json(path)):
            app_name = obj.get("app_name") or obj.get("appName")
            title = obj.get("title") or obj.get("name")
            if not app_name or not isinstance(app_name, str) or not title:
                continue
            entry = index.setdefault(app_name, {})
            entry.setdefault("title", str(title))
            install = obj.get("install")
            if obj.get("is_installed") or isinstance(install, dict):
                entry["installed"] = bool(
                    obj.get("is_installed", bool(install)))
                if isinstance(install, dict):
                    entry.setdefault("install_path",
                                     install.get("install_path"))
    return index


def _default_prefix_root(root: Path) -> Path | None:
    cfg = _read_json(root / "config.json")
    if not isinstance(cfg, dict):
        return None
    settings = cfg.get("defaultSettings")
    if isinstance(settings, dict) and settings.get("defaultWinePrefix"):
        return _expand(str(settings["defaultWinePrefix"]))
    return None


def _prefix_from_game_config(cfg: dict[str, Any]) -> str | None:
    for key in PREFIX_KEYS:
        value = cfg.get(key)
        if value:
            return str(_expand(str(value)))
    return None


def _is_hooked(cfg: dict[str, Any]) -> bool:
    shim = str(paths.WRAPPER_SHIM)
    for entry in cfg.get(WRAPPER_KEY) or []:
        if isinstance(entry, dict) and shim in str(entry.get("exe", "")):
            return True
        if isinstance(entry, str) and shim in entry:
            return True
    return False


def iter_games() -> Iterator[dict[str, Any]]:
    for root in config_roots():
        index = _library_index(root)
        default_root = _default_prefix_root(root)
        gc_dir = games_config_dir(root)
        seen: set[str] = set()

        for path in sorted(gc_dir.glob("*.json")) if gc_dir.is_dir() else []:
            app_id = path.stem
            if app_id in ("config", "backup"):
                continue
            data = _read_json(path)
            if not isinstance(data, dict):
                continue
            # The file wraps its settings in the app name; tolerate both.
            nested = isinstance(data.get(app_id), dict)
            cfg = data[app_id] if nested else data
            meta = index.get(app_id, {})
            prefix = _prefix_from_game_config(cfg)
            title = meta.get("title", app_id)
            if not prefix and default_root:
                guess = default_root / str(title)
                prefix = str(guess) if is_prefix(guess) else None
            seen.add(app_id)
            yield {
                "source": SOURCE,
                "app_id": app_id,
                "game_name": title,
                "installed": bool(meta.get("installed", True)),
                "game_dir": meta.get("install_path"),
                "config_path": str(path),
                "prefix_path": prefix,
                "user_dir": user_dir_for(prefix),
                "managed": _is_hooked(cfg),
            }

        # Installed games that never got a per-game config file.
        for app_id, meta in index.items():
            if app_id in seen or not meta.get("installed"):
                continue
            prefix = None
            if default_root:
                guess = default_root / str(meta.get("title", app_id))
                prefix = str(guess) if is_prefix(guess) else None
            yield {
                "source": SOURCE,
                "app_id": app_id,
                "game_name": meta.get("title", app_id),
                "installed": True,
                "game_dir": meta.get("install_path"),
                "config_path": None,
                "prefix_path": prefix,
                "user_dir": user_dir_for(prefix),
                "managed": False,
            }


def context_from_env() -> dict[str, Any] | None:
    """Heroic exports WINEPREFIX/STEAM_COMPAT_DATA_PATH for the game."""
    prefix = os.environ.get("WINEPREFIX")
    compat = os.environ.get("STEAM_COMPAT_DATA_PATH")
    if not prefix and compat:
        prefix = str(Path(compat) / "pfx")
    if not prefix:
        return None
    real = os.path.realpath(prefix)
    for game in iter_games():
        known = game["prefix_path"]
        if known and os.path.realpath(known) == real:
            return game
    return None


# --- Hook injection ------------------------------------------------------
def _write_game_config(path: Path, app_id: str,
                       mutate: Callable[[dict[str, Any]], None]) -> bool:
    data = _read_json(path)
    if not isinstance(data, dict):
        return False
    cfg = data[app_id] if isinstance(data.get(app_id), dict) else data
    mutate(cfg)
    try:
        shutil.copy2(path, path.with_suffix(".json.bak"))
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    except OSError:
        return False
    return True


def connect(app_id: str) -> HookResult:
    from ..core.i18n import _

    path = config_file_for(app_id)
    if not path:
        return HookResult(False, _("No Heroic config found for '{id}'. Open "
                                   "the game's settings in Heroic once, then "
                                   "try again.", id=app_id))

    def add_wrapper(cfg: dict[str, Any]) -> None:
        entries = [e for e in (cfg.get(WRAPPER_KEY) or [])
                   if not (isinstance(e, dict)
                           and str(paths.WRAPPER_SHIM) in str(e.get("exe")))]
        entries.append({"exe": str(paths.WRAPPER_SHIM), "args": ""})
        cfg[WRAPPER_KEY] = entries

    if not _write_game_config(path, app_id, add_wrapper):
        return HookResult(False, _("Could not write the Heroic config."))
    return HookResult(True, _("Connected. Start the game as usual."),
                      config=str(path))


def disconnect(app_id: str) -> HookResult:
    from ..core.i18n import _

    path = config_file_for(app_id)
    if not path:
        return HookResult(False, _("No Heroic config found for '{id}'.",
                                   id=app_id))

    def drop_wrapper(cfg: dict[str, Any]) -> None:
        cfg[WRAPPER_KEY] = [
            e for e in (cfg.get(WRAPPER_KEY) or [])
            if not (isinstance(e, dict)
                    and str(paths.WRAPPER_SHIM) in str(e.get("exe")))
        ]

    if not _write_game_config(path, app_id, drop_wrapper):
        return HookResult(False, _("Could not write the Heroic config."))
    return HookResult(True, _("Disconnected."), config=str(path))
