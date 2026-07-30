# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Setup flow: the decisions, not the printing.

The split in `gui/welcome.py` exists so the GTK front-end can reuse exactly
this logic, so it is the logic these tests pin down. `systemctl` is stubbed --
a test must never register a unit in the developer's session.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_systemctl(monkeypatch):
    from linux_prefix_hub.core import integrate
    monkeypatch.setattr(integrate.subprocess, "run",
                        lambda *a, **kw: None)


@pytest.mark.parametrize("path,unstable", [
    ("~/Downloads/apps", True),
    ("~/Desktop", True),
    ("/tmp/here", True),
    ("/media/usb/apps", True),
    ("/run/media/u/stick", True),
    ("/mnt/disk/apps", True),
    ("~/.local/share/linux-prefix-hub", False),
    ("~/Applications", False),
])
def test_is_unstable(path, unstable):
    from pathlib import Path

    from linux_prefix_hub.gui import welcome
    assert welcome.is_unstable(Path(path).expanduser()) is unstable


def test_non_interactive_takes_the_default():
    from linux_prefix_hub.core import paths
    from linux_prefix_hub.gui import welcome
    assert welcome.choose_install_dir(interactive=False) == \
        paths.DEFAULT_INSTALL_DIR


def test_empty_input_keeps_the_default(monkeypatch):
    from linux_prefix_hub.core import paths
    from linux_prefix_hub.gui import welcome
    monkeypatch.setattr("builtins.input", lambda *_a: "")
    assert welcome.choose_install_dir() == paths.DEFAULT_INSTALL_DIR


def test_own_path_is_expanded(monkeypatch, isolated_home):
    from linux_prefix_hub.gui import welcome
    monkeypatch.setattr("builtins.input", lambda *_a: "~/tools/lph")
    assert welcome.choose_install_dir() == isolated_home / "tools/lph"


def test_unstable_path_accepted_after_confirmation(monkeypatch):
    from linux_prefix_hub.gui import welcome
    answers = iter(["/tmp/lph", "y"])
    monkeypatch.setattr("builtins.input", lambda *_a: next(answers))
    from pathlib import Path
    assert welcome.choose_install_dir() == Path("/tmp/lph")


def test_declining_an_unstable_path_asks_again(monkeypatch):
    from linux_prefix_hub.core import paths
    from linux_prefix_hub.gui import welcome
    # Offer Downloads, decline, then accept the default with Enter.
    answers = iter(["~/Downloads/lph", "n", ""])
    monkeypatch.setattr("builtins.input", lambda *_a: next(answers))
    assert welcome.choose_install_dir() == paths.DEFAULT_INSTALL_DIR


def test_run_records_the_choice_and_sets_up(monkeypatch):
    from linux_prefix_hub.core import db, paths
    from linux_prefix_hub.gui import welcome
    monkeypatch.setattr(welcome.integrate, "detect_gearlever", lambda: None)

    result = welcome.run(interactive=False)

    cfg = db.load_config()
    assert cfg["setup_done"] is True
    assert cfg["managed_by"] == "self"
    assert cfg["install_dir"] == str(paths.DEFAULT_INSTALL_DIR)
    assert paths.WRAPPER_SHIM.exists() and paths.HOOK_SHIM.exists()
    assert paths.DAEMON_SHIM.exists() and paths.WATCHER_UNIT.exists()
    assert result["wrapper_shim"] == str(paths.WRAPPER_SHIM)


def test_run_defers_to_gearlever(monkeypatch, tmp_path):
    from linux_prefix_hub.core import db
    from linux_prefix_hub.gui import welcome
    managed = tmp_path / "AppImages/LinuxPrefixHub.AppImage"
    monkeypatch.setattr(welcome.integrate, "detect_gearlever",
                        lambda: managed)
    # Would raise if the flow asked for an install dir.
    monkeypatch.setattr("builtins.input", lambda *_a: pytest.fail("asked"))

    welcome.run(interactive=True)

    cfg = db.load_config()
    assert cfg["managed_by"] == "gearlever"
    assert cfg["setup_done"] is True
    assert "install_dir" not in cfg      # GearLever owns placement


# --- the icon ------------------------------------------------------------
def test_setup_installs_the_icon_into_the_theme(monkeypatch):
    """The menu entry and the About dialog reference the icon by *name*;
    without a file in the theme both showed a blank placeholder."""
    from linux_prefix_hub.core import integrate, paths
    monkeypatch.setattr(integrate, "detect_gearlever", lambda: None)

    result = integrate.full_setup(enable_watcher=False)

    assert paths.ICON_FILE.exists()
    assert paths.ICON_FILE.read_bytes() == paths.ICON_SOURCE.read_bytes()
    assert result["icon"] == str(paths.ICON_FILE)
    # ...and the name in the entry is the one we just installed.
    entry = integrate.DESKTOP_FILE.read_text(encoding="utf-8")
    assert f"Icon={paths.APP_NAME}\n" in entry
    assert paths.ICON_FILE.name == f"{paths.APP_NAME}.png"


def test_the_icon_ships_inside_the_package(monkeypatch):
    """Not in packaging/: a pip install has no packaging/ directory, and the
    AppImage copies only the package."""
    from linux_prefix_hub.core import paths
    assert paths.ICON_SOURCE.exists()
    assert paths.ICON_SOURCE.parent.parent.name == "linux_prefix_hub"


def test_a_missing_icon_does_not_fail_setup(monkeypatch, tmp_path):
    from linux_prefix_hub.core import integrate, paths
    monkeypatch.setattr(integrate, "detect_gearlever", lambda: None)
    monkeypatch.setattr(paths, "ICON_SOURCE", tmp_path / "nope.png")

    result = integrate.full_setup(enable_watcher=False)

    assert result["icon"] == "(not shipped with this build)"
    assert result["wrapper_shim"] == str(paths.WRAPPER_SHIM)
