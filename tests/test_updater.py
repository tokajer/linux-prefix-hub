# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Self-update: version maths, caching, and the Velopack hand-over.

No test here touches the network or the real Velopack SDK. `_manager` is
replaced with a fake that records what the updater asked it to do, so these
tests pin down our decisions -- not PyO3's behaviour.
"""
from __future__ import annotations

import pytest


class _Asset:
    def __init__(self, version: str) -> None:
        self.Version = version


class _Info:
    def __init__(self, version: str) -> None:
        self.TargetFullRelease = _Asset(version)


class FakeManager:
    """Stands in for velopack.UpdateManager."""

    def __init__(self, version: str | None = "9.9.9", *,
                 fail_on: str | None = None) -> None:
        self._version = version
        self._fail_on = fail_on
        self.calls: list[str] = []

    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        if self._fail_on == name:
            raise RuntimeError("boom")

    def check_for_updates(self):
        self._maybe_fail("check")
        return _Info(self._version) if self._version else None

    def download_updates(self, info, progress_callback=None):
        self._maybe_fail("download")

    def apply_updates_and_restart(self, info):
        self._maybe_fail("apply")


@pytest.fixture
def fake_velopack(monkeypatch):
    """Install a FakeManager and report the SDK as present."""
    from linux_prefix_hub.core import updater

    def install(manager):
        monkeypatch.setattr(updater, "_manager", lambda: manager)
        monkeypatch.setattr(updater, "available", lambda: True)
        monkeypatch.setattr(updater.integrate, "detect_gearlever",
                            lambda: None)
        return manager

    return install


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


def test_parse_version_is_forgiving():
    from linux_prefix_hub.core import updater
    assert updater.parse_version("v1.2.3") == (1, 2, 3)
    assert updater.parse_version("2.0") == (2, 0)
    assert updater.parse_version("") == (0,)


# --- the release feed ----------------------------------------------------
def test_repo_url_defaults_to_our_repo():
    from linux_prefix_hub.core import updater
    assert updater.repo_url() == \
        "https://github.com/tokajer/linux-prefix-hub"


def test_repo_url_is_overridable_for_forks():
    from linux_prefix_hub.core import db, updater
    db.set_config("github_owner", "someone")
    assert updater.repo_url() == "https://github.com/someone/linux-prefix-hub"
    db.set_config("update_url", "https://example.invalid/feed")
    assert updater.repo_url() == "https://example.invalid/feed"


# --- check ---------------------------------------------------------------
def test_check_reports_and_caches(fake_velopack):
    from linux_prefix_hub.core import db, updater
    manager = fake_velopack(FakeManager("9.9.9"))

    state = updater.check(force=True)
    assert state["available"] is True
    assert state["version"] == "9.9.9"
    assert db.load_config()["update_check"]["result"]["version"] == "9.9.9"

    # Second call inside the interval must not ask again.
    cached = updater.check()
    assert cached["cached"] is True
    assert manager.calls == ["check"]


def test_check_ignores_an_older_remote_version(fake_velopack, monkeypatch):
    """Pinned on purpose: our own version comes from the release tag, and a
    checkout has none -- so the test must say what "we" are."""
    from linux_prefix_hub.core import updater
    monkeypatch.setattr(updater, "__version__", "1.2.3")
    fake_velopack(FakeManager("0.0.1"))
    assert updater.check(force=True)["available"] is False


def test_a_checkout_counts_as_older_than_any_release(fake_velopack):
    """A working copy is not a release, so everything published beats it.

    Harmless in practice -- `update()` refuses to touch anything but the
    AppImage build -- and the honest answer to "which release is this?".
    """
    from linux_prefix_hub import DEV_VERSION
    from linux_prefix_hub.core import updater
    assert updater.is_newer("0.0.1", DEV_VERSION) is True


def test_check_survives_being_offline(fake_velopack):
    from linux_prefix_hub.core import db, updater
    fake_velopack(FakeManager(fail_on="check"))
    state = updater.check(force=True)
    assert state["available"] is False
    assert state["version"] == updater.__version__
    # Not "you are up to date": we never got an answer.
    assert state["reason"] == "unreachable"
    assert "update_check" not in db.load_config()   # and nothing is cached


def test_check_survives_a_missing_sdk(monkeypatch):
    from linux_prefix_hub.core import updater
    monkeypatch.setattr(updater, "_manager", lambda: None)
    state = updater.check(force=True)
    assert state["available"] is False
    assert state["reason"] == "unavailable"


def test_a_check_that_did_not_happen_is_not_up_to_date(fake_velopack):
    """The bug this came from: a build with no updater in it reported
    "you are up to date" while a newer release sat on GitHub. Only an empty
    reason may be read as "we asked, and we are current"."""
    from linux_prefix_hub.core import updater
    assert updater.check(force=True)["reason"] == "unavailable"

    fake_velopack(FakeManager(None))            # asked, nothing newer
    state = updater.check(force=True)
    assert state["available"] is False and state["reason"] == ""


def test_being_current_is_cached_like_an_answer(fake_velopack):
    from linux_prefix_hub.core import db, updater
    manager = fake_velopack(FakeManager(None))
    assert updater.check(force=True)["available"] is False
    assert db.load_config()["update_check"]["result"]["reason"] == ""
    assert updater.check()["cached"] is True
    assert manager.calls == ["check"]           # not asked twice


# --- update --------------------------------------------------------------
def test_update_downloads_then_hands_over(fake_velopack):
    from linux_prefix_hub.core import paths, updater
    manager = fake_velopack(FakeManager("9.9.9"))

    result = updater.update()

    assert result["ok"] is True
    assert "9.9.9" in result["message"]
    # Order matters: download, then swap, and the shims must be rewritten
    # before we hand control to Velopack's restart.
    assert manager.calls == ["check", "download", "apply"]
    assert paths.WRAPPER_SHIM.exists()


def test_update_reports_a_failed_download(fake_velopack):
    from linux_prefix_hub.core import updater
    manager = fake_velopack(FakeManager("9.9.9", fail_on="download"))

    result = updater.update()

    assert result["ok"] is False
    assert "boom" in result["message"]
    assert "apply" not in manager.calls          # nothing was swapped


def test_update_says_up_to_date_when_there_is_nothing(fake_velopack):
    from linux_prefix_hub.core import updater
    fake_velopack(FakeManager(None))
    result = updater.update()
    assert result["ok"] is True
    assert updater.__version__ in result["message"]


def test_update_defers_to_gearlever(monkeypatch, tmp_path):
    from linux_prefix_hub.core import updater
    monkeypatch.setattr(updater.integrate, "detect_gearlever",
                        lambda: tmp_path / "x.AppImage")
    result = updater.update()
    assert result["ok"] is True and result["skipped"] is True
    assert "GearLever" in result["message"]


def test_update_refuses_without_the_sdk(monkeypatch):
    from linux_prefix_hub.core import updater
    monkeypatch.setattr(updater.integrate, "detect_gearlever", lambda: None)
    monkeypatch.setattr(updater, "available", lambda: False)
    result = updater.update()
    assert result["ok"] is False
    assert "pip" in result["message"]


def test_update_refuses_when_the_build_is_not_packaged(monkeypatch):
    from linux_prefix_hub.core import updater
    monkeypatch.setattr(updater.integrate, "detect_gearlever", lambda: None)
    monkeypatch.setattr(updater, "available", lambda: True)
    monkeypatch.setattr(updater, "_manager", lambda: None)
    result = updater.update()
    assert result["ok"] is False
    assert "github.com" in result["message"]


# --- the startup hook ----------------------------------------------------
def test_app_hook_never_raises(monkeypatch):
    """It runs on every single start, including as a launch wrapper."""
    from linux_prefix_hub.core import updater
    updater.app_hook()          # SDK present or not, this must be silent


def test_app_hook_is_skipped_outside_the_appimage(monkeypatch):
    """Velopack's native layer logs to stderr when it is not a packaged
    build, and no Python `except` can swallow that -- so we must not call
    it at all in dev/pip mode."""
    import sys
    import types

    from linux_prefix_hub.core import updater
    called: list[str] = []

    module = types.ModuleType("velopack")

    class Spy:
        def __init__(self, *a, **kw):
            called.append("built")

        def set_auto_apply_on_startup(self, _apply):
            return self

        def run(self):
            called.append("ran")

    module.App = Spy               # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "velopack", module)

    monkeypatch.setattr(updater.integrate, "running_as_appimage",
                        lambda: None)
    updater.app_hook()
    assert called == []

    monkeypatch.setattr(updater.integrate, "running_as_appimage",
                        lambda: "/x/App.AppImage")
    updater.app_hook()
    assert called == ["built", "ran"]


def test_app_hook_swallows_sdk_errors(monkeypatch):
    import sys
    import types

    from linux_prefix_hub.core import updater

    module = types.ModuleType("velopack")

    class Boom:
        def __init__(self, *a, **kw):
            raise RuntimeError("no packaged app")

    module.App = Boom              # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "velopack", module)
    monkeypatch.setattr(updater.integrate, "running_as_appimage",
                        lambda: "/x/App.AppImage")
    updater.app_hook()


# --- locating the bundle -------------------------------------------------
def _fake_velopack(monkeypatch, *, auto_locate_works: bool):
    """A velopack module whose auto-locate fails the way the real one does
    once the window has handed over to a system interpreter."""
    import sys
    import types

    module = types.ModuleType("velopack")
    seen: dict[str, object] = {}

    class GithubSource:
        def __init__(self, url, *a):
            seen["url"] = url

    class VelopackLocatorConfig:
        def __init__(self, **fields):
            seen["locator"] = fields

    class UpdateManager:
        def __init__(self, source, options=None, locator=None):
            if locator is None and not auto_locate_works:
                raise RuntimeError(
                    "This application is not properly installed: UpdateNix "
                    "does not exist at the expected path: usr/bin/UpdateNix")
            seen["built_with_locator"] = locator is not None

    module.GithubSource = GithubSource
    module.UpdateManager = UpdateManager
    module.VelopackLocatorConfig = VelopackLocatorConfig
    monkeypatch.setitem(sys.modules, "velopack", module)
    return seen


def _fake_bundle(monkeypatch, tmp_path, appdir_is_mount_root=True):
    """The two files `_explicit_locator` refuses to work without.

    `appdir_is_mount_root` picks which of the two meanings APPDIR carries:
    the AppImage runtime sets the mount root, our own launcher overwrites it
    with `<mount>/usr/bin`. Both happen, in that order, on every start.
    """
    binary_dir = tmp_path / "mount" / "usr" / "bin"
    binary_dir.mkdir(parents=True)
    (binary_dir / "UpdateNix").write_text("#!/bin/true\n")
    (binary_dir / "sq.version").write_text("0.2.2\n")
    monkeypatch.setenv("APPDIR", str(tmp_path / "mount" if appdir_is_mount_root
                                     else binary_dir))
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "App.AppImage"))
    return binary_dir


def test_the_window_still_gets_a_manager(monkeypatch, tmp_path):
    """The bug: from a system interpreter Velopack cannot find its own
    UpdateNix (it resolves that against the working directory), so the
    window reported "up to date" while the terminal offered the update."""
    from linux_prefix_hub.core import updater
    seen = _fake_velopack(monkeypatch, auto_locate_works=False)
    binary_dir = _fake_bundle(monkeypatch, tmp_path)

    assert updater._manager() is not None
    assert seen["built_with_locator"] is True
    assert seen["locator"]["UpdateExePath"] == str(binary_dir / "UpdateNix")
    assert seen["locator"]["RootAppDir"] == str(tmp_path / "App.AppImage")


def test_the_launchers_appdir_is_found_too(monkeypatch, tmp_path):
    """`LinuxPrefixHub.sh` exports APPDIR as its own directory, so UpdateNix
    sits *in* APPDIR rather than under `usr/bin`. Looking only in the latter
    is why the window still could not check after the first fix."""
    from linux_prefix_hub.core import updater
    seen = _fake_velopack(monkeypatch, auto_locate_works=False)
    binary_dir = _fake_bundle(monkeypatch, tmp_path, appdir_is_mount_root=False)

    assert updater._manager() is not None
    assert seen["locator"]["UpdateExePath"] == str(binary_dir / "UpdateNix")


def test_velopacks_own_locate_is_left_alone_when_it_works(monkeypatch,
                                                          tmp_path):
    """It is the tested path; ours is only the rescue."""
    from linux_prefix_hub.core import updater
    seen = _fake_velopack(monkeypatch, auto_locate_works=True)
    _fake_bundle(monkeypatch, tmp_path)

    assert updater._manager() is not None
    assert seen["built_with_locator"] is False


def test_no_locator_is_invented_outside_the_bundle(monkeypatch, tmp_path):
    """Wrong paths here are what `update()` would overwrite."""
    from linux_prefix_hub.core import updater
    _fake_velopack(monkeypatch, auto_locate_works=False)
    monkeypatch.delenv("APPDIR", raising=False)
    monkeypatch.delenv("APPIMAGE", raising=False)
    assert updater._manager() is None

    # ...and not even inside one when the pieces are missing.
    monkeypatch.setenv("APPDIR", str(tmp_path / "empty"))
    monkeypatch.setenv("APPIMAGE", str(tmp_path / "App.AppImage"))
    assert updater._manager() is None


# --- restarting after an update ------------------------------------------
def test_restart_starts_the_new_build_without_our_bundle(monkeypatch,
                                                         tmp_path):
    """Velopack's own restart does not always come back as one, and then the
    window still runs the old code. The child must not inherit the bundle:
    PYTHONHOME points into a mount that is about to disappear."""
    import subprocess

    from linux_prefix_hub.core import updater
    appimage = tmp_path / "App.AppImage"
    appimage.write_text("#!/bin/true\n")
    monkeypatch.setenv("APPIMAGE", str(appimage))
    monkeypatch.setenv("APPDIR", "/tmp/.mount_x")
    monkeypatch.setenv("PYTHONHOME", "/tmp/.mount_x/opt/python3.12")
    monkeypatch.setenv("LPH_GUI_REEXEC", "4242")
    started: list[dict] = []
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **kw: started.append({"argv": argv,
                                                           **kw}))

    assert updater.restart_app() is True

    assert started[0]["argv"] == [str(appimage), "--gui"]
    assert started[0]["start_new_session"] is True
    env = started[0]["env"]
    assert "PYTHONHOME" not in env and "APPDIR" not in env
    assert "LPH_GUI_REEXEC" not in env


def test_restart_declines_outside_the_appimage(monkeypatch, tmp_path):
    from linux_prefix_hub.core import updater
    monkeypatch.delenv("APPIMAGE", raising=False)
    assert updater.restart_app() is False

    monkeypatch.setenv("APPIMAGE", str(tmp_path / "gone.AppImage"))
    assert updater.restart_app() is False
