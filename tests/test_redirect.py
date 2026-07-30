"""Registry editing and the hybrid (registry + symlink) redirection."""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def never_look_at_real_processes(monkeypatch):
    from linux_prefix_hub.core import registry
    monkeypatch.setattr(registry, "prefix_in_use", lambda prefix: False)


@pytest.fixture
def game(fake_prefix):
    """A learned game with saves in Documents and config in AppData."""
    from linux_prefix_hub.core import db
    docs = fake_prefix / "drive_c/users/steamuser/Documents/My Games/Quake"
    docs.mkdir(parents=True)
    (docs / "save0.sav").write_text("progress")
    (docs / "config.cfg").write_text("sensitivity 3")

    fingerprint = db.upsert_prefix({
        "source": "steam", "app_id": "2310", "game_name": "Quake",
        "prefix_path": str(fake_prefix), "user_dir": "steamuser",
        "storage_locations": [
            {"type": "saves", "win_path": "Documents/My Games/Quake"}],
    })
    return fingerprint, fake_prefix


# --- Registry ------------------------------------------------------------
def test_shell_folder_root_mapping():
    from linux_prefix_hub.core import registry
    assert registry.shell_folder_root("Documents/My Games/Q") == "Documents"
    assert registry.shell_folder_root("AppData/Local/Q") == "AppData/Local"
    assert registry.shell_folder_root("AppData/Roaming") == "AppData/Roaming"
    assert registry.shell_folder_root("Saved Games/Q") == "Saved Games"
    # Not a shell folder: only a symlink could help, so we say no.
    assert registry.shell_folder_root("SomeGame/Data") is None


def test_windows_path_uses_the_z_drive():
    from linux_prefix_hub.core import registry
    assert registry.windows_path("/home/me/Games/Q") == \
        "Z:\\home\\me\\Games\\Q"


def test_set_and_read_shell_folder(fake_prefix):
    from linux_prefix_hub.core import registry
    assert registry.set_shell_folder(fake_prefix, "Documents",
                                     "/home/me/Games/Q/Documents")
    assert registry.get_shell_folder(fake_prefix, "Documents") == \
        "Z:\\home\\me\\Games\\Q\\Documents"
    # Written to both sections Windows uses.
    assert registry.get_value(fake_prefix, registry.USER_SHELL_FOLDERS_KEY,
                              "Personal")
    # Unrelated keys survive.
    text = (fake_prefix / "user.reg").read_text(encoding="utf-8")
    assert '"Desktop"="C:\\\\users\\\\steamuser\\\\Desktop"' in text
    assert "[Software\\\\Wine]" in text


def test_guid_only_folders_are_written(fake_prefix):
    from linux_prefix_hub.core import registry
    registry.set_shell_folder(fake_prefix, "Downloads", "/home/me/Downloads")
    text = (fake_prefix / "user.reg").read_text(encoding="utf-8")
    assert "{374DE290-123F-4565-9164-39C4925E467B}" in text


# --- Redirection ---------------------------------------------------------
def test_redirect_moves_data_and_links_it_back(game, isolated_home):
    from linux_prefix_hub.core import db, redirect, registry
    fingerprint, prefix = game

    result = redirect.redirect(fingerprint, "Documents/My Games/Quake")
    assert result.ok, result.message

    target = isolated_home / "Games/linux-prefix-hub/Quake/Documents"
    assert (target / "My Games/Quake/save0.sav").read_text() == "progress"

    physical = prefix / "drive_c/users/steamuser/Documents"
    assert physical.is_symlink()
    assert os.path.realpath(physical) == os.path.realpath(target)
    # The game's own path still resolves -- that is the point of the symlink.
    assert (physical / "My Games/Quake/save0.sav").exists()

    assert registry.get_shell_folder(prefix, "Documents") == \
        registry.windows_path(target)

    location = db.get_prefix(fingerprint)["storage_locations"][0]
    assert location["redirected"] is True
    assert location["redirect_target"] == str(target)


def test_redirect_is_idempotent(game, isolated_home):
    from linux_prefix_hub.core import redirect
    fingerprint, _prefix = game
    assert redirect.redirect(fingerprint, "Documents/My Games/Quake").ok
    assert redirect.redirect(fingerprint, "Documents/My Games/Quake").ok
    target = isolated_home / "Games/linux-prefix-hub/Quake/Documents"
    assert (target / "My Games/Quake/save0.sav").exists()


def test_redirect_to_a_custom_target(game, tmp_path):
    from linux_prefix_hub.core import redirect
    fingerprint, _prefix = game
    where = tmp_path / "elsewhere/Quake"
    assert redirect.redirect(fingerprint, "Documents", str(where)).ok
    assert (where / "My Games/Quake/save0.sav").exists()


def test_redirect_never_overwrites_existing_files(game, isolated_home):
    from linux_prefix_hub.core import redirect
    fingerprint, _prefix = game
    target = (isolated_home
              / "Games/linux-prefix-hub/Quake/Documents/My Games/Quake")
    target.mkdir(parents=True)
    (target / "save0.sav").write_text("older backup")

    assert redirect.redirect(fingerprint, "Documents").ok
    assert (target / "save0.sav").read_text() == "older backup"


def test_undo_puts_everything_back(game):
    from linux_prefix_hub.core import db, redirect, registry
    fingerprint, prefix = game
    redirect.redirect(fingerprint, "Documents")

    assert redirect.undo(fingerprint, "Documents").ok

    physical = prefix / "drive_c/users/steamuser/Documents"
    assert physical.is_dir() and not physical.is_symlink()
    assert (physical / "My Games/Quake/save0.sav").read_text() == "progress"
    assert registry.get_shell_folder(prefix, "Documents") == \
        "C:\\users\\steamuser\\Documents"
    assert db.get_prefix(fingerprint)["storage_locations"][0]["redirected"] \
        is False


def test_reapply_heals_a_symlink_a_proton_update_ate(game, isolated_home):
    from linux_prefix_hub.core import redirect
    fingerprint, prefix = game
    redirect.redirect(fingerprint, "Documents")

    physical = prefix / "drive_c/users/steamuser/Documents"
    physical.unlink()
    physical.mkdir()                        # what an update leaves behind

    assert redirect.reapply(fingerprint) == ["Documents"]
    assert physical.is_symlink()
    assert (physical / "My Games/Quake/save0.sav").exists()


def test_locations_outside_a_shell_folder_are_refused(fake_prefix):
    from linux_prefix_hub.core import db, redirect
    fingerprint = db.upsert_prefix({
        "source": "steam", "app_id": "1", "game_name": "Odd",
        "prefix_path": str(fake_prefix), "user_dir": "steamuser",
        "storage_locations": [{"type": "saves", "win_path": "Odd/Data"}],
    })
    result = redirect.redirect(fingerprint, "Odd/Data")
    assert not result.ok
    assert "cannot be moved safely" in result.message


def test_a_running_game_blocks_the_move(game, monkeypatch):
    from linux_prefix_hub.core import redirect, registry
    fingerprint, _prefix = game
    monkeypatch.setattr(registry, "prefix_in_use", lambda prefix: True)
    result = redirect.redirect(fingerprint, "Documents")
    assert not result.ok and "still running" in result.message
