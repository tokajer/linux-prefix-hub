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
    assert env[m.REEXEC_FLAG] == "1"


def test_reexec_does_not_loop(monkeypatch):
    from linux_prefix_hub import __main__ as m
    monkeypatch.setenv(m.REEXEC_FLAG, "1")
    monkeypatch.setattr(m, "_system_python_with_gtk",
                        lambda env: pytest.fail("looked again after handover"))
    assert m._reexec_gui() == 1
