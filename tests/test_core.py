# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Core: VDF, snapshots, the prefix DB and the translation layer."""
from __future__ import annotations

import time
from pathlib import Path


# --- VDF parser ----------------------------------------------------------
def test_vdf_parses_appmanifest():
    from linux_prefix_hub.core import vdf
    sample = '''
    "AppState"
    {
        "appid" "1091500"
        "name" "Cyberpunk 2077"
        "StateFlags" "4"
        "UserConfig" { "language" "english" }
    }
    '''
    d = vdf.loads(sample)
    assert d["AppState"]["appid"] == "1091500"
    assert d["AppState"]["name"] == "Cyberpunk 2077"
    assert d["AppState"]["UserConfig"]["language"] == "english"


def test_vdf_parses_libraryfolders():
    from linux_prefix_hub.core import vdf
    libs = '''
    "libraryfolders"
    {
        "0" { "path" "/home/x/.steam/steam" }
        "1" { "path" "/mnt/games/SteamLibrary" }
    }
    '''
    d = vdf.loads(libs)
    paths = [v["path"] for v in d["libraryfolders"].values()]
    assert "/mnt/games/SteamLibrary" in paths


def test_vdf_roundtrip_keeps_values_and_order():
    from linux_prefix_hub.core import vdf
    data = {"UserLocalConfigStore": {
        "Software": {"Valve": {"Steam": {"apps": {
            "1091500": {"LaunchOptions": '"/home/me/bin/hook" %command%'},
        }}}},
    }}
    again = vdf.loads(vdf.dumps(data))
    assert again == data


# --- Snapshot diff -------------------------------------------------------
def test_snapshot_detects_save_location(tmp_path):
    from linux_prefix_hub.core import snapshot
    udir = tmp_path / "drive_c/users/steamuser"
    (udir / "Documents").mkdir(parents=True)

    before = snapshot.snapshot(tmp_path, "steamuser")
    time.sleep(0.01)
    save = udir / "Documents/CD Projekt Red/Cyberpunk 2077"
    save.mkdir(parents=True)
    (save / "MainSave.sav").write_text("x")

    after = snapshot.snapshot(tmp_path, "steamuser")
    locations = snapshot.classify_locations(snapshot.diff(before, after))

    assert any(loc["type"] == "saves" for loc in locations)
    assert any("Cyberpunk" in loc["win_path"] for loc in locations)


def test_snapshot_ignores_wine_scratch_space(tmp_path):
    from linux_prefix_hub.core import snapshot
    udir = tmp_path / "drive_c/users/steamuser"
    temp = udir / "AppData/Local/Temp/wine"
    temp.mkdir(parents=True)
    (temp / "junk.tmp").write_text("x")

    taken = snapshot.snapshot(tmp_path, "steamuser")
    assert taken == {}


def test_snapshot_ignores_the_shader_cache(tmp_path):
    """The Aim Lab case: DXVK's pipeline cache was a "config" location.

    It sits in AppData/Local/dxvk, so nothing about the path says cache --
    only the folder name and the file suffixes do.
    """
    from linux_prefix_hub.core import snapshot
    udir = tmp_path / "drive_c/users/steamuser"
    cache = udir / "AppData/Local/dxvk"
    cache.mkdir(parents=True)
    (cache / "3ba4b7fe7ec2e254.dxvk.bin").write_text("x")
    (cache / "3ba4b7fe7ec2e254.dxvk.lut").write_text("x")
    game = udir / "AppData/LocalLow/Statespace/aimlab_tb"
    game.mkdir(parents=True)
    (game / "prefs.bin").write_text("settings")
    (game / "Player.log").write_text("noise")

    taken = snapshot.snapshot(tmp_path, "steamuser")

    assert list(taken) == ["AppData/LocalLow/Statespace/aimlab_tb/prefs.bin"]


def test_a_filter_the_user_added_is_applied(tmp_path):
    from linux_prefix_hub.core import db, snapshot
    udir = tmp_path / "drive_c/users/steamuser"
    for name in ("Documents/Game/save.sav", "Documents/Game/telemetry/a.dat"):
        path = udir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")

    assert db.add_ignore_path("Documents/Game/telemetry")
    assert list(snapshot.snapshot(tmp_path, "steamuser")) == [
        "Documents/Game/save.sav"]

    assert db.forget_ignore_path("Documents/Game/telemetry")
    assert len(snapshot.snapshot(tmp_path, "steamuser")) == 2


def test_snapshot_finds_saves_in_the_install_folder(tmp_path):
    """The Portal 2 case: nothing in the prefix, saves in the game folder."""
    from linux_prefix_hub.core import snapshot
    game_dir = tmp_path / "common/Portal 2"
    (game_dir / "portal2").mkdir(parents=True)

    before = snapshot.snapshot_game_dir(game_dir)
    time.sleep(0.01)
    saves = game_dir / "portal2/SAVE/76561197990348047"
    saves.mkdir(parents=True)
    (saves / "sp_a2_bts3.sav").write_text("progress")

    after = snapshot.snapshot_game_dir(game_dir)
    locations = snapshot.classify_locations(snapshot.diff(before, after),
                                            snapshot.WHERE_GAME)

    assert locations
    assert locations[0]["where"] == "game_folder"
    assert locations[0]["type"] == "saves"
    assert "SAVE" in locations[0]["win_path"]


def test_snapshot_ignores_install_folder_churn(tmp_path):
    from linux_prefix_hub.core import snapshot
    game_dir = tmp_path / "game"
    (game_dir / "logs").mkdir(parents=True)
    (game_dir / "logs/session.txt").write_text("x")
    (game_dir / "crash.dmp").write_text("x")
    (game_dir / "console.log").write_text("x")
    (game_dir / "steam_appid.txt").write_text("620")
    (game_dir / "save.sav").write_text("keep me")

    assert list(snapshot.snapshot_game_dir(game_dir)) == ["save.sav"]


def test_snapshot_gives_up_on_an_enormous_install_folder(tmp_path,
                                                        monkeypatch):
    """Better no install-folder detection than a delayed launch."""
    from linux_prefix_hub.core import snapshot
    game_dir = tmp_path / "huge"
    game_dir.mkdir()
    for i in range(5):
        (game_dir / f"f{i}.dat").write_text("x")
    monkeypatch.setattr(snapshot, "MAX_GAME_DIR_FILES", 3)

    assert snapshot.snapshot_game_dir(game_dir) is None


def test_not_covered_and_covered_but_empty_are_different(tmp_path):
    """`{}` still teaches us something on the next launch; None does not."""
    from linux_prefix_hub.core import snapshot
    assert snapshot.snapshot_game_dir(None) is None
    assert snapshot.snapshot_game_dir(tmp_path / "nope") is None

    empty = tmp_path / "fresh install"
    empty.mkdir()
    assert snapshot.snapshot_game_dir(empty) == {}


def test_pending_snapshot_survives_between_processes():
    from linux_prefix_hub.core import snapshot
    state = {snapshot.WHERE_PREFIX: {"Documents/a.sav": 1.5},
             snapshot.WHERE_GAME: {"portal2/SAVE/x.sav": 2.5}}
    snapshot.save_pending("abc123", state)
    assert snapshot.load_pending("abc123") == state
    # Consumed: a stale snapshot must not leak into the next launch.
    assert snapshot.load_pending("abc123") == {}


def test_a_pending_snapshot_from_an_older_version_still_loads():
    """Written before the install folder was a second space."""
    import json

    from linux_prefix_hub.core import paths, snapshot
    paths.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    paths.snapshot_file("old").write_text(json.dumps({"Documents/a.sav": 1.5}),
                                          encoding="utf-8")
    assert snapshot.load_pending("old") == {
        snapshot.WHERE_PREFIX: {"Documents/a.sav": 1.5}}


# --- Prefix DB -----------------------------------------------------------
def _entry(tmp_path, **over):
    return {"source": "steam", "app_id": "1091500",
            "game_name": "Cyberpunk 2077",
            "prefix_path": str(tmp_path / "pfx"), "user_dir": "steamuser",
            "storage_locations": [
                {"type": "saves", "win_path": "Documents/CP",
                 "redirected": False}],
            **over}


def test_db_preserves_user_decisions_on_rescan(tmp_path):
    from linux_prefix_hub.core import db
    fingerprint = db.upsert_prefix(_entry(tmp_path))

    db.update_location(fingerprint, "Documents/CP", redirected=True,
                       redirect_target="/home/me/Games/CP")
    db.set_managed(fingerprint, True)

    db.upsert_prefix(_entry(tmp_path))          # a rescan comes along

    stored = db.get_prefix(fingerprint)
    assert stored["storage_locations"][0]["redirected"] is True
    assert stored["storage_locations"][0]["redirect_target"] == \
        "/home/me/Games/CP"
    assert stored["managed"] is True


def test_db_merges_new_locations(tmp_path):
    from linux_prefix_hub.core import db
    fingerprint = db.upsert_prefix(_entry(tmp_path))
    db.upsert_prefix(_entry(tmp_path, storage_locations=[
        {"type": "config", "win_path": "AppData/Local/CP"}]))
    paths = {loc["win_path"]
             for loc in db.get_prefix(fingerprint)["storage_locations"]}
    assert paths == {"Documents/CP", "AppData/Local/CP"}


def test_a_new_filter_forgets_what_it_should_never_have_recorded(tmp_path):
    """A filter that only applies to future launches is barely a filter."""
    from linux_prefix_hub.core import db, snapshot
    fingerprint = db.upsert_prefix(_entry(tmp_path, storage_locations=[
        {"type": "config", "win_path": "AppData/Local/dxvk"},
        {"type": "saves", "win_path": "Documents/Game"},
        # The user moved this one, so it stays even though it matches.
        {"type": "config", "win_path": "AppData/Local/Temp/keepme",
         "redirected": True, "redirect_target": "/home/me/Games/X"},
    ]))

    assert db.prune_locations(fingerprint, snapshot.location_is_noise) == 1

    stored = {loc["win_path"]
              for loc in db.get_prefix(fingerprint)["storage_locations"]}
    assert stored == {"Documents/Game", "AppData/Local/Temp/keepme"}


# --- The prefix DB is SQLite ---------------------------------------------
def test_the_prefix_db_is_a_database(tmp_path):
    import sqlite3

    from linux_prefix_hub.core import db, paths
    db.upsert_prefix(_entry(tmp_path))

    assert paths.PREFIX_DB.is_file()
    assert not paths.LEGACY_PREFIX_DB.exists()   # nothing writes JSON now
    with sqlite3.connect(str(paths.PREFIX_DB)) as raw:
        assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_an_entry_keeps_fields_the_schema_has_no_column_for(tmp_path):
    """`extra` is what lets an adapter add a field without a migration."""
    from linux_prefix_hub.core import db
    fingerprint = db.upsert_prefix(_entry(
        tmp_path, state_flags=4, steamapps="/games/steamapps",
        storage_locations=[{"type": "saves", "win_path": "Documents/CP",
                            "file_count": 12, "detected_by": "diff",
                            "first_seen": "2026-07-31"}]))

    stored = db.get_prefix(fingerprint)
    assert stored["state_flags"] == 4
    assert stored["steamapps"] == "/games/steamapps"
    location = stored["storage_locations"][0]
    assert location["file_count"] == 12
    assert location["detected_by"] == "diff"
    assert location["first_seen"] == "2026-07-31"


def test_one_games_write_leaves_the_other_games_row_alone(tmp_path):
    """The reason this is a database: three processes write it.

    A whole-file rewrite settles two writers by letting the later one win,
    and the decision the earlier one made is gone with nothing to show for
    it. These are row writes, so they cannot overwrite each other -- and a
    connection somebody else already had open sees both.
    """
    import sqlite3

    from linux_prefix_hub.core import db, paths
    first = db.upsert_prefix(_entry(tmp_path))
    db.set_managed(first, True)

    other = sqlite3.connect(str(paths.PREFIX_DB))
    try:
        second = db.upsert_prefix(_entry(tmp_path / "second", app_id="42",
                                         game_name="Other",
                                         prefix_path=str(tmp_path / "two")))
        rows = dict(other.execute("SELECT fingerprint, managed FROM prefixes"))
    finally:
        other.close()

    assert rows == {first: 1, second: 0}


def test_an_old_prefixes_json_is_folded_in_once(tmp_path):
    """Upgrading must not cost anybody what they already learned."""
    import json

    from linux_prefix_hub.core import db, paths
    legacy = {"abc123": {
        "source": "steam", "app_id": "220", "game_name": "Half-Life 2",
        "prefix_path": str(tmp_path / "pfx"), "user_dir": "steamuser",
        "managed": True,
        "storage_locations": [
            {"type": "saves", "win_path": "Documents/HL2",
             "redirected": True, "redirect_target": "/home/me/Games/HL2"}]}}
    paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    paths.LEGACY_PREFIX_DB.write_text(json.dumps(legacy), encoding="utf-8")

    stored = db.get_prefix("abc123")
    assert stored is not None
    assert stored["game_name"] == "Half-Life 2"
    assert stored["managed"] is True
    assert stored["storage_locations"][0]["redirect_target"] == \
        "/home/me/Games/HL2"

    # The file stays as a backup, and what says the import happened is the
    # flag -- so a second start does not import it on top of a later edit.
    assert paths.LEGACY_PREFIX_DB.is_file()
    db.update_location("abc123", "Documents/HL2", redirected=False,
                       redirect_target=None)
    assert db.get_prefix("abc123")["storage_locations"][0]["redirected"] \
        is False


# --- opening a folder ----------------------------------------------------
def test_open_folder_uses_the_first_opener_it_finds(tmp_path, monkeypatch):
    from linux_prefix_hub.core import desktop
    calls: list[list[str]] = []
    monkeypatch.setattr(desktop.shutil, "which",
                        lambda name: "/usr/bin/gio" if name == "gio" else None)
    monkeypatch.setattr(desktop.subprocess, "Popen",
                        lambda argv, **kw: calls.append(argv))

    assert desktop.open_folder(tmp_path) is True
    assert calls == [["gio", "open", str(tmp_path)]]


def test_the_file_manager_gets_a_clean_environment(tmp_path, monkeypatch):
    """It outlives us, so nothing of ours may travel into it.

    KDE keeps one Dolphin per session and hands it every new window; a
    leaked `LPH_GUI_REEXEC` there made the app stop opening a window at all,
    and a leaked `PYTHONHOME` points into a /tmp mount that will be gone.
    """
    from linux_prefix_hub import __main__ as m
    from linux_prefix_hub.core import desktop
    monkeypatch.setenv(desktop.GUI_REEXEC_FLAG, "12345")
    monkeypatch.setenv("APPDIR", "/tmp/.mount_x")
    monkeypatch.setenv("PYTHONHOME", "/tmp/.mount_x/opt/python3.12")
    monkeypatch.setenv("KEEP_ME", "yes")
    seen: list[dict] = []
    monkeypatch.setattr(desktop.shutil, "which", lambda name: "/usr/bin/x")
    monkeypatch.setattr(desktop.subprocess, "Popen",
                        lambda argv, **kw: seen.append(kw["env"]))

    assert desktop.open_folder(tmp_path) is True
    assert desktop.GUI_REEXEC_FLAG not in seen[0]
    assert "PYTHONHOME" not in seen[0]
    assert seen[0]["KEEP_ME"] == "yes"          # only ours is stripped
    assert desktop.GUI_REEXEC_FLAG == m.REEXEC_FLAG   # two spellings, one name


def test_open_folder_refuses_what_is_not_there(tmp_path, monkeypatch):
    from linux_prefix_hub.core import desktop
    monkeypatch.setattr(desktop.shutil, "which", lambda name: "/usr/bin/x")
    assert desktop.open_folder(tmp_path / "gone") is False


def test_open_folder_without_any_file_manager(tmp_path, monkeypatch):
    from linux_prefix_hub.core import desktop
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    assert desktop.open_folder(tmp_path) is False


def test_location_path_points_where_the_files_are(tmp_path):
    from linux_prefix_hub.core import redirect
    entry = {"prefix_path": str(tmp_path / "pfx"), "user_dir": "steamuser",
             "game_dir": str(tmp_path / "common/Portal 2")}

    in_prefix = {"win_path": "Documents/Q", "where": "prefix"}
    assert redirect.location_path(entry, in_prefix) == (
        tmp_path / "pfx/drive_c/users/steamuser/Documents/Q")

    in_game = {"win_path": "portal2/SAVE", "where": "game_folder"}
    assert redirect.location_path(entry, in_game) == (
        tmp_path / "common/Portal 2/portal2/SAVE")

    moved = {"win_path": "Documents/Q", "where": "prefix",
             "redirected": True, "redirect_target": "/home/me/Games/Q"}
    assert redirect.location_path(entry, moved) == Path("/home/me/Games/Q")


def test_db_keeps_the_two_location_spaces_apart(tmp_path):
    """Same relative path in the prefix and in the install folder."""
    from linux_prefix_hub.core import db
    fingerprint = db.upsert_prefix(_entry(tmp_path, storage_locations=[
        {"type": "config", "win_path": "cfg", "where": "prefix"}]))
    db.upsert_prefix(_entry(tmp_path, storage_locations=[
        {"type": "config", "win_path": "cfg", "where": "game_folder"}]))

    locations = db.get_prefix(fingerprint)["storage_locations"]
    assert {db.location_key(loc) for loc in locations} == {
        ("prefix", "cfg"), ("game_folder", "cfg")}


def test_db_updates_the_location_in_the_space_it_was_asked_for(tmp_path):
    from linux_prefix_hub.core import db
    fingerprint = db.upsert_prefix(_entry(tmp_path, storage_locations=[
        {"type": "saves", "win_path": "Documents/CP", "where": "prefix"},
        {"type": "saves", "win_path": "Documents/CP",
         "where": "game_folder"}]))

    db.update_location(fingerprint, "Documents/CP", redirected=True)

    by_space = {db.location_key(loc)[0]: loc
                for loc in db.get_prefix(fingerprint)["storage_locations"]}
    assert by_space["prefix"]["redirected"] is True
    assert by_space["game_folder"]["redirected"] is False


def test_db_resolve_by_name_and_id(tmp_path):
    from linux_prefix_hub.core import db
    fingerprint = db.upsert_prefix(_entry(tmp_path))
    assert db.resolve("1091500")[0] == fingerprint
    assert db.resolve("cyberpunk")[0] == fingerprint
    assert db.resolve("nope") is None


def test_fingerprint_stable(tmp_path):
    from linux_prefix_hub.core import db
    p = tmp_path / "pfx"
    p.mkdir()
    assert db.fingerprint(p) == db.fingerprint(str(p))


# --- Translation ---------------------------------------------------------
def test_german_locale_translates(monkeypatch):
    from linux_prefix_hub.core import i18n
    monkeypatch.delenv("LPH_LANG", raising=False)
    monkeypatch.setenv("LANG", "de_AT.utf8")
    i18n.set_language(None)
    assert i18n.language() == "de"
    assert i18n.translate("Disconnected.") == "Verbindung entfernt."


def test_other_locale_stays_english(monkeypatch):
    from linux_prefix_hub.core import i18n
    monkeypatch.delenv("LPH_LANG", raising=False)
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    i18n.set_language(None)
    assert i18n.language() == "en"
    assert i18n.translate("Disconnected.") == "Disconnected."


def test_config_overrides_the_locale(monkeypatch):
    from linux_prefix_hub.core import db, i18n
    monkeypatch.delenv("LPH_LANG", raising=False)
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    db.set_config("language", "de")
    i18n.set_language(None)
    assert i18n.language() == "de"


def test_placeholders_and_unknown_strings(monkeypatch):
    from linux_prefix_hub.core import i18n
    monkeypatch.setenv("LPH_LANG", "de")
    i18n.set_language(None)
    assert i18n.translate("{n} game(s) found:", n=3) == "3 Spiel(e) gefunden:"
    assert i18n.translate("never translated") == "never translated"


def test_broken_translation_falls_back_to_english(monkeypatch):
    from linux_prefix_hub.core import i18n
    i18n.set_language("de")
    monkeypatch.setitem(i18n._catalog, "Moved to {target}.", "Nach {ziel}.")
    assert i18n.translate("Moved to {target}.", target="/x") == "Moved to /x."


def test_every_german_string_keeps_its_placeholders():
    """A translator typo must not become a runtime crash."""
    import json
    import re

    from linux_prefix_hub.core import i18n
    catalog = json.loads(
        (i18n.LOCALES_DIR / "de.json").read_text(encoding="utf-8"))
    pattern = re.compile(r"{(\w+)}")
    for source, translated in catalog.items():
        if source.startswith("_"):
            continue
        assert set(pattern.findall(source)) == \
            set(pattern.findall(translated)), source


def test_gearlever_configured_folder_wins(isolated_home, monkeypatch):
    """GearLever's target folder is configurable; read it, do not guess."""
    import importlib

    from linux_prefix_hub.core import integrate
    keyfile = (isolated_home
               / ".var/app/it.mijorus.gearlever/config/glib-2.0/settings"
               / "keyfile")
    keyfile.parent.mkdir(parents=True)
    keyfile.write_text("[it/mijorus/gearlever]\n"
                       "appimages-default-folder='~/MyApps'\n",
                       encoding="utf-8")
    importlib.reload(integrate)

    folders = integrate.gearlever_folders()
    assert folders[0] == isolated_home / "MyApps"
    assert len(folders) == len(set(folders))          # no duplicates

    managed = isolated_home / "MyApps/LinuxPrefixHub.AppImage"
    managed.parent.mkdir(parents=True, exist_ok=True)
    managed.write_text("x")
    monkeypatch.setenv("APPIMAGE", str(managed))
    assert integrate.detect_gearlever() == managed

    monkeypatch.setenv("APPIMAGE", str(isolated_home / "elsewhere/x.AppImage"))
    assert integrate.detect_gearlever() is None


# --- Config the window owns ----------------------------------------------
def test_background_running_is_on_until_it_is_turned_off():
    """Default on: the window is not the app -- see `db.background_tray`."""
    from linux_prefix_hub.core import db
    assert db.background_tray() is True

    db.set_config("background_tray", False)
    assert db.background_tray() is False

    db.set_config("background_tray", True)
    assert db.background_tray() is True


def test_pending_redirects_survive_a_corrupt_value():
    from linux_prefix_hub.core import db
    db.set_config("pending_redirects", "not a dict")
    assert db.pending_redirects() == {}


def test_a_second_wish_for_one_game_replaces_the_first():
    from linux_prefix_hub.core import db
    db.add_pending_redirect("steam", "2310", "Quake", ["Documents"])
    db.add_pending_redirect("steam", "2310", "Quake", ["AppData/Roaming"])

    pending = db.pending_redirects()
    assert list(pending) == ["steam:2310"]
    assert pending["steam:2310"]["roots"] == ["AppData/Roaming"]


# --- The tray, where there is none ---------------------------------------
def test_the_tray_degrades_to_nothing_without_a_desktop(monkeypatch):
    """Importable and inert with no `gi` to export anything through.

    The one thing that must never happen is a window closing into a tray
    that is not there, so `live` is the fact the caller asks for -- and it is
    False here rather than an exception the caller has to catch. Forced
    rather than inferred from this interpreter: the suite must not reach a
    real session bus on a machine that happens to have one.
    """
    from linux_prefix_hub.gui import tray
    monkeypatch.setattr(tray, "_gio", lambda: None)

    icon = tray.Tray(title="Test", icon="test", items=[
        tray.Item("quit", "Quit", lambda: None)])

    assert icon.live is False
    icon.set_label("quit", "Beenden")      # all of it stays safe to call
    icon.set_attention(True)
    icon.close()
    icon.close()
