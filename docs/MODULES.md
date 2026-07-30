# Module reference

Every file, what it does, its public functions, and what to watch out for when
building on it.

---

## `__main__.py` — entry point & mode dispatch

One entry, several modes — matches the AppImage (one artifact) and `pipx` (one
command).

| Call | Function | Who calls it |
|------|----------|--------------|
| `linux-prefix-hub` | welcome / overview | user, menu entry |
| `--wrapper CMD…` | `core.wrapper.main` | Steam, Heroic (via shim) |
| `--hook pre\|post --source S --id ID` | `core.wrapper.hook` | Lutris (via shim) |
| `--daemon` | `daemon.watcher.run` | systemd (via shim) |
| `--integrate` | `core.integrate.full_setup` | AppRun (self-heal) |
| `--scan`, `--status` | listing | user |
| `--connect`, `--disconnect` | adapter hook | user |
| `--lookup` | `core.pcgw.lookup_and_store` | user |
| `--open` | `core.desktop.open_folder` | user |
| `--redirect`, `--undo-redirect` | `core.redirect` | user |
| `--check-update`, `--update` | `core.updater` | user |
| `--lang`, `--set-language` | `core.i18n` | user |

**Building on it:** the three shim modes are matched *before* argparse and use
lazy imports, so a game launch never pays for the CLI. Keep it that way.

**Watch out:** `_reexec_gui`'s loop guard is our own **pid**, not a boolean.
`execve` keeps the pid, so the interpreter we hand the window to still sees
"already tried" — while a process that merely inherited the variable does not.
Anything that spawns a program which outlives us must strip it as well
(`core/desktop.py`).

---

## `core/paths.py` — central paths

Every fixed location in one place, XDG-conform:
`DEFAULT_INSTALL_DIR`, `CONFIG_DIR`, `LOCAL_BIN`, the three shims,
`WATCHER_UNIT`, `SNAPSHOT_DIR`, `PCGW_DIR`, `DEFAULT_REDIRECT_ROOT`
(`~/Games`).

The constants are resolved at **import** time, which is why the tests reload
this module after redirecting `HOME`.

**Building on it:** define new persistent paths here, never inline.

---

## `core/i18n.py` — translation

`translate(msg, **kw)` / `_()`, `set_language()`, `detect_language()`,
`available_languages()`.

Order of precedence: `LPH_LANG` env → `language` in config.json → `LANGUAGE`/
`LC_ALL`/`LC_MESSAGES`/`LANG` → English.

Catalogs are plain JSON (`locales/<lang>.json`) so there is no `.mo`
compilation step in the AppImage build. A missing key or a broken placeholder
falls back to the English source.

**Building on it:** new language = one JSON file, nothing else. Keep
`{placeholders}` identical — `test_core.py` enforces it.

---

## `core/db.py` — prefix DB & config

JSON in `~/.config/linux-prefix-hub/`, atomic writes (tmp + replace).

`fingerprint`, `load_config`/`save_config`/`set_config`/`install_dir`,
`extra_game_folders`/`add_game_folder`/`forget_game_folder` (the folders
`adapters/generic.py` looks in beyond its defaults),
`load_prefixes`/`save_prefixes`, `upsert_prefix`, `get_prefix`, `find_prefix`,
`resolve` (fingerprint | app id | partial name), `update_location`,
`set_managed`.

`upsert_prefix` is the important one: it merges and preserves the user-owned
flags listed in `USER_FIELDS` / `LOCATION_USER_FIELDS`.

**Building on it:** if the schema outgrows JSON, SQLite is the next step — but
keep these signatures and the rest of the code will not notice.

---

## `core/snapshot.py` — storage-location detection

Two **spaces**, named by `WHERE_PREFIX` / `WHERE_GAME`; every location carries
`where`.

- `snapshot(prefix, user_dir) -> {rel_path: mtime}` over `INTERESTING_SUBTREES`
  only, skipping `IGNORE_FRAGMENTS` (Temp, crash dumps, shader caches).
- `snapshot_game_dir(game_dir) -> {rel_path: mtime} | None` over the install
  folder, skipping `IGNORE_GAME_*` (logs, dumps, `steam_appid.txt`).
- `diff(before, after) -> [rel_path]`
- `classify_locations(changed, where, known) -> [location]` — aggregates to
  directory level and guesses `type` (saves/config/unknown). `known` are
  locations PCGamingWiki already typed (`core/pcgw.py`); they win over the
  guess, matched by `known_type` on exact path first, containment second.
- `save_pending` / `load_pending` — hands the "before" state of *both* spaces
  from the pre hook to the post hook (Lutris runs them as two processes).
  Reads a flat pre-install-folder file as a prefix-only state.

**Why the install folder at all:** Source-engine games are the classic case.
Portal 2 saves to `<install>/portal2/SAVE/<steamid>/` and touches nothing but
`AppData/Local/Temp` inside the prefix — a prefix-only diff learns exactly
nothing about it and the user sees "no folder detected" after a full session.

**Watch out:** `snapshot_game_dir` returns `None` for "not covered" (no folder,
or more than `MAX_GAME_DIR_FILES` entries — an install folder is not a prefix
and we will not stall a launch walking it) and `{}` for "covered, empty". They
are not interchangeable: a fresh install is legitimately near-empty, and
folding that into "not covered" throws away the first launch, the one with the
most to teach.

**Building on it:** for very large prefixes the `rglob` could be pre-filtered
by directory mtime.

---

## `core/pcgw.py` — what PCGamingWiki already knows

Answers "where does this game save?" without playing the game, and types the
answer properly instead of guessing from the path.

- `lookup(game, refresh=False) -> {ok, reason, page, url, locations, cached,
  message}` — the whole flow, cache first. Never raises; `message` is already
  translated, like `redirect`/`updater` results.
- `lookup_and_store(game)` — the same plus the DB write, adds `stored`.
- `cached_locations(source, app_id)` — cache only, never expires, never online.
  **This is the only entry point the launch hook uses.**
- `parse_game_data(wikitext)` / `expand_path(raw)` — pure, no network, and
  where the actual work happens.
- `enabled()` — config `online_lookup`, default true.

**Resolving the article:** a Steam appid is an exact key and goes through their
Cargo table (`Infobox_game.Steam_AppID`); everything else goes by title, then
by search. The search step is guarded by `_same_game`, because a *wrong*
article is worse than none: "Portal" must not answer for "Portal 2", while
"Cyberpunk 2077" should answer for "Cyberpunk 2077 Ultimate Edition".

**Mapping paths:** the wiki writes `{{p|token}}\rest`. Only tokens that land in
one of our two spaces survive (`PATH_ROOTS`): `userprofile`,
`userprofile\documents`, `userprofile\appdata\locallow`, `appdata`,
`localappdata` → prefix space; `game` → install folder. Dropped: `{{p|steam}}\
userdata` (Steam Cloud, not in the prefix), `{{p|hkcu}}` (the registry),
`{{p|programdata}}`, and every non-Windows row — a Linux row's `{{p|game}}`
path looks exactly like ours and would be a plausible lie. Trailing file names
and wildcards are stripped: a storage location is a folder.

**Watch out:**

- Wikitext is people, not an API. `_templates`/`_split_params` are brace-aware
  (paths contain `{{p|…}}`, so a plain `split("|")` shreds them), and anything
  unparseable is dropped rather than guessed at.
- **Misses are cached, unreachable is not** (`HIT_TTL` 30 days, `MISS_TTL` 1
  day). "No article" is about the game; "no network" is about us, and caching
  it would strand the user offline for a month.
- `store()` returns None when the game has no prefix yet — the DB is keyed by
  the prefix. The answer stays in the cache, and `wrapper._after` folds it in
  the first time the game actually runs.
- **`ssl_context()` is not optional.** The AppImage's bundled CPython has an
  *empty* trust store (`ssl.get_default_verify_paths()` → `None, None`): its
  OpenSSL was built against a certificate directory that does not exist on the
  host. Every HTTPS call then dies with `CERTIFICATE_VERIFY_FAILED`, which
  reaches the user as "Could not reach PCGamingWiki" on a machine that is
  online. So we load the host's CA bundle (`CA_BUNDLES`, `CA_DIRS`) when ours
  is empty. Verification is never turned off — if no store is found the call
  fails like being offline, which is the honest outcome. Anything else in this
  codebase that grows a Python-level HTTPS client needs the same treatment
  (Velopack does not: its native layer brings its own).

---

## `core/wrapper.py` — the launch hook

`main(argv)` (wrap shape) and `hook(phase, source, app_id)` (pre/post shape),
sharing `_before()` / `_after()`, plus `game_env()`.

Read-only towards the game except for `redirect.reapply()`, which repairs
redirections the user already asked for. Every step is guarded so a failure in
our code cannot stop the game, and the game's exit code is passed through.

`_after()` also folds in `_known_locations()` — what a lookup already found,
read from `pcgw`'s **cache**, never from the network. It does two jobs: it
sharpens the type of what the diff saw, and it carries an answer that was
looked up before the game had a prefix into the DB on the first launch.

**Watch out:** `game_env()` undoes what the AppImage did to the environment
(`BUNDLE_VARS`, `BUNDLE_LISTS`) before the game is started. Without it the
child inherits `PYTHONHOME=$APPDIR/opt/python3.12`, and since Proton is a
Python program run by *another* interpreter — inside a container that cannot
even see our `/tmp` mount point — the launch dies before the game appears.
Symptom: "the game stopped starting the moment I connected it". It returns
`None` outside the AppImage, i.e. the child simply inherits. Anything AppRun
starts exporting later belongs in those two tuples.

---

## `core/registry.py` — `user.reg` editing

`SHELL_FOLDERS` (folder → all its registry value names, including GUIDs),
`shell_folder_root(win_path)`, `windows_path(unix)`, `get_value`, `set_values`,
`get_shell_folder`, `set_shell_folder`, `prefix_in_use`.

Edits are surgical: find the section, patch the one value, leave the rest
alone. Sections are created when missing.

**Watch out:** Wine flushes its in-memory registry over `user.reg` when the
last process in the prefix exits — always check `prefix_in_use` first.

---

## `core/redirect.py` — hybrid redirection

`default_target`, `physical_path`, `location_path`, `redirect(fp, win_path,
target, force)`, `undo(fp, win_path)`, `reapply(fp)`.

Sequence: move data (never overwriting) → replace the physical folder with a
symlink → write the registry → set the DB flags. `reapply` is the self-heal
called before each launch.

`default_target` resolves its root through `db.redirect_root()` — configurable
(`redirect_root` in config.json, the Settings dialog, `--set-save-folder`),
default `~/Games/linux-prefix-hub/`. Changing it never strands data: a moved
location stores its absolute `redirect_target`, so only *future* moves follow
the new root.

`location_path(entry, loc)` answers "where are these files right now" for any
location in either space — the redirect target if moved, the install folder for
game-folder locations, the path inside the prefix otherwise. That is what the
open-in-file-manager action needs.

**Building on it:** locations outside a shell folder are refused on purpose.
If you ever want to support them, it can only be the symlink half, and the
install-folder case fights with launcher updaters.

---

## `core/updater.py` — self-update via Velopack

`app_hook()`, `check(force)` (cached for a day in config.json), `update()`,
`available()`, `is_newer`, `parse_version`.

Velopack owns the mechanics: `check_for_updates` → `download_updates` →
`apply_updates_and_restart`, against the GitHub release feed that
`packaging/build-velopack.sh` produces. We keep only two decisions of our own:

1. **GearLever first.** If it manages our AppImage we do nothing — two
   updaters fighting over one file is worse than a slightly stale app.
2. **`app_hook()` only inside the AppImage.** Outside it there is nothing to
   finish, and Velopack's native layer writes a `NotInstalled` complaint
   straight to stderr that no Python `except` can swallow. It is called from
   `__main__.main` *after* the `--wrapper`/`--hook`/`--daemon` fast paths, so a
   game launch never pays for importing a compiled SDK.

Everything degrades to an honest message when the `velopack` wheel is absent
(pip installs, and the local `build-appimage.sh` test build).

**Building on it:** `github_owner`/`github_repo`, or `update_url` for a
completely different feed, in config.json — no rebuild needed.

---

## `core/desktop.py` — handing a folder to the desktop

**Watch out:** the file manager gets `_child_env()`, not our environment. It
outlives us — KDE keeps one Dolphin per session and hands it every new window
— so whatever we leak is inherited by everything the user starts from it for
the rest of the session. That is not theory: leaking `LPH_GUI_REEXEC` once
made every later start of the app skip the GTK hand-over and fall through to
the terminal branch, i.e. *no window at all*, with the explanation printed
into the journal where nobody looks (`__main__._reexec_gui`). The bundle
variables are stripped for the same reason `wrapper.game_env` strips them, and
from the same list.

`open_folder(path) -> bool`. `xdg-open` first (it honours whatever file manager
the user configured), then a per-desktop fallback chain. Never waits for the
file manager, and refuses a path that is not a directory — a file manager's
reaction to a missing folder ranges from silence to an error dialog, and a
clean `False` the caller can report beats both.

Lives in `core/`, not `gui/`, because `--open` uses it too.

---

## `core/vdf.py` — Valve KeyValues

`loads(text) -> dict`, `dumps(dict) -> text`. Key order is preserved so a
localconfig.vdf round-trip stays diff-friendly.

**Limits:** text KeyValues only; binary VDFs (`shortcuts.vdf`) need the `vdf`
PyPI package (`pip install .[full]`).

---

## `core/yamlite.py` — the Lutris YAML subset

`loads(text) -> dict`. Uses PyYAML when installed, otherwise a small
indentation parser: nested mappings, scalars, lists, comments. No anchors, no
block scalars, no flow mappings.

**Read only by design** — writing goes through the line-based editor in the
Lutris adapter so user configs are not reformatted.

---

## `adapters/base.py` — the adapter contract

`SOURCES`, `get_adapter(name)` (lazy import), `iter_games(sources)`,
`context_from_env()`, `context_for(source, app_id)`, `is_prefix(path)`,
`user_dir_for(prefix)`, `source_label(source)` (the id is internal, the label
is what the user reads), and `HookResult` (ok / manual / message / detail).

`iter_games` isolates each adapter: a launcher with a broken config drops out
of the list instead of taking the scan down.

The order of `SOURCES` is not cosmetic: `context_from_env` asks in that order,
and `generic` — which claims *any* game folder — must come last, after every
adapter that can name a game properly.

---

## `adapters/steam.py` — Steam

`find_steam_roots`, `find_library_dirs` (multi-library — essential, otherwise
games on the second disk are invisible), `iter_games`, `context_from_env`,
`launch_options`, `localconfig_files`, `steam_is_running`, `is_connected`,
`connect`, `disconnect`.

**VERIFY-ON-DEVICE:** `STEAM_ROOT_CANDIDATES` per distro/Flatpak; the
`StateFlags & 4` semantics; the localconfig write path (keeps a `.bak`).

---

## `adapters/lutris.py` — Lutris

Discovery from `pga.db` (stdlib `sqlite3`, read-only) for names/runners, plus
the per-game YAML for `game.prefix`. Falls back to scanning YAML files when
pga.db is missing.

`games_dirs()` is where the YAMLs are looked up: Lutris 0.5.23 keeps them under
the **data** root (`~/.local/share/lutris/games/`) and can leave
`~/.config/lutris` non-existent, older versions used the config root. Both are
searched, and `config_roots()` accepts a root pair as soon as *either* half
exists — gating on the config root alone found no games at all on a current
install.

Two de-duplication rules, both learned from a real library:
`used` tracks config *files*, because the slug need not match the file name
(`diablo-iv` lives in `diablo-iv-battlenet-<ts>.yml`) and the YAML fallback
would otherwise yield the game twice; `_is_steam_mirror` drops the entries
Lutris imports from the Steam library (`runner: steam`, `steam-<appid>-<ts>`),
which have no prefix and are already listed — with a real one — by the Steam
adapter. Anything that *does* have a prefix is always kept.

`connect`/`disconnect` edit `prelaunch_command`, `postexit_command` and
`prelaunch_wait` inside the `system:` block, line by line, with a `.bak`.

**VERIFY-ON-DEVICE:** `prelaunch_wait` has moved between Lutris releases;
without it the "before" snapshot can race the game start.

---

## `adapters/heroic.py` — Heroic

Discovery from `GamesConfig/<appName>.json` (prefix) plus a shape-tolerant walk
through the store caches for the human-readable titles. `connect` adds our
wrapper to `wrapperOptions`, which makes Heroic use the same wrap shape as
Steam.

**VERIFY-ON-DEVICE:** the `wrapperOptions` key for your Heroic version; Heroic
should be closed while we write.

---

## `adapters/generic.py` — game folders without a launcher

`roots()` (`DEFAULT_ROOTS` + `~/.wine*` + `db.extra_game_folders()`),
`iter_games`, `game_for`, `game_name_for`, `context_from_env`,
`launch_command`, `connect`, `disconnect`.

Discovery is the shape test and nothing else: `base.is_prefix` at or below each
root, `SCAN_DEPTH` (2) levels deep, stopping at the first hit — `~/Games/X` and
`~/Games/X/pfx` are equally common.

Three things are different here, and each one is a deliberate decision:

- **The path is the `app_id`.** There is no launcher-assigned id, and the path
  is unique, stable and exactly what `connect` needs to write its instructions.
- **`_claimed_prefixes()` asks the other adapters first** and skips everything
  they list. `~/Games/<slug>` is the Lutris default and has the same shape as a
  hand-made folder, so without this half the library appears twice. It costs
  one extra discovery pass; that is the price of not lying to the user.
- **`connect` has nothing to write into**, so it returns a `manual` result with
  the command to put in front of the user's own launch command, and records
  `managed` in *our* DB — for every other source the launcher config is that
  record, here there is no other config.

`context_from_env` deliberately accepts **any** `WINEPREFIX` (except one under
`compatdata`/`steamapps`, or one another adapter claims), not just folders
under a known root: someone who followed `connect` has told us about that game
more clearly than any folder list could.

---

## `daemon/watcher.py` — the background service

`run()` (inotify on all steamapps + periodic rescan of the other sources),
`run_poll(interval)` (fallback), `_notify`, `_maybe_notify_update`.

On the very first run every installed game is marked known instead of reported,
so the user does not get their whole library as "new".

**Building on it:** watching for `compatdata/*/pfx` appearing (= first launch)
would give a safe moment to apply a pending redirection.

---

## `core/integrate.py` — self-integration

`running_as_appimage`, `detect_gearlever`, `relocate_appimage`,
`install_shims` (wrapper/hook/daemon), `install_systemd_unit`,
`install_desktop_entry`, `full_setup`. All idempotent.

**Building on it:** the shims are deliberately dumb. If the entry point
changes, only `_shim_body` needs to know; the fixed paths stay.

---

## `gui/welcome.py` — setup flow (terminal)

`choose_install_dir(interactive)` (default, warns about unstable locations via
`is_unstable`) and `run(interactive)` as the controller.

`is_unstable` matches **path components, not substrings**: inside the home
folder only `Downloads`/`Desktop` count, and the home folder itself may sit
anywhere (`/mnt/...` is a normal place for a home on a gaming box). The naive
substring version warned about the *default* install location on such a setup.

**Building on it:** the GTK front-end reuses this decision logic; only the
presentation differs.

---

## `gui/app.py` — the window (GTK 4 / libadwaita)

`main()`, `LphApplication`, `MainWindow`, `GameRow`, `GameFolderRow`,
`LocationRow`, `FixedLocationRow`, `SettingsDialog`, `path_button()`,
`open_button()`, `esc()`.

`GameFolderRow` is the first row of every game that has been started once: the
folder the game itself lives in, with the path selectable (libadwaita 1.3+,
feature-detected) and the same open button as everything else. It is the only
folder that exists before anything has been learned, so it is shown even when
there is nothing else to show.

One `Adw.ExpanderRow` per game: a switch that calls the same
`adapter.connect()` the CLI uses, a search button that calls
`pcgw.lookup_and_store()` (network, so off the main loop like everything else),
and one row per learned save location.
`LocationRow` (a shell folder) carries the switch that calls `core.redirect`;
`FixedLocationRow` is the read-only twin for everything that cannot be moved —
the install folder, or a prefix path outside any shell folder. Both carry an
`open_button()`. Hiding those locations would be the wrong call: "where does
this game save?" is the question the app exists to answer, and the answer is
useful even when we cannot act on it.

`SettingsDialog` edits `redirect_root` and `online_lookup`.
`Adw.PreferencesDialog` (libadwaita 1.5+) and `Gtk.FileDialog` (GTK 4.10+) are
both feature-detected — the window runs on whatever the *host* has, not on what
the AppImage bundles. That is also why the online switch is an `ActionRow` with
a `Gtk.Switch` rather than an `Adw.SwitchRow` (libadwaita 1.4+).

`HookResult.manual` (Steam running) becomes a dialog with a copy button.

Two rules that are easy to break:

- **Escape every dynamic string** through `esc()`. Titles, subtitles, toasts
  and dialog headings are parsed as Pango markup, and a real game name like
  "Command & Conquer" makes GTK reject the *whole* string — the row then
  renders with an empty title. Pass raw names around internally
  (`GameRow._name`), never `get_title()`, or you get `&amp;` on screen.
- **Never touch a widget from a worker thread.** Everything blocking goes
  through `gui/tasks.run`, which lands the result on the main loop.

Switch handlers use `state-set` and return `True`, driving the visual state
themselves once the work finishes; `_syncing` guards against the feedback loop
when we set the state programmatically.

---

## `gui/tasks.py` — off the main loop

One function, `run(work, done)`: `work()` in a thread, `done(result, error)`
via `GLib.idle_add`. `done` always runs, so a caller can re-enable the button
it disabled without a second code path.

---

## `packaging/`

- **`AppRun`** — finds the bundled Python, passes the shim modes straight
  through, quietly self-heals on a normal start.
- **`build-velopack.sh`** — the release build. Downloads a relocatable
  CPython (python-appimage), unpacks it into a plain directory, injects our
  package plus the `velopack` wheel, writes the launcher and hands the
  directory to `vpk pack`. Needs the .NET SDK. **VERIFY-ON-DEVICE:** never run
  end to end yet.
- **`build-appimage.sh`** — local test build for machines without .NET.
  Deliberately ships no `velopack` wheel, so it cannot self-update and stays
  quiet. Never publish its output.
- **`make-icon.py`** — renders the icon PNG with stdlib only, so the build
  needs no image library.

---

## `.github/workflows/`

- **`ci.yml`** — ruff + pytest on 3.10 and 3.12, once without and once with
  the optional extras (the dependency-free path must keep working).
- **`release.yml`** — on a `v*` tag: take the version *from the tag*, install
  the .NET SDK and `vpk`, build, smoke-test the AppImage in a throwaway HOME
  (including that it reports the tag as its version), publish **all** of
  `build/release/` — that directory is the update feed, and uploading only the
  AppImage breaks `--update`.
