# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Reading and writing shell-folder entries in a prefix' `user.reg`.

This is the "official" half of the hybrid redirection (see redirect.py): tell
Wine where the Windows shell folders live, so well-behaved games follow.

`user.reg` is a text file. We edit it **surgically** -- find the section, patch
or insert the one value, leave every other byte alone. A full parse/rewrite
would churn the file and risk losing keys we do not understand.

Two hard rules, both learned the painful way:
  1. Wine keeps the registry in memory and flushes it on wineserver shutdown.
     Editing `user.reg` while anything runs in that prefix means your change is
     overwritten. `prefix_in_use()` exists to refuse that.
  2. Some folders only have a GUID name (Downloads, Saved Games, LocalLow),
     never a readable one. Hence SHELL_FOLDERS maps to a *list* of value names
     and we write all of them.

VERIFY-ON-DEVICE: check one redirected folder in-game after the first launch
(`winecfg` -> Desktop Integration shows the same values).
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

SHELL_FOLDERS_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders")
USER_SHELL_FOLDERS_KEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")

# Our storage-location root  ->  registry value names to write.
# The GUIDs are Windows' KNOWNFOLDERID values; Wine accepts both spellings.
SHELL_FOLDERS: dict[str, tuple[str, ...]] = {
    "Documents": ("Personal", "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"),
    "Saved Games": ("{4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4}",),
    "AppData/Roaming": ("AppData", "{3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}"),
    "AppData/Local": ("Local AppData",
                      "{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}"),
    "AppData/LocalLow": ("{A520A1A4-1780-4FF6-BD18-167343C5AF16}",),
    "Downloads": ("{374DE290-123F-4565-9164-39C4925E467B}",),
    "Desktop": ("Desktop", "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"),
    "Pictures": ("My Pictures", "{33E28130-4E1E-4676-835A-98395C3BC3BB}"),
    "Music": ("My Music", "{4BD8D571-6D19-48D3-BE97-422220080E43}"),
    "Videos": ("My Video", "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}"),
}

# Longest first, so "AppData/Roaming" wins over a hypothetical "AppData".
_ROOTS_BY_DEPTH = sorted(SHELL_FOLDERS, key=lambda s: -s.count("/"))


def shell_folder_root(win_path: str) -> str | None:
    """Which shell folder does this storage location live in?

    "Documents/My Games/Foo" -> "Documents";  "AppData/Local/Foo" ->
    "AppData/Local". Returns None for paths outside any known shell folder --
    those can only be redirected by symlink.
    """
    norm = win_path.replace("\\", "/").strip("/")
    low = norm.lower()
    for root in _ROOTS_BY_DEPTH:
        if low == root.lower() or low.startswith(root.lower() + "/"):
            return root
    return None


def windows_path(unix_path: str | Path) -> str:
    """/home/you/Games/X -> Z:\\home\\you\\Games\\X (Wine maps Z: to /)."""
    return "Z:" + str(Path(unix_path).absolute()).replace("/", "\\")


def user_reg(prefix_path: str | Path) -> Path:
    return Path(prefix_path) / "user.reg"


def prefix_in_use(prefix_path: str | Path) -> bool:
    """Is a process running against this prefix right now?

    Scans our own processes' environment for WINEPREFIX. Cheap and good
    enough: a prefix is always used by processes of the same user.
    """
    target = os.path.realpath(str(prefix_path))
    try:
        pids = [p for p in Path("/proc").iterdir() if p.name.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            environ = (pid / "environ").read_bytes().decode(
                "utf-8", errors="ignore")
        except OSError:
            continue  # not ours / already gone
        for item in environ.split("\0"):
            if not item.startswith("WINEPREFIX="):
                continue
            value = item.partition("=")[2]
            if value and os.path.realpath(value) == target:
                return True
    return False


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _unescape(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")


def _section_bounds(lines: list[str], key: str) -> tuple[int, int] | None:
    """(header index, end index) of a registry section, or None."""
    header = "[" + _escape(key) + "]"
    for i, line in enumerate(lines):
        if not line.startswith(header):
            continue
        end = len(lines)
        for j in range(i + 1, len(lines)):
            if lines[j].startswith("["):
                end = j
                break
        return i, end
    return None


def get_value(prefix_path: str | Path, key: str, name: str) -> str | None:
    path = user_reg(prefix_path)
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return None
    bounds = _section_bounds(lines, key)
    if not bounds:
        return None
    pattern = re.compile(r'^"' + re.escape(_escape(name)) + r'"="(.*)"$')
    for line in lines[bounds[0] + 1:bounds[1]]:
        m = pattern.match(line.strip())
        if m:
            return _unescape(m.group(1))
    return None


def set_values(prefix_path: str | Path, key: str,
               values: dict[str, str | None]) -> bool:
    """Set (or remove, with None) string values in one registry section.

    Returns True if the file was changed. Creates the section if missing.
    """
    path = user_reg(prefix_path)
    if not path.is_file():
        return False
    try:
        original = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    lines = original.splitlines()

    bounds = _section_bounds(lines, key)
    if bounds is None:
        block = [f"[{_escape(key)}] {int(time.time())}"]
        block += [f'"{_escape(n)}"="{_escape(v)}"'
                  for n, v in values.items() if v is not None]
        if len(block) == 1:
            return False
        new_lines = lines + [""] + block
    else:
        start, end = bounds
        block = lines[start + 1:end]
        for name, value in values.items():
            prefix_str = '"' + _escape(name) + '"='
            idx = next((i for i, ln in enumerate(block)
                        if ln.startswith(prefix_str)), None)
            if value is None:
                if idx is not None:
                    block.pop(idx)
            elif idx is not None:
                block[idx] = f'{prefix_str}"{_escape(value)}"'
            else:
                # after the section's #time= comment, if present
                pos = 1 if block and block[0].startswith("#time=") else 0
                block.insert(pos, f'{prefix_str}"{_escape(value)}"')
        new_lines = lines[:start + 1] + block + lines[end:]

    text = "\n".join(new_lines) + "\n"
    if text == original:
        return False
    tmp = path.with_suffix(".reg.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return False
    return True


def set_shell_folder(prefix_path: str | Path, root: str,
                     target: str | Path | None) -> bool:
    """Point one shell folder at a Linux path (None restores nothing -- see
    redirect.py, which keeps the previous value to restore)."""
    names = SHELL_FOLDERS.get(root)
    if not names:
        return False
    value = windows_path(target) if target is not None else None
    changed = False
    for key in (SHELL_FOLDERS_KEY, USER_SHELL_FOLDERS_KEY):
        if set_values(prefix_path, key, {n: value for n in names}):
            changed = True
    return changed


def get_shell_folder(prefix_path: str | Path, root: str) -> str | None:
    names = SHELL_FOLDERS.get(root) or ()
    for name in names:
        value = get_value(prefix_path, SHELL_FOLDERS_KEY, name)
        if value:
            return value
    return None
