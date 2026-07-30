"""Core: VDF, snapshots, the prefix DB and the translation layer."""
from __future__ import annotations

import time


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


def test_pending_snapshot_survives_between_processes():
    from linux_prefix_hub.core import snapshot
    snapshot.save_pending("abc123", {"Documents/a.sav": 1.5})
    assert snapshot.load_pending("abc123") == {"Documents/a.sav": 1.5}
    # Consumed: a stale snapshot must not leak into the next launch.
    assert snapshot.load_pending("abc123") == {}


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
