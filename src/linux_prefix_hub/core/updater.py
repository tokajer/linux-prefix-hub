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

**An update cannot be installed while we are running**, because installing it
means replacing the file we are executing. Velopack's helper is started with
`--waitPid <us>` and does its work the moment this process is gone. Every
awkward thing about `download()`/`finish()` below follows from that one fact,
and it is why closing the app *is* the install step rather than something that
happens after it.

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


def _source() -> Any:
    from velopack import GithubSource
    return GithubSource(repo_url(), None, False)


def _explicit_locator() -> Any | None:
    """Tell Velopack where the bundle is instead of letting it guess.

    It resolves its `UpdateNix` helper against the **working directory**
    ("UpdateNix does not exist at the expected path: usr/bin/UpdateNix"),
    which holds for as long as the bundled interpreter is the one running.
    The window is not: `__main__._reexec_gui` hands over to a *system*
    Python, and from there auto-locate lands outside the bundle and the
    UpdateManager cannot be built at all. Symptom: the terminal says "update
    available" and the window, same build, same minute, says "you are up to
    date".

    Returns None unless every piece is where we expect it. Guessing wrong
    here is worse than offering no update: these paths are what `update()`
    later overwrites.
    """
    import os
    from pathlib import Path

    appimage = os.environ.get("APPIMAGE")
    appdir = os.environ.get("APPDIR")
    if not appimage or not appdir:
        return None

    # APPDIR means two different things here. The AppImage runtime sets it to
    # the mount root; our own launcher then overwrites it with *its own*
    # directory (`<mount>/usr/bin`, see build-velopack.sh), which is where vpk
    # puts UpdateNix and sq.version. Look in both instead of picking one and
    # being wrong the next time either side moves -- getting this wrong is
    # silent, and its symptom is a window that cannot check for updates.
    for binary_dir in (Path(appdir), Path(appdir) / "usr" / "bin"):
        update_exe = binary_dir / "UpdateNix"
        manifest = binary_dir / "sq.version"
        if update_exe.exists() and manifest.exists():
            break
    else:
        return None

    try:
        from velopack import VelopackLocatorConfig
        return VelopackLocatorConfig(
            RootAppDir=appimage,          # on Linux: the AppImage file
            UpdateExePath=str(update_exe),
            PackagesDir=str(db.install_dir() / "packages"),
            ManifestPath=str(manifest),
            CurrentBinaryDir=str(binary_dir),
            IsPortable=True,
        )
    except Exception:
        return None


def _build_manager(source: Any, locator: Any | None = None) -> Any:
    """The one place that touches Velopack's constructor."""
    from velopack import UpdateManager
    return UpdateManager(source, None, locator)


def _manager() -> Any | None:
    """An UpdateManager for our release feed, or None if unusable.

    Velopack's own auto-locate first -- it is the tested path and works
    wherever the bundled interpreter runs. The explicit locator is the
    rescue for the window; see `_explicit_locator` for what it rescues from.
    """
    try:
        return _build_manager(_source())
    except Exception:
        pass                    # not packaged, no feed, or auto-locate lost
    locator = _explicit_locator()
    if locator is None:
        return None
    try:
        return _build_manager(_source(), locator)
    except Exception:
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


# --- Installing one, in two halves ---------------------------------------
# `download()` fetches, `finish()` hands over, and the caller ends the
# process. The SDK has a one-liner for all three -- and it is the reason this
# is written out longhand.
#
# `apply_updates_and_restart` calls `std::process::exit(0)` when it succeeds
# (it is right there in the wheel's own machine code, after the call to
# `wait_exit_then_apply_updates`). From the window that runs on a GTK worker
# thread, so the whole process dies mid-click: no message, no `done`
# callback, no clean GTK shutdown, and a tray icon left behind on the session
# bus. And when it *fails* it returns an error instead -- which the window
# then reported as "Update installed. Restart now", because the caller had no
# way to tell "there was nothing to install" from "it is installed".
#
# So: nothing here ever ends the process, and `ready`/`staged` say what
# actually happened rather than leaving `ok` to mean three different things.
_STAGED: dict[str, Any] = {}


def _pending_asset(manager: Any) -> Any | None:
    """A package that is downloaded and only waiting for us to exit.

    The fallback for a `finish()` that did not do the `download()` itself --
    a second window, or a `--update` after the download was interrupted.
    Asking Velopack beats a second network round trip.
    """
    ask = getattr(manager, "get_update_pending_restart", None)
    if ask is None:
        return None
    try:
        return ask()
    except Exception:
        return None


def download(force: bool = False) -> dict[str, Any]:
    """Fetch the newest release. Nothing we are running changes yet.

    Returns {ok, ready, version, message} (plus `skipped` for GearLever).
    **`ready` is the only key that means there is something to install** --
    "you are up to date" is `ok` too, and conflating the two is what put an
    "Update installed" dialog in front of users who had just been told they
    were current.
    """
    from .i18n import _

    _STAGED.clear()
    none = {"ok": False, "ready": False, "version": ""}

    if integrate.detect_gearlever():
        return {**none, "ok": True, "skipped": True,
                "message": _("Updates are handled by GearLever on this "
                             "system.")}
    if not available():
        return {**none,
                "message": _("Automatic updates are only available for the "
                             "AppImage build. Use pip/pipx to update this "
                             "installation.")}
    manager = _manager()
    if manager is None:
        return {**none,
                "message": _("This build cannot update itself. Download the "
                             "latest version from {url}.", url=repo_url())}

    try:
        info = manager.check_for_updates()
    except Exception as exc:
        return {**none, "message": _("Update failed: {error}",
                                     error=str(exc))}
    if info is None:
        return {**none, "ok": True, "version": __version__,
                "message": _("You are up to date ({version}).",
                             version=__version__)}

    version = str(info.TargetFullRelease.Version)
    if not force and not is_newer(version):
        return {**none, "ok": True, "version": __version__,
                "message": _("You are up to date ({version}).",
                             version=__version__)}
    try:
        manager.download_updates(info)
    except Exception as exc:
        return {**none, "version": version,
                "message": _("Update failed: {error}", error=str(exc))}

    _STAGED.update(manager=manager, info=info, version=version)
    return {"ok": True, "ready": True, "version": version,
            "message": _("Version {version} is ready to install.",
                         version=version)}


def finish(restart: bool = True) -> dict[str, Any]:
    """Hand the downloaded package over -- and come back.

    Velopack's helper waits for *this* process to end before it replaces the
    file we are executing, so this call changes nothing on its own: the
    caller has to exit, and until it does the app is still the old version.

    `restart` asks the helper to start us again afterwards. That is the only
    way there is to come back: by the time the new build exists on disk,
    nobody is left here to launch it. Starting something ourselves before
    exiting would start the *old* binary -- and run straight into our own
    single-instance lock, which is what the window used to do and why
    "Restart now" only ever closed the app.

    VERIFY-ON-DEVICE: that the helper's `--restart` really brings an AppImage
    back. If it does not, nothing is lost -- `app_hook()` applies what is
    waiting on the next start.
    """
    from .i18n import _

    manager = _STAGED.get("manager") or _manager()
    if manager is None:
        return {"ok": False,
                "message": _("This build cannot update itself. Download the "
                             "latest version from {url}.", url=repo_url())}

    info = _STAGED.get("info") or _pending_asset(manager)
    hand_over = getattr(manager, "wait_exit_then_apply_updates", None)
    if info is None or hand_over is None:
        return {"ok": False,
                "message": _("Nothing has been downloaded yet.")}

    version = str(_STAGED.get("version") or "")
    try:
        # Last thing we do while we still exist: the shims point at a fixed
        # path, and the new build has to find them the way it left them.
        integrate.install_shims()
        hand_over(info, silent=True, restart=restart)
    except Exception as exc:
        return {"ok": False, "message": _("Update failed: {error}",
                                          error=str(exc))}
    _STAGED.clear()
    return {"ok": True, "version": version,
            "message": _("Version {version} is installed as {app} closes.",
                         version=version, app=paths.APP_TITLE)}


def update(restart: bool = False) -> dict[str, Any]:
    """Both halves in one go -- what `--update` does.

    Returns {ok, staged, version, message}. `staged` says an update was
    really handed over; `ok` on its own can just as well mean there was
    nothing to do.

    `restart` defaults to False here because the caller is a terminal
    command: finishing it by opening a window is not what anyone typed.
    """
    got = download()
    if not got["ok"] or not got.get("ready"):
        return {**got, "staged": False}
    done = finish(restart=restart)
    return {**done, "staged": bool(done["ok"]),
            "version": got.get("version", "")}


def installed_path() -> Any:
    """Where the managed binary lives (unchanged concept, Velopack-owned)."""
    return paths.installed_appimage_path(db.install_dir())
