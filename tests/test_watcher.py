# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""New-game watcher: the set logic, without inotify or a real desktop.

`run()`/`run_poll()` loop forever, so the tests drive the pieces they are
built from. Notifications are captured instead of sent.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def watcher(monkeypatch):
    """The watcher module with notifications captured in `watcher.sent`."""
    from linux_prefix_hub.daemon import watcher as mod
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(mod, "_notify",
                        lambda title, body: sent.append((title, body)))
    monkeypatch.setattr(mod, "_maybe_notify_update", lambda: None)
    mod.sent = sent            # type: ignore[attr-defined]
    return mod


def _games(*specs):
    """(name, source, appid, installed) tuples -> discovery dicts."""
    return [{"game_name": n, "source": s, "app_id": a, "installed": i}
            for n, s, a, i in specs]


def test_first_run_marks_everything_known_without_notifying(watcher,
                                                            monkeypatch):
    monkeypatch.setattr(watcher.base, "iter_games", lambda: _games(
        ("Portal 2", "steam", "620", True),
        ("Hogwarts Legacy", "heroic", "abc", True)))

    known = watcher._initial_known()

    assert known == {"steam:620", "heroic:abc"}
    assert watcher.sent == []                     # no library-wide spam
    assert json.loads(watcher.paths.KNOWN_GAMES.read_text()) == sorted(known)


def test_only_the_new_game_is_reported(watcher, monkeypatch):
    monkeypatch.setattr(watcher.base, "iter_games",
                        lambda: _games(("Portal 2", "steam", "620", True)))
    known = watcher._initial_known()

    monkeypatch.setattr(watcher.base, "iter_games", lambda: _games(
        ("Portal 2", "steam", "620", True),
        ("Zenkcraft", "steam", "2098510", True)))
    known = watcher._refresh(known)

    assert watcher.sent and len(watcher.sent) == 1
    assert "Zenkcraft" in watcher.sent[0][1]
    assert known == {"steam:620", "steam:2098510"}


def test_a_reported_game_is_not_reported_again(watcher, monkeypatch):
    games = _games(("Zenkcraft", "steam", "2098510", True))
    monkeypatch.setattr(watcher.base, "iter_games", lambda: games)

    known: set[str] = set()
    known = watcher._refresh(known)          # reports it once
    known = watcher._refresh(known)          # and stays quiet afterwards

    assert len(watcher.sent) == 1


def test_downloading_games_are_not_reported_until_installed(watcher,
                                                            monkeypatch):
    monkeypatch.setattr(watcher.base, "iter_games", lambda: _games(
        ("ELDEN RING", "steam", "1245620", False)))
    known = watcher._refresh(set())

    assert watcher.sent == []
    assert known == set()

    monkeypatch.setattr(watcher.base, "iter_games", lambda: _games(
        ("ELDEN RING", "steam", "1245620", True)))
    known = watcher._refresh(known)

    assert len(watcher.sent) == 1
    assert known == {"steam:1245620"}


def test_known_survives_a_restart(watcher, monkeypatch):
    monkeypatch.setattr(watcher.base, "iter_games",
                        lambda: _games(("Portal 2", "steam", "620", True)))
    watcher._refresh(set())
    watcher.sent.clear()

    # Fresh process: _initial_known must read the file, not re-mark.
    assert watcher._initial_known() == {"steam:620"}
    assert watcher.sent == []


def test_corrupt_known_file_is_survivable(watcher):
    watcher.paths.KNOWN_GAMES.parent.mkdir(parents=True, exist_ok=True)
    watcher.paths.KNOWN_GAMES.write_text("{not json", encoding="utf-8")
    assert watcher._load_known() == set()


def test_update_notification_only_once_per_version(monkeypatch):
    from linux_prefix_hub.core import db
    from linux_prefix_hub.daemon import watcher as mod
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(mod, "_notify",
                        lambda t, b: sent.append((t, b)))
    monkeypatch.setattr("linux_prefix_hub.core.updater.check",
                        lambda: {"available": True, "version": "9.9.9"})

    mod._maybe_notify_update()
    mod._maybe_notify_update()

    assert len(sent) == 1
    assert "9.9.9" in sent[0][1]
    assert db.load_config().get("update_notified") == "9.9.9"


def test_updater_failure_never_takes_the_watcher_down(monkeypatch):
    from linux_prefix_hub.daemon import watcher as mod

    def boom():
        raise OSError("no network")

    monkeypatch.setattr("linux_prefix_hub.core.updater.check", boom)
    mod._maybe_notify_update()          # must not raise


def test_notify_without_notify_send_falls_back_to_stdout(monkeypatch, capsys):
    from linux_prefix_hub.daemon import watcher as mod

    def missing(*_a, **_kw):
        raise FileNotFoundError("notify-send")

    monkeypatch.setattr(mod.subprocess, "run", missing)
    mod._notify("Title", "Body")

    assert "Title" in capsys.readouterr().out


# --- Moves that were waiting for a game to exist -------------------------
@pytest.fixture
def wishful(monkeypatch):
    """The watcher with `_maybe_notify_update` off and notifications caught."""
    from linux_prefix_hub.daemon import watcher as mod
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(mod, "_notify", lambda t, b: sent.append((t, b)))
    monkeypatch.setattr(mod, "_maybe_notify_update", lambda: None)
    mod.sent = sent            # type: ignore[attr-defined]
    return mod


def test_a_pending_move_is_carried_out_by_the_watcher(wishful, monkeypatch):
    from linux_prefix_hub.core import db, redirect

    game = {"game_name": "Quake", "source": "steam", "app_id": "2310",
            "installed": True}
    db.add_pending_redirect("steam", "2310", "Quake")
    monkeypatch.setattr(wishful.base, "iter_games", lambda: [game])
    monkeypatch.setattr(redirect, "apply_pending", lambda g: ["Documents"])

    wishful._refresh({"steam:2310"})

    assert any("Documents" in body for _title, body in wishful.sent)


def test_games_nobody_asked_about_are_left_alone(wishful, monkeypatch):
    from linux_prefix_hub.core import redirect

    asked: list[str] = []
    monkeypatch.setattr(redirect, "apply_pending",
                        lambda g: asked.append(g["app_id"]) or [])
    monkeypatch.setattr(wishful.base, "iter_games", lambda: _games(
        ("Portal 2", "steam", "620", True)))

    wishful._refresh({"steam:620"})

    assert asked == []


def test_a_move_that_is_not_ready_yet_says_nothing(wishful, monkeypatch):
    """Empty means "next pass", and a retry is not news the user wants."""
    from linux_prefix_hub.core import db, redirect

    db.add_pending_redirect("steam", "2310", "Quake")
    monkeypatch.setattr(wishful.base, "iter_games", lambda: _games(
        ("Quake", "steam", "2310", True)))
    monkeypatch.setattr(redirect, "apply_pending", lambda g: [])

    wishful._refresh({"steam:2310"})

    assert wishful.sent == []


def test_a_failing_move_never_takes_the_watcher_down(wishful, monkeypatch):
    from linux_prefix_hub.core import db, redirect

    def boom(_game):
        raise OSError("read-only file system")

    db.add_pending_redirect("steam", "2310", "Quake")
    monkeypatch.setattr(wishful.base, "iter_games", lambda: _games(
        ("Quake", "steam", "2310", True)))
    monkeypatch.setattr(redirect, "apply_pending", boom)

    assert wishful._refresh({"steam:2310"}) == {"steam:2310"}
