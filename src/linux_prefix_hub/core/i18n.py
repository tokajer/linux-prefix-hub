"""Tiny, dependency-free translation layer.

Rules of the house:
  - Source strings are **English**. English needs no catalog and can never be
    "missing" -- a translation is a bonus, never a requirement.
  - The UI language is German when the desktop locale is German, English
    otherwise. `language` in config.json ("auto" | "en" | "de") overrides it.
  - Catalogs are plain JSON: locales/<lang>.json = {"english": "translation"}.
    No gettext/.mo compilation step, so the AppImage build stays trivial.

Usage:
    from ..core.i18n import _
    print(_("{n} game(s) found:", n=len(games)))

Placeholders are `str.format` style and are kept identical in translations;
if a translation has a broken placeholder we fall back to English instead of
crashing (a bad translation must never take the app down).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
DEFAULT_LANGUAGE = "en"

# Env var wins over everything -- handy for testing and for `--lang`.
ENV_OVERRIDE = "LPH_LANG"
# Standard POSIX locale variables, in the order the C library honours them.
LOCALE_ENV_VARS = ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")

_language: str | None = None
_catalog: dict[str, str] | None = None


def available_languages() -> list[str]:
    """Languages we ship a catalog for, plus English (always available)."""
    langs = {DEFAULT_LANGUAGE}
    if LOCALES_DIR.is_dir():
        langs |= {p.stem for p in LOCALES_DIR.glob("*.json")}
    return sorted(langs)


def _lang_from_env() -> str | None:
    for var in LOCALE_ENV_VARS:
        val = os.environ.get(var)
        if not val or val in ("C", "POSIX"):
            continue
        # "de_AT.utf8:en_US" -> "de"
        primary = val.split(":")[0]
        tag = primary.split(".")[0].split("_")[0].split("-")[0].lower()
        if tag:
            return tag
    return None


def _lang_from_config() -> str | None:
    try:
        from . import db  # local import: db imports paths, not i18n
        val = db.load_config().get("language")
    except Exception:
        return None
    if not val or val == "auto":
        return None
    return str(val).lower()


def detect_language() -> str:
    """Resolve the UI language. Falls back to English."""
    for candidate in (os.environ.get(ENV_OVERRIDE),
                      _lang_from_config(),
                      _lang_from_env()):
        if not candidate:
            continue
        tag = candidate.split(".")[0].split("_")[0].split("-")[0].lower()
        if tag in available_languages():
            return tag
    return DEFAULT_LANGUAGE


def _load_catalog(lang: str) -> dict[str, str]:
    if lang == DEFAULT_LANGUAGE:
        return {}
    path = LOCALES_DIR / f"{lang}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


def set_language(lang: str | None) -> str:
    """Force a language (`None` = re-detect). Returns the active language."""
    global _language, _catalog
    _language = lang if lang else detect_language()
    _catalog = _load_catalog(_language)
    return _language


def language() -> str:
    if _language is None:
        set_language(None)
    return _language or DEFAULT_LANGUAGE


def translate(message: str, **kwargs: Any) -> str:
    """Translate + format. Unknown strings pass through in English."""
    if _catalog is None:
        set_language(None)
    text = (_catalog or {}).get(message, message)
    if not kwargs:
        return text
    try:
        return text.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        # Broken placeholder in a translation -> use the English source.
        try:
            return message.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return message


# Short alias, the convention every gettext-shaped codebase uses.
_ = translate


def plural(n: int, singular: str, many: str, **kwargs: Any) -> str:
    """Two-form plural. Enough for en/de; extend if a language needs more."""
    return translate(singular if n == 1 else many, n=n, **kwargs)
