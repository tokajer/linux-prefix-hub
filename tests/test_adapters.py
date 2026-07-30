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


def _fake_lutris(home, tmp_path, monkeypatch, prefix):
    from linux_prefix_hub.adapters import lutris
    cfg_root = home / ".config/lutris"
    data_root = home / ".local/share/lutris"
    (cfg_root / "games").mkdir(parents=True)
    data_root.mkdir(parents=True)

    write(cfg_root / "games/quake-1690000000.yml",
          LUTRIS_YML.format(prefix=prefix))

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
