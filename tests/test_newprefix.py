# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Game folders the user makes themselves.

What is pinned here is mostly the two ways of starting a Windows program and
the fact that the result is an ordinary hand-installed game afterwards -- if
it were not, every other feature would need a second code path for it.
"""
from __future__ import annotations

from pathlib import Path

from conftest import write

# Reloaded per test by the `isolated_home` fixture, so the module is imported
# here and its constants are read at call time -- never bound up front.
from linux_prefix_hub.core import paths


def _steam_with_builds(tmp_path, monkeypatch, builds=("GE-Proton10-34",),
                       shipped=()):
    """A Steam root with compatibility builds in both places they live."""
    from linux_prefix_hub.adapters import steam
    root = tmp_path / ".steam/steam"
    (root / "steamapps").mkdir(parents=True)
    for name in builds:
        write(root / "compatibilitytools.d" / name / "proton", "#!/bin/sh\n")
    for name in shipped:
        write(root / "steamapps/common" / name / "proton", "#!/bin/sh\n")
    monkeypatch.setattr(steam, "STEAM_ROOT_CANDIDATES", [str(root)])
    return root


def _fake_run(monkeypatch, outcome=0, on_call=None):
    """Replace the actual launch; remember what would have been started."""
    import subprocess

    from linux_prefix_hub.core import newprefix
    calls: list[tuple[list[str], dict[str, str], str]] = []

    def fake(command, env=None, cwd=None, **_kw):
        calls.append((list(command), dict(env or {}), str(cwd)))
        if on_call is not None:
            on_call(command, env, cwd)
        return subprocess.CompletedProcess(command, outcome, "", "boom\n")

    monkeypatch.setattr(newprefix.subprocess, "run", fake)
    return calls


def _boots(prefix_of):
    """A stand-in for wineboot: makes the folder look like a real one."""
    def make(_command, env, _cwd):
        prefix = Path(prefix_of(env))
        (prefix / "drive_c/users/steamuser").mkdir(parents=True,
                                                   exist_ok=True)
        write(prefix / "user.reg", "WINE REGISTRY Version 2\n")
        write(prefix / "system.reg", "WINE REGISTRY Version 2\n")
    return make


# --- what can run a Windows program -------------------------------------
def test_engines_lists_both_places_and_the_system_one(tmp_path, monkeypatch):
    from linux_prefix_hub.core import newprefix
    _steam_with_builds(tmp_path, monkeypatch, builds=("GE-Proton10-34",),
                       shipped=("Proton Experimental",))
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: "/usr/bin/wine")

    found = {e["id"]: e["kind"] for e in newprefix.engines()}
    assert found == {"GE-Proton10-34": "proton",
                     "Proton Experimental": "proton",
                     "wine": "wine"}


def test_our_own_per_game_copies_are_not_offered(tmp_path, monkeypatch):
    """`compatibilitytools.d` also holds the copies `gameopts` makes."""
    from linux_prefix_hub.core import gameopts, newprefix
    root = _steam_with_builds(tmp_path, monkeypatch)
    copy = root / "compatibilitytools.d" / "LinuxPrefixHub-Something"
    write(copy / "proton", "#!/bin/sh\n")
    write(copy / gameopts.MARKER, '{"base": "GE-Proton10-34"}')
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: None)

    assert [e["id"] for e in newprefix.engines()] == ["GE-Proton10-34"]


def test_default_follows_the_newest_of_the_usual_family(tmp_path,
                                                        monkeypatch):
    from linux_prefix_hub.core import newprefix
    _steam_with_builds(tmp_path, monkeypatch,
                       builds=("GE-Proton9-27", "GE-Proton10-34"))
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: "/usr/bin/wine")
    assert newprefix.default_engine() == "GE-Proton10-34"


def test_the_system_one_is_the_default_when_nothing_else_is_there(
        tmp_path, monkeypatch):
    from linux_prefix_hub.core import newprefix
    _steam_with_builds(tmp_path, monkeypatch, builds=())
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: "/usr/bin/wine")
    assert newprefix.default_engine() == "wine"


def test_a_replaced_build_falls_back_to_its_family(tmp_path, monkeypatch):
    """The build a folder was made with can be gone; the folder is not."""
    from linux_prefix_hub.core import newprefix
    _steam_with_builds(tmp_path, monkeypatch, builds=("GE-Proton11-5",))
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: None)

    assert newprefix.find_engine("GE-Proton10-34")["id"] == "GE-Proton11-5"
    assert newprefix.find_engine("wine") is None


# --- making one ----------------------------------------------------------
def test_create_with_a_build_sets_the_container_variables(tmp_path,
                                                          monkeypatch):
    from linux_prefix_hub.adapters import base
    from linux_prefix_hub.core import gameopts, newprefix
    root = _steam_with_builds(tmp_path, monkeypatch)
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: None)
    calls = _fake_run(monkeypatch, on_call=_boots(
        lambda env: Path(env["STEAM_COMPAT_DATA_PATH"]) / "pfx"))

    result = newprefix.create("Dark Age of Camelot")
    assert result.ok
    folder = Path(result["path"])
    assert folder == paths.DEFAULT_PREFIX_ROOT / "Dark-Age-of-Camelot-1"
    assert base.is_prefix(folder / "pfx")

    command, env, cwd = calls[0]
    assert command == [str(root / "compatibilitytools.d/GE-Proton10-34"
                           / "proton"), "run", "wineboot", "-u"]
    assert env["STEAM_COMPAT_DATA_PATH"] == str(folder)
    assert env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] == str(root)
    # A folder name where the build expects its own app id: without one it
    # refuses to start anything at all.
    assert env[gameopts.APPID_VAR] == gameopts.APPID_FALLBACK
    assert cwd == str(folder)


def test_create_with_the_system_one_points_it_at_the_folder(tmp_path,
                                                            monkeypatch):
    from linux_prefix_hub.core import newprefix
    _steam_with_builds(tmp_path, monkeypatch, builds=())
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: "/usr/bin/wine")
    calls = _fake_run(monkeypatch,
                      on_call=_boots(lambda env: env["WINEPREFIX"]))

    result = newprefix.create("Old Game", engine="wine")
    assert result.ok
    command, env, _cwd = calls[0]
    assert command == ["/usr/bin/wine", "wineboot", "-u"]
    assert env["WINEPREFIX"] == str(Path(result["path"]) / "pfx")
    assert "STEAM_COMPAT_DATA_PATH" not in env


def test_a_new_folder_is_an_ordinary_hand_installed_game(tmp_path,
                                                         monkeypatch):
    """The whole point: no second code path anywhere else."""
    from linux_prefix_hub.adapters import generic
    from linux_prefix_hub.core import newprefix
    _steam_with_builds(tmp_path, monkeypatch, builds=())
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: "/usr/bin/wine")
    _fake_run(monkeypatch, on_call=_boots(lambda env: env["WINEPREFIX"]))

    newprefix.create("Deus Ex")
    found = {g["game_name"]: g for g in generic.iter_games()}
    assert "Deus Ex" in found
    assert found["Deus Ex"]["prefix_path"] == str(
        paths.DEFAULT_PREFIX_ROOT / "Deus-Ex-1/pfx")


def test_a_folder_somewhere_else_is_remembered_so_it_is_found_again(
        tmp_path, monkeypatch):
    from linux_prefix_hub.core import db, newprefix
    _steam_with_builds(tmp_path, monkeypatch, builds=())
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: "/usr/bin/wine")
    _fake_run(monkeypatch, on_call=_boots(lambda env: env["WINEPREFIX"]))
    elsewhere = tmp_path / "disk2"
    db.set_config(newprefix.ROOT_KEY, str(elsewhere))

    result = newprefix.create("Thief")
    assert result.ok
    assert str(elsewhere) in db.extra_game_folders()


def test_a_target_for_one_folder_leaves_the_default_alone(tmp_path,
                                                          monkeypatch):
    """The disk with room on it is the ordinary reason to make one by hand."""
    from linux_prefix_hub.core import db, newprefix
    _steam_with_builds(tmp_path, monkeypatch, builds=())
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: "/usr/bin/wine")
    _fake_run(monkeypatch, on_call=_boots(lambda env: env["WINEPREFIX"]))
    disk = tmp_path / "disk2/games"

    result = newprefix.create("Gothic", target=disk)
    assert result.ok
    assert Path(result["path"]) == disk / "Gothic-1"
    # Once, not from now on -- and the scan is told where it went.
    assert newprefix.root() == paths.DEFAULT_PREFIX_ROOT
    assert str(disk) in db.extra_game_folders()


def test_the_remembered_place_is_an_absolute_one():
    """It ends up on another disk; a `~` that moves is not a place."""
    from linux_prefix_hub.core import newprefix
    assert newprefix.set_root("~/elsewhere") == Path.home() / "elsewhere"
    assert newprefix.set_root(None) == paths.DEFAULT_PREFIX_ROOT


def test_the_folder_path_always_carries_a_number(tmp_path, monkeypatch):
    """A build reads its app id out of that path, digits only.

    `protonfixes` does `re.findall(digits, STEAM_COMPAT_DATA_PATH)[-1]`, so a
    path without one raises IndexError and the launch dies before the game
    starts -- in the log of whatever started it, looking like broken Proton.
    """
    from linux_prefix_hub.core import newprefix
    _steam_with_builds(tmp_path, monkeypatch, builds=())
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: "/usr/bin/wine")
    _fake_run(monkeypatch, on_call=_boots(lambda env: env["WINEPREFIX"]))

    folder = Path(newprefix.create("DAoC Eden", alias="daoc-eden")["path"])
    assert folder.name == "daoc-eden-1"
    assert newprefix.read_marker(folder)["alias"] == "daoc-eden-1"
    # The name in the list is untouched: only the folder needs the number.
    assert newprefix.display_name(folder / "pfx") == "DAoC Eden"


def test_a_path_that_already_has_one_is_left_alone(tmp_path, monkeypatch):
    """Renaming what the user typed for no reason is the worse answer."""
    from linux_prefix_hub.core import newprefix
    _steam_with_builds(tmp_path, monkeypatch, builds=())
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: "/usr/bin/wine")
    _fake_run(monkeypatch, on_call=_boots(lambda env: env["WINEPREFIX"]))

    assert Path(newprefix.create("Quake 3")["path"]).name == "Quake-3"
    # The name, not the path around it: a folder that works on one disk and
    # stops working when it is copied to another is worse than a suffix.
    disk = tmp_path / "daten2/games"
    made = newprefix.create("Thief", target=disk)
    assert Path(made["path"]).name == "Thief-1"


def test_an_existing_folder_is_never_written_into(tmp_path, monkeypatch):
    from linux_prefix_hub.core import newprefix
    _steam_with_builds(tmp_path, monkeypatch, builds=())
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: "/usr/bin/wine")
    calls = _fake_run(monkeypatch)
    write(paths.DEFAULT_PREFIX_ROOT / "Thief-1/pfx/user.reg", "")

    result = newprefix.create("Thief")
    assert not result.ok
    assert calls == []


def test_a_boot_that_left_nothing_behind_is_a_failure(tmp_path, monkeypatch):
    """The exit code is not the answer -- the folder is."""
    from linux_prefix_hub.core import newprefix
    _steam_with_builds(tmp_path, monkeypatch, builds=())
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: "/usr/bin/wine")
    _fake_run(monkeypatch, outcome=1)

    result = newprefix.create("Broken")
    assert not result.ok
    assert "boom" in result.message
    assert newprefix.read_marker(paths.DEFAULT_PREFIX_ROOT / "Broken-1") == {}


def test_nothing_installed_is_said_plainly(tmp_path, monkeypatch):
    from linux_prefix_hub.core import newprefix
    _steam_with_builds(tmp_path, monkeypatch, builds=())
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: None)
    assert not newprefix.create("Anything").ok


# --- running something in one -------------------------------------------
def test_the_folder_remembers_which_version_made_it(tmp_path, monkeypatch):
    from linux_prefix_hub.core import newprefix
    _steam_with_builds(tmp_path, monkeypatch)
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: "/usr/bin/wine")
    _fake_run(monkeypatch, on_call=_boots(
        lambda env: Path(env["STEAM_COMPAT_DATA_PATH"]) / "pfx"))

    folder = Path(newprefix.create("Camelot", engine="GE-Proton10-34")
                  ["path"])
    assert newprefix.engine_of(folder) == "GE-Proton10-34"
    assert newprefix.owned(folder / "pfx") == folder
    # Somebody else's game folder is not ours to start programs in.
    assert newprefix.owned(tmp_path / "elsewhere/pfx") is None

    calls = _fake_run(monkeypatch)
    write(tmp_path / "setup.exe", "MZ")
    assert newprefix.install(folder, tmp_path / "setup.exe").ok
    command, env, cwd = calls[0]
    assert command[1:] == ["run", str(tmp_path / "setup.exe")]
    assert env["STEAM_COMPAT_DATA_PATH"] == str(folder)
    # An installer looks for its data files next to itself.
    assert cwd == str(tmp_path)


def test_the_settings_are_just_another_program(tmp_path, monkeypatch):
    from linux_prefix_hub.core import newprefix
    _steam_with_builds(tmp_path, monkeypatch, builds=())
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: "/usr/bin/wine")
    _fake_run(monkeypatch, on_call=_boots(lambda env: env["WINEPREFIX"]))
    folder = Path(newprefix.create("Old Game")["path"])

    calls = _fake_run(monkeypatch)
    assert newprefix.settings(folder).ok
    assert calls[0][0] == ["/usr/bin/wine", "winecfg"]


def test_a_program_that_is_not_there_is_never_started(tmp_path, monkeypatch):
    from linux_prefix_hub.core import newprefix
    calls = _fake_run(monkeypatch)
    result = newprefix.install(tmp_path, tmp_path / "nope.exe")
    assert not result.ok
    assert calls == []


# --- taking one away again ----------------------------------------------
def _made(tmp_path, monkeypatch, name="Gothic"):
    """One folder made by us, with the launch stubbed out."""
    from linux_prefix_hub.core import newprefix
    _steam_with_builds(tmp_path, monkeypatch, builds=())
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: "/usr/bin/wine")
    _fake_run(monkeypatch, on_call=_boots(lambda env: env["WINEPREFIX"]))
    return Path(newprefix.create(name)["path"])


def test_delete_takes_the_folder_and_everything_we_knew(tmp_path,
                                                        monkeypatch):
    from linux_prefix_hub.adapters import generic
    from linux_prefix_hub.core import db, newprefix
    folder = _made(tmp_path, monkeypatch)
    write(folder / "game/data.pak", "x")
    db.hide_game(generic.SOURCE, str(folder / "pfx"))

    result = newprefix.delete(folder)
    assert result.ok
    assert not folder.exists()
    assert db.get_prefix(db.fingerprint(folder / "pfx")) is None
    assert db.hidden_games() == []
    assert list(generic.iter_games()) == []


def test_delete_never_touches_a_folder_that_is_not_ours(tmp_path,
                                                        monkeypatch,
                                                        fake_prefix):
    """The folder this could be pointed at by mistake is full of games."""
    from linux_prefix_hub.core import newprefix
    theirs = fake_prefix.parent
    result = newprefix.delete(theirs)
    assert not result.ok
    assert fake_prefix.is_dir()


def test_moved_game_data_is_named_and_survives(tmp_path, monkeypatch):
    """It is not in the folder -- only a link to it is."""
    from linux_prefix_hub.core import db, newprefix
    folder = _made(tmp_path, monkeypatch)
    home = tmp_path / "moved/Gothic/Documents"
    write(home / "save.dat", "keep me")
    entry = db.get_prefix(db.fingerprint(folder / "pfx")) or {}
    entry["storage_locations"] = [
        {"where": "prefix", "win_path": "C:\\users\\steamuser\\Documents",
         "redirected": True, "redirect_target": str(home)}]
    db.upsert_prefix(entry)
    link = folder / "pfx/drive_c/users/steamuser/Documents"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(home)

    assert newprefix.moved_out(folder / "pfx") == [str(home)]
    result = newprefix.delete(folder)
    assert result.ok
    assert str(home) in result.message
    assert (home / "save.dat").read_text() == "keep me"


def test_a_folder_we_only_watched_for_that_game_is_dropped(tmp_path,
                                                           monkeypatch):
    from linux_prefix_hub.core import db, newprefix
    _steam_with_builds(tmp_path, monkeypatch, builds=())
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: "/usr/bin/wine")
    _fake_run(monkeypatch, on_call=_boots(lambda env: env["WINEPREFIX"]))
    disk = tmp_path / "disk2"
    folder = Path(newprefix.create("Sacred", target=disk)["path"])
    assert str(disk) in db.extra_game_folders()

    newprefix.delete(folder)
    assert db.extra_game_folders() == []


def test_a_folder_with_another_game_in_it_stays_watched(tmp_path,
                                                        monkeypatch):
    from linux_prefix_hub.core import db, newprefix
    _steam_with_builds(tmp_path, monkeypatch, builds=())
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: "/usr/bin/wine")
    _fake_run(monkeypatch, on_call=_boots(lambda env: env["WINEPREFIX"]))
    disk = tmp_path / "disk2"
    first = Path(newprefix.create("Sacred", target=disk)["path"])
    newprefix.create("Gothic", target=disk)

    newprefix.delete(first)
    assert db.extra_game_folders() == [str(disk)]


def test_a_running_game_is_never_deleted(tmp_path, monkeypatch):
    from linux_prefix_hub.core import newprefix, registry
    folder = _made(tmp_path, monkeypatch)
    monkeypatch.setattr(registry, "prefix_in_use", lambda _p: True)

    result = newprefix.delete(folder)
    assert not result.ok
    assert folder.is_dir()


# --- starting the game, which is where this app learns -------------------
def test_a_game_started_here_is_watched_like_any_other(tmp_path,
                                                       monkeypatch):
    """No launcher means no hook -- so the launch itself has to observe."""
    from linux_prefix_hub.core import db, newprefix
    folder = _made(tmp_path, monkeypatch, "Camelot")
    prefix = folder / "pfx"
    exe = folder / "game/camelot.exe"
    write(exe, "MZ")
    monkeypatch.setattr(newprefix, "_wait_until_idle", lambda _p: True)

    def writes_a_save(_command, _env, _cwd):
        write(prefix / "drive_c/users/steamuser/Documents/Camelot/save.dat",
              "progress")

    _fake_run(monkeypatch, on_call=writes_a_save)
    result = newprefix.launch(folder, exe)
    assert result.ok, result.message

    entry = db.get_prefix(db.fingerprint(prefix))
    assert entry is not None
    assert [loc["win_path"] for loc in entry["storage_locations"]] == \
        ["Documents/Camelot"]
    # And the program is remembered, relative to the folder that holds it.
    assert newprefix.read_marker(folder)["program"] == "game/camelot.exe"
    assert newprefix.program_of(folder) == exe


def test_the_wait_is_what_a_game_with_its_own_launcher_needs(tmp_path,
                                                             monkeypatch):
    """The launcher exits first; diffing then would read half a save file."""
    from linux_prefix_hub.core import newprefix, registry
    folder = _made(tmp_path, monkeypatch, "Camelot")
    write(folder / "launcher.exe", "MZ")
    _fake_run(monkeypatch)
    monkeypatch.setattr(newprefix, "POLL_SECONDS", 0)
    busy = [True, True, False]
    monkeypatch.setattr(registry, "prefix_in_use",
                        lambda _p: busy.pop(0) if busy else False)

    assert newprefix.launch(folder, folder / "launcher.exe").ok
    assert busy == []                  # waited until nothing was left


def test_a_game_next_to_the_windows_part_is_watched_too(tmp_path,
                                                        monkeypatch):
    """Saves written into the install folder (the Portal 2 case)."""
    from linux_prefix_hub.core import db, newprefix
    folder = _made(tmp_path, monkeypatch, "Camelot")
    exe = folder / "game/camelot.exe"
    write(exe, "MZ")
    monkeypatch.setattr(newprefix, "_wait_until_idle", lambda _p: True)

    def writes_beside_itself(_command, _env, _cwd):
        write(folder / "game/SAVE/slot1.sav", "progress")

    _fake_run(monkeypatch, on_call=writes_beside_itself)
    assert newprefix.launch(folder, exe).ok
    entry = db.get_prefix(db.fingerprint(folder / "pfx"))
    assert [loc["where"] for loc in entry["storage_locations"]] == \
        ["game_folder"]


def test_a_launcher_of_the_games_own_is_started_as_it_is(tmp_path,
                                                         monkeypatch):
    """The game that made this necessary comes with an AppImage launcher.

    It runs the game through a compatibility build of its own, pointed at
    this very folder -- so what it starts belongs here, and sending the
    launcher itself through `proton run` would hand Windows a Linux binary.
    """
    from linux_prefix_hub.core import db, newprefix
    folder = _made(tmp_path, monkeypatch, "Camelot")
    launcher = tmp_path / "AppImages/eden.appimage"
    write(launcher, "#!/bin/sh\n")
    monkeypatch.setattr(newprefix, "_wait_until_idle", lambda _p: True)

    def writes_through_its_own_proton(_command, env, _cwd):
        write(Path(env["WINEPREFIX"])
              / "drive_c/users/steamuser/AppData/Roaming/EA/eden.ini", "x")

    calls = _fake_run(monkeypatch, on_call=writes_through_its_own_proton)
    assert newprefix.launch(folder, launcher).ok

    command, env, _cwd = calls[0]
    assert command == [str(launcher)]          # as it is, not through wine
    assert env["WINEPREFIX"] == str(folder / "pfx")
    assert env["STEAM_COMPAT_DATA_PATH"] == str(folder)
    entry = db.get_prefix(db.fingerprint(folder / "pfx"))
    assert [loc["win_path"] for loc in entry["storage_locations"]] == \
        ["AppData/Roaming/EA"]
    # Where the launcher happens to live is somebody else's directory.
    assert entry.get("game_dir") in (None, "")


def test_the_folder_itself_is_never_a_second_space(tmp_path, monkeypatch):
    """It holds the prefix -- naming it would report every change twice."""
    from linux_prefix_hub.core import db, newprefix
    folder = _made(tmp_path, monkeypatch, "Camelot")
    exe = folder / "game.exe"
    write(exe, "MZ")
    monkeypatch.setattr(newprefix, "_wait_until_idle", lambda _p: True)

    def writes_a_save(_command, _env, _cwd):
        write(folder / "pfx/drive_c/users/steamuser/Documents/C/save.dat",
              "progress")

    _fake_run(monkeypatch, on_call=writes_a_save)
    assert newprefix.launch(folder, exe).ok
    entry = db.get_prefix(db.fingerprint(folder / "pfx"))
    assert [loc["where"] for loc in entry["storage_locations"]] == ["prefix"]


def test_a_launcher_that_handed_the_start_over_is_said_out_loud(tmp_path,
                                                                monkeypatch):
    """It exits at once, and "nothing changed" would be a useless answer.

    A launcher that finds itself already running gives the start to that
    instance and returns immediately -- which looks exactly like a game that
    came and went, except that nothing ever ran in the folder.
    """
    from linux_prefix_hub.core import newprefix
    folder = _made(tmp_path, monkeypatch, "Camelot")
    launcher = tmp_path / "eden.appimage"
    write(launcher, "#!/bin/sh\n")
    monkeypatch.setattr(newprefix, "_wait_until_idle", lambda _p: False)
    _fake_run(monkeypatch)

    result = newprefix.launch(folder, launcher)
    assert not result.ok
    assert "already open" in result.message


def test_the_wait_gives_up_only_when_nothing_ever_ran(tmp_path, monkeypatch):
    from linux_prefix_hub.core import newprefix, registry
    folder = _made(tmp_path, monkeypatch, "Camelot")
    monkeypatch.setattr(newprefix, "POLL_SECONDS", 0)
    monkeypatch.setattr(newprefix, "STARTUP_GRACE_SECONDS", 0)
    monkeypatch.setattr(registry, "prefix_in_use", lambda _p: False)
    assert newprefix._wait_until_idle(folder / "pfx") is False

    busy = [False, True, True, False]
    monkeypatch.setattr(newprefix, "STARTUP_GRACE_SECONDS", 30)
    monkeypatch.setattr(registry, "prefix_in_use",
                        lambda _p: busy.pop(0) if busy else False)
    assert newprefix._wait_until_idle(folder / "pfx") is True


def test_a_folder_that_is_not_ours_starts_nothing(tmp_path, monkeypatch,
                                                  fake_prefix):
    from linux_prefix_hub.core import newprefix
    calls = _fake_run(monkeypatch)
    write(tmp_path / "game.exe", "MZ")
    result = newprefix.launch(fake_prefix.parent, tmp_path / "game.exe")
    assert not result.ok
    assert calls == []


# --- the two names, and the version behind them --------------------------
def test_the_folder_carries_the_short_name_and_the_list_the_long_one(
        tmp_path, monkeypatch):
    from linux_prefix_hub.adapters import generic
    from linux_prefix_hub.core import newprefix
    _steam_with_builds(tmp_path, monkeypatch, builds=())
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: "/usr/bin/wine")
    _fake_run(monkeypatch, on_call=_boots(lambda env: env["WINEPREFIX"]))

    result = newprefix.create("Dark Age of Camelot", alias="daoc")
    assert result.ok
    folder = Path(result["path"])
    assert folder.name == "daoc-1"
    assert [g["game_name"] for g in generic.iter_games()] == \
        ["Dark Age of Camelot"]

    # Renaming changes the list, never the folder: a path is what everything
    # else points at by now.
    assert newprefix.rename(folder, "DAoC Eden").ok
    assert folder.name == "daoc-1"
    assert [g["game_name"] for g in generic.iter_games()] == ["DAoC Eden"]


def test_the_version_can_be_changed_afterwards(tmp_path, monkeypatch):
    """Which build to use is exactly what people try one after another."""
    from linux_prefix_hub.core import newprefix
    _steam_with_builds(tmp_path, monkeypatch,
                       builds=("GE-Proton10-34", "GE-Proton11-5"))
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: None)
    _fake_run(monkeypatch, on_call=_boots(
        lambda env: Path(env["STEAM_COMPAT_DATA_PATH"]) / "pfx"))
    folder = Path(newprefix.create("Camelot",
                                   engine="GE-Proton10-34")["path"])

    assert newprefix.set_engine(folder, "GE-Proton11-5").ok
    assert newprefix.engine_of(folder) == "GE-Proton11-5"
    assert not newprefix.set_engine(folder, "GE-Proton42-1").ok
    assert newprefix.engine_of(folder) == "GE-Proton11-5"

    calls = _fake_run(monkeypatch)
    newprefix.settings(folder)
    assert calls[0][0][0].endswith("GE-Proton11-5/proton")


# --- extra options, and a version of this folder's own -------------------
def _with_a_build(tmp_path, monkeypatch, name="Camelot"):
    """A folder made with a real-enough compatibility build to copy."""
    from linux_prefix_hub.core import gameopts, newprefix
    root = _steam_with_builds(tmp_path, monkeypatch)
    build = root / "compatibilitytools.d/GE-Proton10-34"
    write(build / gameopts.DEFAULT_PFX / "drive_c/windows/x.dll", "")
    write(build / "compatibilitytool.vdf",
          '"compatibilitytools"\n{\n  "compat_tools"\n  {\n'
          '    "GE-Proton10-34"\n    {\n'
          '      "display_name" "GE-Proton10-34"\n    }\n  }\n}\n')
    write(build / "version", "1700000000 GE-Proton10-34\n")
    write(build / "user_settings.py", "user_settings = {}\n")
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: None)
    _fake_run(monkeypatch, on_call=_boots(
        lambda env: Path(env["STEAM_COMPAT_DATA_PATH"]) / "pfx"))
    return Path(newprefix.create(name, alias="camelot")["path"]), root


def test_options_for_our_own_folder_need_no_build_at_all(tmp_path,
                                                         monkeypatch):
    """We start that game ourselves, so the profile is just its environment.

    The whole private-build machinery exists because Steam starts its games
    inside a container that filters the environment. Nobody filters ours.
    """
    from linux_prefix_hub.core import gameopts, newprefix
    folder = _made(tmp_path, monkeypatch, "Camelot")
    write(folder / "game.exe", "MZ")
    game = newprefix.as_game(folder)
    gameopts.write(game["source"], game["app_id"],
                   {"switches": ["overlay"], "custom": "DXVK_HUD=fps\n"})

    assert gameopts.turn_on(game).ok
    monkeypatch.setattr(newprefix, "_wait_until_idle", lambda _p: True)
    calls = _fake_run(monkeypatch)
    assert newprefix.launch(folder, folder / "game.exe").ok

    _command, env, _cwd = calls[0]
    assert env["MANGOHUD"] == "1"
    assert env["DXVK_HUD"] == "fps"

    # And off again means off: nothing is left in the environment.
    assert gameopts.turn_off(game).ok
    calls = _fake_run(monkeypatch)
    newprefix.launch(folder, folder / "game.exe")
    assert "MANGOHUD" not in calls[0][1]


def test_a_version_of_its_own_is_what_a_foreign_launcher_can_read(
        tmp_path, monkeypatch):
    """The reason this exists: something else starts the game.

    A launcher of the game's own does not ask us for an environment, but it
    can be pointed at a build -- and a build reads its own settings file.
    """
    from linux_prefix_hub.core import gameopts, newprefix
    folder, root = _with_a_build(tmp_path, monkeypatch)
    game = newprefix.as_game(folder)
    gameopts.write(game["source"], game["app_id"], {"switches": ["overlay"]})

    result = newprefix.make_private(folder)
    assert result.ok, result.message
    copy = newprefix.private_build(folder)
    assert copy is not None
    assert copy.parent == root / "compatibilitytools.d"
    assert copy.name == "LinuxPrefixHub-camelot-1"
    assert str(copy) in result.message      # so it can be pointed at

    # Turning the options on writes them where such a launch can read them.
    assert gameopts.turn_on(game).ok
    settings = (copy / gameopts.SETTINGS).read_text(encoding="utf-8")
    assert '"MANGOHUD": "1"' in settings
    # The original build is untouched -- the copy is hardlinks (rule 15).
    assert '"MANGOHUD"' not in (root / "compatibilitytools.d/GE-Proton10-34"
                                / gameopts.SETTINGS).read_text(
                                    encoding="utf-8")


def test_the_copy_is_what_starts_the_game_once_it_exists(tmp_path,
                                                         monkeypatch):
    from linux_prefix_hub.core import newprefix
    folder, _root = _with_a_build(tmp_path, monkeypatch)
    assert newprefix.make_private(folder).ok

    calls = _fake_run(monkeypatch)
    newprefix.settings(folder)
    assert calls[0][0][0].endswith("LinuxPrefixHub-camelot-1/proton")

    assert newprefix.drop_private(folder).ok
    assert newprefix.private_build(folder) is None
    calls = _fake_run(monkeypatch)
    newprefix.settings(folder)
    assert calls[0][0][0].endswith("GE-Proton10-34/proton")


def test_deleting_the_folder_takes_its_own_version_with_it(tmp_path,
                                                           monkeypatch):
    from linux_prefix_hub.core import gameopts, newprefix
    folder, _root = _with_a_build(tmp_path, monkeypatch)
    assert newprefix.make_private(folder).ok
    copy = newprefix.private_build(folder)

    assert newprefix.delete(folder).ok
    assert not copy.exists()
    assert gameopts.profiles() == {}


def test_a_folder_on_the_system_wine_cannot_have_a_copy(tmp_path,
                                                        monkeypatch):
    from linux_prefix_hub.core import newprefix
    folder = _made(tmp_path, monkeypatch, "Camelot")
    result = newprefix.make_private(folder)
    assert not result.ok
    assert newprefix.private_build(folder) is None


# --- what a build asks for, and what else can start it -------------------
def test_the_runtime_a_build_needs_is_read_and_said(tmp_path, monkeypatch):
    """The trap: a launcher that always uses one runtime, and a build that
    asks for another. It fails inside that launcher, as a Python traceback.
    """
    from linux_prefix_hub.core import newprefix
    root = _steam_with_builds(tmp_path, monkeypatch,
                              builds=("GE-Proton10-34", "GE-Proton11-5"))
    tools = root / "compatibilitytools.d"
    write(tools / "GE-Proton10-34/toolmanifest.vdf",
          '"manifest"\n{\n  "version" "2"\n'
          '  "require_tool_appid" "1628350"\n}\n')
    write(tools / "GE-Proton11-5/toolmanifest.vdf",
          '"manifest"\n{\n  "version" "2"\n'
          '  "require_tool_appid" "4183110"\n}\n')
    monkeypatch.setattr(newprefix.shutil, "which", lambda _n: None)

    assert newprefix.required_runtime("GE-Proton11-5")[1] == \
        "Steam Linux Runtime 4.0"
    assert "4.0" in newprefix.runtime_warning("GE-Proton11-5")
    # The one everything uses anyway needs no sentence.
    assert newprefix.runtime_warning("GE-Proton10-34") == ""
    assert newprefix.required_runtime("nothing-installed") == ("", "")


def test_a_second_folder_can_be_named_and_is_watched(tmp_path, monkeypatch):
    """A launcher of the game's own keeps the install where it likes."""
    from linux_prefix_hub.core import db, newprefix
    folder = _made(tmp_path, monkeypatch, "Camelot")
    install = tmp_path / "disk/dark-age-of-camelot"
    install.mkdir(parents=True)
    launcher = tmp_path / "eden.appimage"
    write(launcher, "#!/bin/sh\n")

    # Not the folder itself: it holds the prefix, so everything inside would
    # be reported twice.
    assert not newprefix.set_watch_dir(folder, folder).ok
    assert newprefix.set_watch_dir(folder, install).ok
    assert newprefix.watch_dir(folder) == install

    monkeypatch.setattr(newprefix, "_wait_until_idle", lambda _p: True)

    def writes_into_the_install(_command, _env, _cwd):
        write(install / "SAVE/slot1.sav", "progress")

    _fake_run(monkeypatch, on_call=writes_into_the_install)
    assert newprefix.launch(folder, launcher).ok
    entry = db.get_prefix(db.fingerprint(folder / "pfx"))
    assert entry["game_dir"] == str(install)
    assert [loc["where"] for loc in entry["storage_locations"]] == \
        ["game_folder"]

    assert newprefix.set_watch_dir(folder, None).ok
    assert newprefix.watch_dir(folder) is None


def test_a_menu_entry_starts_the_game_through_us(tmp_path, monkeypatch):
    """Not through the window: it would stay busy for the whole session."""
    from linux_prefix_hub.core import newprefix
    folder = _made(tmp_path, monkeypatch, "Camelot")
    write(folder / "game.exe", "MZ")

    # Nothing to start yet, so nothing to put in a menu.
    assert not newprefix.make_shortcut(folder).ok
    newprefix.set_program(folder, folder / "game.exe")

    result = newprefix.make_shortcut(folder)
    assert result.ok
    entry = newprefix.shortcut_file(folder)
    text = entry.read_text(encoding="utf-8")
    assert f'--play "{folder}"' in text
    assert "Name=Camelot" in text

    assert newprefix.drop_shortcut(folder).ok
    assert not entry.exists()


def test_deleting_the_folder_takes_its_menu_entry_with_it(tmp_path,
                                                          monkeypatch):
    from linux_prefix_hub.core import newprefix
    folder = _made(tmp_path, monkeypatch, "Camelot")
    write(folder / "game.exe", "MZ")
    newprefix.set_program(folder, folder / "game.exe")
    assert newprefix.make_shortcut(folder).ok
    entry = newprefix.shortcut_file(folder)

    assert newprefix.delete(folder).ok
    assert not entry.exists()


def test_rebuilding_covers_a_folders_own_version(tmp_path, monkeypatch):
    """It is the copy that ages, and nothing else was refreshing it."""
    from linux_prefix_hub.core import gameopts, newprefix
    folder, root = _with_a_build(tmp_path, monkeypatch)
    assert newprefix.make_private(folder).ok
    copy = newprefix.private_build(folder)
    write(copy / "left-over-from-the-old-one", "x")

    results = gameopts.rebuild_all()
    assert results and all(r.ok for r in results)
    again = newprefix.private_build(folder)
    assert again is not None and again.name == copy.name
    assert not (again / "left-over-from-the-old-one").exists()


def test_a_copy_for_two_folders_of_the_same_name_stays_a_directory_name(
        tmp_path, monkeypatch):
    """A hand-installed game's id is its path, and paths have slashes."""
    from linux_prefix_hub.core import gameopts, newprefix
    folder, root = _with_a_build(tmp_path, monkeypatch)
    assert newprefix.make_private(folder).ok

    # A second folder of the same short name, somewhere else entirely.
    other = tmp_path / "disk2"
    second = Path(newprefix.create("Camelot", alias="camelot",
                                   target=other)["path"])
    name = gameopts.wanted_name(newprefix.as_game(second))
    assert "/" not in name
    assert newprefix.make_private(second).ok
    copies = sorted(p.name for p in (root / "compatibilitytools.d").iterdir()
                    if p.name.startswith("LinuxPrefixHub-"))
    assert len(copies) == 2 and all("/" not in c for c in copies)


# --- the same, for a folder this app did not make ------------------------
def _lutris_game(tmp_path, monkeypatch):
    """A game folder somebody else made, as discovery hands it over."""
    from linux_prefix_hub.adapters import base
    prefix = tmp_path / "lutris/skyrim"
    (prefix / "drive_c/users/steamuser").mkdir(parents=True)
    write(prefix / "user.reg", "WINE REGISTRY Version 2\n")
    write(prefix / "system.reg", "WINE REGISTRY Version 2\n")
    assert base.is_prefix(prefix)
    return {"source": "lutris", "app_id": "skyrim", "game_name": "Skyrim",
            "prefix_path": str(prefix)}


def test_any_game_folder_can_get_a_version_of_its_own(tmp_path,
                                                      monkeypatch):
    """The point of dropping "your own environments": this covers it.

    A named build carrying settings, for a game some other launcher starts
    -- that was the whole use of a standalone environment, and here it comes
    with the game's own folder attached instead of floating free.
    """
    from linux_prefix_hub.core import gameopts, newprefix
    _with_a_build(tmp_path, monkeypatch)          # a build to copy
    game = _lutris_game(tmp_path, monkeypatch)
    assert newprefix.foreign(game)

    gameopts.write(game["source"], game["app_id"], {"switches": ["overlay"]})
    result = newprefix.make_private_for(game)
    assert result.ok, result.message

    copy = newprefix.private_build_for(game)
    assert copy is not None and copy.name == "LinuxPrefixHub-Skyrim"
    settings = (copy / gameopts.SETTINGS).read_text(encoding="utf-8")
    assert '"MANGOHUD": "1"' in settings
    # Where to point the launcher is in the message, because nothing else
    # can point it there for them.
    assert str(copy) in result.message


def test_the_version_of_a_foreign_folder_lives_in_the_profile(tmp_path,
                                                              monkeypatch):
    """Their folder is not ours to write a marker into."""
    from linux_prefix_hub.core import newprefix
    _with_a_build(tmp_path, monkeypatch, "Camelot")
    game = _lutris_game(tmp_path, monkeypatch)

    assert newprefix.set_engine_for(game, "GE-Proton10-34").ok
    assert newprefix.engine_for(game) == "GE-Proton10-34"
    assert not (Path(game["prefix_path"]).parent
                / newprefix.MARKER).exists()
    assert not newprefix.set_engine_for(game, "GE-Proton42-1").ok


def test_a_steam_game_keeps_the_one_way_it_already_has(tmp_path,
                                                       monkeypatch):
    """Steam can be pointed at the copy itself, so it is not offered twice."""
    from linux_prefix_hub.core import newprefix
    assert not newprefix.foreign({"source": "steam", "app_id": "1091500",
                                  "prefix_path": str(tmp_path / "pfx")})
    # And a game nobody has started has no folder to give a build to.
    assert not newprefix.foreign({"source": "lutris", "app_id": "x",
                                  "prefix_path": None})


def test_a_hand_made_folder_is_left_untouched_apart_from_the_copy(
        tmp_path, monkeypatch):
    """`~/.wine-osu` and friends: the copy is ours, the folder is theirs."""
    from linux_prefix_hub.adapters import generic
    from linux_prefix_hub.core import gameopts, newprefix
    _with_a_build(tmp_path, monkeypatch)
    prefix = tmp_path / "wine-osu"
    (prefix / "drive_c/users/tokajer").mkdir(parents=True)
    write(prefix / "user.reg", "WINE REGISTRY Version 2\n")
    write(prefix / "system.reg", "WINE REGISTRY Version 2\n")
    game = generic.game_for(prefix)

    assert newprefix.foreign(game)
    assert gameopts.turn_on(game).ok
    assert newprefix.private_build_for(game) is not None
    assert not (prefix / newprefix.MARKER).exists()
    assert sorted(p.name for p in prefix.iterdir()) == \
        ["drive_c", "system.reg", "user.reg"]

    # And off again leaves the copy, because that is the version, not the
    # options.
    assert gameopts.turn_off(game).ok
    assert newprefix.private_build_for(game) is not None
