"""Talking to the desktop environment -- currently: open a folder.

Kept out of `gui/` on purpose. The GUI is presentation, and "show me this
folder" is just as useful from the terminal (`--open`), so the knowledge of
*how* to hand a path to the desktop lives here, once.

`xdg-open` first, because it honours whatever file manager the user actually
configured; the rest is a fallback chain for the desktops that ship their own.
We never wait for the file manager to exit -- it outlives us.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# In order of preference: the portable one, then per-desktop file managers.
OPENERS = ("xdg-open", "gio", "nautilus", "dolphin", "nemo", "thunar",
           "pcmanfm")


def _argv(opener: str, path: str) -> list[str]:
    return [opener, "open", path] if opener == "gio" else [opener, path]


def open_folder(path: str | Path) -> bool:
    """Show `path` in the user's file manager. False if nothing could do it.

    A missing folder is not opened: file managers react to that anywhere
    between a silent no-op and an error dialog, and "nothing happened" is a
    worse answer than a clean False the caller can report.
    """
    folder = Path(path)
    if not folder.is_dir():
        return False
    for opener in OPENERS:
        if not shutil.which(opener):
            continue
        try:
            subprocess.Popen(_argv(opener, str(folder)),
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False
