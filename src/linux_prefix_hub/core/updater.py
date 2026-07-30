# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Self-update via Velopack.

Velopack owns both ends of this: `vpk pack` builds the AppImage (see
`packaging/build-velopack.sh`) and the `velopack` SDK talks to the release
feed, downloads, verifies and swaps the binary. We keep none of that logic
ourselves -- the previous hand-rolled GitHub/zsync path is gone on purpose.

Two things stay true regardless:

  1. **GearLever wins.** If it manages our AppImage, it does the updating and
     we do not touch the file underneath it.
  2. **Offline is not an error.** No network, no problem: the app keeps running
     the version it has. Nothing in here raises.

`app_hook()` must run once at process start, before anything else. Velopack
uses it to finish an update that is waiting and to fire first-run/restart
callbacks; skipping it means updates never get applied.

The `velopack` wheel is a hard dependency of the AppImage build. A plain
`pip install linux-prefix-hub` without it still works -- every entry point
here degrades to an honest "not available" message.
"""
from __future__ import annotations

import time
from typing import Any

from .. import __version__
from . import db, integrate, paths

GITHUB_OWNER = "tokajer"
GITHUB_REPO = "linux-prefix-hub"
REPO_URL = "https://github.com/{owner}/{repo}"

CHECK_INTERVAL = 24 * 3600      # once a day is plenty


def repo_url() -> str:
    """The release feed. Overridable in config.json (for forks)."""
    cfg = db.load_config()
    owner = str(cfg.get("github_owner") or GITHUB_OWNER)
    repo = str(cfg.get("github_repo") or GITHUB_REPO)
    return str(cfg.get("update_url")
               or REPO_URL.format(owner=owner, repo=repo))


def available() -> bool:
    """Is the Velopack SDK importable?"""
    try:
        import velopack  # noqa: F401
    except ImportError:
        return False
    return True


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.2.3' -> (1, 2, 3). Unparseable parts become 0."""
    cleaned = text.strip().lstrip("vV").split("-")[0].split("+")[0]
    parts: list[int] = []
    for chunk in cleaned.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(remote: str, local: str | None = None) -> bool:
    """Is `remote` a newer release than what we are?

    `local` is read at call time, not bound as a default: the version now
    comes from the release tag (see the package's `__init__`), so the one
    place that must not freeze an early copy of it is the comparison.
    """
    return parse_version(remote) > parse_version(local or __version__)


def app_hook() -> None:
    """Velopack's startup hook. Best effort, never fatal.

    Only meaningful in the packaged build: outside it there is no update to
    finish, and Velopack's native layer logs a "NotInstalled" complaint
    straight to stderr that no Python `except` can swallow. So we do not
    call it at all unless we are the AppImage.
    """
    if not integrate.running_as_appimage():
        return
    try:
        from velopack import App
        App().set_auto_apply_on_startup(True).run()
    except Exception:
        pass


def _manager() -> Any | None:
    """An UpdateManager for our release feed, or None if unusable."""
    try:
        from velopack import GithubSource, UpdateManager
        return UpdateManager(GithubSource(repo_url(), None, False))
    except Exception:
        # Not installed, not a packaged build, or no feed configured.
        return None


def check(force: bool = False) -> dict[str, Any]:
    """Is there a newer release? Cached for a day unless `force`.

    Returns {available, version, reason, cached}. Never raises.

    `reason` is the part that took a bug report to get right: "" means we
    really asked and really are current, "unavailable" means this build has
    no updater in it (a pip install, or `build-appimage.sh`, which ships no
    velopack wheel on purpose), "unreachable" means the feed did not answer.
    Without it every one of those looks like "you are up to date", which is
    the one answer a user cannot argue with -- and the two failure cases are
    exactly when they should.
    """
    cfg = db.load_config()
    cached = cfg.get("update_check") or {}
    if (not force and cached.get("at")
            and time.time() - float(cached["at"]) < CHECK_INTERVAL):
        return {**cached.get("result", {}), "cached": True}

    current: dict[str, Any] = {"available": False, "version": __version__,
                               "reason": ""}
    manager = _manager()
    if manager is None:
        return {**current, "reason": "unavailable", "cached": False}

    try:
        info = manager.check_for_updates()
    except Exception:
        return {**current, "reason": "unreachable", "cached": False}

    # Velopack answers None for "nothing newer than you" -- that is a real
    # answer and worth caching, unlike the two returns above.
    result = current if info is None else {
        "available": is_newer(str(info.TargetFullRelease.Version)),
        "version": str(info.TargetFullRelease.Version),
        "reason": "",
    }
    db.set_config("update_check", {"at": time.time(), "result": result})
    return {**result, "cached": False}


def update(force: bool = False) -> dict[str, Any]:
    """Download and apply the newest release. Returns {ok, message}.

    On success the app restarts into the new version, so this call does not
    return -- anything after `apply_updates_and_restart` is an error path.
    """
    from .i18n import _

    if integrate.detect_gearlever():
        return {"ok": True, "message": _("Updates are handled by GearLever on "
                                         "this system."), "skipped": True}

    if not available():
        return {"ok": False,
                "message": _("Automatic updates are only available for the "
                             "AppImage build. Use pip/pipx to update this "
                             "installation.")}

    manager = _manager()
    if manager is None:
        return {"ok": False,
                "message": _("This build cannot update itself. Download the "
                             "latest version from {url}.", url=repo_url())}

    try:
        info = manager.check_for_updates()
    except Exception as exc:
        return {"ok": False, "message": _("Update failed: {error}",
                                          error=str(exc))}
    if info is None and not force:
        return {"ok": True, "message": _("You are up to date ({version}).",
                                         version=__version__)}
    if info is None:
        return {"ok": False, "message": _("Could not reach GitHub.")}

    version = str(info.TargetFullRelease.Version)
    try:
        manager.download_updates(info)
        # Keep the shims pointing at the binary before we hand over control.
        integrate.install_shims()
        manager.apply_updates_and_restart(info)
    except Exception as exc:
        return {"ok": False, "message": _("Update failed: {error}",
                                          error=str(exc))}
    return {"ok": True,
            "message": _("Updated to {version}. Restart the app to use it.",
                         version=version)}


def installed_path() -> Any:
    """Where the managed binary lives (unchanged concept, Velopack-owned)."""
    return paths.installed_appimage_path(db.install_dir())
