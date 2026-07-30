"""Self-update from GitHub releases.

Three ways this app can be updated, in order of preference:

  1. **GearLever** -- if it manages our AppImage, it also updates it (it reads
     the zsync update info we embed at build time). We stay out of the way.
  2. **appimageupdatetool** -- if the user has it, it does a delta update from
     the same embedded zsync info. Cheaper than a full download.
  3. **Us** -- ask the GitHub releases API, download the new AppImage, verify
     its SHA-256 against the release's SHA256SUMS, and swap it in atomically.

Only the AppImage build can self-update: a pipx/pip install belongs to pip,
and rewriting someone's site-packages behind their back would be rude.

Everything here is best-effort and offline-safe: no network, no problem, the
app just keeps running the version it has.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .. import __version__
from . import db, integrate, paths

GITHUB_OWNER = "tokajer"
GITHUB_REPO = "linux-prefix-hub"
API_URL = "https://api.github.com/repos/{owner}/{repo}/releases/latest"
USER_AGENT = f"{paths.APP_NAME}/{__version__}"

CHECK_INTERVAL = 24 * 3600      # once a day is plenty
NETWORK_TIMEOUT = 15


def _repo() -> tuple[str, str]:
    """Owner/repo, overridable in config.json (for forks)."""
    cfg = db.load_config()
    return (str(cfg.get("github_owner") or GITHUB_OWNER),
            str(cfg.get("github_repo") or GITHUB_REPO))


def _arch_tag() -> str:
    machine = platform.machine()
    return {"x86_64": "x86_64", "amd64": "x86_64",
            "aarch64": "aarch64", "arm64": "aarch64"}.get(machine, machine)


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.2.3' -> (1, 2, 3). Unparseable parts become 0."""
    cleaned = text.strip().lstrip("vV").split("-")[0].split("+")[0]
    parts: list[int] = []
    for chunk in cleaned.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(remote: str, local: str = __version__) -> bool:
    return parse_version(remote) > parse_version(local)


def _get(url: str, timeout: int = NETWORK_TIMEOUT) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def latest_release() -> dict[str, Any] | None:
    """The latest GitHub release, or None when offline/rate-limited."""
    owner, repo = _repo()
    try:
        raw = _get(API_URL.format(owner=owner, repo=repo))
        data = json.loads(raw.decode("utf-8"))
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("tag_name"):
        return None
    return data


def _asset(release: dict[str, Any], suffix: str) -> dict[str, Any] | None:
    arch = _arch_tag()
    assets = [a for a in release.get("assets", []) if isinstance(a, dict)]
    for asset in assets:
        name = str(asset.get("name", ""))
        if name.endswith(suffix) and arch in name:
            return asset
    for asset in assets:               # arch-less name is fine too
        if str(asset.get("name", "")).endswith(suffix):
            return asset
    return None


def check(force: bool = False) -> dict[str, Any]:
    """Is there a newer release? Cached for a day unless `force`.

    Returns {available, version, url, checked}. Never raises.
    """
    cfg = db.load_config()
    cached = cfg.get("update_check") or {}
    if (not force and cached.get("at")
            and time.time() - float(cached["at"]) < CHECK_INTERVAL):
        return {**cached.get("result", {}), "cached": True}

    release = latest_release()
    result: dict[str, Any] = {"available": False, "version": __version__,
                              "url": None}
    if release:
        tag = str(release["tag_name"])
        asset = _asset(release, ".AppImage")
        result = {
            "available": is_newer(tag),
            "version": tag.lstrip("vV"),
            "url": asset.get("browser_download_url") if asset else None,
            "page": release.get("html_url"),
        }
        db.set_config("update_check", {"at": time.time(), "result": result})
    return {**result, "cached": False}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _expected_sha(release: dict[str, Any], filename: str) -> str | None:
    """Look up `filename` in the release's SHA256SUMS asset, if it has one."""
    sums = _asset(release, "SHA256SUMS")
    if not sums or not sums.get("browser_download_url"):
        return None
    try:
        text = _get(str(sums["browser_download_url"])).decode("utf-8")
    except (urllib.error.URLError, OSError, UnicodeDecodeError):
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == filename:
            return parts[0]
    return None


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request,
                                timeout=NETWORK_TIMEOUT) as response, \
            destination.open("wb") as out:
        shutil.copyfileobj(response, out)


def _appimageupdatetool() -> str | None:
    return shutil.which("appimageupdatetool") or shutil.which("AppImageUpdate")


def update(force: bool = False) -> dict[str, Any]:
    """Download and install the newest AppImage. Returns {ok, message}."""
    from .i18n import _

    if integrate.detect_gearlever():
        return {"ok": True, "message": _("Updates are handled by GearLever on "
                                         "this system."), "skipped": True}

    target = paths.installed_appimage_path(db.install_dir())
    if not integrate.running_as_appimage() and not target.exists():
        return {"ok": False,
                "message": _("Automatic updates are only available for the "
                             "AppImage build. Use pip/pipx to update this "
                             "installation.")}

    state = check(force=True)
    if not state.get("available") and not force:
        return {"ok": True, "message": _("You are up to date ({version}).",
                                         version=__version__)}

    tool = _appimageupdatetool()
    running = integrate.running_as_appimage()
    if tool and running:
        try:
            proc = subprocess.run([tool, running], timeout=900, check=False)
            if proc.returncode == 0:
                return {"ok": True,
                        "message": _("Updated to {version}. Restart the app "
                                     "to use it.",
                                     version=state.get("version", "?"))}
        except (OSError, subprocess.SubprocessError):
            pass  # fall through to the plain download

    release = latest_release()
    if not release:
        return {"ok": False, "message": _("Could not reach GitHub.")}
    asset = _asset(release, ".AppImage")
    url = asset.get("browser_download_url") if asset else None
    if not url:
        return {"ok": False,
                "message": _("The latest release has no AppImage for this "
                             "system ({arch}).", arch=_arch_tag())}

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(dir=str(target.parent)))
    tmp_file = tmp_dir / str(asset.get("name", "update.AppImage"))
    try:
        _download(str(url), tmp_file)
        expected = _expected_sha(release, tmp_file.name)
        if expected and _sha256(tmp_file) != expected:
            return {"ok": False,
                    "message": _("Download did not match its checksum -- "
                                 "nothing was changed.")}
        tmp_file.chmod(tmp_file.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
                       | stat.S_IXOTH)
        os.replace(tmp_file, target)      # atomic within the same filesystem
    except (urllib.error.URLError, OSError) as exc:
        return {"ok": False, "message": _("Update failed: {error}",
                                          error=str(exc))}
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    integrate.install_shims()             # keep shims pointing at the binary
    return {"ok": True,
            "message": _("Updated to {version}. Restart the app to use it.",
                         version=state.get("version", "?"))}
