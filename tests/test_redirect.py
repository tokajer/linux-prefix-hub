# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

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

    target = isolated_home / "Games/linux-prefix-hub/Games/Quake/Documents"
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
    target = isolated_home / "Games/linux-prefix-hub/Games/Quake/Documents"
    assert (target / "My Games/Quake/save0.sav").exists()


def test_redirect_to_a_custom_target(game, tmp_path):
    from linux_prefix_hub.core import redirect
    fingerprint, _prefix = game
    where = tmp_path / "elsewhere/Quake"
    assert redirect.redirect(fingerprint, "Documents", str(where)).ok
    assert (where / "My Games/Quake/save0.sav").exists()


def test_redirect_refuses_when_a_file_is_on_both_sides(game, isolated_home):
    """Two copies is a question, and deleting one is not an answer.

    This is the Steam Cloud case (`adapters/steam.cloud_paths`): the launcher
    restored its copy into the game folder while ours sat in the home folder.
    Nothing here can know which one the player wants, so nothing moves --
    including the files that would not have clashed.
    """
    from linux_prefix_hub.core import redirect
    fingerprint, prefix = game
    target = (isolated_home
              / "Games/linux-prefix-hub/Games/Quake/Documents/My Games/Quake")
    target.mkdir(parents=True)
    (target / "save0.sav").write_text("older backup")

    result = redirect.redirect(fingerprint, "Documents")

    assert not result.ok and "exists in both places" in result.message
    assert result["conflicts"] == ["My Games/Quake/save0.sav"]
    # Both versions survive, and the folder is untouched -- not half moved.
    assert (target / "save0.sav").read_text() == "older backup"
    physical = prefix / "drive_c/users/steamuser/Documents"
    assert not physical.is_symlink()
    assert (physical / "My Games/Quake/save0.sav").read_text() == "progress"
    assert (physical / "My Games/Quake/config.cfg").exists()


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


# --- The other writer on the same folder ---------------------------------
@pytest.fixture
def synced(monkeypatch):
    """Steam Cloud is syncing part of what this game keeps in Documents."""
    from linux_prefix_hub.adapters import steam
    monkeypatch.setattr(steam, "cloud_paths", lambda app_id: [
        "Documents/My Games/Quake/save0.sav",
        "AppData/Local/Quake/log.txt"])


def test_nothing_syncing_means_nothing_to_warn_about(game):
    from linux_prefix_hub.core import db, redirect
    fingerprint, _prefix = game
    assert redirect.cloud_warning(db.get_prefix(fingerprint),
                                  "Documents") is None


def test_the_second_writer_is_named_before_the_move(game, synced):
    from linux_prefix_hub.core import db, redirect
    fingerprint, _prefix = game
    entry = db.get_prefix(fingerprint)

    # Per shell folder, not per game: the log in AppData is Steam's business
    # and none of ours while we are moving Documents.
    assert redirect.cloud_conflicts(entry, "Documents") == [
        "Documents/My Games/Quake/save0.sav"]

    warning = redirect.cloud_warning(entry, "Documents")
    assert warning is not None
    assert "Steam" in warning[0] and "Documents" in warning[0]
    assert "1 file" in warning[1]


def test_a_launcher_without_a_cloud_stays_silent(fake_prefix, synced):
    """Only the adapter that has one answers; nobody guesses for the rest."""
    from linux_prefix_hub.core import db, redirect
    fingerprint = db.upsert_prefix({
        "source": "lutris", "app_id": "quake", "game_name": "Quake",
        "prefix_path": str(fake_prefix), "user_dir": "steamuser",
        "storage_locations": [
            {"type": "saves", "win_path": "Documents/My Games/Quake"}]})
    assert redirect.cloud_warning(db.get_prefix(fingerprint),
                                  "Documents") is None


def test_a_finished_move_still_carries_the_warning(game, synced):
    """The window shows it up front, the terminal after -- same words."""
    from linux_prefix_hub.core import redirect
    fingerprint, _prefix = game
    result = redirect.redirect(fingerprint, "Documents")
    assert result.ok and "Steam" in result["warning"]


# --- Asked for before the game ever ran ----------------------------------
def _never_started(name="Quake", app_id="2310"):
    """A discovery dict for a game that has no folder yet."""
    return {"source": "steam", "app_id": app_id, "game_name": name,
            "installed": True, "prefix_path": None, "user_dir": None}


def _started(prefix, name="Quake", app_id="2310"):
    return {**_never_started(name, app_id), "prefix_path": str(prefix),
            "user_dir": "steamuser"}


def test_a_wish_is_stored_under_the_games_own_identity():
    from linux_prefix_hub.core import db, redirect
    game = _never_started()

    assert not redirect.is_requested(game)
    result = redirect.request(game)

    assert result.ok and "has not been started yet" in result.message
    assert redirect.is_requested(game)
    # Not the prefix DB: there is no prefix to key it by, which is the point.
    assert db.load_prefixes() == {}
    assert "steam:2310" in db.pending_redirects()


def test_a_wish_can_be_taken_back():
    from linux_prefix_hub.core import redirect
    game = _never_started()
    redirect.request(game)

    assert redirect.cancel_request(game)
    assert not redirect.is_requested(game)
    assert not redirect.cancel_request(game)      # and again is a no-op


def test_nothing_happens_to_a_game_nobody_asked_about(fake_prefix):
    from linux_prefix_hub.core import redirect
    assert redirect.apply_pending(_started(fake_prefix)) == []


def test_a_wish_waits_while_the_game_has_no_folder():
    from linux_prefix_hub.core import db, redirect
    game = _never_started()
    redirect.request(game)

    assert redirect.apply_pending(game) == []
    assert redirect.is_requested(game)            # still waiting
    assert db.load_prefixes() == {}


def test_the_first_launch_files_the_game_but_does_not_edit_it(fake_prefix,
                                                              monkeypatch):
    """A prefix appearing means the game is *starting*, not that it is idle.

    Wine writes its in-memory registry over user.reg on shutdown, so an edit
    made now would be gone by the time the player quits (CLAUDE.md rule 7).
    Learning about the game is safe, and that is all that may happen here.
    """
    from linux_prefix_hub.core import db, redirect, registry
    monkeypatch.setattr(registry, "prefix_in_use", lambda prefix: True)
    game = _started(fake_prefix)
    redirect.request(game)

    assert redirect.apply_pending(game) == []

    entry = db.find_prefix("steam", "2310")
    assert entry is not None and entry[1]["game_name"] == "Quake"
    assert redirect.is_requested(game)            # retried on a later pass


def test_the_wish_lands_once_the_game_is_idle_again(fake_prefix,
                                                    isolated_home):
    from linux_prefix_hub.core import db, redirect
    docs = fake_prefix / "drive_c/users/steamuser/Documents/My Games/Quake"
    docs.mkdir(parents=True)
    (docs / "save0.sav").write_text("progress")

    game = _started(fake_prefix)
    redirect.request(game)
    # What the game has told us about itself by now (a first session with the
    # hook, or a PCGamingWiki lookup folded in).
    db.upsert_prefix({**game, "storage_locations": [
        {"type": "saves", "win_path": "Documents/My Games/Quake"}]})

    assert redirect.apply_pending(game) == ["Documents"]

    target = isolated_home / "Games/linux-prefix-hub/Games/Quake/Documents"
    assert (target / "My Games/Quake/save0.sav").read_text() == "progress"
    # Carried out in full, so it is not carried out twice.
    assert not redirect.is_requested(game)


def test_a_wish_survives_a_game_that_has_told_us_nothing(fake_prefix):
    from linux_prefix_hub.core import redirect
    game = _started(fake_prefix)
    redirect.request(game)

    assert redirect.apply_pending(game) == []
    assert redirect.is_requested(game)


def _cached_lookup(prefix, on_disk=True):
    """A confirmed PCGamingWiki answer waiting in the cache for this game."""
    from linux_prefix_hub.core import pcgw
    location = {"type": "saves", "where": "prefix",
                "win_path": "Documents/My Games/Quake",
                "detected_by": "pcgamingwiki"}
    pcgw.store_cached("steam", "2310", {"reason": "", "page": "Quake",
                                        "locations": [location]})
    if on_disk:
        (prefix / "drive_c/users/steamuser/Documents/My Games/Quake").mkdir(
            parents=True, exist_ok=True)
    return location


def test_a_confirmed_lookup_waiting_in_the_cache_is_folded_in(fake_prefix,
                                                              isolated_home):
    """The answer PCGamingWiki gave before there was a prefix to file it by."""
    from linux_prefix_hub.core import db, redirect
    location = _cached_lookup(fake_prefix)
    db.confirm_locations("steam", "2310", [location])

    game = _started(fake_prefix)
    redirect.request(game)

    assert redirect.apply_pending(game) == ["Documents"]
    entry = db.find_prefix("steam", "2310")
    assert entry is not None
    assert entry[1]["storage_locations"][0]["detected_by"] == "pcgamingwiki"


def test_a_lookup_nobody_confirmed_moves_nothing(fake_prefix, isolated_home):
    """A suggestion is not a decision -- least of all one that moves files."""
    from linux_prefix_hub.core import db, redirect
    _cached_lookup(fake_prefix)

    game = _started(fake_prefix)
    redirect.request(game)

    assert redirect.apply_pending(game) == []
    assert redirect.is_requested(game)            # the wish is still open
    entry = db.find_prefix("steam", "2310")
    assert entry is not None and not entry[1]["storage_locations"]


def test_a_confirmed_folder_that_is_not_there_moves_nothing(fake_prefix,
                                                            isolated_home):
    """Confirmed, but the game never wrote it: there is nothing to move."""
    from linux_prefix_hub.core import db, redirect
    location = _cached_lookup(fake_prefix, on_disk=False)
    db.confirm_locations("steam", "2310", [location])

    game = _started(fake_prefix)
    redirect.request(game)

    assert redirect.apply_pending(game) == []
    entry = db.find_prefix("steam", "2310")
    assert entry is not None and not entry[1]["storage_locations"]
    # And nothing was conjured up on disk to make it true either.
    assert not (fake_prefix / "drive_c/users/steamuser/Documents/My "
                "Games").exists()


def test_movable_roots_skips_what_cannot_be_moved():
    from linux_prefix_hub.core import redirect
    entry = {"storage_locations": [
        {"win_path": "Documents/My Games/Q"},
        {"win_path": "Documents/Other"},              # same root, once
        {"win_path": "AppData/Roaming/Q"},
        {"win_path": "Q/Data"},                       # no shell folder
        {"win_path": "Documents/X", "where": "game_folder"},
    ]}
    assert redirect.movable_roots(entry) == ["Documents", "AppData/Roaming"]


# --- Moving a moved folder somewhere else --------------------------------
def _redirect_into(fingerprint, where):
    """Redirect and pretend that is where an older version had put it."""
    from linux_prefix_hub.core import redirect
    result = redirect.redirect(fingerprint, "Documents", str(where))
    assert result.ok, result.message
    return where


def test_relocate_moves_the_data_the_link_and_the_registry(game,
                                                           isolated_home,
                                                           tmp_path):
    from linux_prefix_hub.core import db, redirect, registry
    fingerprint, prefix = game
    old = _redirect_into(fingerprint, tmp_path / "old/Quake/Documents")
    new = tmp_path / "new/Quake/Documents"

    result = redirect.relocate(fingerprint, "Documents", str(new))
    assert result.ok, result.message

    assert (new / "My Games/Quake/save0.sav").read_text() == "progress"
    assert not old.exists() or not any(old.iterdir())
    physical = prefix / "drive_c/users/steamuser/Documents"
    assert physical.is_symlink()
    assert os.path.realpath(physical) == os.path.realpath(new)
    assert registry.get_shell_folder(prefix, "Documents") == \
        registry.windows_path(new)
    assert db.get_prefix(fingerprint)["storage_locations"][0][
        "redirect_target"] == str(new)


def test_relocate_stops_when_a_file_is_on_both_sides(game, tmp_path):
    """Same rule as the first move: two copies is a question (rule 9)."""
    from linux_prefix_hub.core import redirect
    fingerprint, _prefix = game
    old = _redirect_into(fingerprint, tmp_path / "old/Quake/Documents")
    new = tmp_path / "new/Quake/Documents"
    (new / "My Games/Quake").mkdir(parents=True)
    (new / "My Games/Quake/save0.sav").write_text("older backup")

    result = redirect.relocate(fingerprint, "Documents", str(new))
    assert not result.ok
    assert (old / "My Games/Quake/save0.sav").read_text() == "progress"
    assert (new / "My Games/Quake/save0.sav").read_text() == "older backup"


def test_the_old_default_folder_is_found_and_moved(game, isolated_home):
    """What an earlier version left in the folder we used to default to."""
    from linux_prefix_hub.core import paths, redirect
    fingerprint, _prefix = game
    old = _redirect_into(fingerprint,
                         paths.APP_GAMES_DIR / "Quake/Documents")

    waiting = redirect.stale_targets()
    assert [item["source"] for item in waiting] == [str(old)]
    assert waiting[0]["target"] == str(
        paths.DEFAULT_REDIRECT_ROOT / "Quake/Documents")

    assert all(r.ok for r in redirect.move_stale())
    assert (paths.DEFAULT_REDIRECT_ROOT
            / "Quake/Documents/My Games/Quake/save0.sav").exists()
    # The emptied folders below our own go with it, ~/Games never does.
    assert not old.exists()
    assert not (paths.APP_GAMES_DIR / "Quake").exists()
    assert paths.APP_GAMES_DIR.exists()
    assert redirect.stale_targets() == []


def test_a_folder_the_user_named_is_never_moved_for_us(game, tmp_path):
    """Changing our mind about a default is not a reason to touch it."""
    from linux_prefix_hub.core import redirect
    fingerprint, _prefix = game
    _redirect_into(fingerprint, tmp_path / "ssd/Saves/Quake")
    assert redirect.stale_targets() == []


def test_data_already_in_the_current_folder_is_left_alone(game):
    from linux_prefix_hub.core import redirect
    fingerprint, _prefix = game
    assert redirect.redirect(fingerprint, "Documents").ok
    assert redirect.stale_targets() == []
