# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""The launch hook and the command line -- the two things users touch."""
from __future__ import annotations

import os
import sys

import pytest


@pytest.fixture
def context(fake_prefix, monkeypatch):
    """Pretend a launcher told us which game is starting."""
    from linux_prefix_hub.adapters import base
    ctx = {"source": "lutris", "app_id": "quake", "game_name": "Quake",
           "prefix_path": str(fake_prefix), "user_dir": "steamuser"}
    monkeypatch.setattr(base, "context_for", lambda source, app_id: ctx)
    monkeypatch.setattr(base, "context_from_env", lambda: ctx)
    return ctx, fake_prefix


def _write_save(prefix, name="save0.sav"):
    folder = prefix / "drive_c/users/steamuser/Documents/My Games/Quake"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text("progress")


# --- pre/post hooks (Lutris style) --------------------------------------
def test_hooks_learn_the_storage_location(context):
    from linux_prefix_hub.core import db, wrapper
    _ctx, prefix = context

    assert wrapper.hook("pre", "lutris", "quake") == 0
    _write_save(prefix)
    assert wrapper.hook("post", "lutris", "quake") == 0

    entry = db.get_prefix(db.fingerprint(prefix))
    assert entry["game_name"] == "Quake"
    assert any("My Games/Quake" in loc["win_path"]
               for loc in entry["storage_locations"])


def test_a_launch_forgets_a_location_a_filter_now_covers(context):
    """Self-heal, next to `redirect.reapply`: the filters we ship grow."""
    from linux_prefix_hub.core import db, wrapper
    _ctx, prefix = context
    fingerprint = db.fingerprint(prefix)
    db.upsert_prefix({
        "source": "lutris", "app_id": "quake", "game_name": "Quake",
        "prefix_path": str(prefix), "user_dir": "steamuser",
        "storage_locations": [
            {"type": "config", "win_path": "AppData/Local/dxvk"}]})

    assert wrapper.hook("pre", "lutris", "quake") == 0

    assert db.get_prefix(fingerprint)["storage_locations"] == []


def test_hook_without_a_prefix_is_not_an_error(monkeypatch):
    from linux_prefix_hub.adapters import base
    from linux_prefix_hub.core import wrapper
    monkeypatch.setattr(base, "context_for", lambda source, app_id: None)
    monkeypatch.setattr(base, "context_from_env", lambda: None)
    assert wrapper.hook("pre", "lutris", "never-started") == 0


# --- wrapping (Steam / Heroic style) ------------------------------------
def test_wrapper_runs_the_game_and_learns(context):
    from linux_prefix_hub.core import db, wrapper
    _ctx, prefix = context
    save = prefix / "drive_c/users/steamuser/Documents/My Games/Quake/s.sav"

    code = wrapper.main([
        sys.executable, "-c",
        f"import pathlib;p=pathlib.Path({str(save)!r});"
        "p.parent.mkdir(parents=True,exist_ok=True);p.write_text('x')"])

    assert code == 0
    entry = db.get_prefix(db.fingerprint(prefix))
    assert any("Quake" in loc["win_path"]
               for loc in entry["storage_locations"])


def test_wrapper_passes_through_the_exit_code(context):
    from linux_prefix_hub.core import wrapper
    assert wrapper.main([sys.executable, "-c", "raise SystemExit(3)"]) == 3


def test_a_broken_detection_still_launches_the_game(monkeypatch):
    from linux_prefix_hub.adapters import base
    from linux_prefix_hub.core import wrapper

    def explode():
        raise RuntimeError("discovery is having a bad day")

    monkeypatch.setattr(base, "context_from_env", explode)
    assert wrapper.main([sys.executable, "-c", "raise SystemExit(7)"]) == 7


def test_wrapper_without_a_command_complains():
    from linux_prefix_hub.core import wrapper
    assert wrapper.main([]) == 2


def test_the_wrapper_learns_saves_in_the_install_folder(fake_prefix,
                                                        tmp_path,
                                                        monkeypatch):
    """Portal 2: nothing lands in the prefix, the saves go to the game dir."""
    from linux_prefix_hub.adapters import base
    from linux_prefix_hub.core import db, wrapper

    game_dir = tmp_path / "common/Portal 2"
    (game_dir / "portal2").mkdir(parents=True)
    ctx = {"source": "steam", "app_id": "620", "game_name": "Portal 2",
           "prefix_path": str(fake_prefix), "user_dir": "steamuser",
           "game_dir": str(game_dir)}
    monkeypatch.setattr(base, "context_from_env", lambda: ctx)

    save = game_dir / "portal2/SAVE/765611/sp_a2.sav"
    code = wrapper.main([
        sys.executable, "-c",
        f"import pathlib;p=pathlib.Path({str(save)!r});"
        "p.parent.mkdir(parents=True,exist_ok=True);p.write_text('x')"])

    assert code == 0
    entry = db.get_prefix(db.fingerprint(fake_prefix))
    assert entry["game_dir"] == str(game_dir)
    found = [loc for loc in entry["storage_locations"]
             if loc["where"] == "game_folder"]
    assert found and "SAVE" in found[0]["win_path"]


# --- what the game inherits ---------------------------------------------
def _as_appimage(monkeypatch, appdir="/tmp/.mount_abc"):
    """Pretend we were started through the AppImage's AppRun."""
    monkeypatch.setenv("APPDIR", appdir)
    monkeypatch.setenv("APPIMAGE", "/home/me/.local/share/x/App.AppImage")
    monkeypatch.setenv("PYTHONHOME", f"{appdir}/opt/python3.12")
    monkeypatch.setenv("PYTHONPATH", f"{appdir}/usr/lib/python:/opt/mine")
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    return appdir


def test_the_game_does_not_inherit_our_interpreter(monkeypatch):
    """PYTHONHOME reaching the game breaks Proton -- it is Python too."""
    from linux_prefix_hub.core import wrapper
    _as_appimage(monkeypatch)
    monkeypatch.setenv("SteamAppId", "620")     # the launcher's own vars stay

    env = wrapper.game_env()

    assert env is not None
    assert "PYTHONHOME" not in env
    assert "APPDIR" not in env and "APPIMAGE" not in env
    assert "PYTHONDONTWRITEBYTECODE" not in env
    assert env["PYTHONPATH"] == "/opt/mine"     # ours dropped, theirs kept
    assert env["SteamAppId"] == "620"


def test_a_bundle_only_path_list_disappears_entirely(monkeypatch):
    from linux_prefix_hub.core import wrapper
    appdir = _as_appimage(monkeypatch)
    monkeypatch.setenv("PYTHONPATH", f"{appdir}/usr/lib/python:")

    assert "PYTHONPATH" not in (wrapper.game_env() or {})


def test_outside_the_appimage_the_environment_is_left_alone(monkeypatch):
    from linux_prefix_hub.core import wrapper
    monkeypatch.delenv("APPDIR", raising=False)
    assert wrapper.game_env() is None


def test_the_game_really_runs_without_pythonhome(context, tmp_path,
                                                 monkeypatch):
    """End to end: the child process must not see our bundle.

    Without the cleanup this does not just fail the assertion -- the child
    interpreter dies on the bogus PYTHONHOME, which is exactly what happens
    to Proton.
    """
    from linux_prefix_hub.core import wrapper
    _as_appimage(monkeypatch)
    seen = tmp_path / "seen.txt"
    code = wrapper.main([
        sys.executable, "-c",
        f"import os,pathlib;pathlib.Path({str(seen)!r}).write_text("
        "os.environ.get('PYTHONHOME','') + '|' + os.environ.get('APPDIR',''))"
    ])
    assert code == 0
    assert seen.read_text() == "|"


# --- CLI -----------------------------------------------------------------
def _run(monkeypatch, *args) -> int:
    from linux_prefix_hub import __main__
    monkeypatch.setattr(sys, "argv", ["linux-prefix-hub", *args])
    return __main__.main()


def test_status_is_helpful_when_nothing_is_known(monkeypatch, capsys):
    assert _run(monkeypatch, "--status") == 0
    assert "Nothing learned yet" in capsys.readouterr().out


def test_scan_reports_an_empty_system(monkeypatch, capsys):
    assert _run(monkeypatch, "--scan") == 0
    assert "No games found" in capsys.readouterr().out


def test_check_update_does_not_claim_to_be_current_without_asking(monkeypatch,
                                                                  capsys):
    """A build with no updater in it (pip, or the local AppImage) must say
    so -- it used to report "you are up to date" past a newer release."""
    from linux_prefix_hub.core import updater
    monkeypatch.setattr(updater, "_manager", lambda: None)

    assert _run(monkeypatch, "--check-update") == 1
    out = capsys.readouterr().out
    assert "cannot update itself" in out
    assert "up to date" not in out
    assert "github.com/tokajer" in out


def test_check_update_says_up_to_date_when_it_really_asked(monkeypatch,
                                                           capsys):
    from linux_prefix_hub.core import updater
    monkeypatch.setattr(updater, "check",
                        lambda force=False: {"available": False,
                                             "version": "1.0.0",
                                             "reason": ""})
    assert _run(monkeypatch, "--check-update") == 0
    assert "up to date" in capsys.readouterr().out


def test_status_lists_a_redirected_location(monkeypatch, capsys, fake_prefix):
    from linux_prefix_hub.core import db
    fingerprint = db.upsert_prefix({
        "source": "steam", "app_id": "2310", "game_name": "Quake",
        "prefix_path": str(fake_prefix), "user_dir": "steamuser",
        "storage_locations": [{"type": "saves", "win_path": "Documents/Q"}],
    })
    db.update_location(fingerprint, "Documents/Q", redirected=True,
                       redirect_target="/home/me/Games/Quake/Documents")

    assert _run(monkeypatch, "--status") == 0
    out = capsys.readouterr().out
    assert "Quake" in out and "moved to /home/me/Games/Quake/Documents" in out


def test_status_marks_install_folder_saves_as_immovable(monkeypatch, capsys,
                                                        fake_prefix):
    from linux_prefix_hub.core import db
    db.upsert_prefix({
        "source": "steam", "app_id": "620", "game_name": "Portal 2",
        "prefix_path": str(fake_prefix), "user_dir": "steamuser",
        "storage_locations": [{"type": "saves", "win_path": "portal2/SAVE",
                               "where": "game_folder"}],
    })
    assert _run(monkeypatch, "--status") == 0
    assert "stays there" in capsys.readouterr().out


def test_redirect_explains_why_it_cannot_move_the_game_folder(monkeypatch,
                                                              capsys,
                                                              fake_prefix):
    from linux_prefix_hub.core import db
    db.upsert_prefix({
        "source": "steam", "app_id": "620", "game_name": "Portal 2",
        "prefix_path": str(fake_prefix), "user_dir": "steamuser",
        "storage_locations": [{"type": "saves", "win_path": "portal2/SAVE",
                               "where": "game_folder"}],
    })
    assert _run(monkeypatch, "--redirect", "Portal 2") == 1
    out = capsys.readouterr().out
    assert "its own folder" in out and "--open" in out


def test_version_shows_the_licence_notice(monkeypatch, capsys):
    """GPL section 5d: an interactive program says who owns it, under what
    terms, and that there is no warranty. argparse would re-wrap all four
    lines into one paragraph, hence our own action."""
    import pytest as _pytest

    from linux_prefix_hub import __version__
    with _pytest.raises(SystemExit) as exit_info:
        _run(monkeypatch, "--version")
    assert exit_info.value.code == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].endswith(__version__)
    assert "Copyright (C)" in lines[1]
    assert "GPL-3.0-or-later" in lines[2]
    assert "NO WARRANTY" in "\n".join(lines)


def test_no_version_is_written_down_anywhere(monkeypatch):
    """The release tag is the version. A number in the source is a number
    that can contradict the tag -- which is exactly what broke a release."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    init = (root / "src/linux_prefix_hub/__init__.py").read_text(
        encoding="utf-8")
    assert '__version__ = "' not in init          # derived, never assigned
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'dynamic = ["version"]' in pyproject
    assert "\nversion = " not in pyproject

    for script in ("build-appimage.sh", "build-velopack.sh"):
        text = (root / "packaging" / script).read_text(encoding="utf-8")
        # They write the version in, they no longer read it out.
        assert "_version.py" in text
        assert "s/^__version__" not in text


def test_the_licence_file_is_the_gpl(monkeypatch):
    """The metadata must not promise terms the LICENSE does not carry."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    text = (root / "LICENSE").read_text(encoding="utf-8")
    assert "GNU GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 29 June 2007" in text
    assert "GPL-3.0-or-later" in (root / "pyproject.toml").read_text(
        encoding="utf-8")


def test_open_falls_back_to_the_game_folder(monkeypatch, capsys, fake_prefix):
    """Nothing learned yet -- then the game folder is the answer."""
    from linux_prefix_hub.adapters import base
    from linux_prefix_hub.core import desktop
    opened: list[str] = []
    monkeypatch.setattr(desktop, "open_folder",
                        lambda p: opened.append(str(p)) or True)
    monkeypatch.setattr(base, "iter_games", lambda sources=None: iter([
        {"source": "steam", "app_id": "620", "game_name": "Portal 2",
         "prefix_path": str(fake_prefix), "user_dir": "steamuser"}]))

    assert _run(monkeypatch, "--open", "Portal 2") == 0
    assert opened == [str(fake_prefix)]
    assert "Portal 2" not in capsys.readouterr().out    # the path is printed


def test_open_prefers_the_save_locations(monkeypatch, capsys, fake_prefix):
    """The game folder is the fallback, not the answer."""
    from linux_prefix_hub.core import db, desktop
    db.upsert_prefix({
        "source": "steam", "app_id": "620", "game_name": "Portal 2",
        "prefix_path": str(fake_prefix), "user_dir": "steamuser",
        "storage_locations": [{"type": "saves", "where": "prefix",
                               "win_path": "Documents"}],
    })
    opened: list[str] = []
    monkeypatch.setattr(desktop, "open_folder",
                        lambda p: opened.append(str(p)) or True)

    assert _run(monkeypatch, "--open", "Portal 2") == 0
    assert opened == [str(fake_prefix / "drive_c/users/steamuser/Documents")]


def test_open_says_so_when_there_is_no_folder_at_all(monkeypatch, capsys):
    from linux_prefix_hub.adapters import base
    monkeypatch.setattr(base, "iter_games", lambda sources=None: iter([]))
    assert _run(monkeypatch, "--open", "Nothing") == 1
    assert "not in the list yet" in capsys.readouterr().out


def test_save_folder_is_configurable(monkeypatch, capsys, isolated_home):
    from linux_prefix_hub.core import db, redirect
    assert _run(monkeypatch, "--set-data-folder",
                str(isolated_home / "Spielstaende")) == 0
    assert "Spielstaende" in capsys.readouterr().out

    assert db.redirect_root() == isolated_home / "Spielstaende"
    assert redirect.default_target("Quake", "Documents") == \
        isolated_home / "Spielstaende/Quake/Documents"


def test_ignoring_a_path_cleans_up_what_was_already_recorded(monkeypatch,
                                                             capsys,
                                                             tmp_path):
    """Typing this is usually about a folder the user is looking at now."""
    from linux_prefix_hub.core import db
    fingerprint = db.upsert_prefix({
        "source": "steam", "app_id": "620", "game_name": "Portal 2",
        "prefix_path": str(tmp_path / "pfx"), "user_dir": "steamuser",
        "storage_locations": [
            {"type": "config", "win_path": "AppData/Roaming/Telemetry"},
            {"type": "saves", "win_path": "Documents/Portal 2"}]})

    assert _run(monkeypatch, "--ignore-path", "AppData/Roaming/Telemetry") == 0
    out = capsys.readouterr().out
    assert "Ignoring" in out and "1 known location" in out

    stored = [loc["win_path"]
              for loc in db.get_prefix(fingerprint)["storage_locations"]]
    assert stored == ["Documents/Portal 2"]

    assert _run(monkeypatch, "--unignore-path",
                "AppData/Roaming/Telemetry") == 0
    assert db.extra_ignore_paths() == []


def test_the_default_save_folder_is_our_own(isolated_home):
    from linux_prefix_hub.core import db
    assert db.redirect_root() == isolated_home / "Games/linux-prefix-hub/Games"


def test_language_flag_translates_this_run(monkeypatch, capsys):
    assert _run(monkeypatch, "--lang", "de", "--status") == 0
    assert "Noch nichts gelernt" in capsys.readouterr().out


def test_set_language_is_remembered(monkeypatch, capsys):
    from linux_prefix_hub.core import db
    # The env override is for testing/one-off runs; drop it so the stored
    # preference is what decides.
    monkeypatch.delenv("LPH_LANG", raising=False)
    monkeypatch.setenv("LANG", "en_US.UTF-8")

    assert _run(monkeypatch, "--set-language", "de") == 0
    assert db.load_config()["language"] == "de"
    assert "Sprache" in capsys.readouterr().out

    assert _run(monkeypatch, "--status") == 0     # and it sticks
    assert "Noch nichts gelernt" in capsys.readouterr().out


# --- --lookup asks before it keeps anything ------------------------------
def _one_game_with_saves(monkeypatch, fake_prefix):
    """One Steam game whose Documents folder is on disk, plus one wiki hit."""
    from linux_prefix_hub.adapters import base
    from linux_prefix_hub.core import pcgw
    saves = fake_prefix / "drive_c/users/steamuser/Documents/My Games/Quake"
    saves.mkdir(parents=True)
    game = {"source": "steam", "app_id": "2310", "game_name": "Quake",
            "installed": True, "prefix_path": str(fake_prefix),
            "user_dir": "steamuser", "game_dir": None}
    monkeypatch.setattr(base, "iter_games",
                        lambda sources=None: iter([dict(game)]))
    monkeypatch.setattr(pcgw, "lookup", lambda g, refresh=False: {
        "ok": True, "reason": "", "page": "Quake", "url": None,
        "cached": False, "message": "PCGamingWiki knows 2 location(s).",
        "locations": [
            {"type": "saves", "where": "prefix", "file_count": 0,
             "win_path": "Documents/My Games/Quake"},
            {"type": "config", "where": "prefix", "file_count": 0,
             "win_path": "AppData/Roaming/Quake"}]})
    return game


def test_lookup_keeps_nothing_until_it_is_answered(monkeypatch, capsys,
                                                   fake_prefix):
    """The suggestion is shown, the answer is no, the DB stays empty."""
    from linux_prefix_hub.core import db
    _one_game_with_saves(monkeypatch, fake_prefix)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    assert _run(monkeypatch, "--lookup", "Quake") == 0
    out = capsys.readouterr().out
    assert "Documents/My Games/Quake" in out
    assert "(not there yet)" in out            # the AppData one
    assert "Nothing was added." in out
    assert db.load_prefixes() == {}
    assert db.confirmed_lookups() == {}


def test_lookup_keeps_what_was_confirmed(monkeypatch, capsys, fake_prefix):
    from linux_prefix_hub.core import db
    _one_game_with_saves(monkeypatch, fake_prefix)
    monkeypatch.setattr("builtins.input", lambda _prompt: "y")

    assert _run(monkeypatch, "--lookup", "Quake") == 0
    out = capsys.readouterr().out
    assert "Added 1 storage location(s)." in out
    assert "1 of them do not exist yet" in out

    found = db.find_prefix("steam", "2310")
    assert [loc["win_path"] for loc in found[1]["storage_locations"]] == [
        "Documents/My Games/Quake"]


def test_a_pipe_is_not_a_yes(monkeypatch, capsys, fake_prefix):
    """No terminal to ask means no, not "go ahead" -- `--yes` is the yes."""
    from linux_prefix_hub.core import db

    def no_terminal(_prompt):
        raise EOFError

    _one_game_with_saves(monkeypatch, fake_prefix)
    monkeypatch.setattr("builtins.input", no_terminal)
    assert _run(monkeypatch, "--lookup", "Quake") == 0
    assert "Nothing was added." in capsys.readouterr().out
    assert db.load_prefixes() == {}

    assert _run(monkeypatch, "--lookup", "Quake", "--yes") == 0
    assert "Added 1 storage location(s)." in capsys.readouterr().out
    assert db.find_prefix("steam", "2310") is not None


def test_unknown_game_for_redirect(monkeypatch, capsys):
    """A name that is no game at all says so -- it is not a pending wish.

    "Not learned about yet" and "does not exist" used to share one message.
    Now that the first case can be answered (the wish is stored, see
    `test_redirect_before_the_first_launch_is_remembered`), the second one
    has to be told apart or every typo turns into a wish nothing can meet.
    """
    assert _run(monkeypatch, "--redirect", "nothing-like-this") == 1
    assert "No game found" in capsys.readouterr().out


def test_redirect_before_the_first_launch_is_remembered(monkeypatch, capsys):
    """Nothing to move yet is not the same as nothing to answer."""
    from linux_prefix_hub.adapters import base
    from linux_prefix_hub.core import db

    monkeypatch.setattr(base, "iter_games", lambda sources=None: iter([
        {"source": "steam", "app_id": "2310", "game_name": "Quake",
         "installed": True, "prefix_path": None, "user_dir": None}]))

    assert _run(monkeypatch, "--redirect", "Quake") == 0
    assert "first time you play it" in capsys.readouterr().out
    assert "steam:2310" in db.pending_redirects()

    assert _run(monkeypatch, "--undo-redirect", "Quake") == 0
    assert "left where it is" in capsys.readouterr().out
    assert db.pending_redirects() == {}


# --- Hiding a game from the lists ----------------------------------------
def _library(monkeypatch):
    from linux_prefix_hub.adapters import base
    games = [{"source": "steam", "app_id": "620", "game_name": "Portal 2",
              "installed": True, "prefix_path": None, "user_dir": None},
             {"source": "lutris", "app_id": "quake", "game_name": "Quake",
              "installed": True, "prefix_path": None, "user_dir": None}]
    monkeypatch.setattr(base, "iter_games",
                        lambda sources=None: iter(list(games)))
    return games


def test_a_hidden_game_leaves_the_scan(monkeypatch, capsys):
    from linux_prefix_hub.core import db
    _library(monkeypatch)

    assert _run(monkeypatch, "--hide", "Portal 2") == 0
    assert "is hidden" in capsys.readouterr().out
    assert db.hidden_games() == ["steam:620"]

    assert _run(monkeypatch, "--scan") == 0
    out = capsys.readouterr().out
    assert "Portal 2" not in out
    assert "Quake" in out
    assert "1 hidden game(s) not listed" in out


def test_show_hidden_lists_it_again_and_marks_it(monkeypatch, capsys):
    _library(monkeypatch)
    assert _run(monkeypatch, "--hide", "Portal 2") == 0
    capsys.readouterr()

    assert _run(monkeypatch, "--scan", "--show-hidden") == 0
    out = capsys.readouterr().out
    assert "Portal 2" in out
    assert "[hidden]" in out
    assert "not listed" not in out           # nothing was left out


def test_unhide_finds_a_game_that_is_hidden(monkeypatch, capsys):
    """`--unhide` has to reach past its own filter, or nothing comes back."""
    from linux_prefix_hub.core import db
    _library(monkeypatch)
    _run(monkeypatch, "--hide", "Portal 2")
    capsys.readouterr()

    assert _run(monkeypatch, "--unhide", "Portal 2") == 0
    assert "back in the list" in capsys.readouterr().out
    assert db.hidden_games() == []

    assert _run(monkeypatch, "--unhide", "Portal 2") == 0
    assert "was not hidden" in capsys.readouterr().out


def test_a_scan_with_everything_hidden_says_why(monkeypatch, capsys):
    """"No games found" in front of a library nobody lost reads as a bug."""
    _library(monkeypatch)
    _run(monkeypatch, "--hide", "Portal 2")
    _run(monkeypatch, "--hide", "Quake")
    capsys.readouterr()

    assert _run(monkeypatch, "--scan") == 0
    out = capsys.readouterr().out
    assert "No games found" not in out
    assert "All 2 game(s) found are hidden" in out


def test_hiding_an_unknown_game_is_an_error(monkeypatch, capsys):
    _library(monkeypatch)
    assert _run(monkeypatch, "--hide", "nothing-like-this") == 1
    assert "No game found" in capsys.readouterr().out


def test_version_flag(monkeypatch, capsys):
    from linux_prefix_hub import __version__
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "--version")
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


# --- how a bare start reaches the window ---------------------------------
def _gui_env(monkeypatch, *, display, gtk_here):
    """Pretend we have/haven't a display and GTK in this interpreter."""
    from linux_prefix_hub import __main__ as m
    monkeypatch.setattr(m, "_has_display", lambda: display)
    monkeypatch.setattr(m, "_has_gtk", lambda: gtk_here)
    return m


def _set_up(monkeypatch):
    """Mark setup as done, so the terminal flow prints the overview instead
    of running the whole first-run setup (which shells out to systemctl)."""
    from linux_prefix_hub.core import db, integrate
    db.set_config("setup_done", True)
    monkeypatch.setattr(integrate.subprocess, "run", lambda *a, **kw: None)


def _stub_gui(monkeypatch, main):
    """Stand in for gui.app without importing it.

    The real module imports `gi`, which the venv does not have -- the suite
    must keep running on the dependency-free path.
    """
    import sys
    import types

    import linux_prefix_hub.gui as gui_pkg
    module = types.ModuleType("linux_prefix_hub.gui.app")
    module.main = main
    monkeypatch.setitem(sys.modules, "linux_prefix_hub.gui.app", module)
    monkeypatch.setattr(gui_pkg, "app", module, raising=False)
    return module


def test_bare_start_opens_the_window(monkeypatch):
    """No arguments must mean the window, not the overview."""
    _gui_env(monkeypatch, display=True, gtk_here=True)
    opened: list[str] = []
    _stub_gui(monkeypatch, lambda *a: opened.append("gui") or 0)

    assert _run(monkeypatch) == 0
    assert opened == ["gui"]


def test_bare_start_hands_over_when_this_interpreter_lacks_gtk(monkeypatch,
                                                               capsys):
    """The AppImage case: its bundled CPython has no PyGObject, so the old
    `_gui_available()` check sent a bare start to the terminal instead of
    trying the hand-over at all."""
    m = _gui_env(monkeypatch, display=True, gtk_here=False)
    tried: list[str] = []
    monkeypatch.setattr(m, "_reexec_gui",
                        lambda: tried.append("reexec") or 1)

    _run(monkeypatch)

    assert tried == ["reexec"]


def test_no_display_falls_back_without_probing(monkeypatch, capsys):
    """On a TTY or over ssh there is nothing to draw on -- and no point
    probing every interpreter on the box for GTK."""
    m = _gui_env(monkeypatch, display=False, gtk_here=False)
    _set_up(monkeypatch)
    monkeypatch.setattr(m, "_reexec_gui",
                        lambda: pytest.fail("probed without a display"))
    _stub_gui(monkeypatch, lambda *a: pytest.fail("opened a window"))

    assert _run(monkeypatch) == 0
    assert "--scan" in capsys.readouterr().out      # the overview


def test_terminal_flag_skips_the_window(monkeypatch, capsys):
    _gui_env(monkeypatch, display=True, gtk_here=True)
    _set_up(monkeypatch)
    _stub_gui(monkeypatch, lambda *a: pytest.fail("opened a window"))

    assert _run(monkeypatch, "--terminal") == 0
    assert "--scan" in capsys.readouterr().out


def test_uninstall_shows_the_plan_and_asks_first(monkeypatch, capsys,
                                                 fake_prefix):
    """The interesting part of an uninstall is not what gets deleted.

    It is which game folders move back and which launcher configs get
    edited -- both happen whether the user thought to ask for them or not,
    so both are on screen before the question.
    """
    from linux_prefix_hub.core import db, paths
    db.upsert_prefix({
        "source": "steam", "app_id": "2310", "game_name": "Quake",
        "prefix_path": str(fake_prefix), "user_dir": "steamuser",
        "storage_locations": [{"type": "saves", "win_path": "Documents/Q",
                               "redirected": True,
                               "redirect_target": "/home/me/Games/Q"}],
    })
    paths.WRAPPER_SHIM.parent.mkdir(parents=True, exist_ok=True)
    paths.WRAPPER_SHIM.write_text("#!/bin/sh\n")
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")

    assert _run(monkeypatch, "--uninstall") == 0

    out = capsys.readouterr().out
    assert "Quake" in out
    assert str(paths.WRAPPER_SHIM) in out
    assert "Nothing was changed." in out
    assert paths.WRAPPER_SHIM.exists()          # answered no, so nothing went


def test_uninstall_refuses_while_a_game_is_running(monkeypatch, capsys,
                                                   fake_prefix):
    from linux_prefix_hub.core import db, registry
    db.upsert_prefix({
        "source": "steam", "app_id": "2310", "game_name": "Quake",
        "prefix_path": str(fake_prefix), "user_dir": "steamuser",
        "storage_locations": [{"type": "saves", "win_path": "Documents/Q",
                               "redirected": True,
                               "redirect_target": "/home/me/Games/Q"}],
    })
    monkeypatch.setattr(registry, "prefix_in_use", lambda prefix: True)
    monkeypatch.setattr("builtins.input",
                        lambda _p="": pytest.fail("asked anyway"))

    assert _run(monkeypatch, "--uninstall") == 1
    out = capsys.readouterr().out
    assert "Quake is running" in out
    assert "Nothing was changed." in out


def test_handover_env_drops_pythonhome(monkeypatch):
    """PYTHONHOME points at the bundled interpreter; inheriting it makes any
    other interpreter load the wrong standard library."""
    from linux_prefix_hub import __main__ as m
    monkeypatch.setenv("PYTHONHOME", "/appimage/opt/python3.12")
    monkeypatch.setenv("PYTHONPATH", "/appimage/usr/lib/python")

    env = m._handover_env()

    assert "PYTHONHOME" not in env
    assert env["PYTHONPATH"] != "/appimage/usr/lib/python"
    assert env["PYTHONPATH"].endswith("src") or "linux_prefix_hub" not in \
        env["PYTHONPATH"].rsplit("/", 1)[-1]
    assert env[m.REEXEC_FLAG] == str(os.getpid())


def test_reexec_does_not_loop(monkeypatch):
    """`execve` keeps the pid, so the flag still applies to the handover."""
    from linux_prefix_hub import __main__ as m
    monkeypatch.setenv(m.REEXEC_FLAG, str(os.getpid()))
    monkeypatch.setattr(m, "_system_python_with_gtk",
                        lambda env: pytest.fail("looked again after handover"))
    assert m._reexec_gui() == 1


def test_an_inherited_flag_does_not_block_the_handover(monkeypatch):
    """A flag from *another* process must not count as "already tried".

    This cost a whole session once: our own "open folder" button starts the
    file manager, KDE keeps that Dolphin alive and hands it every new window,
    and everything started from it inherited the guard -- so the app fell
    through to the terminal branch and simply never showed a window again.
    """
    from linux_prefix_hub import __main__ as m
    monkeypatch.setenv(m.REEXEC_FLAG, str(os.getpid() + 1))
    looked: list[str] = []
    monkeypatch.setattr(m, "_system_python_with_gtk",
                        lambda env: looked.append("looked") or None)

    assert m._reexec_gui() == 1        # no interpreter found in this test
    assert looked == ["looked"]        # but it did try, which is the point
