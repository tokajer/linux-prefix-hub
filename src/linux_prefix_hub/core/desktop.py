"""Talking to the desktop environment -- currently: open a folder.

Kept out of `gui/` on purpose. The GUI is presentation, and "show me this
folder" is just as useful from the terminal (`--open`), so the knowledge of
*how* to hand a path to the desktop lives here, once.

`xdg-open` first, because it honours whatever file manager the user actually
configured; the rest is a fallback chain for the desktops that ship their own.
We never wait for the file manager to exit -- it outlives us.

**Which is exactly why it gets a clean environment** (`_child_env`). A file
manager is not a program we start and forget: KDE keeps one Dolphin for the
whole session and hands every new window to it, so anything we leak into it is
inherited by everything the user opens from it afterwards.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# In order of preference: the portable one, then per-desktop file managers.
OPENERS = ("xdg-open", "gio", "nautilus", "dolphin", "nemo", "thunar",
           "pcmanfm")

# Our GUI re-exec guard. Defined in `__main__` (REEXEC_FLAG); repeated here
# rather than imported, because `python -m` would give us a second copy of
# that module. A test keeps the two spellings in sync.
GUI_REEXEC_FLAG = "LPH_GUI_REEXEC"


def _argv(opener: str, path: str) -> list[str]:
    return [opener, "open", path] if opener == "gio" else [opener, path]


def _child_env() -> dict[str, str]:
    """The environment the file manager would have had without us.

    Same reasoning as `wrapper.game_env` -- and deliberately the same list,
    imported rather than repeated: nothing the AppImage set up for *our*
    bundled interpreter (`PYTHONHOME` into a /tmp mount that will be gone)
    may travel into a long-lived desktop process.

    On top of it our own `LPH_GUI_REEXEC`. Leaking that one cost a whole
    session of "the app does not start any more" -- see
    `__main__._reexec_gui`.
    """
    from .wrapper import game_env
    env = game_env() or dict(os.environ)
    env.pop(GUI_REEXEC_FLAG, None)
    return env


def open_folder(path: str | Path) -> bool:
    """Show `path` in the user's file manager. False if nothing could do it.

    A missing folder is not opened: file managers react to that anywhere
    between a silent no-op and an error dialog, and "nothing happened" is a
    worse answer than a clean False the caller can report.
    """
    folder = Path(path)
    if not folder.is_dir():
        return False
    env = _child_env()
    for opener in OPENERS:
        if not shutil.which(opener):
            continue
        try:
            subprocess.Popen(_argv(opener, str(folder)),
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             start_new_session=True, env=env)
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False
