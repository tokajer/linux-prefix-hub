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

## 🔨 Graphical interface (GTK4 / libadwaita)

Sits on the existing logic — the split between logic and presentation in
`gui/welcome.py` exists for exactly this.

- Game list ("Cyberpunk 2077 · saves: ~/Games/… · [Connect]").
- **No** Wine/prefix vocabulary in the UI.
- Welcome dialog: replace `choose_install_dir` with a folder chooser, reuse
  `run` as the controller.
- "Connect" button per game, calling the same `adapter.connect()` the CLI uses;
  show `HookResult.manual` (Steam running) as a copy-paste card.
- "Move saves home" switch per storage location → `core.redirect`.
- PyGObject as a dependency; either bundle it in the AppImage or use system
  GTK. Bundling GTK is the bigger part of this task — budget for it.

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
