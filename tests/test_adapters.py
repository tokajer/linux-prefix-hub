"""Adapters: discovery and hook injection against fake launcher installs."""
from __future__ import annotations

import sqlite3

from conftest import write


# --- Steam ---------------------------------------------------------------
def _fake_steam(tmp_path, monkeypatch, *, started=True):
    from linux_prefix_hub.adapters import steam
    root = tmp_path / ".steam/steam"
    steamapps = root / "steamapps"
    lib2 = tmp_path / "games/SteamLibrary/steamapps"
    steamapps.mkdir(parents=True)
    lib2.mkdir(parents=True)

    write(steamapps / "libraryfolders.vdf",
          f'"libraryfolders"{{"0"{{"path" "{root}"}}'
          f'"1"{{"path" "{tmp_path / "games/SteamLibrary"}"}}}}')
    write(steamapps / "appmanifest_1091500.acf",
          '"AppState"{"appid" "1091500" "name" "Cyberpunk 2077" '
          '"StateFlags" "4" "installdir" "Cyberpunk 2077"}')
    write(lib2 / "appmanifest_1245620.acf",
          '"AppState"{"appid" "1245620" "name" "ELDEN RING" '
          '"StateFlags" "1026" "installdir" "ELDEN RING"}')
    if started:
        (steamapps / "compatdata/1091500/pfx/drive_c/users/steamuser").mkdir(
            parents=True)

    monkeypatch.setattr(steam, "STEAM_ROOT_CANDIDATES", [str(root)])
    monkeypatch.setattr(steam, "steam_is_running", lambda: False)
    return steam, root


def test_steam_multi_library_discovery(tmp_path, monkeypatch):
    steam, _root = _fake_steam(tmp_path, monkeypatch)
    games = {g["app_id"]: g for g in steam.iter_games()}

    assert games["1091500"]["installed"] is True
    assert games["1245620"]["installed"] is False      # still downloading
    assert games["1091500"]["user_dir"] == "steamuser"
    assert games["1245620"]["prefix_path"] is None     # never started


def test_user_dir_prefers_steamuser(tmp_path):
    from linux_prefix_hub.adapters import base
    prefix = tmp_path / "pfx"
    (prefix / "drive_c/users/steamuser").mkdir(parents=True)
    (prefix / "drive_c/users/Public").mkdir(parents=True)
    assert base.user_dir_for(str(prefix)) == "steamuser"


def test_steam_context_from_env(tmp_path, monkeypatch):
    steam, _root = _fake_steam(tmp_path, monkeypatch)
    monkeypatch.setenv("SteamAppId", "1091500")
    ctx = steam.context_from_env()
    assert ctx and ctx["game_name"] == "Cyberpunk 2077"


def test_steam_connect_writes_launch_options(tmp_path, monkeypatch):
    steam, root = _fake_steam(tmp_path, monkeypatch)
    config = root / "userdata/1234/config/localconfig.vdf"
    write(config,
          '"UserLocalConfigStore"{"Software"{"Valve"{"Steam"{"apps"{'
          '"1091500"{"LastPlayed" "1700000000"}}}}}}')

    assert steam.is_connected("1091500") is False
    result = steam.connect("1091500")
    assert result.ok

    text = config.read_text(encoding="utf-8")
    assert "linux-prefix-hub-wrapper" in text
    assert "%command%" in text
    assert "LastPlayed" in text                 # nothing else was lost
    assert config.with_suffix(".vdf.bak").exists()
    assert steam.is_connected("1091500") is True

    assert steam.disconnect("1091500").ok
    assert steam.is_connected("1091500") is False


def test_steam_connect_is_manual_while_steam_runs(tmp_path, monkeypatch):
    steam, _root = _fake_steam(tmp_path, monkeypatch)
    monkeypatch.setattr(steam, "steam_is_running", lambda: True)
    monkeypatch.setattr(steam, "_copy_to_clipboard", lambda text: False)
    result = steam.connect("1091500")
    assert not result.ok and result.manual
    assert "%command%" in result.message


# --- Lutris --------------------------------------------------------------
LUTRIS_YML = """\
# handmade config -- keep my comments!
game:
  exe: /games/quake/quake.exe
  prefix: {prefix}
  arch: win64
wine:
  version: lutris-ge-8.26
system:
  env:
    DXVK_HUD: fps
"""


def _fake_lutris(home, tmp_path, monkeypatch, prefix, layout="config"):
    """A Lutris install. `layout` picks where the per-game YAMLs live:
    "config" is the old location, "data" what 0.5.23 actually uses."""
    from linux_prefix_hub.adapters import lutris
    cfg_root = home / ".config/lutris"
    data_root = home / ".local/share/lutris"
    data_root.mkdir(parents=True)
    games = (data_root if layout == "data" else cfg_root) / "games"
    games.mkdir(parents=True)

    write(games / "quake-1690000000.yml", LUTRIS_YML.format(prefix=prefix))

    con = sqlite3.connect(data_root / "pga.db")
    con.execute("CREATE TABLE games (id INTEGER, name TEXT, slug TEXT, "
                "runner TEXT, directory TEXT, configpath TEXT, "
                "installed INTEGER)")
    con.execute("INSERT INTO games VALUES (1, 'Quake', 'quake', 'wine', "
                "'/games/quake', 'quake-1690000000', 1)")
    con.commit()
    con.close()

    monkeypatch.setattr(lutris, "LUTRIS_ROOTS",
                        [(str(cfg_root), str(data_root))])
    return lutris


def test_lutris_discovery(isolated_home, tmp_path, monkeypatch, fake_prefix):
    lutris = _fake_lutris(isolated_home, tmp_path, monkeypatch, fake_prefix)
    games = list(lutris.iter_games())
    assert len(games) == 1
    assert games[0]["game_name"] == "Quake"          # name from pga.db
    assert games[0]["prefix_path"] == str(fake_prefix)   # prefix from YAML
    assert games[0]["managed"] is False


def test_lutris_discovery_without_a_config_root(isolated_home, tmp_path,
                                                monkeypatch, fake_prefix):
    """Lutris 0.5.23 keeps everything under the data root and may never
    create ~/.config/lutris -- gating on it found no games at all."""
    lutris = _fake_lutris(isolated_home, tmp_path, monkeypatch, fake_prefix,
                          layout="data")
    assert not (isolated_home / ".config/lutris").exists()

    games = list(lutris.iter_games())
    assert [g["game_name"] for g in games] == ["Quake"]
    assert games[0]["prefix_path"] == str(fake_prefix)

    # ... and the hook still lands in the right file.
    assert lutris.connect("quake").ok
    text = (isolated_home / ".local/share/lutris/games/quake-1690000000.yml"
            ).read_text(encoding="utf-8")
    assert "prelaunch_command:" in text
    assert next(iter(lutris.iter_games()))["managed"] is True


def test_lutris_yields_a_game_once_when_slug_and_file_disagree(
        isolated_home, tmp_path, monkeypatch, fake_prefix):
    """Lutris does not name the config file after the slug: "diablo-iv"
    lives in "diablo-iv-battlenet-<ts>.yml". The YAML fallback must
    recognise the file as already taken."""
    lutris = _fake_lutris(isolated_home, tmp_path, monkeypatch, fake_prefix,
                          layout="data")
    data_root = isolated_home / ".local/share/lutris"
    write(data_root / "games/diablo-iv-battlenet-1767711128.yml",
          LUTRIS_YML.format(prefix=fake_prefix))
    con = sqlite3.connect(data_root / "pga.db")
    con.execute("INSERT INTO games VALUES (2, 'Diablo IV', 'diablo-iv', "
                "'wine', '/games/d4', 'diablo-iv-battlenet-1767711128', 1)")
    con.commit()
    con.close()

    names = [g["game_name"] for g in lutris.iter_games()]
    assert names == ["Quake", "Diablo IV"]


def test_lutris_hides_mirrored_steam_entries(isolated_home, tmp_path,
                                             monkeypatch, fake_prefix):
    """Lutris imports the Steam library; those rows have no prefix and the
    Steam adapter already lists the same games."""
    lutris = _fake_lutris(isolated_home, tmp_path, monkeypatch, fake_prefix,
                          layout="data")
    data_root = isolated_home / ".local/share/lutris"
    write(data_root / "games/steam-730-1764867908.yml",
          "game_slug: counter-strike-2\nname: Counter-Strike 2\n")
    con = sqlite3.connect(data_root / "pga.db")
    con.execute("INSERT INTO games VALUES (2, 'Counter-Strike 2', "
                "'counter-strike-2', 'steam', NULL, "
                "'steam-730-1764867908', 1)")
    con.commit()
    con.close()

    assert [g["game_name"] for g in lutris.iter_games()] == ["Quake"]


def test_lutris_connect_injects_hooks_and_keeps_the_file(
        isolated_home, tmp_path, monkeypatch, fake_prefix):
    lutris = _fake_lutris(isolated_home, tmp_path, monkeypatch, fake_prefix)
    assert lutris.connect("quake").ok

    text = (isolated_home / ".config/lutris/games/quake-1690000000.yml"
            ).read_text(encoding="utf-8")
    assert "prelaunch_command:" in text and "postexit_command:" in text
    assert "prelaunch_wait: true" in text            # boolean, not "true"
    assert "# handmade config -- keep my comments!" in text
    assert "DXVK_HUD: fps" in text                   # user settings survive

    assert next(iter(lutris.iter_games()))["managed"] is True

    assert lutris.disconnect("quake").ok
    text = (isolated_home / ".config/lutris/games/quake-1690000000.yml"
            ).read_text(encoding="utf-8")
    assert "prelaunch_command:" not in text
    assert "DXVK_HUD: fps" in text


def test_lutris_adds_a_system_block_when_missing(
        isolated_home, tmp_path, monkeypatch, fake_prefix):
    lutris = _fake_lutris(isolated_home, tmp_path, monkeypatch, fake_prefix)
    config = isolated_home / ".config/lutris/games/quake-1690000000.yml"
    write(config, f"game:\n  prefix: {fake_prefix}\n")

    assert lutris.connect("quake").ok
    text = config.read_text(encoding="utf-8")
    assert text.startswith("game:")
    assert "system:" in text and "prelaunch_command:" in text


def test_lutris_slug_keeps_trailing_numbers():
    from linux_prefix_hub.adapters import lutris
    assert lutris._slug_from_configpath("half-life-2-1690000000") == \
        "half-life-2"
    assert lutris._slug_from_configpath("half-life-2") == "half-life-2"


# --- Heroic --------------------------------------------------------------
def _fake_heroic(home, monkeypatch, prefix):
    import json

    from linux_prefix_hub.adapters import heroic
    root = home / ".config/heroic"
    (root / "GamesConfig").mkdir(parents=True)
    (root / "store_cache").mkdir(parents=True)

    write(root / "GamesConfig/9a1b2c.json", json.dumps({
        "9a1b2c": {"winePrefix": str(prefix), "autoInstallDxvk": True},
    }))
    write(root / "store_cache/legendary_library.json", json.dumps({
        "library": [{"app_name": "9a1b2c", "title": "ELDEN RING",
                     "is_installed": True,
                     "install": {"install_path": "/games/elden"}}],
    }))
    monkeypatch.setattr(heroic, "HEROIC_ROOTS", [str(root)])
    return heroic, root


def test_heroic_discovery(isolated_home, monkeypatch, fake_prefix):
    heroic, _root = _fake_heroic(isolated_home, monkeypatch, fake_prefix)
    games = list(heroic.iter_games())
    assert len(games) == 1
    assert games[0]["game_name"] == "ELDEN RING"     # title from the cache
    assert games[0]["prefix_path"] == str(fake_prefix)
    assert games[0]["game_dir"] == "/games/elden"


def test_heroic_connect_adds_the_wrapper(isolated_home, monkeypatch,
                                         fake_prefix):
    import json

    heroic, root = _fake_heroic(isolated_home, monkeypatch, fake_prefix)
    assert heroic.connect("9a1b2c").ok

    data = json.loads((root / "GamesConfig/9a1b2c.json").read_text())
    wrappers = data["9a1b2c"]["wrapperOptions"]
    assert any("linux-prefix-hub-wrapper" in w["exe"] for w in wrappers)
    assert data["9a1b2c"]["autoInstallDxvk"] is True   # untouched

    assert next(iter(heroic.iter_games()))["managed"] is True

    # Connecting twice must not stack up wrappers.
    heroic.connect("9a1b2c")
    data = json.loads((root / "GamesConfig/9a1b2c.json").read_text())
    assert len(data["9a1b2c"]["wrapperOptions"]) == 1

    assert heroic.disconnect("9a1b2c").ok
    data = json.loads((root / "GamesConfig/9a1b2c.json").read_text())
    assert data["9a1b2c"]["wrapperOptions"] == []


# --- Aggregation ---------------------------------------------------------
def test_a_broken_adapter_does_not_break_the_scan(tmp_path, monkeypatch):
    from linux_prefix_hub.adapters import base
    steam, _root = _fake_steam(tmp_path, monkeypatch)

    def explode():
        raise RuntimeError("Lutris config is a mess")

    import linux_prefix_hub.adapters.lutris as lutris
    monkeypatch.setattr(lutris, "iter_games", explode)

    names = {g["game_name"] for g in base.iter_games()}
    assert "Cyberpunk 2077" in names


# --- Regressions found by verifying against a real installation ----------
def test_steam_skips_tools_and_runtimes(tmp_path, monkeypatch):
    """Proton and the Linux runtimes are not games.

    Real finding: no field in appmanifest_*.acf distinguishes them -- the key
    sets are identical. A toolmanifest.vdf in the install dir does.
    """
    steam, root = _fake_steam(tmp_path, monkeypatch)
    steamapps = root / "steamapps"
    write(steamapps / "appmanifest_1493710.acf",
          '"AppState"{"appid" "1493710" "name" "Proton Experimental" '
          '"StateFlags" "4" "installdir" "Proton - Experimental"}')
    write(steamapps / "common/Proton - Experimental/toolmanifest.vdf",
          '"manifest"{"commandline" "/proton run"}')
    write(steamapps / "appmanifest_228980.acf",
          '"AppState"{"appid" "228980" '
          '"name" "Steamworks Common Redistributables" '
          '"StateFlags" "4" "installdir" "Steamworks Shared"}')

    ids = {g["app_id"] for g in steam.iter_games()}
    assert "1091500" in ids                      # the game survives
    assert "1493710" not in ids                  # toolmanifest.vdf
    assert "228980" not in ids                   # known depot-only app


def test_steam_yields_a_shared_appid_once(tmp_path, monkeypatch):
    """The same manifest in two libraries used to appear twice."""
    steam, root = _fake_steam(tmp_path, monkeypatch)
    manifest = ('"AppState"{"appid" "1091500" "name" "Cyberpunk 2077" '
                '"StateFlags" "4" "installdir" "Cyberpunk 2077"}')
    write(tmp_path / "games/SteamLibrary/steamapps/appmanifest_1091500.acf",
          manifest)

    games = [g for g in steam.iter_games() if g["app_id"] == "1091500"]
    assert len(games) == 1
    # The copy that has the prefix wins -- that is the one being played.
    assert games[0]["prefix_path"] is not None


def test_steam_duplicate_prefers_the_installed_copy(tmp_path, monkeypatch):
    steam, root = _fake_steam(tmp_path, monkeypatch)
    # A stale, half-removed manifest for a game installed elsewhere.
    write(root / "steamapps/appmanifest_1245620.acf",
          '"AppState"{"appid" "1245620" "name" "ELDEN RING" '
          '"StateFlags" "4" "installdir" "ELDEN RING"}')

    games = [g for g in steam.iter_games() if g["app_id"] == "1245620"]
    assert len(games) == 1
    assert games[0]["installed"] is True


def test_heroic_download_queue_cannot_unset_installed(isolated_home,
                                                      monkeypatch,
                                                      fake_prefix):
    """Real finding: download-manager.json carries `is_installed: false`
    plus an `install` dict for a game that is long since installed."""
    import json

    heroic, root = _fake_heroic(isolated_home, monkeypatch, fake_prefix)
    write(root / "download-manager.json", json.dumps({
        "queue": [{"app_name": "9a1b2c", "title": "ELDEN RING",
                   "is_installed": False,
                   "install": {"install_path": "/games/elden"}}],
    }))

    game = next(iter(heroic.iter_games()))
    assert game["installed"] is True


def test_heroic_installed_is_or_ed_across_caches(isolated_home, monkeypatch,
                                                 fake_prefix):
    """A cache that does not know about the install must not overrule one
    that does -- regardless of which file sorts last."""
    import json

    heroic, root = _fake_heroic(isolated_home, monkeypatch, fake_prefix)
    write(root / "store_cache/zz_last_alphabetically.json", json.dumps({
        "games": [{"app_name": "9a1b2c", "title": "ELDEN RING",
                   "is_installed": False, "install": {}}],
    }))

    assert next(iter(heroic.iter_games()))["installed"] is True
