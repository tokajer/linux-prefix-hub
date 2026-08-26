# CLAUDE.md — orientation map

Read this first; it is written so you rarely have to open more than the two or
three files a task actually touches.

**What this is:** finds Windows games on Linux (Steam/Lutris/Heroic), learns
**where each one saves** by diffing the prefix around a launch, and can move
those saves into `~/Games/<Game>/` with a registry entry + symlink. Users never
see the words prefix, Wine or `steamuser`.



## Approach
- Read existing files before writing. Don't re-read unless changed.
- Thorough in reasoning, concise in output.
- Skip files over 100KB unless required.
- No sycophantic openers or closing fluff.
- No emojis or em-dashes.
- Do not guess APIs, versions, flags, commit SHAs, or package names. Verify by reading code or docs before asserting.

## Output
- Return code first. Explanation after, only if non-obvious.
- No inline prose. Use comments sparingly - only where logic is unclear.
- No boilerplate unless explicitly requested.

## Code Rules
- Simplest working solution. No over-engineering.
- No abstractions for single-use operations.
- No speculative features or "you might also want..."
- Read the file before modifying it. Never edit blind.
- No docstrings or type annotations on code not being changed.
- No error handling for scenarios that cannot happen.
- Three similar lines is better than a premature abstraction.
- Do not commit anything

## Review Rules
- State the bug. Show the fix. Stop.
- No suggestions beyond the scope of the review.
- No compliments on the code before or after the review.

## Debugging Rules
- Never speculate about a bug without reading the relevant code first.
- State what you found, where, and the fix. One pass.
- If cause is unclear: say so. Do not guess.

## Simple Formatting
- No em dashes, smart quotes, or decorative Unicode symbols.
- Plain hyphens and straight quotes only.
- Natural language characters (accented letters, CJK, etc.) are fine when the content requires them.
- Code output must be copy-paste safe.


## Commands

```bash
.venv/bin/python -m pytest -q            # 349 tests, ~1s, no real Steam needed
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
| `core/pcgw.py` | PCGamingWiki lookup: article → `{{Game data/…}}` → our locations. Optional, cached, **never on the launch path** (the wrapper reads the cache only). It **suggests**: `lookup()` writes nothing, `confirm()` is the user's yes, `on_disk()` drops what is not there |
| `core/gameopts.py` | Extra options per game. `own_folder()`: a folder `newprefix` made needs no build at all -- we start that game, so the profile is just its environment. `launcher_for()`: Lutris and Heroic keep an environment per game themselves, so the profile goes into their config (`set_env`) and needs no build either -- what we put there is remembered in the profile (`applied`/`restore`), because a variable in their config cannot be told from one the user typed. A hand-installed game folder gets the profile through a build of its own, which the user points their launcher at (`newprefix.make_private_for`). Steam is the exception that has both halves: `set_compat_tool` points Steam at the copy itself. The profile (switches + free `KEY=value`, launcher-neutral) and the private compatibility build that carries them into the container. **Hardlink copy** -- `_replace` unlinks before writing, or the write lands in the build it was copied from. Refuses a base with no `default_pfx` — copying a half-installed build faithfully gets you a faithful copy of something broken. Copies are named after the **game**, found again by the `key` in their marker; `outdated()` compares the base's `version` file, because a "latest" name never changes while its contents do. Never on the launch path |
| `core/newprefix.py` | The one place that **makes** a game folder instead of finding one. `<root>/<Name>/pfx` for both layers -- a compatibility build (`proton run`, `STEAM_COMPAT_DATA_PATH`) and the system's `wine` (`WINEPREFIX`) -- so `adapters/generic` discovers it and nothing else needs a second code path. Which layer built it is in the folder's own marker, not in our config: the folder is what gets moved and copied. **A folder's name must contain a digit** (`_numbered`): a build's `protonfixes` reads the app id out of `STEAM_COMPAT_DATA_PATH` with a digit regex and dies on a path without one -- invisible to us (we set `SteamAppId`), fatal for a launcher of the game's own. **Starting the game is the only place these folders are ever observed** -- no launcher means no hook, so `launch()` goes through `wrapper.observed()` and waits for the folder to go quiet first (a game's own launcher exits before the game does). `delete()` needs that marker before it removes anything, and leaves moved data where the user put it -- the game is what goes here, so fetching saves back into a doomed folder would lose them. Never on the launch path |
| `core/desktop.py` | Hand a folder to the user's file manager. `xdg-open` first |
| `core/wrapper.py` | The launch hook, both shapes (wrap and pre/post). Must never break a launch |
| `core/registry.py` | Surgical `user.reg` editing, `SHELL_FOLDERS` map, `prefix_in_use()` |
| `core/redirect.py` | Hybrid redirect: move data → symlink → registry → DB flags. `reapply()` self-heals. `cloud_warning()` names the other writer on that folder before the move. `relocate()` moves an already moved folder without walking it back through the prefix; `stale_targets()` finds only what an older default put there, never a target the user named |
| `core/integrate.py` | AppImage relocation, the three shims, systemd unit, desktop entry (named after `paths.APP_ID`, see rule 15), icon. Idempotent |
| `core/updater.py` | Velopack: `check`, then `download()` / `finish()` — two halves because an update can only be applied once **this process is gone**. `app_hook()` only in the AppImage. GearLever wins if present |
| `core/uninstall.py` | Remove the app: revert every moved folder, disconnect every hook, hand every game with extra options back to Steam, *then* delete. A failed step stops the whole thing |
| `core/vdf.py` | Valve KeyValues read **and write** (localconfig round-trip) |
| `core/yamlite.py` | Lutris-shaped YAML subset; uses PyYAML when installed. **Read only** |
| `adapters/base.py` | Adapter contract, `iter_games()`, `visible_games()` (drops what the user hid — only the two places that draw a list use it), `context_from_env()`, `user_dir_for()` |
| `adapters/steam.py` | Multi-library discovery; hook = launch options (needs Steam closed, else manual); `cloud_paths()` = Auto-Cloud from `remotecache.vdf` (UFS does not count — those files never enter the prefix); `set_compat_tool()` = which compatibility build a game uses, in `config.vdf`, same Steam-closed rule and a `.bak` |
| `adapters/lutris.py` | pga.db + YAML discovery; hook = `prelaunch_command`/`postexit_command`; `set_env()` = the game's own `system: env:` block, edited line by line like the hook keys |
| `adapters/heroic.py` | GamesConfig JSON discovery; hook = `wrapperOptions` (wrap shape, like Steam); `set_env()` = `enviromentOptions` (their spelling, their key) |
| `adapters/generic.py` | Hand-rolled setups: discovery by shape alone, path = id, no config to hook — the user gets a command. Runs **last**, skips what the others claim |
| `daemon/watcher.py` | inotify on steamapps + periodic rescan; new-game and update notifications |
| `gui/welcome.py` | Terminal setup flow. Logic split from presentation, shared with the GTK UI |
| `gui/app.py` | GTK4/libadwaita window: game list grouped per launcher, connect switch, lookup button, hide button, game-folder row, move-home switch; an extra-options row per Steam game with its own dialog; the header's eye toggle shows hidden games again; the settings dialog holds the data folder, the two switches and removing the app. Presentation only |
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
12. **A PCGamingWiki lookup suggests; the user decides, and the disk has the
    last word.** `pcgw.lookup()` writes nothing but its cache. Only
    `pcgw.confirm()` — behind the prompt in `--lookup` and the Add button in
    the window — turns a suggestion into something we keep, and only
    `pcgw.on_disk()` decides which of them are real. Both gates are asked
    again wherever the answer is used (`pcgw.cached_locations`, so the
    wrapper and `redirect.apply_pending` too), never once when it was given:
    a yes is permission, not proof. A path the wiki names and the disk does
    not have is never written, created or redirected — otherwise a storage
    location is invented out of an article and the first thing the user does
    with it is move data into it.
13. **An update cannot be installed while we run, and nothing may end the
    process behind the caller's back.** Installing means replacing the file
    we are executing, so Velopack's helper waits for our pid — which is why
    `updater.download()` and `updater.finish()` are two calls and the *caller*
    does the exiting. The SDK's `apply_updates_and_restart` is
    `std::process::exit(0)` on success and an error return on failure; from a
    GTK worker thread that is the window vanishing mid-click. Nor can we
    restart ourselves: anything started before we exit starts the *old* build
    and collides with our own single-instance lock.
14. **Removing the app is the redirect in reverse, and it comes first.** A
    moved folder and a launch hook both live in someone else's config, so
    `uninstall.run()` reverts and disconnects before it deletes anything, and
    a step that fails stops it there — each stage on its own leaves a machine
    that works. Cleanup is `rmdir` only, never `rmtree` on anything that
    could hold game data.
15. **A hardlink copy is one file with two names, and writing to it writes
    into the original.** `core/gameopts.py` copies an installed
    compatibility build with `os.link` so a per-game copy costs nothing --
    which means `<copy>/user_settings.py` *is* `<GE-Proton>/user_settings.py`
    until somebody breaks the link. Opening it for writing truncates the
    build the user installed. Everything this module writes into a copy goes
    through `_replace`, which unlinks first, and nothing it deletes is
    touched without the `MARKER` file that says the directory is ours -- the
    same directory holds builds the user installed themselves.
16. **`paths.APP_ID` is one string in three places, and they have to agree.**
    The window carries it (`gui.app.main` sets it as the *program* name —
    that, not the application id, is what GTK sends as the Wayland `app_id` /
    X11 `WM_CLASS`), the desktop entry is named after it, and the icon is
    installed under it. The desktop matches an open window to an entry by
    that name and takes the icon from there; disagree on any one of them and
    the task bar draws the interpreter's icon next to our window.

## Adding a launcher

New file in `adapters/`, add its name to `base.SOURCES` **before `generic`**
(it claims every game folder nobody else does, so it stays last), implement
`SOURCE`, `iter_games`, `context_from_env`, `connect`, `disconnect`. Two more
are optional and are asked for by name, never by source: `cloud_paths` (this
launcher syncs a folder inside the prefix) and `set_env` (this launcher keeps
an environment per game, so the extra options go there instead of into a
build of ours). Nothing in `core/` should need to change — if it does, that is a design smell worth
raising.

## State on disk

`~/.config/linux-prefix-hub/{config.json,prefixes.db,known_games.json,snapshots/,pcgamingwiki/}`
(`prefixes.json` is the pre-SQLite file: folded in once, then kept as a backup
and never written again — the `migrated_from_json` flag in the DB's `meta`
table, not the file's absence, is what says the import happened),
(`config.json` keys: `install_dir`, `redirect_root`, `language`,
`online_lookup`, `game_folders`, `ignore_paths`, `setup_done`,
`background_tray`, `pending_redirects`, `hidden_games`,
`confirmed_lookups`, `game_options`, `prefix_root`,
`update_check`/`update_notified`),
`~/Games/linux-prefix-hub/{Games,prefix}/<Game>/` (moved game data and game
folders the user made here -- one app folder inside `~/Games`, which stays
theirs; `adapters/generic.DEFAULT_ROOTS` lists the `prefix` one so a scan
finds those folders without the config),
`~/.local/share/linux-prefix-hub/LinuxPrefixHub.AppImage`,
`~/.local/bin/linux-prefix-hub-{wrapper,hook,daemon}`,
`~/.config/systemd/user/linux-prefix-hub-watcher.service`.

## Where the deeper docs are

`docs/ARCHITECTURE.md` (the *why*), `docs/MODULES.md` (per-file detail),
`docs/ROADMAP.md` (what is next), `docs/DEVELOPING.md` (setup/debugging),
`docs/RELEASING.md` (tag → AppImage → auto-update).
