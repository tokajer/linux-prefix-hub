# CLAUDE.md — orientation map

Read this first; it is written so you rarely have to open more than the two or
three files a task actually touches.

**What this is:** finds Windows games on Linux (Steam/Lutris/Heroic), learns
**where each one saves** by diffing the prefix around a launch, and can move
those saves into `~/Games/<Game>/` with a registry entry + symlink. Users never
see the words prefix, Wine or `steamuser`.

## Commands

```bash
.venv/bin/python -m pytest -q            # 253 tests, ~1s, no real Steam needed
.venv/bin/ruff check src tests           # lint (config pinned in pyproject)
PYTHONPATH=src python -m linux_prefix_hub --scan
HOME=/tmp/x PYTHONPATH=src python -m linux_prefix_hub   # setup flow, safely
PYTHONPATH=src /usr/bin/python3 -m linux_prefix_hub --gui   # needs system gi
./packaging/build-velopack.sh            # release build; needs vpk (.NET) + cc
./packaging/build-appimage.sh            # local test build, cannot self-update
```

Never run the app with the real `HOME` while testing. **Redirecting only
`XDG_*` is not enough:** `paths.LOCAL_BIN` and `DEFAULT_REDIRECT_ROOT` key off
`Path.home()`, and the AppImage's AppRun self-heals (`--integrate`) on every
non-shim start — so a single `--scan` through the AppImage rewrites the real
`~/.local/bin` shims to point wherever that AppImage happens to live.

The GUI needs system PyGObject, which the venv does not have. Run it with
`/usr/bin/python3` as above, or let the AppImage hand over by itself
(`__main__._reexec_gui`, which must strip `PYTHONHOME`).

## Layout — one line per file

| File | Responsibility |
|---|---|
| `__main__.py` | CLI. `--wrapper/--hook/--daemon` dispatch **before** argparse (launch path stays cheap), rest via argparse |
| `__init__.py` | `__version__`, **derived** — generated `_version.py` (build) > install metadata > `0.0.0+dev`. The release tag is the only version there is |
| `core/paths.py` | Every persistent path. Constants resolved at **import** time (tests reload it) |
| `core/i18n.py` | `_()`; English source strings, `locales/de.json` catalog, `LPH_LANG` > config > `LANG` |
| `core/db.py` | `config.json` (JSON) + `prefixes.db` (**SQLite**, three writers). `upsert_prefix` merges and preserves `USER_FIELDS`/`LOCATION_USER_FIELDS`; `prune_locations` drops what a filter should have caught, never a user's. Columns for what we query, `extra` JSON for the rest; a pre-SQLite `prefixes.json` is folded in once. Anything a game can own **before it has a prefix** (`pending_redirects`, `hidden_games`) lives in the config, keyed by `game_key` = `source:app_id` |
| `core/snapshot.py` | mtime snapshot → diff → storage locations, in **two** spaces (prefix + install folder); the `IGNORE_*` filters (shader caches, logs, …) plus the user's own; pending snapshots for the 2-process hook flow |
| `core/pcgw.py` | PCGamingWiki lookup: article → `{{Game data/…}}` → our locations. Optional, cached, **never on the launch path** (the wrapper reads the cache only) |
| `core/desktop.py` | Hand a folder to the user's file manager. `xdg-open` first |
| `core/wrapper.py` | The launch hook, both shapes (wrap and pre/post). Must never break a launch |
| `core/registry.py` | Surgical `user.reg` editing, `SHELL_FOLDERS` map, `prefix_in_use()` |
| `core/redirect.py` | Hybrid redirect: move data → symlink → registry → DB flags. `reapply()` self-heals. `cloud_warning()` names the other writer on that folder before the move |
| `core/integrate.py` | AppImage relocation, the three shims, systemd unit, desktop entry. Idempotent |
| `core/updater.py` | Velopack: `check`/`download`/`apply`. `app_hook()` only in the AppImage. GearLever wins if present |
| `core/vdf.py` | Valve KeyValues read **and write** (localconfig round-trip) |
| `core/yamlite.py` | Lutris-shaped YAML subset; uses PyYAML when installed. **Read only** |
| `adapters/base.py` | Adapter contract, `iter_games()`, `visible_games()` (drops what the user hid — only the two places that draw a list use it), `context_from_env()`, `user_dir_for()` |
| `adapters/steam.py` | Multi-library discovery; hook = launch options (needs Steam closed, else manual); `cloud_paths()` = Auto-Cloud from `remotecache.vdf` (UFS does not count — those files never enter the prefix) |
| `adapters/lutris.py` | pga.db + YAML discovery; hook = `prelaunch_command`/`postexit_command` |
| `adapters/heroic.py` | GamesConfig JSON discovery; hook = `wrapperOptions` (wrap shape, like Steam) |
| `adapters/generic.py` | Hand-rolled setups: discovery by shape alone, path = id, no config to hook — the user gets a command. Runs **last**, skips what the others claim |
| `daemon/watcher.py` | inotify on steamapps + periodic rescan; new-game and update notifications |
| `gui/welcome.py` | Terminal setup flow. Logic split from presentation, shared with the GTK UI |
| `gui/app.py` | GTK4/libadwaita window: game list grouped per launcher, connect switch, lookup button, hide button, game-folder row, move-home switch; the header's eye toggle shows hidden games again. Presentation only |
| `gui/tray.py` | Tray icon spoken straight onto the session bus (StatusNotifierItem + dbusmenu). **No GTK in it** — AppIndicator is GTK3-linked and would abort a GTK4 process. Degrades to `live == False` |
| `gui/tasks.py` | One function: run blocking work off the GTK main loop, land the result via `idle_add` |

## Rules that are easy to break

1. **A rescan must never overwrite a user decision.** New user-controlled
   field → add it to `USER_FIELDS`/`LOCATION_USER_FIELDS` in `core/db.py`.
   A location's identity is `db.location_key(loc)` = `(where, win_path)` —
   the prefix and the install folder are two namespaces, not one.
2. **Never round-trip someone else's config through a parser.** Lutris YAML is
   edited line by line, Steam VDF and Heroic JSON keep a `.bak`. Reformatting
   a user's launcher config is a bug.
3. **The wrapper may not break a launch.** Anything we do around the game is
   wrapped in try/except and the game's exit code is passed through — *and*
   the game gets the environment it would have had without us
   (`wrapper.game_env`). The AppImage's `PYTHONHOME`/`PYTHONPATH` leaking into
   the child kills Proton, which is a Python program itself. **No network on
   that path either** — `pcgw` is asked only via its on-disk cache.
4. **Discovery is defensive.** One broken manifest/config skips that entry, it
   never aborts the scan (`base.iter_games` isolates whole adapters too).
   The same care applies to **every process we start**: the game
   (`wrapper.game_env`) and the file manager (`desktop._child_env`) get the
   environment they would have had without us. A file manager outlives us and
   passes what we leak to everything the user opens from it — that is how a
   stray `LPH_GUI_REEXEC` once stopped the window from ever appearing.
5. **User-visible strings go through `_()` in English**, then into
   `locales/de.json` with identical `{placeholders}` (a test enforces that).
6. **No Wine/prefix/Proton vocabulary in user-visible text** — "game folder",
   "connect", "moved to". Internal names and comments stay technical.
   **And not "saves"/"Spielstände" for the whole either:** what we detect and
   move is settings, logs and caches as much as save games. The user-facing
   words are "game data"/"Spieldaten" for the content and "storage
   location"/"Speicherort" for a place. `location["type"]` still says `saves`
   — that is the type of one location, not the name of the thing.
7. **Registry edits need the prefix idle** (`registry.prefix_in_use`), because
   Wine flushes its in-memory registry over `user.reg` on shutdown. A prefix
   *appearing* is therefore the worst moment to write into it, not the best:
   the game is booting. `redirect.apply_pending` files the game then and
   moves it on a later pass — an empty return means "next pass", never
   "give up".
8. **Nothing may close into a tray that is not there.** `gui/tray.py` answers
   `live`; if it is False the window keeps GTK's own behaviour and closing
   ends the app. An app the user can neither see nor quit is worse than one
   that exits when closed. `live` is asked again on every close, because a
   desktop shell restart takes the tray host away.
9. **Two copies of a file is a question, and deleting one is not an answer.**
   `redirect._conflicts` compares the whole tree before anything moves and
   stops the move with both versions intact. Steam's Auto-Cloud is the reason
   this happens (`redirect.cloud_warning` says so in advance), a Proton update
   that ate our symlink is how. Merging "never overwrites" is only half of it
   — what you do with what the merge skipped is the other half.
10. Line length 79. `from __future__ import annotations`. Lazy imports inside
    CLI branches.
11. Things that need real hardware are marked `VERIFY-ON-DEVICE` in the code
    and collected in the README.

## Adding a launcher

New file in `adapters/`, add its name to `base.SOURCES` **before `generic`**
(it claims every game folder nobody else does, so it stays last), implement
`SOURCE`, `iter_games`, `context_from_env`, `connect`, `disconnect`. Nothing in
`core/` should need to change — if it does, that is a design smell worth
raising.

## State on disk

`~/.config/linux-prefix-hub/{config.json,prefixes.db,known_games.json,snapshots/,pcgamingwiki/}`
(`prefixes.json` is the pre-SQLite file: folded in once, then kept as a backup
and never written again — the `migrated_from_json` flag in the DB's `meta`
table, not the file's absence, is what says the import happened),
(`config.json` keys: `install_dir`, `redirect_root`, `language`,
`online_lookup`, `game_folders`, `ignore_paths`, `setup_done`,
`background_tray`, `pending_redirects`, `hidden_games`,
`update_check`/`update_notified`),
`~/.local/share/linux-prefix-hub/LinuxPrefixHub.AppImage`,
`~/.local/bin/linux-prefix-hub-{wrapper,hook,daemon}`,
`~/.config/systemd/user/linux-prefix-hub-watcher.service`.

## Where the deeper docs are

`docs/ARCHITECTURE.md` (the *why*), `docs/MODULES.md` (per-file detail),
`docs/ROADMAP.md` (what is next), `docs/DEVELOPING.md` (setup/debugging),
`docs/RELEASING.md` (tag → AppImage → auto-update).
