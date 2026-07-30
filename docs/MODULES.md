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
| `--redirect`, `--undo-redirect` | `core.redirect` | user |
| `--check-update`, `--update` | `core.updater` | user |
| `--lang`, `--set-language` | `core.i18n` | user |

**Building on it:** the three shim modes are matched *before* argparse and use
lazy imports, so a game launch never pays for the CLI. Keep it that way.

---

## `core/paths.py` — central paths

Every fixed location in one place, XDG-conform:
`DEFAULT_INSTALL_DIR`, `CONFIG_DIR`, `LOCAL_BIN`, the three shims,
`WATCHER_UNIT`, `SNAPSHOT_DIR`, `DEFAULT_REDIRECT_ROOT` (`~/Games`).

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
`load_prefixes`/`save_prefixes`, `upsert_prefix`, `get_prefix`, `find_prefix`,
`resolve` (fingerprint | app id | partial name), `update_location`,
`set_managed`.

`upsert_prefix` is the important one: it merges and preserves the user-owned
flags listed in `USER_FIELDS` / `LOCATION_USER_FIELDS`.

**Building on it:** if the schema outgrows JSON, SQLite is the next step — but
keep these signatures and the rest of the code will not notice.

---

## `core/snapshot.py` — storage-location detection

- `snapshot(prefix, user_dir) -> {rel_path: mtime}` over `INTERESTING_SUBTREES`
  only, skipping `IGNORE_FRAGMENTS` (Temp, crash dumps, shader caches).
- `diff(before, after) -> [rel_path]`
- `classify_locations(changed) -> [location]` — aggregates to directory level
  and guesses `type` (saves/config/unknown).
- `save_pending` / `load_pending` — hands the "before" snapshot from the pre
  hook to the post hook (Lutris runs them as two processes).

**Building on it:** `_guess_type` is coarse; PCGamingWiki data would sharpen
it. For very large prefixes the `rglob` could be pre-filtered by directory
mtime.

---

## `core/wrapper.py` — the launch hook

`main(argv)` (wrap shape) and `hook(phase, source, app_id)` (pre/post shape),
sharing `_before()` / `_after()`.

Read-only towards the game except for `redirect.reapply()`, which repairs
redirections the user already asked for. Every step is guarded so a failure in
our code cannot stop the game, and the game's exit code is passed through.

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

`default_target`, `physical_path`, `redirect(fp, win_path, target, force)`,
`undo(fp, win_path)`, `reapply(fp)`.

Sequence: move data (never overwriting) → replace the physical folder with a
symlink → write the registry → set the DB flags. `reapply` is the self-heal
called before each launch.

**Building on it:** locations outside a shell folder are refused on purpose.
If you ever want to support them, it can only be the symlink half, and the
install-folder case fights with launcher updaters.

---

## `core/updater.py` — self-update from GitHub

`check(force)` (cached for a day in config.json), `latest_release()`,
`update()`, `is_newer`, `parse_version`.

Order of preference: GearLever → `appimageupdatetool` (delta) → our own
download with SHA-256 verification against the release's `SHA256SUMS`, then an
atomic `os.replace` of the installed AppImage.

Refuses politely for pip/pipx installs — that is pip's job.

**Building on it:** `GITHUB_OWNER`/`GITHUB_REPO` can be overridden in
config.json (`github_owner`, `github_repo`) so forks work without a rebuild.

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
`user_dir_for(prefix)`, and `HookResult` (ok / manual / message / detail).

`iter_games` isolates each adapter: a launcher with a broken config drops out
of the list instead of taking the scan down.

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

**Building on it:** for the GTK version, replace `choose_install_dir` with a
dialog and reuse `run`. The decision logic stays put.

---

## `packaging/`

- **`AppRun`** — finds the bundled Python, passes the shim modes straight
  through, quietly self-heals on a normal start.
- **`build-appimage.sh`** — downloads a relocatable CPython
  (python-appimage), injects our package, packs with appimagetool and embeds
  zsync update info. Degrades to a build without update info when `zsyncmake`
  is missing.
- **`make-icon.py`** — renders the icon PNG with stdlib only, so the build
  needs no image library.

---

## `.github/workflows/`

- **`ci.yml`** — ruff + pytest on 3.10 and 3.12, once without and once with
  the optional extras (the dependency-free path must keep working).
- **`release.yml`** — on a `v*` tag: verify the tag matches `__version__`,
  build, smoke-test the AppImage in a throwaway HOME, publish AppImage +
  `.zsync` + `SHA256SUMS` to the release.
