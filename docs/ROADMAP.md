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

The three decisions worth remembering:

- **It never runs on its own.** The launch hook reads `pcgw`'s cache and never
  the network (`wrapper._known_locations`); the wiki is only asked when the
  user asks. `online_lookup` in `config.json` (a switch in Settings) turns the
  whole thing off for a machine that should stay offline.
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



## 🔭 Watch for first launch

The watcher can also react to `compatdata/<appid>/pfx` appearing, which is the
safe moment to apply a redirection the user asked for while the game was never
started.



## 🔭 Backlog

- **Steam Cloud collision guard**: warn when redirected saves could clash with
  cloud sync.
- **SQLite** instead of JSON once the schema grows (keep the `db.py`
  signatures).
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
