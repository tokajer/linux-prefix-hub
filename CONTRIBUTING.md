# Contributing

Short conventions so the code stays coherent.

## Structural rules

- **New source (launcher) = new adapter** in `src/linux_prefix_hub/adapters/`,
  plus its name in `base.SOURCES`. The core stays untouched. An adapter does
  three things: discovery, installing the hook, providing context.
- **Persistent paths** are defined in `core/paths.py`, never inline.
- **User decisions** (`redirected`, `redirect_target`, `managed`, …) must
  survive a rescan → add them to `USER_FIELDS` / `LOCATION_USER_FIELDS` in
  `core/db.py`.
- **Separate logic from presentation** (see `gui/welcome.py`), so the future
  GTK UI can reuse the same logic.

## Never reformat a foreign config

We write into Lutris YAML, Steam VDF and Heroic JSON. Edit the lines that are
ours, keep a `.bak`, leave comments and ordering alone. Our YAML reader is
read-only on purpose.

## It should feel like Windows (product principle)

No Wine/prefix/Proton vocabulary in user-visible text. The user sees games,
storage locations, "connect" and "play". The plumbing stays invisible —
internal names and comments can be as technical as they like.

## Language

- User-visible strings are **English in the code**, wrapped in `_()`.
- Translations live in `src/linux_prefix_hub/locales/<code>.json`, keyed by the
  English source string, with identical `{placeholders}`.
- Never put a non-English string in the code itself.

## Code style

- Line length 79 (`ruff check src tests` enforces it; rules are pinned in
  `pyproject.toml`).
- `from __future__ import annotations` at the top, type annotations welcome.
- Defensive against broken foreign files (VDF/YAML/JSON): one bad manifest must
  never take the whole discovery down — catch, skip, continue.
- Lazy imports inside `__main__.py` branches (fast start per mode).
- The launch path (`--wrapper`, `--hook`) must never be able to stop a game
  from starting.

## Tests

- Test against **fake environments** under `tmp_path`; no real Steam, no
  network. `tests/conftest.py` already redirects `HOME`.
- New adapter: at minimum discovery + hook injection, including that the user's
  own settings survive.
- `pytest` green before committing; CI runs it with and without the optional
  extras.

## VERIFY-ON-DEVICE

Anything needing real Steam/desktop/hardware is marked `VERIFY-ON-DEVICE` in
the code and collected in the README. If you add something like that, mark it
the same way — it keeps honest track of what is still unproven.

## Commits

Short, imperative, with an area:
`steam: write launch options into localconfig`,
`redirect: hybrid registry + symlink`,
`docs: update roadmap`.
