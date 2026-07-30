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

- Game list, a connect switch per game, a "keep saves in your home folder"
  switch per save location.
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
builds the release. The old zsync/GitHub-API path is gone. **Not yet run end to
end** — see the VERIFY-ON-DEVICE note in the README.

## 🔭 Install-experience layer

Mostly falls out of the watcher and discovery that already exist:

- "Newly installed" greeting with a connect offer (uses the watcher).
- First-launch preparation: create the prefix in a controlled way
  (`wineboot`-style), set the redirection *before* the user really plays.
- Optional: set the Proton version per game, suggest ProtonDB tweaks.

**Honest limit:** Steam's own install dialog is untouchable, and writing VDF
needs Steam closed.

## 🔭 PCGamingWiki lookup

Instant storage-location hits without playing first, and a much better
`snapshot._guess_type`. Needs care: it is a network dependency, so it must stay
optional and cached.

## 🔭 Watch for first launch

The watcher can also react to `compatdata/<appid>/pfx` appearing, which is the
safe moment to apply a redirection the user asked for while the game was never
started.

## 🔭 More sources

- **Bottles**: show as "detected, not managed"; full management only on
  request (the gaming overlap is small).
- **Generic prefix scan**: treat anything with `user.reg` + `drive_c` as a
  prefix (`base.is_prefix` already does this) for hand-rolled Wine setups.

## 🔭 Backlog

- **Steam Cloud collision guard**: warn when redirected saves could clash with
  cloud sync.
- **SQLite** instead of JSON once the schema grows (keep the `db.py`
  signatures).
- **More languages**: one JSON file each; the machinery is done.
- **aarch64 AppImage**: the build script already takes `ARCH`; add a matrix
  entry to `release.yml` once there is hardware to test on.
