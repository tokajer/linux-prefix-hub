"""Minimaler Parser fuer Valves KeyValues-Format (.acf / text-.vdf).

Bewusst klein gehalten: appmanifest_*.acf und libraryfolders.vdf sind
text-basiertes KeyValues. Fuer den vollen Funktionsumfang (inkl. binaerer
VDFs wie manche localconfig-Varianten) waere das 'vdf'-PyPI-Paket besser --
das setzen wir in pyproject als Dependency, dieser Parser ist der
dependency-freie Fallback fuer die reinen Text-Manifeste.

VERIFY-ON-DEVICE: Gegen echte appmanifest_*.acf und libraryfolders.vdf auf
einem System mit mehreren Steam-Libraries testen. Valve aendert das Layout
selten, aber Verschachtelung/Escapes solltest du an echten Dateien pruefen.
"""
from __future__ import annotations

from typing import Any


def loads(text: str) -> dict[str, Any]:
    """Parst KeyValues-Text in verschachtelte dicts."""
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

    # Top-Level ist meist { "AppState" { ... } }
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
            # Kommentar bis Zeilenende
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == '"':
            i += 1
            start = i
            buf = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    buf.append({"n": "\n", "t": "\t", "\\": "\\",
                                '"': '"'}.get(nxt, nxt))
                    i += 2
                    continue
                buf.append(text[i])
                i += 1
            i += 1  # schliessendes "
            tokens.append("".join(buf))
            continue
        if c in "{}":
            tokens.append(c)
            i += 1
            continue
        # unquoted token (selten in acf, aber sicherheitshalber)
        start = i
        while i < n and text[i] not in ' \t\r\n"{}':
            i += 1
        tokens.append(text[start:i])
    return tokens
