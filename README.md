# Linux Prefix Hub

Finds your Steam/Lutris/Heroic games, learns **where they save** while you
play, and can move those saves into your home folder — without you ever having
to think about prefixes, `steamuser` or `%appdata%`.

The interface speaks your language: **German on a German desktop, English
everywhere else.** Nothing to configure.

```
linux-prefix-hub --scan                  # your games, from every launcher
linux-prefix-hub --connect "Cyberpunk"   # let us learn from it
linux-prefix-hub --status                # what we learned
linux-prefix-hub --redirect "Cyberpunk"  # saves -> ~/Games/Cyberpunk 2077/
```

## What it does

- **Discovery** across Steam (all libraries), Lutris (pga.db + YAML) and
  Heroic (GamesConfig JSON) — with the real game names, not app ids.
- **Learning where a game saves**, by snapshotting the game folder before and
  after a session and diffing. No database of known games required; it works
  for obscure titles too.
- **Connecting** a game installs the launch hook *in the launcher's own
  config*. Lutris and Heroic do it silently; Steam needs one click while Steam
  is closed (it overwrites its config on exit, so there is no way around that).
- **Moving saves home**, optionally: a registry entry plus a symlink pointing
  at the same folder in `~/Games/<Game>/`. Games that respect Windows folders
  follow the registry, stubborn ones hit the symlink. Idempotent and
  self-healing — a Proton update that eats the symlink is repaired before the
  next launch.
- **Noticing new games** via a small background service, and telling you when
  a new version of this app is out.
- **Self-contained AppImage** that installs itself, and updates itself from
  GitHub releases (or lets GearLever do it).

## Install

**AppImage (recommended)** — download the latest from
[Releases](https://github.com/tokajer/linux-prefix-hub/releases), make it
executable, run it once:

```bash
chmod +x LinuxPrefixHub-*-x86_64.AppImage
./LinuxPrefixHub-*-x86_64.AppImage
```

It copies itself to `~/.local/share/linux-prefix-hub/`, creates the launcher
hooks in `~/.local/bin/`, a background service and a menu entry. After that
the downloaded file can go.

**From source:**

```bash
pipx install .        # or: pip install -e ".[full]"
linux-prefix-hub --scan
```

The core has **no dependencies**. The `[full]` extra adds `inotify_simple`
(instant new-game detection), `vdf` and `PyYAML` (sturdier parsers).

## Connecting a game

```bash
linux-prefix-hub --connect "Elden Ring"
```

- **Lutris / Heroic** — done, nothing else to do.
- **Steam** — with Steam closed we write the launch options ourselves. With
  Steam running we put the string on your clipboard; paste it into the game's
  launch options:

  ```
  "$HOME/.local/bin/linux-prefix-hub-wrapper" %command%
  ```

Then play once. `--status` will show what the game touched.

## Moving saves into your home folder

```bash
linux-prefix-hub --redirect "Elden Ring"              # -> ~/Games/Elden Ring/
linux-prefix-hub --redirect "Elden Ring" --target /mnt/ssd/Saves/Elden
linux-prefix-hub --undo-redirect "Elden Ring"         # back into the game folder
```

Existing files are **never overwritten** — if both sides have a `save0.sav`,
the one in the target wins and nothing is lost. The game must be closed.

Locations outside a standard Windows folder (a game writing into its own
install directory) are reported but not moved: there is no safe way to do it,
and pretending otherwise would risk your data.

## Language

German desktop → German UI. Anything else → English. Override it:

```bash
linux-prefix-hub --lang de --scan          # just this run
linux-prefix-hub --set-language de         # remember it
linux-prefix-hub --set-language auto       # follow the desktop again
```

Adding a language is one JSON file in `src/linux_prefix_hub/locales/`.

## Updates

```bash
linux-prefix-hub --check-update
linux-prefix-hub --update
```

The AppImage carries zsync update information, so **GearLever** and
**AppImageUpdate** can update it too — whichever you prefer is fine, we detect
GearLever and stay out of its way. Downloads are verified against the
release's `SHA256SUMS`.

## All modes

```
linux-prefix-hub                 setup on first run, overview afterwards
  --scan [--source X]            list games (steam | lutris | heroic)
  --status                       learned storage locations
  --connect GAME                 install the launch hook
  --disconnect GAME              remove it again
  --redirect GAME [--target P]   move storage into your home folder
  --undo-redirect GAME           move it back
  --check-update / --update      GitHub releases
  --integrate                    recreate shims/service/menu entry
  --lang / --set-language        language for this run / permanently
  --wrapper CMD...               internal: called by Steam/Heroic
  --hook pre|post ...            internal: called by Lutris
  --daemon                       internal: called by systemd
```

## Layout on disk

```
~/.local/share/linux-prefix-hub/LinuxPrefixHub.AppImage   the binary
~/.local/bin/linux-prefix-hub-wrapper                     hook for Steam/Heroic
~/.local/bin/linux-prefix-hub-hook                        hook for Lutris
~/.local/bin/linux-prefix-hub-daemon                      hook for systemd
~/.config/linux-prefix-hub/                               config, database
~/.config/systemd/user/linux-prefix-hub-watcher.service
~/Games/<Game>/                                           where saves go
```

## ⚠️ Still to verify on real hardware (VERIFY-ON-DEVICE)

Marked in the code at the relevant spots:

1. **Steam roots** (`adapters/steam.py: STEAM_ROOT_CANDIDATES`) — extend per
   distro/Flatpak if your install lives elsewhere.
2. **`StateFlags` semantics** — `& 4 = installed`, checked against real
   `appmanifest_*.acf` (Valve does not document it).
3. **localconfig.vdf writing** — we keep a `.bak`; try it once on a game whose
   launch options you can afford to lose.
4. **Lutris `prelaunch_wait`** — makes Lutris wait for our pre-hook; the key
   has moved between Lutris releases.
5. **Heroic `wrapperOptions`** — correct for Heroic 2.x; check your version.
6. **Registry redirection** — after the first redirected launch, confirm in
   `winecfg` → Desktop Integration that the folder points where you expect.
7. **Desktop notifications from a systemd user service** — need a reachable
   D-Bus (`DBUS_SESSION_BUS_ADDRESS`).
8. **GearLever target folder** (`core/integrate.py: detect_gearlever`) — it is
   configurable.

## Documentation

- **`docs/ARCHITECTURE.md`** — the *why*: design decisions, pitfalls, data
  flow. Read this first.
- **`docs/MODULES.md`** — every file, its functions, what to watch out for.
- **`docs/DEVELOPING.md`** — venv, VSCodium, debugging, tests.
- **`docs/RELEASING.md`** — tag → AppImage → auto-update.
- **`docs/ROADMAP.md`** — what comes next.
- **`CONTRIBUTING.md`** — conventions.
- **`CLAUDE.md`** — compact orientation map (also handy for humans).

## Licence

MIT — see [LICENSE](LICENSE).
