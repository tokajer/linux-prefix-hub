# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Extra options: the profile, and the private compatibility build.

Most of what is pinned here is a way this could destroy something the user
did not ask it to touch. The copy is made of hardlinks, so a careless write
lands in the build it was copied from; the directory it lives in also holds
builds the user installed themselves.
"""
from __future__ import annotations

import ast
import shutil

import pytest

from conftest import write

# The real thing, comments and all -- what `rewrite_manifest` has to survive.
MANIFEST = '''"compatibilitytools"
{
  "compat_tools"
  {
    "GE-Proton10-34" // Internal name of this tool
    {
      // Can register this tool with Steam in two ways:
      //
      // - The tool can be placed as a subdirectory in compatibilitytools.d
      "install_path" "."

      // For this template, we're going to substitute the display_name key:
      "display_name" "GE-Proton10-34"

      "from_oslist"  "windows"
      "to_oslist"    "linux"
    }
  }
}
'''


def _fake_steam(tmp_path, monkeypatch, builds=("GE-Proton10-34",)):
    """A Steam root with compatibility builds and one installed game."""
    from linux_prefix_hub.adapters import steam
    from linux_prefix_hub.core import gameopts
    root = tmp_path / ".steam/steam"
    steamapps = root / "steamapps"
    steamapps.mkdir(parents=True)
    write(steamapps / "appmanifest_1091500.acf",
          '"AppState"{"appid" "1091500" "name" "Cyberpunk 2077" '
          '"StateFlags" "4" "installdir" "Cyberpunk 2077"}')
    (steamapps / "compatdata/1091500/pfx/drive_c/users/steamuser").mkdir(
        parents=True)
    write(root / "config/config.vdf",
          '"InstallConfigStore"{"Software"{"Valve"{"Steam"{'
          '"CompatToolMapping"{}}}}}')

    tools = root / "compatibilitytools.d"
    for name in builds:
        build = tools / name
        build.mkdir(parents=True)
        write(build / "proton", "#!/usr/bin/env python3\n")
        write(build / "compatibilitytool.vdf",
              MANIFEST.replace("GE-Proton10-34", name))
        write(build / "user_settings.py", "user_settings = {}\n")
        # The folder every game folder is stamped out of. A build without it
        # starts nothing, so a fixture without it is not a build.
        write(build / gameopts.DEFAULT_PFX
              / "drive_c/windows/system32/d3d8.dll", "")
        # Every build carries one, and it is what `outdated` compares.
        write(build / "version", f"1700000000 {name}\n")

    monkeypatch.setattr(steam, "STEAM_ROOT_CANDIDATES", [str(root)])
    monkeypatch.setattr(steam, "steam_is_running", lambda: False)
    return root, tools


def _game():
    return {"source": "steam", "app_id": "1091500",
            "game_name": "Cyberpunk 2077", "prefix_path": None}


# --- the profile ---------------------------------------------------------
def test_profile_round_trip():
    from linux_prefix_hub.core import gameopts
    gameopts.write("steam", "1091500",
                   {"enabled": True, "switches": ["overlay", "nonsense"],
                    "custom": "FOO=bar\n", "base": "GE-Proton"})
    profile = gameopts.read("steam", "1091500")

    assert profile["enabled"] is True
    assert profile["switches"] == ["overlay"]     # unknown ones are dropped
    assert gameopts.enabled_games() == [("steam", "1091500")]


def test_parse_custom_skips_what_it_cannot_read():
    from linux_prefix_hub.core import gameopts
    env = gameopts.parse_custom(
        "\n# a comment\nFOO = bar \nnot an assignment\n  = novalue\nB=\n")
    assert env == {"FOO": "bar", "B": ""}


def test_custom_lines_win_over_a_switch():
    from linux_prefix_hub.core import gameopts
    env = gameopts.env_for({"switches": ["overlay", "fps"],
                            "custom": "MANGOHUD=0\n"})
    assert env["MANGOHUD"] == "0"
    # The other switch is untouched.
    assert env["DXVK_HUD"] == "fps,frametime"


def test_the_overlay_switch_does_not_replace_your_mangohud_settings():
    """MANGOHUD_CONFIG *replaces* ~/.config/MangoHud/MangoHud.conf.

    So a helpful default layout here would silently throw away whatever the
    user set up in Goverlay. Turning it on is ours; what it shows is theirs.
    """
    from linux_prefix_hub.core import gameopts
    assert gameopts.env_for({"switches": ["overlay"]}) == {"MANGOHUD": "1"}


def test_non_numeric_app_id_gets_an_app_id_to_report():
    """A game folder with no number in it makes the build refuse to start."""
    from linux_prefix_hub.core import gameopts
    assert gameopts.env_for({}, "1091500") == {}
    assert gameopts.env_for({}, "my-old-game") == {"SteamAppId": "1"}
    # ...unless the user named one themselves.
    env = gameopts.env_for({"custom": "SteamAppId=480\n"}, "my-old-game")
    assert env["SteamAppId"] == "480"


# --- generating user_settings.py ----------------------------------------
def test_windows_path_survives_into_valid_python():
    """`C:\\Users\\...` unescaped is `\\U` to Python, and the build aborts."""
    from linux_prefix_hub.core import gameopts
    text = gameopts.render_settings(
        {"DXVK_CONFIG_FILE": r"C:\Users\steamuser\dxvk.conf",
         "QUOTED": 'a "b" c'})
    body = text.partition("=")[2].strip()
    parsed = ast.literal_eval(body)

    assert parsed["DXVK_CONFIG_FILE"] == r"C:\Users\steamuser\dxvk.conf"
    assert parsed["QUOTED"] == 'a "b" c'


def test_writing_settings_leaves_the_original_build_alone(tmp_path,
                                                          monkeypatch):
    """The whole point of unlinking first: the copy shares its files."""
    import os

    from linux_prefix_hub.core import gameopts
    root, tools = _fake_steam(tmp_path, monkeypatch)
    base = tools / "GE-Proton10-34"
    copy = tools / "LinuxPrefixHub-Cyberpunk-2077"
    copy.mkdir()
    os.link(base / "user_settings.py", copy / "user_settings.py")
    assert (base / "user_settings.py").stat().st_nlink == 2

    gameopts.write_user_settings(copy, {"MANGOHUD": "1"})

    assert (base / "user_settings.py").read_text() == "user_settings = {}\n"
    assert "MANGOHUD" in (copy / "user_settings.py").read_text()


def test_manifest_keeps_every_comment(tmp_path, monkeypatch):
    from linux_prefix_hub.core import gameopts
    _root, tools = _fake_steam(tmp_path, monkeypatch)
    directory = tools / "GE-Proton10-34"

    gameopts.rewrite_manifest(directory, "LinuxPrefixHub-Cyberpunk-2077",
                              "Cyberpunk 2077 (Linux Prefix Hub)")
    text = (directory / "compatibilitytool.vdf").read_text()

    assert '"LinuxPrefixHub-Cyberpunk-2077" // Internal name of this tool' in text
    assert '"display_name" "Cyberpunk 2077 (Linux Prefix Hub)"' in text
    assert "GE-Proton10-34" not in text
    # The build author's explanation of the format is not ours to delete.
    assert text.count("//") == MANIFEST.count("//")
    assert '"install_path" "."' in text


# --- picking a build ------------------------------------------------------
def test_newest_is_found_within_a_family_only(tmp_path, monkeypatch):
    """Sorting across vendors ranks Proton-Tkg over GE-Proton on the T."""
    from linux_prefix_hub.core import gameopts
    _root, _tools = _fake_steam(
        tmp_path, monkeypatch,
        builds=("GE-Proton9-27", "GE-Proton10-34", "Proton-Tkg-2493"))

    assert gameopts.detect_base("GE-Proton") == "GE-Proton10-34"
    assert gameopts.detect_base("Proton-Tkg") == "Proton-Tkg-2493"
    assert gameopts.resolve_base("GE-Proton9-27") == "GE-Proton9-27"
    assert gameopts.detect_base("Nothing-Like-This") == ""


def test_our_own_copies_are_never_offered_as_a_base(tmp_path, monkeypatch):
    from linux_prefix_hub.core import gameopts
    _root, tools = _fake_steam(tmp_path, monkeypatch)
    gameopts.turn_on(_game())

    assert gameopts.list_bases() == ["GE-Proton10-34"]
    assert (tools / "LinuxPrefixHub-Cyberpunk-2077").is_dir()


# --- the destructive half -------------------------------------------------
def test_a_directory_that_is_not_ours_is_left_alone(tmp_path, monkeypatch):
    """Someone else's folder sitting on the name we would use.

    `build` refuses rather than overwrite it, and `remove` does not claim it
    -- what is not ours is not ours to delete, and the alternative is an
    `rm -rf` on a build the user installed themselves.
    """
    from linux_prefix_hub.core import gameopts
    _root, tools = _fake_steam(tmp_path, monkeypatch)
    stranger = tools / "LinuxPrefixHub-Cyberpunk-2077"
    stranger.mkdir()
    write(stranger / "important", "not ours")

    built = gameopts.build(_game(), gameopts.read("steam", "1091500"))

    # We step around the name rather than take it, and their folder is whole.
    assert built.ok is True
    assert built["name"] == "LinuxPrefixHub-Cyberpunk-2077-1091500"
    assert (stranger / "important").read_text() == "not ours"

    removed = gameopts.remove("steam", "1091500")
    assert removed.ok is True
    assert (stranger / "important").exists()


def test_build_refuses_while_the_game_is_running(tmp_path, monkeypatch):
    """Rebuilding deletes the copy the running game executes out of."""
    from linux_prefix_hub.core import gameopts, registry
    _root, _tools = _fake_steam(tmp_path, monkeypatch)
    monkeypatch.setattr(registry, "prefix_in_use", lambda _p: True)

    game = dict(_game(), prefix_path=str(tmp_path / "pfx"))
    result = gameopts.build(game, gameopts.read("steam", "1091500"))

    assert result.ok is False
    assert "running" in result.message


# --- the two whole operations --------------------------------------------
def test_turn_on_builds_and_points_steam_at_it(tmp_path, monkeypatch):
    from linux_prefix_hub.adapters import steam
    from linux_prefix_hub.core import gameopts
    root, tools = _fake_steam(tmp_path, monkeypatch)

    gameopts.write("steam", "1091500",
                   {"enabled": True, "switches": ["overlay"], "custom": ""})
    result = gameopts.turn_on(_game())

    assert result.ok, result.message
    copy = tools / "LinuxPrefixHub-Cyberpunk-2077"
    assert (copy / "proton").exists()
    assert "MANGOHUD" in (copy / "user_settings.py").read_text()
    assert gameopts.built_from(copy) == "GE-Proton10-34"
    assert steam.compat_tool("1091500") == "LinuxPrefixHub-Cyberpunk-2077"
    assert gameopts.read("steam", "1091500")["built"] == "GE-Proton10-34"


def test_turn_off_clears_the_mapping_and_removes_the_copy(tmp_path,
                                                          monkeypatch):
    from linux_prefix_hub.adapters import steam
    from linux_prefix_hub.core import gameopts
    root, tools = _fake_steam(tmp_path, monkeypatch)
    gameopts.turn_on(_game())

    result = gameopts.turn_off(_game())

    assert result.ok, result.message
    assert not (tools / "LinuxPrefixHub-Cyberpunk-2077").exists()
    assert steam.compat_tool("1091500") == ""
    assert gameopts.read("steam", "1091500")["enabled"] is False


def test_a_choice_the_user_made_themselves_is_left_alone(tmp_path,
                                                         monkeypatch):
    """`turn_off` only takes back a mapping that still names our own copy."""
    from linux_prefix_hub.adapters import steam
    from linux_prefix_hub.core import gameopts
    root, _tools = _fake_steam(tmp_path, monkeypatch)
    gameopts.turn_on(_game())
    steam.set_compat_tool("1091500", "GE-Proton10-34")

    gameopts.turn_off(_game())

    assert steam.compat_tool("1091500") == "GE-Proton10-34"


def test_rebuild_all_follows_the_family(tmp_path, monkeypatch):
    from linux_prefix_hub.core import gameopts
    root, tools = _fake_steam(tmp_path, monkeypatch)
    gameopts.turn_on(_game())
    assert gameopts.outdated(gameopts.read("steam", "1091500")) == ""

    newer = tools / "GE-Proton10-35"
    newer.mkdir()
    write(newer / "proton", "#!/usr/bin/env python3\n")
    write(newer / "compatibilitytool.vdf",
          MANIFEST.replace("GE-Proton10-34", "GE-Proton10-35"))
    write(newer / gameopts.DEFAULT_PFX / "drive_c/windows/system32/d3d8.dll",
          "")

    assert gameopts.outdated(gameopts.read("steam", "1091500")) \
        == "GE-Proton10-35"
    results = gameopts.rebuild_all()

    assert [r.ok for r in results] == [True]
    assert gameopts.built_from(tools / "LinuxPrefixHub-Cyberpunk-2077") \
        == "GE-Proton10-35"


def test_an_incomplete_build_is_refused_before_it_is_copied(tmp_path,
                                                             monkeypatch):
    """A "latest" folder mid-update is a build with nothing in it.

    Copying it works perfectly and produces a faithful copy of something
    broken; the game then fails inside Steam's log, a long way from the
    switch that caused it. Real case: `Proton-GE Latest` with its
    `default_pfx` gone.
    """
    from linux_prefix_hub.core import gameopts
    _root, tools = _fake_steam(tmp_path, monkeypatch)
    shutil.rmtree(tools / "GE-Proton10-34" / gameopts.DEFAULT_PFX)

    result = gameopts.turn_on(_game())

    assert result.ok is False
    assert "GE-Proton10-34" in result.message
    assert not (tools / "LinuxPrefixHub-Cyberpunk-2077").exists()


def test_only_steam_for_now():
    from linux_prefix_hub.core import gameopts
    result = gameopts.turn_on({"source": "lutris", "app_id": "x",
                               "game_name": "X"})
    assert result.ok is False
    assert "Steam" in result.message


# --- Steam's own settings file -------------------------------------------
def test_compat_tool_write_keeps_a_backup(tmp_path, monkeypatch):
    from linux_prefix_hub.adapters import steam
    root, _tools = _fake_steam(tmp_path, monkeypatch)
    cfg = write(root / "config/config.vdf",
                '"InstallConfigStore"{"Software"{"Valve"{"Steam"{'
                '"CompatToolMapping"{"7"{"name" "Proton" "config" "" '
                '"priority" "250"}}}}}}')
    before = cfg.read_text()

    assert steam.set_compat_tool("1091500", "LinuxPrefixHub-Cyberpunk-2077").ok
    assert cfg.with_suffix(".vdf.bak").read_text() == before
    # Somebody else's entry is still there.
    assert steam.compat_tool("7") == "Proton"


def test_compat_tool_refuses_while_steam_runs(tmp_path, monkeypatch):
    from linux_prefix_hub.adapters import steam
    root, _tools = _fake_steam(tmp_path, monkeypatch)
    monkeypatch.setattr(steam, "steam_is_running", lambda: True)

    result = steam.set_compat_tool("1091500", "LinuxPrefixHub-Cyberpunk-2077")

    assert result.ok is False
    assert result.manual is True
    # The name is handed over so the user can pick it themselves.
    assert result["detail"]["tool_name"] == "LinuxPrefixHub-Cyberpunk-2077"
    assert steam.compat_tool("1091500") == ""


@pytest.mark.parametrize("running", [True, False])
def test_uninstall_takes_the_options_back_first(tmp_path, monkeypatch,
                                                running):
    from linux_prefix_hub.adapters import steam
    from linux_prefix_hub.core import gameopts, uninstall
    root, tools = _fake_steam(tmp_path, monkeypatch)
    gameopts.turn_on(_game())

    monkeypatch.setattr(steam, "steam_is_running", lambda: running)
    plan = uninstall.plan()
    assert [g["app_id"] for g in plan["options"]] == ["1091500"]

    if running:
        # Steam holds the file the mapping lives in, so this is not the moment.
        assert plan["blockers"]
        return

    assert not plan["blockers"]
    assert uninstall.clear_options_all()["ok"]
    assert steam.compat_tool("1091500") == ""
    assert not (tools / "LinuxPrefixHub-Cyberpunk-2077").exists()


# --- naming a copy --------------------------------------------------------
def test_a_copy_is_named_after_the_game(tmp_path, monkeypatch):
    """A folder list full of app ids tells nobody which game is which."""
    from linux_prefix_hub.core import gameopts
    root, tools = _fake_steam(tmp_path, monkeypatch)

    assert gameopts.turn_on(_game()).ok
    copy = tools / "LinuxPrefixHub-Cyberpunk-2077"

    assert copy.is_dir()
    assert gameopts.find_instance("steam", "1091500") == copy
    # One name, used for both keys in the manifest: a launcher that reads
    # both would otherwise list the same copy twice.
    text = (copy / "compatibilitytool.vdf").read_text()
    assert '"LinuxPrefixHub-Cyberpunk-2077" // Internal name' in text
    assert '"display_name" "LinuxPrefixHub-Cyberpunk-2077"' in text


def test_two_games_of_the_same_name_do_not_collide(tmp_path, monkeypatch):
    from linux_prefix_hub.core import gameopts
    _root, tools = _fake_steam(tmp_path, monkeypatch)
    first = dict(_game(), app_id="1", game_name="Doom")
    second = dict(_game(), app_id="2", game_name="Doom")

    assert gameopts.turn_on(first).ok
    assert gameopts.turn_on(second).ok

    assert (tools / "LinuxPrefixHub-Doom").is_dir()
    assert (tools / "LinuxPrefixHub-Doom-2").is_dir()
    assert gameopts.find_instance("steam", "1").name == "LinuxPrefixHub-Doom"
    assert gameopts.find_instance("steam", "2").name == "LinuxPrefixHub-Doom-2"


def test_a_copy_from_the_old_naming_is_replaced_not_stranded(tmp_path,
                                                             monkeypatch):
    """The first release named copies after the app id."""
    from linux_prefix_hub.core import gameopts
    _root, tools = _fake_steam(tmp_path, monkeypatch)
    old = tools / "LinuxPrefixHub-1091500"
    old.mkdir()
    write(old / gameopts.MARKER, "GE-Proton10-34\n")   # the plain-text form

    assert gameopts.find_instance("steam", "1091500") == old
    assert gameopts.turn_on(_game()).ok

    assert not old.exists()
    assert (tools / "LinuxPrefixHub-Cyberpunk-2077").is_dir()


def test_a_swapped_latest_folder_is_noticed(tmp_path, monkeypatch):
    """A name that never changes while what is behind it is replaced.

    Comparing names calls such a copy current forever. This is the case the
    build's own version file is read for.
    """
    from linux_prefix_hub.core import gameopts
    _root, tools = _fake_steam(tmp_path, monkeypatch, builds=("Latest",))
    write(tools / "Latest" / "version", "1700000000 GE-Proton10-34\n")
    gameopts.write("steam", "1091500",
                   {"enabled": True, "base": "Latest", "switches": [],
                    "custom": ""})
    assert gameopts.turn_on(_game()).ok
    assert gameopts.outdated(gameopts.read("steam", "1091500")) == ""

    # The other tool replaces what is behind the name. Nothing renamed.
    write(tools / "Latest" / "version", "1800000000 GE-Proton11-5\n")

    assert gameopts.outdated(gameopts.read("steam", "1091500")) == "Latest"


def test_a_family_that_moved_on_is_still_noticed(tmp_path, monkeypatch):
    """The case a name does show -- it must not have been broken by the fix."""
    from linux_prefix_hub.core import gameopts
    _root, tools = _fake_steam(tmp_path, monkeypatch)
    assert gameopts.turn_on(_game()).ok
    assert gameopts.outdated(gameopts.read("steam", "1091500")) == ""

    newer = tools / "GE-Proton11-5"
    newer.mkdir()
    write(newer / "proton", "#!/usr/bin/env python3\n")
    write(newer / gameopts.DEFAULT_PFX / "drive_c/windows/system32/d3d8.dll",
          "")

    assert gameopts.outdated(gameopts.read("steam", "1091500")) \
        == "GE-Proton11-5"


