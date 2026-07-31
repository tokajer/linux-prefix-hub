# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Taking the app back off the machine without taking anything else with it.

Every test here is about the same invariant: an uninstall that cannot put a
game's data back does not happen at all. Deleting is the last step and the
only one that is irreversible, so it never runs before the two that are.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def nothing_is_running(monkeypatch):
    from linux_prefix_hub.core import registry
    monkeypatch.setattr(registry, "prefix_in_use", lambda prefix: False)


@pytest.fixture(autouse=True)
def no_launcher_of_our_own(monkeypatch):
    """Discovery must not walk the developer's real Steam library."""
    from linux_prefix_hub.adapters import base
    monkeypatch.setattr(base, "iter_games", lambda sources=None: iter(()))


@pytest.fixture(autouse=True)
def no_systemd(monkeypatch):
    import subprocess

    from linux_prefix_hub.core import uninstall
    calls: list[list[str]] = []
    monkeypatch.setattr(uninstall.subprocess, "run",
                        lambda argv, **kw: calls.append(argv)
                        or subprocess.CompletedProcess(argv, 0))
    return calls


@pytest.fixture
def moved_game(fake_prefix, isolated_home):
    """A game whose Documents folder we have already moved into the home."""
    from linux_prefix_hub.core import db, redirect
    docs = fake_prefix / "drive_c/users/steamuser/Documents/My Games/Quake"
    docs.mkdir(parents=True)
    (docs / "save0.sav").write_text("progress")

    fingerprint = db.upsert_prefix({
        "source": "steam", "app_id": "2310", "game_name": "Quake",
        "prefix_path": str(fake_prefix), "user_dir": "steamuser",
        "storage_locations": [
            {"type": "saves", "win_path": "Documents/My Games/Quake"}],
    })
    assert redirect.redirect(fingerprint, "Documents/My Games/Quake").ok
    return fingerprint, fake_prefix


@pytest.fixture
def installed(isolated_home):
    """What `integrate.full_setup` leaves behind, without running it."""
    from linux_prefix_hub.core import db, integrate, paths
    integrate.install_shims()
    integrate.install_systemd_unit(enable=False)
    integrate.install_desktop_entry()
    appimage = paths.installed_appimage_path(db.install_dir())
    appimage.parent.mkdir(parents=True, exist_ok=True)
    appimage.write_text("#!/bin/true\n")
    (db.install_dir() / "packages").mkdir(exist_ok=True)
    return appimage


# --- What is out there ---------------------------------------------------
def test_moved_folders_are_listed_by_shell_folder(moved_game):
    """One moved folder, not one per storage location inside it."""
    from linux_prefix_hub.core import db, uninstall
    fingerprint, _prefix = moved_game
    db.update_location(fingerprint, "Documents/Other", where="prefix",
                       redirected=True,
                       redirect_target=str(db.redirect_root() / "Quake"))

    folders = uninstall.moved_folders()
    assert len(folders) == 1
    assert folders[0]["root"] == "Documents"
    assert folders[0]["game_name"] == "Quake"


def test_a_running_game_blocks_before_anything_moves(moved_game, monkeypatch):
    from linux_prefix_hub.core import registry, uninstall
    monkeypatch.setattr(registry, "prefix_in_use", lambda prefix: True)

    blockers = uninstall.blockers()
    assert blockers and "Quake" in blockers[0]

    # And the plan says so rather than pretending it can go ahead.
    assert uninstall.plan()["blockers"] == blockers


# --- Putting the data back ------------------------------------------------
def test_revert_puts_the_data_back_and_removes_the_link(moved_game):
    from linux_prefix_hub.core import db, uninstall
    fingerprint, prefix = moved_game
    docs = prefix / "drive_c/users/steamuser/Documents"
    assert docs.is_symlink()

    result = uninstall.revert_all()

    assert result["ok"] is True and len(result["reverted"]) == 1
    assert not docs.is_symlink()
    assert (docs / "My Games/Quake/save0.sav").read_text() == "progress"
    # And the DB no longer claims a folder that is not there any more.
    entry = db.get_prefix(fingerprint)
    assert entry["storage_locations"][0]["redirected"] is False


def test_revert_empties_our_own_folder_but_only_with_rmdir(moved_game):
    """The folder we made goes; a folder with anything left in it stays."""
    from linux_prefix_hub.core import db, paths, uninstall
    root = db.redirect_root()
    uninstall.revert_all()
    assert not (root / "Quake").exists()
    # Our own subfolder goes with the last game in it -- but ~/Games, which
    # it sits inside precisely because it is the user's, stays.
    assert not root.exists()
    assert paths.DEFAULT_REDIRECT_ROOT.parent.exists()

    leftover = root / "Elsewhere"
    leftover.mkdir(parents=True)
    (leftover / "mine.txt").write_text("x")
    uninstall._prune_empty(leftover)
    assert (leftover / "mine.txt").exists()


def test_a_folder_the_user_named_is_left_alone(moved_game, tmp_path):
    """`--target` puts data somewhere the user picked. Tidying up in there
    is not part of the deal."""
    from linux_prefix_hub.core import uninstall
    mine = tmp_path / "my-saves" / "quake"
    mine.mkdir(parents=True)
    uninstall._prune_empty(mine)
    assert mine.exists()


def test_a_lost_symlink_still_brings_the_data_home(moved_game):
    """The case this is actually for: Proton recreated the folder.

    The link is gone, so there is nothing in the prefix pointing at the
    saves -- but the DB still remembers where we put them, and an uninstall
    that only reset the registry would leave them in a folder nobody
    references any more.
    """
    from linux_prefix_hub.core import uninstall
    _fingerprint, prefix = moved_game
    docs = prefix / "drive_c/users/steamuser/Documents"
    docs.unlink()
    docs.mkdir()

    assert uninstall.revert_all()["ok"] is True
    assert (docs / "My Games/Quake/save0.sav").read_text() == "progress"


def test_two_copies_are_kept_and_named(moved_game):
    """Never overwrite, never delete, and never stay quiet about it."""
    from linux_prefix_hub.core import db, uninstall
    _fingerprint, prefix = moved_game
    docs = prefix / "drive_c/users/steamuser/Documents"
    docs.unlink()
    (docs / "My Games/Quake").mkdir(parents=True)
    (docs / "My Games/Quake/save0.sav").write_text("newer progress")

    result = uninstall.revert_all()

    assert result["ok"] is True and result["notes"]
    assert (docs / "My Games/Quake/save0.sav").read_text() == "newer progress"
    kept = db.redirect_root() / "Quake/Documents/My Games/Quake/save0.sav"
    assert kept.read_text() == "progress"


def test_a_revert_that_fails_stops_the_whole_uninstall(moved_game,
                                                       installed,
                                                       monkeypatch):
    from linux_prefix_hub.core import paths, redirect, uninstall
    monkeypatch.setattr(
        redirect, "undo",
        lambda fp, root, force=False: redirect.RedirectResult(False, "no"))

    result = uninstall.run()

    assert result["ok"] is False and result["stage"] == "revert"
    assert result["failed"]
    assert paths.WRAPPER_SHIM.exists()          # nothing was removed
    assert installed.exists()


# --- Taking the hooks back out --------------------------------------------
def _connected(monkeypatch, *games):
    from linux_prefix_hub.adapters import base
    monkeypatch.setattr(base, "iter_games", lambda sources=None: iter(games))


def test_connected_games_are_disconnected(monkeypatch):
    from linux_prefix_hub.adapters import base
    from linux_prefix_hub.core import uninstall
    _connected(monkeypatch, {"source": "steam", "app_id": "10",
                             "game_name": "CS", "managed": True},
               {"source": "steam", "app_id": "20", "game_name": "TF",
                "managed": False})
    seen: list[str] = []
    monkeypatch.setattr(base, "get_adapter", lambda source: type(
        "A", (), {"disconnect": staticmethod(
            lambda app_id: seen.append(app_id)
            or base.HookResult(True, "gone"))})())

    result = uninstall.disconnect_all()

    assert seen == ["10"]                       # only the connected one
    assert result["ok"] is True and result["disconnected"] == ["CS"]


def test_a_hook_we_cannot_remove_stops_the_uninstall(monkeypatch, installed):
    """Steam is running. Removing the shim now would leave a launch option
    pointing at a file that is gone -- and the game would not start."""
    from linux_prefix_hub.adapters import base
    from linux_prefix_hub.core import paths, uninstall
    _connected(monkeypatch, {"source": "steam", "app_id": "10",
                             "game_name": "CS", "managed": True})
    monkeypatch.setattr(base, "get_adapter", lambda source: type(
        "A", (), {"disconnect": staticmethod(
            lambda app_id: base.HookResult(False, "Steam is running.",
                                           manual=True))})())

    result = uninstall.run()

    assert result["ok"] is False and result["stage"] == "disconnect"
    assert paths.WRAPPER_SHIM.exists()
    assert installed.exists()


def test_hand_installed_games_are_named_rather_than_pretended_about(
        monkeypatch):
    """There is no config to edit -- the wrapper is in a command the user
    wrote, and only they can take it out."""
    from linux_prefix_hub.adapters import base
    from linux_prefix_hub.core import uninstall
    _connected(monkeypatch, {"source": "generic", "app_id": "/games/x",
                             "game_name": "Handmade", "managed": True})
    monkeypatch.setattr(base, "get_adapter", lambda source: type(
        "A", (), {"disconnect": staticmethod(
            lambda app_id: base.HookResult(True, "gone"))})())

    assert uninstall.disconnect_all()["manual"] == ["Handmade"]


# --- Deleting, last ------------------------------------------------------
def test_a_clean_run_removes_everything_we_installed(moved_game, installed,
                                                     no_systemd):
    from linux_prefix_hub.core import db, integrate, paths, uninstall

    result = uninstall.run()

    assert result["ok"] is True and result["stage"] == "done"
    assert not paths.WRAPPER_SHIM.exists()
    assert not paths.HOOK_SHIM.exists()
    assert not paths.DAEMON_SHIM.exists()
    assert not paths.WATCHER_UNIT.exists()
    assert not integrate.DESKTOP_FILE.exists()
    assert not installed.exists()
    assert not db.install_dir().exists()
    assert not paths.CONFIG_DIR.exists()
    # The watcher is stopped before its unit file disappears.
    assert ["systemctl", "--user", "disable", "--now",
            paths.WATCHER_UNIT.name] in no_systemd

    # And the game itself is untouched apart from being back to normal.
    _fingerprint, prefix = moved_game
    saves = prefix / "drive_c/users/steamuser/Documents/My Games/Quake"
    assert (saves / "save0.sav").read_text() == "progress"


def test_settings_can_be_kept(moved_game, installed):
    from linux_prefix_hub.core import paths, uninstall
    assert uninstall.run(keep_settings=True)["ok"] is True
    assert paths.CONFIG_DIR.exists()
    assert not paths.WRAPPER_SHIM.exists()


def test_gearlevers_appimage_is_left_to_gearlever(installed, monkeypatch):
    from linux_prefix_hub.core import integrate, uninstall
    monkeypatch.setattr(integrate, "detect_gearlever", lambda: installed)

    assert installed not in uninstall.removable_files()
    result = uninstall.run()
    assert result["gearlever"] == str(installed)
    assert installed.exists()


def test_pending_moves_are_dropped_too(installed):
    """A move waiting for a first launch is a promise we can no longer
    keep, and it lives in the config we are about to delete anyway."""
    from linux_prefix_hub.core import db, uninstall
    db.add_pending_redirect("steam", "42", "Later")
    assert uninstall.run(keep_settings=True)["pending"] == 1
    assert db.pending_redirects() == {}
