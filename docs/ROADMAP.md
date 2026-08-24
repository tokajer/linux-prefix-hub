# Roadmap

The next building blocks in a sensible order, each with the context behind it
so you do not have to re-derive *why* it is meant to work that way.

Legend: ✅ done · 🔨 next · 🔭 later

---

## ✅ Foundation

- Steam discovery (multi-library, ACF, prefix + user dir)
- Snapshot-diff detection of storage locations
- Prefix DB (idempotent, preserves user decisions)
- New-game watcher (inotify + poll fallback)
- Self-integration (relocate + shims + systemd, GearLever-aware)
- Terminal welcome flow

## ✅ Adapters, redirection, packaging (this iteration)

- Lutris adapter (pga.db + YAML discovery, line-based hook injection)
- Heroic adapter (GamesConfig JSON, `wrapperOptions`)
- Source-agnostic wrapper with both hook shapes (wrap and pre/post)
- Hybrid redirection (registry + symlink), self-healing via `reapply()`
- Steam launch options written directly into `localconfig.vdf`
- English source strings + German catalog, chosen by desktop locale
- AppImage with bundled CPython, GitHub Actions release, self-update

---

## ✅ Graphical interface (GTK4 / libadwaita)

`gui/app.py` + `gui/tasks.py`, sitting on the existing logic exactly as the
split in `gui/welcome.py` intended.

- Game list, a connect switch per game, a "keep this data in your home
  folder" switch per storage location.
- `HookResult.manual` (Steam running) becomes a dialog with a copy button.
- **The GTK-bundling question resolved itself.** We bundle nothing: the
  AppImage's `--gui` hands over to a *system* interpreter that has PyGObject
  (`__main__._reexec_gui`), because our package is pure Python. That keeps the
  AppImage at 17 MB instead of shipping a second GTK stack.

Still open on the GUI:

- Folder chooser in the first-run dialog (it currently confirms and uses the
  default location; `choose_install_dir` is still terminal-only).
- A language switcher — `--set-language` exists, the window has no control yet.
- The move-home switch always uses the default `~/Games/<Game>/` target; no way
  to pick a different one from the window.

## ✅ Velopack updater

`core/updater.py` talks to the Velopack SDK, `packaging/build-velopack.sh`
builds the release. The old zsync/GitHub-API path is gone.

The first real CI run answered the open question in that script: **`--mainExe`
has to be an ELF binary.** `vpk` reads the machine straight out of it
(`LinuxPackCommandRunner.GetMachineForBinary`), so a shebang script fails the
build with "Given stream is not a proper ELF file". The main executable is now
a ~30-line compiled shim that execs `LinuxPrefixHub.sh` beside it — all logic
stays in the script, where it can be read. Same run, second finding: the
bundled CPython brings its own pip, so the wheel is installed with *that* one
instead of the host's, and no `--platform`/`--abi` has to be kept in step with
`PY_SERIES` (Fedora's `python3` has no pip at all).

`vpk` itself still only runs in CI — see the VERIFY-ON-DEVICE note in the
README for what that leaves open.

## ✅ Generic game folders (`adapters/generic.py`)

Hand-rolled Wine setups are now a source like any other: anything with
`drive_c` + both registry hives (`base.is_prefix`) counts, found in the usual
places (`~/.wine*`, `~/Games`, `~/.local/share/wineprefixes`, …) plus whatever
`--add-game-folder` adds. Everything downstream — snapshot diff, redirection,
`--open` — was already source-agnostic and needed no change.

The two decisions worth remembering:

- **It runs last and skips what the launchers claim.** `~/Games/<slug>` is the
  Lutris default and looks exactly like a hand-made folder, so discovery asks
  the other adapters first (one extra pass) rather than list the library twice.
- **`connect` cannot connect anything** — there is no config that starts the
  game. It hands the user the line to put in front of their own command
  (`WINEPREFIX="…" "…/linux-prefix-hub-wrapper"`) and remembers the intent in
  our own DB, because here that is the only config there is.

Open: the install folder stays unknown (a hand-made setup installs into
`drive_c`), so only the prefix space is diffed for these games.

## ✅ PCGamingWiki lookup (`core/pcgw.py`)

Storage locations without playing first: `--lookup GAME`, or the search button
next to a game in the window. The article's `{{Game data/saves|…}}` and
`{{Game data/config|…}}` rows are mapped into the same two spaces the diff
uses, so everything downstream — DB, `--status`, redirection, the window —
needed no change. `snapshot.classify_locations` now takes those locations as
`known` and lets them decide `type`, which is the part `_guess_type` could
only guess at.

The four decisions worth remembering:

- **It never runs on its own.** The launch hook reads `pcgw`'s cache and never
  the network (`wrapper._known_locations`); the wiki is only asked when the
  user asks. `online_lookup` in `config.json` (a switch in Settings) turns the
  whole thing off for a machine that should stay offline.
- **It suggests; the user decides, and the disk has the last word.** A lookup
  writes nothing on its own: `--lookup` prints what came back and asks (`--yes`
  answers for a script), the window shows it with Cancel/Add, and only
  `pcgw.confirm()` keeps any of it. What the user accepted goes into
  `confirmed_lookups` in `config.json` — not into the lookup's own cache,
  which expires and gets overwritten, and a decision a rescan can undo is the
  one thing this app must not have. A path the wiki names but the disk does
  not have (`pcgw.on_disk`) is never written, created or redirected, no matter
  who confirmed it: an article is written by people and describes Windows, and
  a storage location invented out of one is a folder the user then moves data
  into. Both gates are asked again wherever the answer is used, not once when
  it was given — which is what keeps "look it up before playing it once"
  useful: the yes waits for a folder that does not exist yet, and the first
  launch that really creates it folds it in.
- **Misses are cached, unreachable is not.** "No article" is about the game and
  holds for a day; "no network" is about us and must not strand the user for a
  month.
- **A wrong article is worse than none.** Steam goes through the appid (their
  Cargo table, an exact key); a name search is guarded so "Portal" cannot
  answer for "Portal 2" while "Cyberpunk 2077" still answers for its Ultimate
  Edition.

Building it also turned up the first thing in this codebase that needs TLS from
Python: the AppImage's bundled CPython has **no CA certificates**, so every
lookup said "could not reach PCGamingWiki" from the packaged build while
working from a checkout. `pcgw.ssl_context()` hands it the host's store.

Open: a game with no prefix yet cannot be written to the DB (that is what the
DB is keyed by), so the answer waits in the cache until the first launch folds
it in. Giving the user a way to correct a wrongly matched article would be the
next useful step.

## ✅ Granular file filters (`core/snapshot.py`)

A session changes more than the save game. Aim Lab was the case that made this
visible: DXVK 2.4+ writes its pipeline cache as `<hash>.dxvk.bin` plus a `.lut`
into `AppData/Local/dxvk`, and since that is three path segments of its own,
`classify_locations` reported it as a "config" **storage location** — not just
noise in a file count, but a folder we offered to move into the user's home.

- The `IGNORE_*` lists now come in three shapes (folder fragment, file name,
  file suffix) and are shared between the two spaces where they apply to both.
  Matching is against `"/" + rel_path`, so a fragment carries a slash on both
  ends and `"/logs/"` cannot hit `mylogs/`.
- `--ignore-path FRAGMENT` / `--unignore-path FRAGMENT` (config key
  `ignore_paths`) for the rest, because every engine invents its own cache
  folder and a built-in list can only ever hold the ones we have seen.

The two decisions worth remembering:

- **A filter has to clean up after itself.** One that only applies to future
  launches leaves the junk it was written for sitting in the DB — and the user
  who just typed `--ignore-path` sees nothing happen. `db.prune_locations`
  drops what today's filters would never have recorded, at launch time (next
  to `redirect.reapply`, the other self-heal) and immediately when a filter is
  added. Anything with a `LOCATION_USER_FIELDS` value set is never touched:
  a moved folder is a decision, and silently undoing it would leave a symlink
  pointing at a folder nobody tracks any more.
- **The rule and the invariant live apart.** "This is churn" belongs to
  `snapshot`, "this is the user's" belongs to `db`, so `db.prune_locations`
  takes the predicate as an argument and `snapshot` imports `db` lazily, in
  one function, for the user's list. Neither module needs the other at import
  time.

Open: `AppData/Local/Epic Games` (163 files after a Rocket League session) is
the EOS SDK's cache and still counts as a location. Vendor SDK folders are the
next candidates, but they are a guess in a way `dxvk` is not.

**It also cost us a word.** Filtering out what is *not* a save made it obvious
that the rest is not all saves either — settings, profiles and logs sit in the
same folders. So the user-facing vocabulary dropped "saves"/"Spielstände" for
the whole and now says **game data / Spieldaten** for the content and
**storage location / Speicherort** for a place (CLAUDE.md rule 6). Every
`_()` string, both catalogs, the desktop entry and the README moved with it;
`location["type"] == "saves"` did not, because there it really is the type of
one location. `--set-save-folder` still works — it is a flag in somebody's
script, and a better word is not worth breaking it — but `--set-data-folder`
is the name now.

## ✅ The list, cut by launcher

`base.group_by_source()` buckets a library per source and the window draws one
`Adw.PreferencesGroup` per bucket; `--scan` prints the same shape. The order is
`base.SOURCES` — the order the adapters are asked in — so nobody has to keep a
second one in step, and a source we do not know gets a heading of its own
rather than being dropped.

The heading also took a word off every row: the game subtitle used to read
"Steam - ready" and now reads "ready", because the group above it already
said Steam.

## ✅ Hiding games from the list

A library is not a to-do list. Demos, tools, the Steamworks runtime entries
and the game somebody installed once are all things you scroll past, and past
a certain length the list stops being an answer to "which of my games do I
want to do something about?". So a game can be taken out of it: an eye button
per row, and an eye toggle in the header that brings the hidden ones back.

The three decisions worth remembering:

- **Hiding is about the list, and nothing else.** A hidden game keeps its
  launch hook, keeps everything we learned, still gets the move that was asked
  for, and the wrapper still files what a session changed. The only thing it
  loses is the row — and the new-game notification, because a notification is
  the loudest kind of list there is. A filter that quietly turns features off
  is a filter nobody dares to use, and the first version of this that also
  skipped `_apply_pending` would have cancelled a move by hiding a row.
- **So the filter is not in `iter_games`.** `base.visible_games()` is a
  separate call, made by exactly the two places that draw a list
  (`MainWindow._render`, `--scan`). The wrapper, `context_for` and the watcher
  go on seeing everything, which is what makes the point above true rather
  than merely intended.
- **The way back has to be where the way out was.** The header toggle appears
  the moment something is hidden and disappears when nothing is — a permanent
  button for a list nobody has ever filtered is one more thing to explain. And
  an empty list has to say *which* kind of empty it is: "No games found" in
  front of a library the user has just hidden reads as a broken scan, with the
  cure sitting in a button that sentence does not mention.

Where it lives answers itself the same way `pending_redirects` did: keyed by
`db.game_key` = `<source>:<app_id>` in `config.json`, because a game can be
hidden long before it has a prefix to be keyed by.

## ✅ Tray icon (`gui/tray.py`)

The window is not the app: an update check, a new-game notification and a move
waiting for a game's first launch all outlive it. So closing the window now
hides it and the app stays reachable from the tray.

**There is no library for this.** GTK4 removed `Gtk.StatusIcon`, and the usual
answer — AppIndicator, Ayatana's fork or Canonical's original — is linked
against GTK3: loading its typelib in a GTK4 process aborts the app outright
("Using GTK 2/3 and GTK 4 in the same process is not supported"). What sits
underneath those libraries on every desktop that has a tray is two D-Bus
interfaces, `org.kde.StatusNotifierItem` and `com.canonical.dbusmenu`, and Gio
can export both without caring which GTK is loaded. So we speak them directly.

The three decisions worth remembering:

- **Nothing may close into a tray that is not there.** No `gi`, no session
  bus, or no `StatusNotifierWatcher` (plain GNOME without the AppIndicator
  extension) and `Tray.live` stays False, the close handler is never
  connected, and the window behaves exactly as it did before. An app you can
  neither see nor quit is a far worse bug than one that exits when you close
  it.
- **`live` had to be answerable before the main loop runs.** The caller
  decides whether to connect that handler the moment the window is built, so
  asking the bus who owns the watcher name is a *synchronous* call at startup
  (`_watcher_present`); the asynchronous name watch only keeps it current
  afterwards. Built the other way round it read False for every caller that
  ever asked, and the tray silently did nothing at all.
- **Registration waits for both halves.** The host has to be on the bus *and*
  we have to own the name we are handing it, and those arrive in either
  order — so both callbacks funnel into `_register_with_host` and whichever
  is second does the work.

`background_tray` in `config.json` (a switch in Settings) puts the old
behaviour back for anyone who wants the window to be the app after all.

The update entry lives in that menu too, and an update waiting turns the icon
to `NeedsAttention` — which is the one thing an icon can say without a
notification somebody has to dismiss.

## ✅ Watch for first launch

The watcher now also watches every library's `compatdata` directory, and
carries out moves the user asked for before the game had anything to move
(`redirect.request` → `redirect.apply_pending`).

**The roadmap entry that asked for this had the timing backwards, and the code
says so.** `compatdata/<appid>` appearing is *not* the safe moment to apply a
redirection — it is the moment the game is booting and Wine is creating the
prefix, and Wine writes its in-memory registry over `user.reg` when it shuts
down (rule 7). An edit made then is gone by the time the player quits. So the
appearance is what we *learn* from — the game is filed in the DB, with any
PCGamingWiki answer that has been waiting in the cache for a prefix to be
keyed by — and the move happens on the first pass that finds the folder idle.
`apply_pending` returning an empty list means "next pass", never "give up".

That also answers where such a wish can live: not in the prefix DB, which is
keyed by the prefix, because the whole point is that there is none yet.
`pending_redirects` in `config.json` is keyed by `<source>:<app_id>` — the
only identity a game has before it has a folder.



## ✅ Steam Cloud collision guard

We are not the only one writing into that folder. Steam's **Auto-Cloud** copies
files in and out of Windows folders *inside the prefix*, while the game is not
running — the same folder we replace with a symlink.

`adapters/steam.cloud_paths()` reads `remotecache.vdf`, `core/redirect.py`
turns it into `cloud_warning()`, the terminal prints it before the move and the
window asks with a dialog whose default is "Leave it".

The three decisions worth remembering:

- **Only Auto-Cloud counts.** The other thing called Steam Cloud — the UFS
  API — keeps its files in `userdata/<account>/<appid>/remote/`, outside the
  prefix entirely, where nothing we do can reach them. Warning about those
  would be a warning nobody can act on. `remotecache.vdf` tells the two apart:
  an Auto-Cloud entry is keyed by a path that names its Windows root
  (`%WinMyDocuments%/…`), a UFS entry by a bare file name. An unknown root
  token costs us a warning, never a wrong move.
- **It warns, it does not refuse.** With the link in place both sides follow
  it and the arrangement works fine. What goes wrong is the link *not* being
  there — a recreated prefix, a Proton update — and Steam restoring its copy
  into a real folder while ours sits in the home folder. That is worth saying
  in advance and not worth forbidding.
- **The adapter answers, the core asks.** `redirect.cloud_paths()` looks for a
  `cloud_paths` function on the adapter and stays silent when there is none.
  No shared guess, no `if source == "steam"` in `core/`.

**Writing it turned up the bug it was warning about.** `_replace_with_symlink`
merged the folder into the target — never overwriting, correctly — and then
`rmtree`'d what was left, with a comment claiming only empty directories were.
They were not: every file that existed on *both* sides had been skipped by the
merge and was sitting right there. So the exact situation this guard is about
was resolved by deleting the game folder's copy, silently. Now `_conflicts()`
compares the whole tree first, a clash stops the move with both copies intact,
and the message names the count and both places. Two versions is a question
only the player can answer.

## ✅ SQLite instead of JSON (`core/db.py`)

`prefixes.json` → `prefixes.db`, with every signature in `db.py` unchanged.

**The schema was never the reason; the writers were.** Three processes reach
for this file — the launch wrapper files what a session changed, the watcher
files a game it has just seen, the window files what the user just decided —
and two of them can land in the same second. Read the whole file, change one
field, write the whole file back, and the later writer wins: the other
decision is gone, with nothing to show that it ever existed. That is the same
invariant rule 1 is about, broken one level further down where no amount of
careful merging in `upsert_prefix` could see it.

The three decisions worth remembering:

- **Columns for what we look up, `extra` for the rest.** `source`/`app_id`,
  `where_space`/`win_path` and the user-owned flags get real columns; every
  other key goes into an `extra` JSON column. So an adapter can put a new
  field into an entry without a migration here, and callers still get the
  same nested dicts they always did. A NULL column is left *out* of that dict
  rather than handed over as `None` — it was not there when it went in.
- **Read and write share one transaction.** `upsert_prefix` opens
  `BEGIN IMMEDIATE` before it reads the existing entry, because read-then-write
  is precisely the pattern being fixed.
- **The old file stays.** It is folded in once, on the first connection that
  finds no flag in `meta`, and then left alone. It costs nothing, it is the
  only backup of a database that takes months of playing to fill, and the flag
  rather than the file's absence is what says the import happened — so
  deleting the `.db` picks the JSON back up and deleting the JSON loses
  nothing.

`config.json` stays JSON on purpose: a handful of settings one person changes
now and then, worth keeping openable in an editor.

Open: `known_games.json` (the watcher's new-game set) is still a file. It has
one writer, so it has none of this problem.


## ✅ Uninstalling (`core/uninstall.py`)

`--uninstall` (with `--keep-settings`), and "Remove Linux Prefix Hub" at the
bottom of the window's settings page. The requirement it was written to was "no data loss is key",
and that turned out to decide the whole shape of the module.

**Uninstalling is not "delete some files"**, because two of the things this
app did live inside *other* people's configuration and outlive it:

- A **moved folder** is in the home directory, and the game only finds it
  through a symlink and a registry entry inside its own folder. Delete the app
  and leave that standing and it works — until Proton recreates the folder,
  the link goes, and the game starts a fresh save next to one nobody knows
  about any more.
- A **launch hook** is a Steam launch option, a Lutris `prelaunch_command`, a
  Heroic `wrapperOptions`, and each of them names a shim in `~/.local/bin`.
  Remove the shim while the option still points at it and the game does not
  start. Not data loss, but the user's library broken by our own cleanup, so
  it is treated exactly as seriously.

The three decisions worth remembering:

- **A step that fails stops the uninstall where it is.** Revert, disconnect,
  then delete — and each stage leaves a machine that works: data in the game
  folder is the default arrangement, and a hook whose shim still exists is a
  hook that still runs. So stopping is always safe, and "otherwise the
  uninstallation should not be performed" needs no separate transaction, only
  the right order. `blockers()` asks the same questions *before* anything
  moves, because "close Steam" is a sentence to hear first rather than after
  forty folders have moved.
- **Undoing a move has to read the DB, not only the symlink.** This was a real
  hole: `redirect.undo` took its source from the link inside the prefix, so
  the one case the whole feature exists for — a Proton update that recreated
  the folder and took the link with it — found nothing to bring back and
  quietly reset the registry, leaving the saves in a folder nobody points at.
  `_recorded_target()` is what the prefix forgot. Files that exist on both
  sides are still merged the usual way (never overwritten, never deleted) and
  are now *named* in the result, because two versions is a question only the
  player can answer.
- **Cleanup is `rmdir` and nothing else.** Never `rmtree` on anything holding
  game data: `rmdir` refuses on a directory with something left in it, which
  is exactly the guarantee needed. It also removes our own redirect root
  (`~/Games/linux-prefix-hub/Games`) once the last game has left it — but never
  `~/Games`, which is the reason the default put us one level below it, and
  never a root the user configured.

Open: a hand-installed game has no config to edit — its wrapper sits in a
launch command the user wrote. Those games are named in the result rather than
counted as done, which is the honest answer and not a satisfying one.

## ✅ The update that closed the app instead of restarting it

The bug report was "shows restart now, then it does not restart, the app
closes, and after opening it the update was done". Three separate faults, and
the first one is why the other two were reachable at all.

**`apply_updates_and_restart` calls `std::process::exit(0)`.** Not documented
anywhere we could find — it is in the wheel's own machine code, right after
the call to `wait_exit_then_apply_updates`. The window ran it on a GTK worker
thread (`gui/tasks.py`), so a successful install killed the process mid-click:
no message, no `done` callback, no clean shutdown, and the tray icon left
sitting on the session bus. And when it *failed* it returned an error instead
— which is the only way that call ever came back.

**So `ok` meant three different things.** `update()` returned `ok=True` for
"you are up to date" as well as for "installed", and `_on_install_update`
offered a restart for any `ok` that was not GearLever's. That is the "Restart
now" the report describes: a dialog about an update that had never been
downloaded, shown to someone whose app had already updated itself.

**And the restart could not have worked either.** `restart_app()` started the
AppImage *before* this process exited — but Velopack's helper waits for our
pid before it replaces anything, so that file was still the old build; and the
new process then met our own single-instance lock, handed its activation to
the instance that was about to quit, and exited. Window flashes, app closes,
nothing comes back.

The fix is to stop hiding the one fact all of this follows from: **an update
cannot be installed while we are running**, because installing it means
replacing the file we are executing. So the flow is two halves now —
`download()` fetches and returns `ready`, `finish()` hands over and *returns*,
and the caller ends the process. Closing the app is the install step, the
window says so in those words, and "Later" hands nothing over at all (the
download waits for `app_hook()`'s auto-apply at the next start). `restart_app`
is gone: coming back is the helper's job, because by then nobody is left here
to do it.

## 🔭 Backlog

- **More languages**: one JSON file each; the machinery is done.
- **aarch64 AppImage**: the build script already takes `ARCH`; add a matrix
  entry to `release.yml` once there is hardware to test on.
- **Bottles**: show as "detected, not managed"; full management only on
  request (the gaming overlap is small).


## 🔭 Install-experience layer (also backlog)

Mostly falls out of the watcher and discovery that already exist:

- "Newly installed" greeting with a connect offer (uses the watcher).
- First-launch preparation: create the prefix in a controlled way
  (`wineboot`-style), set the redirection *before* the user really plays.
- Optional: set the Proton version per game, suggest ProtonDB tweaks.

**Honest limit:** Steam's own install dialog is untouchable, and writing VDF
needs Steam closed.
