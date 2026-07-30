# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Minimal parser/writer for Valve's KeyValues format (.acf / text .vdf).

Deliberately small: appmanifest_*.acf, libraryfolders.vdf and localconfig.vdf
are text KeyValues. For the full feature set (including binary VDFs such as
shortcuts.vdf) the `vdf` PyPI package is better -- it is an optional extra
(`pip install .[full]`); this module is the dependency-free fallback.

`loads` preserves key order, and `dumps` writes Valve's own formatting, so a
parse/serialise round-trip of localconfig.vdf stays diff-friendly.

VERIFY-ON-DEVICE: test against real appmanifest_*.acf and libraryfolders.vdf
on a system with several Steam libraries. Valve rarely changes the layout, but
nesting/escaping is worth checking against real files.
"""
from __future__ import annotations

from typing import Any

_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"'}


def loads(text: str) -> dict[str, Any]:
    """Parse KeyValues text into nested dicts."""
    tokens = _tokenize(text)
    pos = 0

    def parse_block() -> dict[str, Any]:
        nonlocal pos
        obj: dict[str, Any] = {}
        while pos < len(tokens):
            tok = tokens[pos]
            if tok == "}":
                pos += 1
                return obj
            key = tok
            pos += 1
            if pos >= len(tokens):
                break
            nxt = tokens[pos]
            if nxt == "{":
                pos += 1
                obj[key] = parse_block()
            else:
                obj[key] = nxt
                pos += 1
        return obj

    # Top level is usually { "AppState" { ... } }
    result: dict[str, Any] = {}
    while pos < len(tokens):
        key = tokens[pos]
        pos += 1
        if pos < len(tokens) and tokens[pos] == "{":
            pos += 1
            result[key] = parse_block()
        elif pos < len(tokens):
            result[key] = tokens[pos]
            pos += 1
    return result


def dumps(data: dict[str, Any], indent: int = 0) -> str:
    """Serialise back to KeyValues text (tabs, Valve style)."""
    pad = "\t" * indent
    out: list[str] = []
    for key, value in data.items():
        if isinstance(value, dict):
            out.append(f'{pad}"{_escape(str(key))}"')
            out.append(f"{pad}{{")
            out.append(dumps(value, indent + 1).rstrip("\n"))
            out.append(f"{pad}}}")
        else:
            out.append(f'{pad}"{_escape(str(key))}"\t\t'
                       f'"{_escape(str(value))}"')
    return "\n".join(out) + "\n"


def _escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace('"', '\\"')
                .replace("\n", "\\n").replace("\t", "\\t"))


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":  # comment to end of line
                i += 1
            continue
        if c == '"':
            i += 1
            buf: list[str] = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    buf.append(_ESCAPES.get(text[i + 1], text[i + 1]))
                    i += 2
                    continue
                buf.append(text[i])
                i += 1
            i += 1  # closing quote
            tokens.append("".join(buf))
            continue
        if c in "{}":
            tokens.append(c)
            i += 1
            continue
        # unquoted token (rare in .acf, but be safe)
        start = i
        while i < n and text[i] not in ' \t\r\n"{}':
            i += 1
        tokens.append(text[start:i])
    return tokens
