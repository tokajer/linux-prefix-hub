"""The launch hook and the command line -- the two things users touch."""
from __future__ import annotations

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


def test_unknown_game_for_redirect(monkeypatch, capsys):
    assert _run(monkeypatch, "--redirect", "nothing-like-this") == 1
    assert "not in the list yet" in capsys.readouterr().out


def test_version_flag(monkeypatch, capsys):
    from linux_prefix_hub import __version__
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "--version")
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out
