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
| `--scan [--show-hidden]`, `--status` | listing | user |
| `--connect`, `--disconnect` | adapter hook | user |
| `--hide`, `--unhide` | `core.db.hide_game` | user |
| `--lookup [--yes]` | `core.pcgw.lookup` + `confirm` | user |
| `--open` | `core.desktop.open_folder` | user |
| `--redirect`, `--undo-redirect` | `core.redirect` | user |
| `--check-update`, `--update` | `core.updater` | user |
| `--uninstall [--keep-settings]` | `core.uninstall` | user |
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
(`~/Games`), `ICON_SOURCE`/`ICON_DIR`/`ICON_FILE`.

The icon ships **inside the package** (`data/linux-prefix-hub.png`), not in
`packaging/`: a pip install has no `packaging/` directory and the AppImage
copies only the package, so anywhere else it would be missing exactly where
it is needed.

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

Two stores, because they are used differently. `config.json` is a handful of
settings one person changes now and then, written atomically (tmp + replace)
and readable in an editor. `prefixes.db` is **SQLite**, because three
processes write it: the launch wrapper files what a session changed, the
watcher files a game it has just seen, and the window files what the user just
decided. Read-whole-file/write-whole-file settles two of those by letting the
later one win — and the earlier decision is gone, with nothing to show that it
ever existed.

`fingerprint`, `load_config`/`save_config`/`set_config`/`install_dir`,
`extra_game_folders`/`add_game_folder`/`forget_game_folder` (the folders
`adapters/generic.py` looks in beyond its defaults),
`extra_ignore_paths`/`add_ignore_path`/`forget_ignore_path` (path fragments
that are never a storage location — `core/snapshot.py` applies them),
`background_tray` (whether closing the window ends the app — default on),
`game_key` (`source:app_id`),
`pending_key`/`pending_redirects`/`add_pending_redirect`/
`drop_pending_redirect` (moves asked for before the game had a folder;
`core/redirect.py` owns what they mean),
`hidden_games`/`is_hidden`/`hide_game`/`unhide_game` (games the user does not
want in the lists),
`confirm_key`/`confirmed_lookups`/`confirmed_locations`/`confirm_locations`/
`forget_confirmed` (the storage locations the user accepted from a
PCGamingWiki lookup; `core/pcgw.py` owns what they mean),
`load_prefixes`/`save_prefixes`, `upsert_prefix`, `get_prefix`, `find_prefix`,
`resolve` (fingerprint | app id | partial name), `update_location`,
`prune_locations`, `set_managed`.

`upsert_prefix` is the important one: it merges and preserves the user-owned
flags listed in `USER_FIELDS` / `LOCATION_USER_FIELDS`.

`prune_locations(fp | None, is_noise)` is the counterpart: it drops locations
a filter we have *today* would never have recorded, so a shader cache stored
last month does not stay a "config" location forever. The predicate comes from
`core/snapshot.py` — the DB does not know what churn looks like, and snapshot
does not know what the user decided. Locations with any `LOCATION_USER_FIELDS`
set are never even offered to it.

`game_key(source, app_id)` is the other identity in this module: the prefix DB
is keyed by the prefix, which does not exist until the game has run once, so
everything we want to remember *before* that lives in `config.json` under this
key instead — a move asked for early (`pending_redirects`), a game the user
does not want to see (`hidden_games`), and a lookup's suggestions they said
yes to (`confirmed_lookups`). `pending_key` is the same function under the
name it shipped with.

`confirmed_lookups` is there rather than in the lookup's own cache for one
reason: that cache expires after a month and the next refresh overwrites it,
and a user decision no rescan may undo cannot live in a file a rescan
rewrites. `confirm_key` case-folds the path so that an article respelling
"Documents" as "documents" is not read as something the user has not seen.

Hiding is deliberately shallow: it takes a game out of a list and does nothing
else. The launch hook stays installed, learned locations stay learned, a
pending move still happens, and the wrapper still files what a session
changed. A filter that quietly turns features off is a filter nobody dares to
use.

### The schema

Two tables plus `meta`. `prefixes` and `locations` have a column for the
fields we look things up by (`source`/`app_id`, `where_space`/`win_path`, the
user-owned flags) and an `extra` JSON column for everything else — so an
adapter can put a new key into an entry without a migration here, and
`_entry_from_row` still hands back the same nested dict the rest of the app
has always seen. A NULL column is *left out* of that dict rather than handed
over as `None`: it was not there when it went in.

`location_key(loc)` is still the identity of a location, and it is now also
the primary key of the `locations` table — the two spaces stay two
namespaces.

Nothing internal calls `save_prefixes` any more; it exists because handing
back what `load_prefixes` gave you has to keep working. `_connect()` opens a
connection per call on purpose (`paths` resolves at import time and the tests
reload it, so a cached one would write into the previous run's directory), and
writes go through `BEGIN IMMEDIATE` so the read half of a merge is inside the
same lock as the write half.

**Migration:** a pre-SQLite `prefixes.json` is folded in once, on the first
connection that finds no `migrated_from_json` flag in `meta`. The file is then
left alone — it costs nothing, it is the only backup of a database that takes
months of playing to fill, and the flag rather than the file's absence is what
says the import happened. Delete the `.db` and the next start picks the JSON
up again; delete the JSON and nothing is lost.

**Building on it:** keep these signatures. Everything above the module reads
and writes plain dicts and does not know there is a database under it.

---

## `core/snapshot.py` — storage-location detection

Two **spaces**, named by `WHERE_PREFIX` / `WHERE_GAME`; every location carries
`where`.

- `snapshot(prefix, user_dir) -> {rel_path: mtime}` over `INTERESTING_SUBTREES`
  only, skipping `IGNORE_FRAGMENTS` (Temp, Windows scratch space, driver
  caches) on top of the shared `IGNORE_ANY_*`.
- `snapshot_game_dir(game_dir) -> {rel_path: mtime} | None` over the install
  folder, skipping `IGNORE_GAME_FRAGMENTS` (logs, downloads, crashes) on top
  of the same shared lists.
- `user_ignores()` — the fragments the user added (`db.extra_ignore_paths`),
  read once per snapshot, never per file.
- `location_is_noise(loc)` — the same question about a location already in the
  DB; `db.prune_locations` acts on it.
- `diff(before, after) -> [rel_path]`
- `classify_locations(changed, where, known) -> [location]` — aggregates to
  directory level and guesses `type` (saves/config/unknown). `known` are
  locations PCGamingWiki already typed (`core/pcgw.py`); they win over the
  guess, matched by `known_type` on exact path first, containment second.
- `save_pending` / `load_pending` — hands the "before" state of *both* spaces
  from the pre hook to the post hook (Lutris runs them as two processes).
  Reads a flat pre-install-folder file as a prefix-only state.

**The filters come in three shapes** — a folder fragment, a file name, a file
suffix — matched case-insensitively against `"/" + rel_path`, which is why
every fragment is written with a slash on both ends (`"/logs/"` must not match
`mylogs/`). Most churn only inflates a file count, because
`classify_locations` aggregates to three path segments anyway; the ones that
matter are those whose first three segments are their own, like DXVK's
`AppData/Local/dxvk`, which was reported as a "config" location of its own.

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
  translated, like `redirect`/`updater` results. **Writes nothing** anywhere
  but the cache: what comes back is a proposal.
- `on_disk(game, locations) -> (there, not_there)` — the same locations split
  by whether the folder actually exists, the existing ones respelled the way
  the disk spells them.
- `confirm(game, locations) -> {added, waiting, stored}` — the user said yes.
  Records that in `config.json` and writes the folders that exist into the DB.
- `cached_locations(game)` — cache only, never expires, never online, and
  only what is **confirmed and on disk**. **This is the only entry point the
  launch hook uses.**
- `parse_game_data(wikitext)` / `expand_path(raw)` / `resolve_dir(root,
  win_path)` — pure, no network, and where the actual work happens.
- `enabled()` — config `online_lookup`, default true.

**It suggests, the user decides** (rule 4 in the module). An article is
written by people and may be about another edition, and what it says lands in
the list the user then moves data around with — so two gates stand between a
lookup and the DB, and *both are asked again every time the answer is used*:

- **Confirmed.** `--lookup` prints the proposal and asks (`--yes` answers for
  a script); the window shows it in a dialog with Cancel/Add. The yes goes
  into `confirmed_lookups` in config.json keyed by `<source>:<app_id>` — not
  into the lookup's own cache, which expires after a month and is overwritten
  by the next refresh. A decision a rescan could undo is exactly what
  CLAUDE.md rule 1 forbids.
- **There.** A path the wiki names but the disk does not have is never
  written, never created, never redirected. Otherwise a storage location gets
  invented out of an article and the first thing the user does with it is
  move data into it.

That combination is what keeps "look it up before playing it once" useful:
the yes is remembered for locations that do not exist yet, and the first
launch that actually creates one folds it in (`cached_locations`). A folder
the game never creates is never written anywhere.

**Spelling:** Windows paths are case-insensitive and articles spell them
freely; the filesystem under Wine is not and does not. `resolve_dir` walks the
path segment by segment, case-insensitively, and matches `*` (what
`expand_path` leaves where a profile id was) against what is there. What gets
stored is the disk's spelling, so the entry a lookup writes and the entry the
diff writes for the same folder stay *one* entry (`db.location_key`).

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

`_after()` also folds in `_known_locations()` — what a lookup found *and the
user confirmed*, read from `pcgw`'s **cache**, never from the network. It does
two jobs: it sharpens the type of what the diff saw, and it carries an answer
that was confirmed before the game had a prefix into the DB on the first
launch that creates the folder. A suggestion nobody has looked at stays in the
cache: a launch is not a moment at which one gets promoted quietly.

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

`default_target`, `physical_path`, `location_path`, `movable_roots(entry)`,
`redirect(fp, win_path, target, force)`, `undo(fp, win_path)`, `reapply(fp)`,
`request(game, roots, target)`, `cancel_request`, `is_requested`,
`apply_pending(game)`.

Sequence: move data (never overwriting) → replace the physical folder with a
symlink → write the registry → set the DB flags. `reapply` is the self-heal
called before each launch.

`default_target` resolves its root through `db.redirect_root()` — configurable
(`redirect_root` in config.json, the Settings dialog, `--set-data-folder`),
default `~/Games/linux-prefix-hub/Games/`. Changing it never strands data: a moved
location stores its absolute `redirect_target`, so only *future* moves follow
the new root.

### Moving a moved folder again

`relocate(fingerprint, win_path, target)` is not undo + redirect: that walks
the data through the prefix and back out, and in between the game folder holds
everything the move exists to keep out of it. The link, the registry and the
DB all name one directory, so the directory moves once (through the same
`_conflicts` / `_merge_move` pair, with the same refusal on two copies) and
all three are re-pointed. `_prune_ours` then drops what the move emptied —
`rmdir` only, inside-out, and upwards no further than `paths.APP_GAMES_DIR`.

`stale_targets()` is the one-off this exists for: the default redirect root
moved a level down into `paths.APP_GAMES_DIR/Games`, so a target sitting
*directly* below the app folder is one an earlier version put there. That
shape is the whole test on purpose — a folder the user named with `--target`
is theirs, and moving it because we changed our mind about a default is the
kind of surprise this app exists to prevent. `move_stale()` runs them all and
lets each one fail on its own (`--move-old-data`, and a row in Settings that
appears only while there is something to move).

`location_path(entry, loc)` answers "where are these files right now" for any
location in either space — the redirect target if moved, the install folder for
game-folder locations, the path inside the prefix otherwise. That is what the
open-in-file-manager action needs.

### The other writer on the same folder

`cloud_paths(entry)`, `cloud_conflicts(entry, root)`,
`cloud_warning(entry, root) -> (headline, detail) | None`.

A launcher with a cloud of its own writes into the very folder we replace with
a symlink, while the game is not running and nobody is looking. Source-agnostic
by *asking*: an adapter that has one defines `cloud_paths(app_id)`, every other
one simply does not and stays silent. Steam is the only one today
(`adapters/steam.py`).

Nothing here refuses anything — with the link in place both sides follow it and
the arrangement works. The guard exists so the one case where it does not (the
link went missing, the other side put its copy back) is something the user was
told about beforehand instead of finding out from two differing versions of
their progress. The terminal prints it before the move, the window asks with a
dialog whose default is *Leave it*, and `RedirectResult["warning"]` carries the
same words back out of a finished move.

The half that is not a warning: `_conflicts(src, dst)` compares the whole tree
**before** anything moves, and a non-empty answer stops the move with both
copies intact. This used to be a silent `rmtree` of whatever `_merge_move`
skipped — that is, of the copy the game folder had. Two versions is a question
only the player can answer, and deleting one is not an answer.

### Asked for before the game ever ran

`request()` stores a wish, `apply_pending()` carries it out. Everything above
needs a prefix — a registry to point elsewhere, a directory to replace with a
link — and a prefix only exists after the first launch, while *before* it is
when people decide where a game's data should go (right after a lookup told
them what it will write).

The wish cannot live in the prefix DB: that is keyed by the prefix, and its
absence is the whole situation. So it goes into `pending_redirects` in
config.json under `<source>:<app_id>`, the only identity a game has this early.

`apply_pending` has three honest reasons to do nothing and come back later,
and the watcher simply retries every pass: the game still has no prefix; the
prefix exists but is *in use* (which is the normal state right after it
appears — the game is booting); or nothing movable is known about the game
yet. It returns the roots it moved, and only drops the wish when every one of
them landed. It also files the game in the DB on the way through, folding in
whatever `pcgw` has been holding in its cache for want of a prefix to key by —
confirmed locations only, and only those whose folder now exists.

`movable_roots(entry)` is the shared answer to "which shell folders of this
game can redirection actually express" — used by `apply_pending` and by
`--redirect`, and it skips install-folder locations rather than reporting them
as failures.

**Building on it:** locations outside a shell folder are refused on purpose.
If you ever want to support them, it can only be the symlink half, and the
install-folder case fights with launcher updaters.

---

## `core/updater.py` — self-update via Velopack

`app_hook()`, `check(force)` (cached for a day in config.json),
`download()`, `finish(restart)`, `update(restart)`, `available()`,
`repo_url()`, `is_newer`, `parse_version`.

Velopack owns the mechanics: `check_for_updates` → `download_updates` →
`wait_exit_then_apply_updates`, against the GitHub release feed that
`packaging/build-velopack.sh` produces. We keep only two decisions of our own:

1. **GearLever first.** If it manages our AppImage we do nothing — two
   updaters fighting over one file is worse than a slightly stale app.
2. **`app_hook()` only inside the AppImage.** Outside it there is nothing to
   finish, and Velopack's native layer writes a `NotInstalled` complaint
   straight to stderr that no Python `except` can swallow. It is called from
   `__main__.main` *after* the `--wrapper`/`--hook`/`--daemon` fast paths, so a
   game launch never pays for importing a compiled SDK.

3. **We tell Velopack where the bundle is when it cannot work it out.**
   It resolves its `UpdateNix` helper against the *working directory*,
   which holds until the window hands over to a system interpreter
   (`__main__._reexec_gui`) — from there auto-locate lands outside the
   bundle and no UpdateManager can be built at all. `_explicit_locator()`
   fills in `$APPDIR/usr/bin/{UpdateNix,sq.version}` and returns None
   unless both exist: `update()` overwrites exactly these paths, so a
   wrong guess is worse than no update. Auto-locate stays the default.

`check()` returns a `reason` — `""` (asked, current), `"unavailable"` (no
updater in this build), `"unreachable"` (feed silent). Only an empty one
may be shown as "you are up to date"; conflating them is how a window
claimed to be current while the terminal offered the update.

Everything degrades to an honest message when the `velopack` wheel is absent
(pip installs, and the local `build-appimage.sh` test build).

### Installing one, in two halves

**An update cannot be installed while we are running**, because installing it
means replacing the file we are executing. Velopack starts its helper with
`--waitPid <us>` and the helper does the work the moment we are gone. So:

- `download()` fetches and nothing else. It returns `ready`, which is the
  only key that means there is something to install — `ok` is also true for
  "you are up to date" and for GearLever.
- `finish(restart)` hands the package to the helper and **returns**. The
  caller then has to end the process; until it does, the app is still the
  old version. `restart=True` asks the helper to start us again afterwards,
  which is the only way to come back: by the time the new build exists,
  nobody is left here to launch it.
- `update(restart=False)` is both, for `--update`.

The SDK has a one-liner for all three, and it is the reason this is written
out longhand: **`apply_updates_and_restart` calls `std::process::exit(0)`**
on success (visible in the wheel's own machine code, right after the call to
`wait_exit_then_apply_updates`). From the window that runs on a GTK worker
thread, so the whole process dies mid-click — no message, no `done`
callback, no clean shutdown, tray icon left on the session bus. And when it
*fails* it returns instead, which the window then reported as "Update
installed. Restart now", because `ok` meant three different things.

There is no `restart_app()` any more. Starting the AppImage before this
process exits starts the *old* build (nothing has been applied yet) and runs
into our own single-instance lock, so the new process hands its activation
to the instance that is about to quit and disappears — the exact symptom of
"Restart now closes the app and nothing comes back".

**VERIFY-ON-DEVICE:** that the helper's `--restart` really brings an AppImage
back. If it does not, nothing is lost — `app_hook()` sets
`set_auto_apply_on_startup(True)`, so the next start applies what is waiting.

**Building on it:** `github_owner`/`github_repo`, or `update_url` for a
completely different feed, in config.json — no rebuild needed.

---

## `core/uninstall.py` — taking the app back off the machine

`plan()`, `blockers()`, `revert_all()`, `disconnect_all()`, `remove_files()`,
`run(keep_settings)`. Used by `--uninstall` and by "Remove {app}" at the
bottom of the window's settings page.

Uninstalling is not "delete some files", because two of the things this app
did live inside *other* people's configuration and outlive it:

1. **Moved game data.** A redirected folder is in the home directory, and the
   game only finds it through a symlink and a registry entry inside its own
   folder. Delete the app and leave that standing and it still works — until
   Proton recreates the folder, the link goes, and the game starts a fresh
   save next to one nobody knows about.
2. **Launch hooks.** Steam launch options, Lutris `prelaunch_command`, Heroic
   `wrapperOptions` all name a shim in `~/.local/bin`. Remove the shim while
   the option still points at it and the game does not start.

Hence the order in `run()` — revert, disconnect, then delete — and the rule
that gives the module its shape: **a step that fails stops the uninstall where
it is.** Each stage leaves a machine that works (data in the game folder is
the default arrangement; a hook whose shim exists is a hook that runs), so
stopping is always safe and never half-done. `blockers()` is the same
information asked *before* anything moves, so the user hears "close Steam"
first rather than after forty folders have moved.

Two details worth keeping:

- **`redirect.undo` reads the DB, not just the symlink.** A Proton update
  that recreated the folder took the link with it and left the data in the
  home directory; undoing from the link alone finds nothing to bring back and
  quietly resets the registry, stranding the saves. `_recorded_target()` is
  what the prefix forgot. Files that exist on both sides are merged the usual
  way — never overwritten, never deleted, and *named* in the result, because
  two versions is a question only the player can answer.
- **Cleanup is `rmdir` and nothing else.** `_prune_empty` removes the folders
  we made once they are empty and stops the moment one is not. It also drops
  the default redirect root itself — `~/Games/linux-prefix-hub/Games`, which
  is ours — but never `~/Games`, and never a root the user configured.

`keep_settings` decides whether `~/.config/linux-prefix-hub` goes with it.
The AppImage GearLever manages is never deleted: it placed that file.

**Honest limit:** a hand-installed game has no config to edit. Its wrapper
sits in a launch command the user wrote, so those games are *named* in the
result instead of silently counted as done.

---

## `core/gameopts.py` — extra options for one game

Everything else in this app answers "where does this game keep its things".
This module answers a different question — "how does this game run" — and it
is here because the obvious way to answer it does not work.

**Why the obvious way fails.** Steam does not start the game; it starts a
container (the Steam Linux Runtime), and the container decides which
environment variables reach what runs inside it. Launch options and our own
launch hook both sit outside it. So "give this one game a performance
overlay" cannot be done from where the rest of the app stands.

**What works instead.** The compatibility build reads a `user_settings.py`
next to itself, from inside the container, and puts what it finds into the
game's environment. That file belongs to the build, not to a game — so we
give the game a build of its own: a copy of an installed one, made of
hardlinks, with its own `user_settings.py`, and Steam pointed at it.

### Two halves

**The profile** is what the user chose, and knows nothing about any of the
above. Named switches (`SWITCHES`) for the things people actually want, plus
free `KEY=value` lines for anyone who knows the names. Stored in
`config.json` under `game_options`, keyed by `db.game_key` like everything a
game can own before it has a folder. `env_for()` folds the two together, and
the user's own lines come last and therefore win.

When Lutris and Heroic get this, they set the same variables their own way
and read this same profile. Nothing in this half will have to change.

**The private build** is Steam's mechanism and only Steam's:
`list_bases()` / `resolve_base()` pick what to copy, `build()` copies it,
`remove()` takes it away, `rebuild_all()` moves every game onto the newest
build of the family it follows.

### The two rules that keep it from destroying something

**A hardlink copy is one file with two names.** `<copy>/user_settings.py`
*is* `<GE-Proton>/user_settings.py` until somebody breaks the link, and
opening it for writing truncates the build the user installed. Everything
written into a copy goes through `_replace`, which unlinks first. This is
not a hypothetical: it is the first thing that goes wrong when the copy is
made with `cp -al` and the settings file written with a plain redirect.

**The directory we delete has to be ours.** `compatibilitytools.d` holds
builds the user installed themselves, and `remove()` is an `rm -rf`. Every
destructive path checks for the `MARKER` file first, and `build()` refuses
rather than overwrite a directory that does not have it. `build()` also
refuses while the game is running — the first thing it does is delete the
copy the running game is executing out of.

### Environments that belong to no game

A game is not the only thing that wants its own environment: a hand-installed
game, a launcher that is not Steam, or just "this one setup I keep coming back
to". Those get `source == "custom"` and an id that is a slug of the name the
user typed, which means `db.game_key` gives them `custom:daoc` and every
function here — `read`, `write`, `build`, `turn_on`, `turn_off`,
`rebuild_all` — takes them without a second code path. `as_game()` is the
whole adapter: a dict with a source, an id and a name, which is all any of it
ever looked at.

An environment has **two names**, and they are not the same thing. The
*alias* is what it is called on disk and in Steam's own list — short, typed
once, never changed, because that folder name ends up inside somebody else's
configuration (the Eden launcher's `protonSteamPath` points straight at this
directory) and moving it takes their setup with it. The *title* is what our
own window calls it, and `rename()` changes only that: "Dark Age of Camelot"
in the list, `LinuxPrefixHub-daoc` on disk.

Two differences, both because there is no game behind one:

- **Nothing is pointed at it.** `turn_on` skips `set_compat_tool` entirely and
  says so; the user picks it themselves, wherever they want it. `turn_off`
  likewise never touches `config.vdf`.
- **Its id is never a number**, which is exactly the case `APPID_FALLBACK`
  exists for — so a custom environment always carries one.

`title` is stored alongside, because the slug cannot be turned back into what
was typed: "Old Game" and "Old-Game" are one directory but two different
words.

### Taking over the script this grew out of

`importable()` reads `~/.config/proton-instances/*.env` — the profiles left by
the shell script this module started as — and offers the ones we do not have.
`import_legacy()` copies one into our config and **nothing else**: the
script's own instance directory stays exactly where it is. Two tools writing
into one directory is how a setup that works gets lost.

The base stored next to those profiles can be an absolute path into some other
launcher's runner folder. `_legacy_base()` reduces it to a name and keeps it
only if a build of that name is actually installed — an import carries a
choice across or falls back to the default family, never a broken one.

### The folder name is for people; the marker is the identity

A copy is called `LinuxPrefixHub-<game name>`, because it is a row somebody
scrolls past in Steam's list and in `compatibilitytools.d`, and a column of
app ids there tells nobody which game is which. The id is appended only when
two games really do share a name — the one case a name cannot settle itself.

That means the folder name cannot be *computed* from a game any more, so
`find_instance()` looks a copy up by the `key` in its marker instead. Which is
what makes renaming safe: `build()` removes whatever copy already belongs to
this game before writing the new one, so a game that was renamed — or a copy
from the first release, when copies were named after the id — is replaced
rather than stranded.

`display_name()` returns that same string rather than a prettier one. Internal
name and display name are two keys in the same manifest, and a launcher that
reads both lists one copy twice.

### A name that never changes over contents that do

`outdated()` compares the build's own `version` file, not just its name.

A profile that follows a *family* moves visibly: `GE-Proton10-34` becomes
`GE-Proton11-5`. A profile that follows a **fixed name kept up to date by
something else** — `Proton-GE Latest`, maintained by ProtonPlus — does not
move at all. The name is constant while what is behind it is replaced, so
comparing names calls such a copy current forever.

The copy keeps working: the hardlinks hold the files it was made from alive
even after the folder they came from is overwritten. It is frozen, not broken
— which is exactly why somebody has to be told. `base_version()` reads the
file, `build()` records it in the marker and in the profile, and `outdated()`
compares it.

Nothing rebuilds on its own. Copying gigabytes is not something to start
behind someone's back, possibly while they are playing.

### The overlay switch sets one variable, deliberately

`SWITCHES["overlay"]` is `{"MANGOHUD": "1"}` and nothing else. The obvious
next step — adding a sensible `MANGOHUD_CONFIG` layout — is wrong, because
that variable *replaces* the user's `~/.config/MangoHud/MangoHud.conf` rather
than adding to it. A helpful default there silently throws away whatever they
set up in Goverlay. Switching the overlay on is ours to do; what it shows is
theirs.

### A build that cannot start anything

`build()` refuses a base with no `files/share/default_pfx` before it copies
anything. That is the folder every game folder is stamped out of, and a build
without it starts nothing — but the failure surfaces in Steam's log at the
moment the game starts, a long way from the switch that caused it. It goes
missing for real: a "latest" directory some other tool keeps up to date is one
interrupted update away from being empty, and copying that faithfully gets you
a faithful copy of something broken.

### The manifest is edited line by line

`compatibilitytool.vdf` gets a new internal name and `display_name` so Steam
does not collide the copy with the build it came from. Not through
`core/vdf.py` (rule 2): most of that file is the build author's comments
explaining the format, and our tokeniser drops them. The internal name is
matched by shape — the lone quoted token inside the `compat_tools` block —
not by value, because the value is whatever the build is called.

### A game folder with no number in it

A compatibility build reads its own app id back out of the game folder's
path. A game whose id is not a number has no number there either, the build
finds nothing, and it refuses to start the game at all — which looks exactly
like "turning the extra options on broke my game". `env_for()` names an app
id for those games (`APPID_FALLBACK`) and stays out of the way of anyone who
names their own.

### Where the pointer lives

`adapters/steam.set_compat_tool()` writes `CompatToolMapping` in
`config.vdf`. Same rules as the launch options next to it: only while Steam
is closed, with a `.bak`, and a `manual` result naming the build when Steam
is open so the user can pick it themselves. `clear_compat_tool()` takes back
only a mapping that still names our own copy — a choice the user has since
made themselves is theirs.

`turn_on()` and `turn_off()` are the two whole operations, and `turn_off()`
runs them in the reverse order for a reason: the pointer goes first, because
a build Steam still points at but that is no longer there is a game Steam
quietly starts with a different one.

## `core/newprefix.py` — game folders the user makes

Every other module in this project *finds* game folders: a launcher made one
and we read it. This one makes one, for the games no launcher has — an
installer from the publisher, an old disc, a tool that has to live next to the
game.

### Two layers, one shape

A Windows environment can be created by a compatibility build or by the
system's own Wine, and the only difference is which program gets started:

| | command | what points at the folder |
|---|---|---|
| compatibility build | `<build>/proton run wineboot -u` | `STEAM_COMPAT_DATA_PATH=<folder>` |
| the system's Wine | `wine wineboot -u` | `WINEPREFIX=<folder>/pfx` |

A build creates `pfx` below the folder it is given; Wine creates whatever
`WINEPREFIX` names. Pointing the second one at the same `pfx` is what makes
both end at `<folder>/pfx` — which is the shape `adapters/generic` already
discovers, and `pfx` is one of its `CONTAINER_NAMES`, so the game is named
after the folder above it. That is the whole integration: a folder made here
is listed, connected, looked up and redirected by code that knows nothing
about this module. The folder above `pfx` stays empty and is where the game
itself gets installed.

`engines()` collects both places builds live — `compatibilitytools.d` (minus
the per-game copies `core/gameopts.py` puts there) and `steamapps/common` —
plus `wine` from `PATH`. `default_engine()` follows the same default the extra
options do (`gameopts.DEFAULT_FAMILY`), so the app has one answer to "which
one" and not two.

### Where it goes

`root()` is the remembered place (`prefix_root` in `config.json`,
`paths.DEFAULT_PREFIX_ROOT` = `~/Games/linux-prefix-hub/prefix` until somebody
says otherwise, `set_root()` to change it), and `create()`
takes a `target` on top of it for one folder only. Both exist because these
folders hold the *install*, not a save file: the disk with room on it is
routinely not the one the home folder is on. The default is a subfolder of
the app's own folder in `~/Games` (next to the redirect root, never the same
folder) — that directory is the user's and very likely older than this app — and
`adapters/generic.DEFAULT_ROOTS` names it, so the scan finds these folders
even when the config that made them is gone. Anything outside the places
`adapters/generic` already looks is remembered as a game folder
(`_findable`), or the app would not find what it just made.

### Starting the game is where this app learns

Every other source has a launcher whose config carries our hook. A folder made
here has none, so a game started from the user's own desktop file is invisible
however long they play it — which is exactly what "it never notices what my
game changed" looks like. `launch()` starts it instead, through
`wrapper.observed()`: the same two snapshots, the same diff, the same DB entry
a Steam game gets. `install()` deliberately does not observe — what an
installer writes is the game, not the place the game saves.

What gets started is not always a Windows program: `is_native()` recognises a
launcher of the game's own — an ordinary Linux binary that runs the game
through a compatibility build it manages itself, pointed at this folder — and
starts it as it is, with `WINEPREFIX` and `STEAM_COMPAT_DATA_PATH` naming this
folder in both the spellings a launcher might read. Sending that through
`proton run` would hand Windows a Linux binary.

`_wait_until_idle()` is the part that makes this true for real games. A game
with its own launcher outlives the process we waited for, so the diff would
compare a save file that is still being written; the prefix itself answers
when everything is over (`registry.prefix_in_use`, polled).

The install folder is named as a second space only when it is *not* the folder
we made: that one holds `pfx`, and naming it would report every change inside
the prefix a second time as a change in the game's own folder.

### Extra options, and a version for one folder alone

`core/gameopts.py` builds a private compatibility build for a Steam game
because Steam starts that game inside a container that filters the
environment. A folder made here is started by `launch()`, so none of that is
needed: `gameopts.own_folder()` says so, `turn_on`/`turn_off` store the profile
and nothing else, and `launch()` passes `env_for(profile)` straight into the
process.

`make_private()` exists for the other case — when something else starts the
game. A launcher of the game's own does not ask us for an environment, but it
can be pointed at a build, and a build reads its own `user_settings.py` from
inside the container. So the folder gets a copy of its build (hardlinks, rule
15, the same code the Steam side uses), the copy carries this game's options,
its path is in the result so it can be pasted into that launcher, and `run()`
starts the game with it from then on. `set_engine()` rebuilds it — a version
stored next to a copy of the old one is not a version change — and `delete()`
takes it with the folder.

### The folder name has to contain a number

`_numbered()`. A compatibility build's `protonfixes` works out which game it is
running from the environment, and its last resort is to read the app id
straight out of the path:

```python
re.findall(r'\d+', os.environ['STEAM_COMPAT_DATA_PATH'])[-1]   # IndexError
```

A path without a single digit raises `IndexError` and the launch dies before
the game starts. We set `SteamAppId` when we start the game ourselves, so this
never shows up there — it shows up in the log of whatever *else* starts the
game, as a Python traceback that reads like a broken Proton install. So the
folder name carries a digit: the name and not the whole path, because paths
move and a folder that works in `/mnt/daten2` and stops working in
`/mnt/spiele` is worse to own than one called `Thief-1`. The name in our lists
is unaffected.

### Two names, and a version that can change

`create(name, engine, target, alias)` — the alias is the folder on disk, typed
once and never moved because a path is what everything else points at; the
name is what the list says, kept in the marker, read back by
`generic.game_name_for()` and changeable with `rename()`. `set_engine()`
re-points the folder at a different build (by exact name — `find_engine`'s
family fallback answers "what runs this now", which is not an answer to
somebody choosing). Nothing is rebuilt: the build is not part of the folder.

### The marker travels with the folder

`<folder>/.linux-prefix-hub-env` holds which engine made it. Not our config:
the folder is the thing that gets moved to another disk, copied and restored
from a backup, and an answer that travels with it is still true afterwards.
It sits above `pfx` and never inside it — what is below `pfx` belongs to
Windows and ends up in every copy of that game.

`find_engine()` accepts that the build named there can be gone, and falls back
to the newest one of the same family (`gameopts.family`). A folder whose build
was replaced would otherwise be one nobody can start a program in again, and
there is no screen anywhere that repairs that.

### Deleting one is the reverse, and it is not `uninstall`

`delete()` refuses anything without the marker — the folder it could be
pointed at by mistake is one with somebody's games in it — and refuses while
the game runs. Then it removes the tree and everything keyed by that game
(the DB entry, a hidden flag, a pending move, confirmed lookups), because
what is left otherwise is a row in the list nobody can open.

What it deliberately does **not** do is fetch moved data back first. Removing
the app does that (rule 14) because the game stays and only we go; here the
game is what goes, and moving saves into a folder that is about to be deleted
is how they are lost. `shutil.rmtree` removes the links to that data without
following them, and the result names where it stayed. `_forget_root()` stops
watching a folder we only watched for that one game — never one the user
named themselves, never one that still holds another game.

### Two things that would bite

A build reads its own app id back out of `STEAM_COMPAT_DATA_PATH`, finds a
folder name where it expects a number, and refuses to start anything — the
same trap `gameopts.env_for()` documents, and the same fallback
(`APPID_FALLBACK`) is used here.

Every process started here gets `desktop.child_env()`. A compatibility build
is a Python program, and the AppImage's `PYTHONHOME` points at a stdlib the
build cannot use (rule 4). `install()` additionally runs from the program's
own directory, because an installer looks for its data files next to itself.

**Whether it worked is decided by the folder, not by the exit code**:
`create()` asks `base.is_prefix()` afterwards, and a boot that left nothing
behind is a failure however it exited.

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
`visible_games(games)`, `context_from_env()`, `context_for(source, app_id)`,
`is_prefix(path)`,
`user_dir_for(prefix)`, `source_label(source)` (the id is internal, the label
is what the user reads — and `generic` reads as the app's own name, because
that group is the one this app manages and where the folders it makes land),
and `HookResult` (ok / manual / message / detail).

`iter_games` isolates each adapter: a launcher with a broken config drops out
of the list instead of taking the scan down.

`visible_games` is the hidden-games filter, and it is deliberately *not* part
of `iter_games`: hiding takes a game out of a list, not out of the app. The
launch wrapper, `context_for` and the moves the watcher still owes the user
all have to keep working for a game nobody wants to look at, so exactly the
two places that draw a list call it — `MainWindow._render` and `--scan`.

The order of `SOURCES` is not cosmetic: `context_from_env` asks in that order,
`generic` — which claims *any* game folder — must come last, after every
adapter that can name a game properly, and `group_by_source(games)` reuses it
as the reading order so the window and `--scan` cannot drift apart. That
helper buckets a library per source (each bucket sorted by name) and keeps a
source it does not recognise, at the end: an unexpected heading beats a game
that silently disappeared.

---

## `adapters/steam.py` — Steam

`find_steam_roots`, `find_library_dirs` (multi-library — essential, otherwise
games on the second disk are invisible), `iter_games`, `context_from_env`,
`launch_options`, `userdata_dirs`, `localconfig_files`, `steam_is_running`,
`is_connected`, `connect`, `disconnect`, `remote_caches`, `cloud_paths`.

### Steam Cloud (`cloud_paths`)

Two different things carry that name, and only one can collide with a folder
we moved:

| | where the files are | can it clash? |
|---|---|---|
| **UFS / the Cloud API** | `userdata/<account>/<appid>/remote/` | no — never inside the prefix |
| **Auto-Cloud** | Windows folders *inside the prefix*, matched by pattern | yes — that is the folder we symlink |

`remotecache.vdf` (per account, per game) tells them apart: an Auto-Cloud entry
is keyed by a path that names the Windows root it came from
(`%WinMyDocuments%/…`), a UFS entry by a bare file name. So `cloud_paths`
returns only entries whose root token is in `CLOUD_ROOTS`, translated into our
`win_path` spelling — an unknown token costs a warning, never a wrong move.
`core/redirect.py` turns that into the user-facing guard.

**VERIFY-ON-DEVICE:** `STEAM_ROOT_CANDIDATES` per distro/Flatpak; the
`StateFlags & 4` semantics; the localconfig write path (keeps a `.bak`); the
`CLOUD_ROOTS` token spelling against a real `remotecache.vdf` (Valve documents
the root names, not how they are written into that file).

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

`run()` (inotify on all steamapps *and* their `compatdata` + periodic rescan of
the other sources), `run_poll(interval)` (fallback), `_notify`,
`_maybe_notify_update`, `_apply_pending`.

On the very first run every installed game is marked known instead of reported,
so the user does not get their whole library as "new".

A `compatdata/<appid>` directory appearing means a game is creating its folder
for the first time. Only the parent is watched — `pfx` shows up inside it
moments later and waiting for that buys nothing, because the game holds the
folder open either way. That is also why the watch is a latency improvement
and not a correctness requirement: `_apply_pending` runs on *every* pass and
`redirect.apply_pending` keeps a wish until it has been carried out in full
(see `core/redirect.py`). Missing the watch costs a minute.

`_refresh` does one `base.iter_games()` per cycle and feeds both the new-game
scan and the pending moves from it — asking the adapters twice would stat
every library and every prefix again.

A hidden game is recorded as known but does not knock: the user took it out of
the list, and a notification is the loudest kind of list there is. Recording
it anyway is what keeps unhiding it later quiet. Pending moves are *not*
filtered that way — a move the user asked for is a decision, and hiding a row
never cancels one.

**Building on it:** the same hook is where "newly installed" greetings and
first-launch preparation would go (see the roadmap's install-experience
layer).

---

## `core/integrate.py` — self-integration

`running_as_appimage`, `detect_gearlever`, `relocate_appimage`,
`install_shims` (wrapper/hook/daemon), `install_systemd_unit`,
`install_desktop_entry`, `install_icon`, `full_setup`. All idempotent.

`install_icon` copies the packaged PNG into
`~/.local/share/icons/hicolor/256x256/apps/`, under **two** names. Nothing
that shows the icon carries it, they all name it: the About dialog and the
tray ask for `linux-prefix-hub`, the desktop entry for `paths.APP_ID`. Without
a copy in the theme each of them renders a blank placeholder — which is what
the application menu showed.

`install_desktop_entry` writes `io.github.tokajer.LinuxPrefixHub.desktop`, not
`linux-prefix-hub.desktop`, and adds `StartupWMClass`. That name is not
cosmetic: it is how the task bar gets from an **open window** to this app.
GTK sends the *program* name to the compositor as the window's app id
(X11: `WM_CLASS`), `gui.app.main` sets it to `paths.APP_ID`, and the shell
then looks for the entry of exactly that name. While they disagreed the
program name was the interpreter's and an open window drew python's icon.
The entry it replaces is deleted as it is written (`LEGACY_DESKTOP_FILE`) —
two files would be two menu items for one app.

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
`LocationRow`, `FixedLocationRow`, `PendingRow`, `SettingsDialog`,
`path_button()`, `open_button()`, `esc()`.

The list is cut by launcher: `MainWindow._render` draws one
`Adw.PreferencesGroup` per bucket of `base.group_by_source()`, titled with
`base.source_label()`. Because the heading names the source, `GameRow`'s
subtitle no longer repeats it ("ready", not "Steam - ready"). The general
instruction sits once above all the groups instead of once per heading.

`_show` keeps that scan in `_scanned` and `_render` draws it; hiding a game
only calls `refilter()`, because nothing on disk changed — only which of the
games we already found belong on screen. Each `GameRow` gets an eye button
(`db.hide_game`/`unhide_game`), and the header grows an eye *toggle* the
moment something is hidden: a permanent button for a list nobody has ever
filtered is one more thing to explain, a way back that exists exactly when it
is needed is not. With the toggle on, hidden games come back into their own
groups, dimmed only by their subtitle ("hidden — ready") and carrying the
reverse button. A launcher whose games are all hidden loses its heading too,
and an empty list says *which* kind of empty it is: "No games found" in front
of a library the user has just hidden reads as a broken scan, and the way back
is the very button that sentence does not mention.

`GameRow._on_hide` defers the redraw with `GLib.idle_add`, because it removes
the group the clicked button sits in — a widget that destroys itself from
inside its own signal handler is a crash waiting for the wrong GTK version.

`GameFolderRow` is the first row of every game that has been started once: the
folder the game itself lives in, with the path selectable (libadwaita 1.3+,
feature-detected) and the same open button as everything else. It is the only
folder that exists before anything has been learned, so it is shown even when
there is nothing else to show.

One `Adw.ExpanderRow` per game: a switch that calls the same
`adapter.connect()` the CLI uses, a search button that calls `pcgw.lookup()`
(network, so off the main loop like everything else) and then shows what came
back in a Cancel/Add dialog (`_propose` → `_accept` → `pcgw.confirm`; nothing
is stored on the way past), and one row per learned storage location.
`LocationRow` (a shell folder) carries the switch that calls `core.redirect`;
`FixedLocationRow` is the read-only twin for everything that cannot be moved —
the install folder, or a prefix path outside any shell folder. Both carry an
`open_button()`. Hiding those locations would be the wrong call: "where does
this game save?" is the question the app exists to answer, and the answer is
useful even when we cannot act on it.

`PendingRow` is the switch a game gets *instead of* a game-folder row when it
has never been started: the same decision as `LocationRow`, in the same place,
worded the same way — only the moment it can be acted on differs. It writes a
wish through `redirect.request()` and the watcher carries it out.

`SettingsDialog` edits `redirect_root`, `online_lookup` and `background_tray`.
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

`LphApplication._start_tray` is what lets the window close into the tray: it
builds a `gui.tray.Tray`, and **only if `tray.live`** does it `hold()` the
application and connect `close-request`. Without `hold()` GTK ends the app with
its last window; without the `live` check the app would vanish with no way
back. `_on_close` asks `live` again every time, because a desktop shell
restart takes the tray host away mid-session.

The update entry follows `updater`'s two halves: `_on_install_update` only
calls `download()`, and only a `ready` answer reaches `_finish_update`, which
asks whether the app may close — because closing it *is* the install step
(`core/updater.py`). "Later" hands nothing over, so the next exit is an
ordinary one and the download waits for `app_hook()` at the next start.

Removing the app hangs off the settings dialog, not the header menu
(`SettingsDialog._remove_group`): it is the one thing in the app no switch
flicks back, and one slip away from "About" is a poor place for it. The
button closes the dialog and activates `app.uninstall`, because everything
after it puts its dialogs on the window — underneath the settings dialog, if
that were still open.

`_on_uninstall` runs `uninstall.plan()` off the main loop first: a
confirmation written before we know what there is to confirm would ask
"remove everything?" and only then discover that a game is running. A blocked
plan gets a dialog with **no** "remove anyway" button — that button would
leave a game's data in a folder the game no longer points at. The confirm
dialog carries a "keep what was learned" check button as its extra child, and
the final dialog is the last thing the app ever shows before `quit()`.

---

## `gui/tray.py` — the tray icon

`Tray(title, icon, items, on_activate)`, `Item(key, label, action)`,
`Tray.live`, `set_label(key, label)`, `set_attention(on)`, `close()`.

**Contains no GTK, deliberately.** GTK4 removed `Gtk.StatusIcon` and the usual
replacement — AppIndicator, Ayatana's fork or Canonical's original — links
against GTK3, so importing its typelib inside a GTK4 process aborts with
"Using GTK 2/3 and GTK 4 in the same process is not supported". It is not a
dependency we declined; it is one that cannot be taken. Underneath those
libraries every tray speaks `org.kde.StatusNotifierItem` and
`com.canonical.dbusmenu`, and Gio exports both without caring about GTK.

`gi` itself is imported lazily (`_gio()`), so the module imports in a plain
test interpreter and every entry point degrades instead of raising.

Three things that are easy to get wrong:

- **`live` must be answerable synchronously.** The caller decides whether to
  connect its close handler before the main loop has run, so the initial
  answer comes from a blocking `NameHasOwner` call (`_watcher_present`); the
  asynchronous `bus_watch_name` only keeps it current. Getting this from the
  watch alone made `live` False for every caller that ever asked, and the
  tray silently did nothing at all.
- **Registration needs the host *and* the name.** They arrive in either order,
  so `_on_name_acquired` and the watch's `appeared` both call
  `_register_with_host`, which no-ops until both are true.
- **Menu actions land on the main loop** via `GLib.idle_add`, never straight
  out of the D-Bus callback — an action opens dialogs and touches widgets.

Menu ids start at 1 because dbusmenu reserves 0 for the root. `set_label`
bumps the revision and emits `LayoutUpdated`; `set_attention` flips `Status`
between `Active` and `NeedsAttention` and emits `NewStatus`.

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
  directory to `vpk pack`. Needs the .NET SDK **and a C compiler**: `vpk`
  parses `--mainExe` as ELF to find the target machine, so the main
  executable is a compiled shim that execs `LinuxPrefixHub.sh` next to it.
  A shell script there fails the build with "Given stream is not a proper
  ELF file".
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
