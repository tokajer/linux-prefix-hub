# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""PCGamingWiki lookup: parsing, mapping, cache, and what the DB gets.

The wikitext samples are real (trimmed) article source, because the parser's
whole job is to survive what people actually write. Nothing here goes online:
`_get` is replaced everywhere the code would have called it, and a test that
forgets to do so fails with "offline" rather than hitting someone's server.
"""
from __future__ import annotations

import json

import pytest

PORTAL2 = """
===Configuration file(s) location===
{{Game data|
{{Game data/config|Steam|{{p|game}}\\portal2\\cfg\\|{{p|game}}\\update\\cfg\\}}
}}
{{XDG|false}}

===Save game data location===
{{Game data|
{{Game data/saves|Steam|{{p|game}}\\portal2\\SAVE\\|{{P|steam}}\\userdata\\{{p|uid}}\\620\\remote\\cfg\\config.cfg<ref>{{Refcheck|user=x|date=2024-05-21|comment=Contains chapter progress}}</ref>}}
}}
"""

SKYRIM = """
{{Game data|
{{Game data/config|Windows|{{p|userprofile\\Documents}}\\My Games\\Skyrim\\}}
{{Game data/saves|Windows|{{p|userprofile\\Documents}}\\My Games\\Skyrim\\Saves\\}}
}}
"""

HOLLOW_KNIGHT = """
{{Game data|
{{Game data/config|Windows|{{p|hkcu}}\\Software\\Team Cherry\\Hollow Knight\\|{{p|game}}\\hollow_knight_Data\\Config.ini}}
{{Game data/config|Linux|{{p|xdgconfighome}}/unity3d/Team Cherry/Hollow Knight/prefs|{{p|game}}/hollow_knight_Data/Config.ini}}
{{Game data/saves|Windows|{{p|userprofile}}\\AppData\\LocalLow\\Team Cherry\\Hollow Knight\\*.dat | {{p|userprofile}}\\AppData\\LocalLow\\Team Cherry\\Hollow Knight\\*.bak}}
{{Game data/saves|OS X|{{p|osxhome}}/Library/Application Support/unity.Team Cherry.Hollow Knight/}}
}}
"""

CYBERPUNK = """
{{Game data|
{{Game data/config|Windows|{{P|localappdata}}\\CD Projekt Red\\Cyberpunk 2077}}
{{Game data/saves|Windows|{{p|userprofile}}\\Saved Games\\CD Projekt Red\\Cyberpunk 2077}}
}}
"""


def locations(wikitext):
    from linux_prefix_hub.core import pcgw
    return [(loc["type"], loc["where"], loc["win_path"])
            for loc in pcgw.parse_game_data(wikitext)]


# --- Parsing -------------------------------------------------------------
def test_install_folder_paths_land_in_the_game_space():
    """Portal 2 is the reason the install folder is a space of its own."""
    assert locations(PORTAL2) == [
        ("saves", "game_folder", "portal2/SAVE"),
        ("config", "game_folder", "portal2/cfg"),
        ("config", "game_folder", "update/cfg"),
    ]


def test_steam_cloud_and_the_registry_are_dropped():
    """`{{p|steam}}\\userdata` is Steam Cloud, `{{p|hkcu}}` the registry."""
    for _type, _where, path in locations(PORTAL2) + locations(HOLLOW_KNIGHT):
        assert "userdata" not in path
        assert "Software/Team Cherry" not in path


def test_other_operating_systems_are_dropped():
    """The Linux row has a {{p|game}} path too -- for a build we never run."""
    assert ("config", "game_folder", "hollow_knight_Data") in \
        locations(HOLLOW_KNIGHT)
    assert not any("unity3d" in path or "Library" in path
                   for _t, _w, path in locations(HOLLOW_KNIGHT))


def test_file_names_and_wildcards_become_the_folder():
    assert ("saves", "prefix",
            "AppData/LocalLow/Team Cherry/Hollow Knight") in \
        locations(HOLLOW_KNIGHT)


def test_documents_and_saved_games_keep_their_shell_folder():
    """The redirect machinery keys off exactly these first segments."""
    from linux_prefix_hub.core import registry
    found = locations(SKYRIM) + locations(CYBERPUNK)
    roots = {registry.shell_folder_root(path) for _t, _w, path in found}
    assert roots == {"Documents", "Saved Games", "AppData/Local"}


def test_saves_win_over_config_for_the_same_folder():
    from linux_prefix_hub.core import pcgw
    both = ("{{Game data/config|Windows|{{p|appdata}}\\Foo}}\n"
            "{{Game data/saves|Windows|{{p|appdata}}\\Foo}}")
    parsed = pcgw.parse_game_data(both)
    assert [(loc["type"], loc["win_path"]) for loc in parsed] == \
        [("saves", "AppData/Roaming/Foo")]


def test_a_location_carries_its_provenance():
    from linux_prefix_hub.core import pcgw
    loc = pcgw.parse_game_data(CYBERPUNK)[0]
    assert loc["detected_by"] == "pcgamingwiki"
    assert loc["redirected"] is False
    assert loc["file_count"] == 0


@pytest.mark.parametrize("raw,expected", [
    ("{{p|appdata}}\\Foo", ("prefix", "AppData/Roaming/Foo")),
    ("{{p|localappdata}}\\Foo\\", ("prefix", "AppData/Local/Foo")),
    ("{{p|userprofile\\appdata\\locallow}}\\F",
     ("prefix", "AppData/LocalLow/F")),
    ("{{p|userprofile}}\\Saved Games\\F", ("prefix", "Saved Games/F")),
    ("{{p|game}}\\{{p|uid}}\\saves", ("game_folder", "*/saves")),
    ("{{p|programdata}}\\F", None),         # outside both spaces
    ("{{p|game}}", None),                   # the folder itself, not a location
    ("{{p|userprofile}}", None),
    ("Documents\\Foo", None),               # no root token at all
])
def test_path_tokens(raw, expected):
    from linux_prefix_hub.core import pcgw
    assert pcgw.expand_path(raw) == expected


def test_garbage_never_raises():
    from linux_prefix_hub.core import pcgw
    for text in ("", "{{Game data/saves", "{{Game data/saves|Windows}}",
                 "{{Game data/saves|Windows|{{p|}}}}", "{{{{}}}}"):
        assert pcgw.parse_game_data(text) == []


# --- Picking the right article -------------------------------------------
@pytest.mark.parametrize("title,name,same", [
    ("Portal 2", "Portal 2", True),
    ("Portal", "Portal 2", False),           # the sequel trap
    ("Cyberpunk 2077", "Cyberpunk 2077 Ultimate Edition", True),
    ("The Witcher 3: Wild Hunt", "The Witcher 3 Wild Hunt", True),
    ("Doom", "Doom Eternal", True),          # decorated, not numbered
    ("X", "X4: Foundations", False),         # too short to trust
])
def test_search_hits_are_guarded(title, name, same):
    from linux_prefix_hub.core import pcgw
    assert pcgw._same_game(title, name) is same


def test_steam_appid_is_asked_first(monkeypatch):
    from linux_prefix_hub.core import pcgw
    calls = []

    def fake_get(params):
        calls.append(params["action"])
        return {"cargoquery": [{"title": {"Page": "Portal 2"}}]}

    monkeypatch.setattr(pcgw, "_get", fake_get)
    page = pcgw.resolve_page({"source": "steam", "app_id": "620",
                              "game_name": "Portal 2"})
    assert page == "Portal 2"
    assert calls == ["cargoquery"]           # no name search needed


def test_a_non_numeric_id_never_reaches_the_cargo_query(monkeypatch):
    """Lutris slugs are not appids, and the where-clause is a query."""
    from linux_prefix_hub.core import pcgw
    seen = []

    def fake_get(params):
        seen.append(params)
        return {"query": {"pages": [{"title": "Terraria", "pageid": 1}]}}

    monkeypatch.setattr(pcgw, "_get", fake_get)
    pcgw.resolve_page({"source": "lutris", "app_id": 'x" OR "1',
                       "game_name": "Terraria"})
    assert all(p["action"] != "cargoquery" for p in seen)


def test_a_missing_title_falls_back_to_search(monkeypatch):
    from linux_prefix_hub.core import pcgw

    def fake_get(params):
        if params.get("titles"):
            return {"query": {"pages": [{"title": params["titles"],
                                         "missing": True}]}}
        return {"query": {"search": [{"title": "Terraria"}]}}

    monkeypatch.setattr(pcgw, "_get", fake_get)
    assert pcgw.resolve_page({"source": "lutris", "app_id": "terraria",
                              "game_name": "Terraria (GOG)"}) == "Terraria"


# --- TLS ------------------------------------------------------------------
def test_the_host_ca_store_is_used_when_ours_is_empty(monkeypatch):
    """The AppImage's bundled CPython has no trust store of its own.

    Without this, every lookup from the packaged build reports "offline" on a
    machine that is perfectly online.
    """
    import ssl

    from linux_prefix_hub.core import pcgw
    monkeypatch.setattr(
        ssl, "create_default_context",
        lambda *a, **k: ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))

    context = pcgw.ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED      # never switched off
    assert context.cert_store_stats()["x509_ca"] > 0


def test_a_working_interpreter_keeps_its_own_store():
    from linux_prefix_hub.core import pcgw
    assert pcgw.ssl_context().cert_store_stats()["x509_ca"] > 0


# --- lookup(), cache, and the DB -----------------------------------------
def _wire(monkeypatch, wikitext=CYBERPUNK, page="Cyberpunk 2077"):
    """Answer every API call with one article. Returns the call counter."""
    from linux_prefix_hub.core import pcgw
    calls = []

    def fake_get(params):
        calls.append(params["action"])
        if params["action"] == "cargoquery":
            return {"cargoquery": [{"title": {"Page": page}}]}
        if params["action"] == "parse":
            return {"parse": {"wikitext": wikitext}}
        return {"query": {"pages": [{"title": page, "pageid": 1}]}}

    monkeypatch.setattr(pcgw, "_get", fake_get)
    return calls


def test_lookup_caches_and_does_not_ask_twice(monkeypatch):
    from linux_prefix_hub.core import pcgw
    calls = _wire(monkeypatch)
    game = {"source": "steam", "app_id": "1091500",
            "game_name": "Cyberpunk 2077"}

    first = pcgw.lookup(game)
    assert first["ok"] and len(first["locations"]) == 2
    assert first["cached"] is False
    assert first["url"].endswith("/Cyberpunk_2077")

    before = len(calls)
    second = pcgw.lookup(game)
    assert second["cached"] is True
    assert second["locations"] == first["locations"]
    assert len(calls) == before               # nothing went out again


def test_an_empty_article_is_remembered_as_such(monkeypatch):
    from linux_prefix_hub.core import pcgw
    _wire(monkeypatch, wikitext="==Video==\nnothing useful here")
    game = {"source": "steam", "app_id": "1", "game_name": "Nothing"}

    result = pcgw.lookup(game)
    assert not result["ok"] and result["reason"] == "not-found"
    assert pcgw.load_cached("steam", "1") is not None


def test_offline_is_not_an_error_and_is_not_cached(monkeypatch):
    from linux_prefix_hub.core import pcgw

    def boom(_params):
        raise pcgw._Unreachable("no network")

    monkeypatch.setattr(pcgw, "_get", boom)
    result = pcgw.lookup({"source": "steam", "app_id": "7",
                          "game_name": "Game"})
    assert result["ok"] is False
    assert result["reason"] == "offline"
    assert result["locations"] == []
    assert pcgw.load_cached("steam", "7") is None


def test_switched_off_means_no_call_at_all(monkeypatch):
    from linux_prefix_hub.core import db, pcgw
    calls = _wire(monkeypatch)
    db.set_config("online_lookup", False)

    result = pcgw.lookup({"source": "steam", "app_id": "620",
                          "game_name": "Portal 2"})
    assert result["reason"] == "disabled"
    assert calls == []


def test_a_stale_cache_expires_but_stays_readable(monkeypatch):
    from linux_prefix_hub.core import paths, pcgw
    _wire(monkeypatch)
    pcgw.lookup({"source": "steam", "app_id": "1091500",
                 "game_name": "Cyberpunk 2077"})

    path = paths.pcgw_cache_file(pcgw.cache_key("steam", "1091500"))
    data = json.loads(path.read_text(encoding="utf-8"))
    data["at"] = data["at"] - pcgw.HIT_TTL - 1
    path.write_text(json.dumps(data), encoding="utf-8")

    assert pcgw.load_cached("steam", "1091500") is None
    # The launch hook has no way to refresh it, so for that path it stands.
    assert len(pcgw.cached_locations("steam", "1091500")) == 2


def test_lookup_and_store_writes_into_the_db(monkeypatch, fake_prefix):
    from linux_prefix_hub.core import db, pcgw
    _wire(monkeypatch)
    game = {"source": "steam", "app_id": "1091500",
            "game_name": "Cyberpunk 2077", "prefix_path": str(fake_prefix),
            "user_dir": "steamuser", "game_dir": None}

    result = pcgw.lookup_and_store(game)
    assert result["stored"] is True

    found = db.find_prefix("steam", "1091500")
    assert found is not None
    paths_in_db = {loc["win_path"]
                   for loc in found[1]["storage_locations"]}
    assert "Saved Games/CD Projekt Red/Cyberpunk 2077" in paths_in_db
    assert found[1]["managed"] is False       # a lookup connects nothing


def test_a_game_without_a_folder_is_cached_but_not_stored(monkeypatch):
    """Never started: the DB is keyed by the game folder, so there is none."""
    from linux_prefix_hub.core import db, pcgw
    _wire(monkeypatch)
    result = pcgw.lookup_and_store({"source": "steam", "app_id": "1091500",
                                    "game_name": "Cyberpunk 2077",
                                    "prefix_path": None, "user_dir": None})
    assert result["locations"] and result["stored"] is False
    assert db.load_prefixes() == {}
    assert pcgw.cached_locations("steam", "1091500")


def test_a_lookup_does_not_reset_a_user_decision(monkeypatch, fake_prefix):
    from linux_prefix_hub.core import db, pcgw
    _wire(monkeypatch)
    game = {"source": "steam", "app_id": "1091500",
            "game_name": "Cyberpunk 2077", "prefix_path": str(fake_prefix),
            "user_dir": "steamuser"}
    fingerprint = db.upsert_prefix({
        **game, "managed": True,
        "storage_locations": [{
            "type": "saves", "where": "prefix",
            "win_path": "Saved Games/CD Projekt Red/Cyberpunk 2077",
            "redirected": True, "redirect_target": "/home/me/Games/CP"}]})

    pcgw.lookup_and_store(game)
    entry = db.get_prefix(fingerprint)
    assert entry["managed"] is True
    moved = [loc for loc in entry["storage_locations"]
             if loc["redirected"]]
    assert len(moved) == 1
    assert moved[0]["redirect_target"] == "/home/me/Games/CP"


# --- What the launch hook does with it -----------------------------------
def test_known_locations_sharpen_the_type_guess():
    from linux_prefix_hub.core import snapshot
    known = [{"type": "saves", "where": "prefix",
              "win_path": "AppData/LocalLow/Team Cherry/Hollow Knight"}]
    # The diff aggregates to three segments, the wiki names four.
    locs = snapshot.classify_locations(
        ["AppData/LocalLow/Team Cherry/Hollow Knight/user1.dat"],
        snapshot.WHERE_PREFIX, known)
    assert locs[0]["type"] == "saves"         # not "config" from the path
    assert locs[0]["detected_by"] == "diff"   # we did see it change


def test_an_exact_match_beats_a_containing_one():
    from linux_prefix_hub.core import snapshot
    known = [{"type": "saves", "where": "prefix",
              "win_path": "Documents/My Games/Skyrim/Saves"},
             {"type": "config", "where": "prefix",
              "win_path": "Documents/My Games/Skyrim"}]
    locs = snapshot.classify_locations(["Documents/My Games/Skyrim/x.ini"],
                                       snapshot.WHERE_PREFIX, known)
    assert locs[0]["type"] == "config"


def test_the_spaces_stay_separate():
    from linux_prefix_hub.core import snapshot
    known = [{"type": "saves", "where": "game_folder", "win_path": "cfg"}]
    locs = snapshot.classify_locations(["cfg/x.dat"], snapshot.WHERE_PREFIX,
                                       known)
    assert locs[0]["type"] != "saves"         # same path, other namespace


def test_the_hook_adds_what_was_looked_up_before(monkeypatch, fake_prefix,
                                                 tmp_path):
    """Looked up while the game had no folder yet -> stored on first launch."""
    from linux_prefix_hub.core import db, pcgw, wrapper
    _wire(monkeypatch)
    game = {"source": "steam", "app_id": "1091500",
            "game_name": "Cyberpunk 2077"}
    pcgw.lookup_and_store(game)               # cached only, no prefix yet

    ctx = {**game, "prefix_path": str(fake_prefix), "user_dir": "steamuser",
           "game_dir": None}
    fingerprint, before = wrapper._before(ctx)
    wrapper._after(ctx, before)               # nothing changed on disk

    entry = db.get_prefix(fingerprint)
    assert {loc["win_path"] for loc in entry["storage_locations"]} == {
        "AppData/Local/CD Projekt Red/Cyberpunk 2077",
        "Saved Games/CD Projekt Red/Cyberpunk 2077"}


def test_the_hook_never_goes_online(monkeypatch, fake_prefix):
    from linux_prefix_hub.core import pcgw, wrapper

    def boom(_params):
        raise AssertionError("the launch path must not talk to the wiki")

    monkeypatch.setattr(pcgw, "_get", boom)
    ctx = {"source": "steam", "app_id": "1091500", "game_name": "CP",
           "prefix_path": str(fake_prefix), "user_dir": "steamuser"}
    fingerprint, before = wrapper._before(ctx)
    wrapper._after(ctx, before)
    assert fingerprint
