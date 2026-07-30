"""Self-update: version maths, caching, checksum verification.

No test here touches the network -- `latest_release` and the download are
replaced with fakes.
"""
from __future__ import annotations

import hashlib

import pytest

ASSET = "LinuxPrefixHub-9.9.9-x86_64.AppImage"


def _release(tag="v9.9.9", with_sums=True):
    assets = [{"name": ASSET,
               "browser_download_url": f"https://example.invalid/{ASSET}"}]
    if with_sums:
        assets.append({"name": "SHA256SUMS",
                       "browser_download_url":
                           "https://example.invalid/SHA256SUMS"})
    return {"tag_name": tag, "assets": assets,
            "html_url": "https://example.invalid/release"}


@pytest.fixture
def installed_appimage(isolated_home):
    """Pretend we are an installed AppImage, not a pip install."""
    from linux_prefix_hub.core import db, paths
    target = paths.installed_appimage_path(db.install_dir())
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"OLD")
    return target


# --- versions ------------------------------------------------------------
@pytest.mark.parametrize("remote,local,expected", [
    ("v0.3.0", "0.2.0", True),
    ("0.2.1", "0.2.0", True),
    ("v0.2.0", "0.2.0", False),
    ("0.1.9", "0.2.0", False),
    ("v1.0.0-beta", "0.9.9", True),
    ("garbage", "0.2.0", False),
])
def test_is_newer(remote, local, expected):
    from linux_prefix_hub.core import updater
    assert updater.is_newer(remote, local) is expected


# --- check ---------------------------------------------------------------
def test_check_reports_and_caches(monkeypatch):
    from linux_prefix_hub.core import db, updater
    calls = []

    def fake_latest():
        calls.append(1)
        return _release()

    monkeypatch.setattr(updater, "latest_release", fake_latest)

    state = updater.check(force=True)
    assert state["available"] is True and state["version"] == "9.9.9"
    assert db.load_config()["update_check"]["result"]["available"] is True

    cached = updater.check()          # inside the interval -> no second call
    assert cached["cached"] is True and len(calls) == 1


def test_check_survives_being_offline(monkeypatch):
    from linux_prefix_hub.core import updater
    monkeypatch.setattr(updater, "latest_release", lambda: None)
    assert updater.check(force=True)["available"] is False


# --- update --------------------------------------------------------------
def test_update_refuses_for_pip_installs(monkeypatch):
    from linux_prefix_hub.core import updater
    monkeypatch.setattr(updater.integrate, "running_as_appimage",
                        lambda: None)
    result = updater.update()
    assert not result["ok"]
    assert "AppImage build" in result["message"]


def test_update_defers_to_gearlever(monkeypatch, installed_appimage):
    from linux_prefix_hub.core import updater
    monkeypatch.setattr(updater.integrate, "detect_gearlever",
                        lambda: installed_appimage)
    result = updater.update()
    assert result["ok"] and result["skipped"]


def _stub_download(monkeypatch, payload: bytes, sums: bytes | None):
    from linux_prefix_hub.core import updater
    monkeypatch.setattr(updater, "latest_release", lambda: _release())
    monkeypatch.setattr(updater, "_appimageupdatetool", lambda: None)
    monkeypatch.setattr(updater, "_download",
                        lambda url, dest: dest.write_bytes(payload))
    monkeypatch.setattr(updater, "_get",
                        lambda url, timeout=15: sums if sums else b"")


def test_update_installs_the_new_appimage(monkeypatch, installed_appimage):
    from linux_prefix_hub.core import updater
    payload = b"NEW-APPIMAGE-BYTES"
    digest = hashlib.sha256(payload).hexdigest()
    _stub_download(monkeypatch, payload,
                   f"{digest}  {ASSET}\n".encode())

    result = updater.update()
    assert result["ok"], result["message"]
    assert installed_appimage.read_bytes() == payload
    assert installed_appimage.stat().st_mode & 0o111       # executable


def test_a_bad_checksum_changes_nothing(monkeypatch, installed_appimage):
    from linux_prefix_hub.core import updater
    _stub_download(monkeypatch, b"TAMPERED",
                   f"{'0' * 64}  {ASSET}\n".encode())

    result = updater.update()
    assert not result["ok"]
    assert "checksum" in result["message"]
    assert installed_appimage.read_bytes() == b"OLD"


def test_update_without_sha256sums_still_works(monkeypatch,
                                               installed_appimage):
    from linux_prefix_hub.core import updater
    monkeypatch.setattr(updater, "latest_release",
                        lambda: _release(with_sums=False))
    monkeypatch.setattr(updater, "_appimageupdatetool", lambda: None)
    monkeypatch.setattr(updater, "_download",
                        lambda url, dest: dest.write_bytes(b"NEW"))
    assert updater.update()["ok"]
    assert installed_appimage.read_bytes() == b"NEW"
