"""Tests gegen Fake-Umgebungen -- kein echtes Steam noetig.

Ausfuehren:  pytest    (oder: python -m pytest tests/)
"""
import os
import time
from pathlib import Path

import pytest


# --- VDF-Parser ----------------------------------------------------------
def test_vdf_parses_appmanifest():
    from deinapp.core import vdf
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
    from deinapp.core import vdf
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


# --- Snapshot-Diff -------------------------------------------------------
def test_snapshot_detects_save_location(tmp_path):
    from deinapp.core import snapshot
    udir = tmp_path / "drive_c/users/steamuser"
    (udir / "Documents").mkdir(parents=True)

    before = snapshot.snapshot(tmp_path, "steamuser")
    time.sleep(0.01)
    save = udir / "Documents/CD Projekt Red/Cyberpunk 2077"
    save.mkdir(parents=True)
    (save / "MainSave.sav").write_text("x")

    after = snapshot.snapshot(tmp_path, "steamuser")
    changed = snapshot.diff(before, after)
    locs = snapshot.classify_locations(changed)

    assert any(l["type"] == "saves" for l in locs)
    assert any("Cyberpunk" in l["win_path"] for l in locs)


# --- DB-Idempotenz -------------------------------------------------------
def test_db_preserves_redirected_flag(tmp_path, monkeypatch):
    # DB in tmp umbiegen
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    import importlib
    from deinapp.core import paths as P
    importlib.reload(P)
    from deinapp.core import db
    importlib.reload(db)

    entry = {
        "source": "steam", "app_id": "1091500",
        "game_name": "Cyberpunk 2077",
        "prefix_path": str(tmp_path / "pfx"),
        "user_dir": "steamuser",
        "storage_locations": [
            {"type": "saves", "win_path": "Documents/CP", "redirected": False},
        ],
    }
    fp = db.upsert_prefix(entry)

    # Nutzer setzt redirected=True
    d = db.load_prefixes()
    d[fp]["storage_locations"][0]["redirected"] = True
    db.save_prefixes(d)

    # Erneuter Scan (upsert) darf das nicht kippen
    db.upsert_prefix(entry)
    d2 = db.load_prefixes()
    assert d2[fp]["storage_locations"][0]["redirected"] is True


def test_fingerprint_stable(tmp_path):
    from deinapp.core import db
    p = tmp_path / "pfx"
    p.mkdir()
    assert db.fingerprint(p) == db.fingerprint(str(p))


# --- Steam-Discovery -----------------------------------------------------
def test_steam_multi_library_discovery(tmp_path, monkeypatch):
    root = tmp_path / ".steam/steam"
    steamapps = root / "steamapps"
    steamapps.mkdir(parents=True)
    lib2 = tmp_path / "games/SteamLibrary/steamapps"
    lib2.mkdir(parents=True)

    (steamapps / "libraryfolders.vdf").write_text(
        f'"libraryfolders"{{"0"{{"path" "{root}"}}'
        f'"1"{{"path" "{tmp_path / "games/SteamLibrary"}"}}}}')
    (steamapps / "appmanifest_1091500.acf").write_text(
        '"AppState"{"appid" "1091500" "name" "Cyberpunk 2077" '
        '"StateFlags" "4" "installdir" "Cyberpunk 2077"}')
    (lib2 / "appmanifest_1245620.acf").write_text(
        '"AppState"{"appid" "1245620" "name" "ELDEN RING" '
        '"StateFlags" "1026" "installdir" "ELDEN RING"}')

    from deinapp.adapters import steam
    monkeypatch.setattr(steam, "STEAM_ROOT_CANDIDATES", [str(root)])

    games = {g["app_id"]: g for g in steam.iter_installed_games()}
    assert "1091500" in games and games["1091500"]["installed"] is True
    assert "1245620" in games and games["1245620"]["installed"] is False


def test_user_dir_prefers_steamuser(tmp_path):
    from deinapp.adapters import steam
    pfx = tmp_path / "pfx"
    (pfx / "drive_c/users/steamuser").mkdir(parents=True)
    (pfx / "drive_c/users/Public").mkdir(parents=True)
    assert steam.user_dir_for(str(pfx)) == "steamuser"
