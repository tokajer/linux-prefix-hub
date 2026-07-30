# CLAUDE.md — orientation map

Read this first; it is written so you rarely have to open more than the two or
three files a task actually touches.

**What this is:** finds Windows games on Linux (Steam/Lutris/Heroic), learns
**where each one saves** by diffing the prefix around a launch, and can move
those saves into `~/Games/<Game>/` with a registry entry + symlink. Users never
see the words prefix, Wine or `steamuser`.

## Commands

```bash
.venv/bin/python -m pytest -q            # 66 tests, ~0.2s, no real Steam needed
.venv/bin/ruff check src tests           # lint (config pinned in pyproject)
PYTHONPATH=src python -m linux_prefix_hub --scan
HOME=/tmp/x PYTHONPATH=src python -m linux_prefix_hub   # setup flow, safely
./packaging/build-appimage.sh            # needs network; zsync for release-grade
```

Never run the app with the real `HOME` while testing setup: it writes shims,
a systemd unit and a desktop entry.

## Layout — one line per file

| File | Responsibility |
|---|---|
| `__main__.py` | CLI. `--wrapper/--hook/--daemon` dispatch **before** argparse (launch path stays cheap), rest via argparse |
| `core/paths.py` | Every persistent path. Constants resolved at **import** time (tests reload it) |
| `core/i18n.py` | `_()`; English source strings, `locales/de.json` catalog, `LPH_LANG` > config > `LANG` |
| `core/db.py` | `prefixes.json`. `upsert_prefix` merges and preserves `USER_FIELDS`/`LOCATION_USER_FIELDS` |
| `core/snapshot.py` | mtime snapshot → diff → storage locations; pending snapshots for the 2-process hook flow |
| `core/wrapper.py` | The launch hook, both shapes (wrap and pre/post). Must never break a launch |
| `core/registry.py` | Surgical `user.reg` editing, `SHELL_FOLDERS` map, `prefix_in_use()` |
| `core/redirect.py` | Hybrid redirect: move data → symlink → registry → DB flags. `reapply()` self-heals |
| `core/integrate.py` | AppImage relocation, the three shims, systemd unit, desktop entry. Idempotent |
| `core/updater.py` | GitHub releases → check/download/verify SHA256/swap. GearLever wins if present |
| `core/vdf.py` | Valve KeyValues read **and write** (localconfig round-trip) |
| `core/yamlite.py` | Lutris-shaped YAML subset; uses PyYAML when installed. **Read only** |
| `adapters/base.py` | Adapter contract, `iter_games()`, `context_from_env()`, `user_dir_for()` |
| `adapters/steam.py` | Multi-library discovery; hook = launch options (needs Steam closed, else manual) |
| `adapters/lutris.py` | pga.db + YAML discovery; hook = `prelaunch_command`/`postexit_command` |
| `adapters/heroic.py` | GamesConfig JSON discovery; hook = `wrapperOptions` (wrap shape, like Steam) |
| `daemon/watcher.py` | inotify on steamapps + periodic rescan; new-game and update notifications |
| `gui/welcome.py` | Terminal setup flow. Logic split from presentation for the future GTK UI |

## Rules that are easy to break

1. **A rescan must never overwrite a user decision.** New user-controlled
   field → add it to `USER_FIELDS`/`LOCATION_USER_FIELDS` in `core/db.py`.
2. **Never round-trip someone else's config through a parser.** Lutris YAML is
   edited line by line, Steam VDF and Heroic JSON keep a `.bak`. Reformatting
   a user's launcher config is a bug.
3. **The wrapper may not break a launch.** Anything we do around the game is
   wrapped in try/except and the game's exit code is passed through.
4. **Discovery is defensive.** One broken manifest/config skips that entry, it
   never aborts the scan (`base.iter_games` isolates whole adapters too).
5. **User-visible strings go through `_()` in English**, then into
   `locales/de.json` with identical `{placeholders}` (a test enforces that).
6. **No Wine/prefix/Proton vocabulary in user-visible text** — "game folder",
   "connect", "moved to". Internal names and comments stay technical.
7. **Registry edits need the prefix idle** (`registry.prefix_in_use`), because
   Wine flushes its in-memory registry over `user.reg` on shutdown.
8. Line length 79. `from __future__ import annotations`. Lazy imports inside
   CLI branches.
9. Things that need real hardware are marked `VERIFY-ON-DEVICE` in the code
   and collected in the README.

## Adding a launcher

New file in `adapters/`, add its name to `base.SOURCES`, implement `SOURCE`,
`iter_games`, `context_from_env`, `connect`, `disconnect`. Nothing in `core/`
should need to change — if it does, that is a design smell worth raising.

## State on disk

`~/.config/linux-prefix-hub/{config.json,prefixes.json,known_games.json,snapshots/}`,
`~/.local/share/linux-prefix-hub/LinuxPrefixHub.AppImage`,
`~/.local/bin/linux-prefix-hub-{wrapper,hook,daemon}`,
`~/.config/systemd/user/linux-prefix-hub-watcher.service`.

## Where the deeper docs are

`docs/ARCHITECTURE.md` (the *why*), `docs/MODULES.md` (per-file detail),
`docs/ROADMAP.md` (what is next), `docs/DEVELOPING.md` (setup/debugging),
`docs/RELEASING.md` (tag → AppImage → auto-update).
