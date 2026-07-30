"""Every test runs against a throwaway HOME.

`core/paths.py` resolves its constants at import time, so we point HOME and
the XDG variables at a tmp directory and reload the modules that captured
them. Nothing in the suite may touch the developer's real config, Steam
install or ~/Games.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config").mkdir(parents=True)
    (home / ".local" / "share").mkdir(parents=True)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("LPH_LANG", "en")   # deterministic assertions
    monkeypatch.delenv("LANGUAGE", raising=False)

    from linux_prefix_hub.core import i18n, integrate, paths
    importlib.reload(paths)
    importlib.reload(integrate)
    i18n.set_language(None)

    yield home

    i18n.set_language(None)


@pytest.fixture
def fake_prefix(tmp_path):
    """A minimal but realistic Wine prefix."""
    prefix = tmp_path / "pfx"
    (prefix / "drive_c" / "users" / "steamuser" / "Documents").mkdir(
        parents=True)
    (prefix / "drive_c" / "users" / "Public").mkdir(parents=True)
    (prefix / "system.reg").write_text("WINE REGISTRY Version 2\n")
    (prefix / "user.reg").write_text(
        "WINE REGISTRY Version 2\n"
        ";; All keys relative to \\\\User\\\\S-1-5-21-0-0-0-1000\n\n"
        "#arch=win64\n\n"
        "[Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Explorer"
        "\\\\Shell Folders] 1700000000\n"
        "#time=1d9f000000000000\n"
        '"Personal"="C:\\\\users\\\\steamuser\\\\Documents"\n'
        '"Desktop"="C:\\\\users\\\\steamuser\\\\Desktop"\n\n'
        "[Software\\\\Wine] 1700000000\n"
        '"Version"="win10"\n',
        encoding="utf-8")
    return prefix


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
