# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Steam adapter: installed games, Proton prefixes, launch-option hook.

Covers:
  - multi-library (libraryfolders.vdf) -> every steamapps location
  - appmanifest_*.acf -> appid, name, StateFlags, installdir
  - compatdata/<appid>/pfx -> Proton prefix (only exists after the 1st launch!)
  - the user folder inside the prefix (Proton: almost always 'steamuser')

StateFlags bits (the relevant ones):
  4     = fully installed
  1026  = update/download running
We treat "installed & ready to play" as (StateFlags & 4).

Steam is the one source that cannot hook itself completely: its config UI is a
black box, so the launch-options step is either written directly into
localconfig.vdf (only safe while Steam is closed) or handed to the user.

VERIFY-ON-DEVICE:
  - Steam roots vary by distro/Flatpak. The list below covers the common
    cases; check on your system and extend if needed.
  - Verify the StateFlags semantics against real manifests (Valve does not
    document them officially).
  - localconfig.vdf writing: Steam overwrites the file when it exits, so we
    refuse to write while Steam is running. Test the round-trip once with a
    game you do not mind losing launch options on (we keep a .bak).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..core import paths, vdf
from .base import HookResult, user_dir_for  # noqa: F401  (re-export)

SOURCE = "steam"

# Steam Play tools (Proton, the Linux runtimes) ship a toolmanifest.vdf in
# their install dir. Verified against a real 21-manifest library: the marker
# catches every Proton/runtime entry and no actual game. No field inside
# appmanifest_*.acf distinguishes them -- the key sets are identical.
TOOL_MANIFEST = "toolmanifest.vdf"

# Depot-only helper apps that carry no toolmanifest. Valve installs these
# into libraries alongside games, but they are not playable.
NON_GAME_APPIDS = {"228980"}  # Steamworks Common Redistributables

# Common Steam roots (native + Flatpak). realpath de-duplicates symlinks.
STEAM_ROOT_CANDIDATES = [
    "~/.steam/steam",
    "~/.local/share/Steam",
    "~/.steam/root",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",  # Flatpak
    "~/snap/steam/common/.local/share/Steam",                 # Snap
]


def _expand(p: str) -> Path:
    return Path(os.path.expanduser(p))


def find_steam_roots() -> list[Path]:
    seen: set[str] = set()
    roots: list[Path] = []
    for cand in STEAM_ROOT_CANDIDATES:
        path = _expand(cand)
        if path.exists():
            real = os.path.realpath(path)
            if real not in seen:
                seen.add(real)
                roots.append(Path(real))
    return roots


def find_library_dirs() -> list[Path]:
    """Every steamapps directory across all libraries/disks."""
    libs: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        if not path.exists():
            return
        real = os.path.realpath(path)
        if real not in seen:
            seen.add(real)
            libs.append(Path(real))

    for root in find_steam_roots():
        steamapps = root / "steamapps"
        add(steamapps)

        # Additional libraries from libraryfolders.vdf
        lf = steamapps / "libraryfolders.vdf"
        if not lf.exists():
            continue
        try:
            data = vdf.loads(lf.read_text(encoding="utf-8", errors="ignore"))
            for _key, entry in data.get("libraryfolders", {}).items():
                if isinstance(entry, dict) and "path" in entry:
                    add(Path(entry["path"]) / "steamapps")
        except Exception:
            # Defensive: a broken vdf must not break discovery.
            pass
    return libs


def is_tool(appid: str, game_dir: Path | None) -> bool:
    """Is this a Steam Play tool/runtime rather than a game?"""
    if appid in NON_GAME_APPIDS:
        return True
    return bool(game_dir and (game_dir / TOOL_MANIFEST).exists())


def iter_games() -> Iterator[dict[str, Any]]:
    """Yield one dict per game found (across all libraries).

    Tools and runtimes are skipped, and an appid is yielded once even when
    several libraries carry a manifest for it -- a shared library folder on a
    second disk really does produce duplicate manifests.
    """
    found: dict[str, dict[str, Any]] = {}

    for steamapps in find_library_dirs():
        for acf in steamapps.glob("appmanifest_*.acf"):
            try:
                data = vdf.loads(acf.read_text(encoding="utf-8",
                                               errors="ignore"))
            except Exception:
                continue
            state = data.get("AppState", {})
            appid = state.get("appid")
            if not appid:
                continue
            try:
                flags = int(state.get("StateFlags", "0"))
            except ValueError:
                flags = 0

            installdir = state.get("installdir", "")
            game_dir = (steamapps / "common" / installdir
                        if installdir else None)
            if is_tool(appid, game_dir):
                continue
            prefix = _prefix_for(steamapps, appid)

            entry = {
                "source": SOURCE,
                "app_id": appid,
                "game_name": state.get("name", f"App {appid}"),
                "installed": bool(flags & 4),
                "state_flags": flags,
                "steamapps": str(steamapps),
                "game_dir": str(game_dir) if game_dir else None,
                "prefix_path": prefix,
                "user_dir": user_dir_for(prefix),
                "managed": is_connected(appid),
            }
            previous = found.get(appid)
            # Keep the copy that is actually installed, and prefer one that
            # already has a prefix -- that is the library being played from.
            if previous is None or (
                    (entry["installed"], bool(prefix))
                    > (previous["installed"], bool(previous["prefix_path"]))):
                found[appid] = entry

    yield from found.values()


# Kept for older call sites / scripts.
iter_installed_games = iter_games


def _prefix_for(steamapps: Path, appid: str) -> str | None:
    """Proton prefix path; None if the game was never started (no pfx)."""
    pfx = steamapps / "compatdata" / appid / "pfx"
    return str(pfx) if pfx.exists() else None


def context_from_env() -> dict[str, Any] | None:
    """Prefix + user dir from SteamAppId (Steam sets it for the game)."""
    # Steam really does spell it in mixed case -- not our choice.
    appid = (os.environ.get("SteamAppId")  # noqa: SIM112
             or os.environ.get("STEAM_COMPAT_APP_ID"))
    if not appid:
        return None
    for game in iter_games():
        if (game["app_id"] == appid and game["prefix_path"]
                and game["user_dir"]):
            return game
    return None


# --- Launch-options hook -------------------------------------------------
def launch_options() -> str:
    """The string the user (or we) put into Steam's launch options."""
    return f'"{paths.WRAPPER_SHIM}" %command%'


def localconfig_files() -> list[Path]:
    """localconfig.vdf of every Steam account on this machine."""
    found: list[Path] = []
    for root in find_steam_roots():
        userdata = root / "userdata"
        if not userdata.is_dir():
            continue
        for user in userdata.iterdir():
            cfg = user / "config" / "localconfig.vdf"
            if cfg.is_file():
                found.append(cfg)
    return found


def steam_is_running() -> bool:
    """True if a Steam client process is alive.

    Writing localconfig.vdf while Steam runs is pointless: Steam keeps the
    config in memory and overwrites the file when it exits.
    """
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                comm = (entry / "comm").read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if comm in ("steam", "steamwebhelper", "steam.sh"):
                return True
    except OSError:
        pass
    return False


def _descend(data: dict[str, Any], *keys: str) -> dict[str, Any] | None:
    """Walk nested KeyValues case-insensitively (Steam varies the casing)."""
    node = data
    for key in keys:
        if not isinstance(node, dict):
            return None
        match = next((k for k in node if k.lower() == key.lower()), None)
        if match is None:
            return None
        node = node[match]
    return node if isinstance(node, dict) else None


def _apps_node(data: dict[str, Any]) -> dict[str, Any] | None:
    return _descend(data, "UserLocalConfigStore", "Software", "Valve",
                    "Steam", "apps")


def is_connected(app_id: str) -> bool:
    """Is our wrapper already in this game's launch options?"""
    shim = str(paths.WRAPPER_SHIM)
    for cfg in localconfig_files():
        try:
            data = vdf.loads(cfg.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        apps = _apps_node(data) or {}
        app = apps.get(str(app_id))
        if isinstance(app, dict) and shim in str(app.get("LaunchOptions", "")):
            return True
    return False


def _copy_to_clipboard(text: str) -> bool:
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"]):
        if not shutil.which(cmd[0]):
            continue
        try:
            subprocess.run(cmd, input=text.encode(), check=False, timeout=5)
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def _write_launch_options(app_id: str, value: str | None) -> int:
    """Set (or clear) LaunchOptions in every localconfig.vdf. Returns count."""
    written = 0
    for cfg in localconfig_files():
        try:
            data = vdf.loads(cfg.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        apps = _apps_node(data)
        if apps is None:
            continue
        app = apps.get(str(app_id))
        if not isinstance(app, dict):
            if value is None:
                continue          # nothing to clear
            app = {}
            apps[str(app_id)] = app
        if value is None:
            app.pop("LaunchOptions", None)
        else:
            app["LaunchOptions"] = value
        try:
            shutil.copy2(cfg, cfg.with_suffix(".vdf.bak"))
            cfg.write_text(vdf.dumps(data), encoding="utf-8")
            written += 1
        except OSError:
            continue
    return written


def connect(app_id: str) -> HookResult:
    """Install the launch hook for one Steam game.

    Steam closed -> we write it. Steam running -> the user does it (we put the
    string on the clipboard), because Steam would overwrite our change.
    """
    from ..core.i18n import _

    if is_connected(app_id):
        return HookResult(True, _("Already connected."),
                          launch_options=launch_options())

    opts = launch_options()
    if steam_is_running():
        copied = _copy_to_clipboard(opts)
        msg = _("Steam is running. Close Steam and try again, or paste this "
                "into the game's launch options:\n  {opts}", opts=opts)
        if copied:
            msg += "\n" + _("(copied to clipboard)")
        return HookResult(False, msg, manual=True, launch_options=opts)

    count = _write_launch_options(app_id, opts)
    if count:
        return HookResult(True, _("Connected. Start the game as usual."),
                          launch_options=opts)
    return HookResult(False,
                      _("Could not find a Steam account config for this game. "
                        "Paste this into the launch options:\n  {opts}",
                        opts=opts),
                      manual=True, launch_options=opts)


def disconnect(app_id: str) -> HookResult:
    from ..core.i18n import _

    if steam_is_running():
        return HookResult(False,
                          _("Steam is running. Close Steam and try again."),
                          manual=True)
    _write_launch_options(app_id, None)
    return HookResult(True, _("Disconnected."))
