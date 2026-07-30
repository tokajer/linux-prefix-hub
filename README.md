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
- **A window** (GTK 4 / libadwaita): your games as a list, one switch to
  connect a game, one per save location to move it home. Everything the CLI
  does, without the CLI.
- **Self-contained AppImage** that installs itself, and updates itself via
  [Velopack](https://velopack.io) (or lets GearLever do it).

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

Updates run through **Velopack**, which downloads, verifies and swaps the
AppImage, then restarts into the new version. If **GearLever** manages the
AppImage we detect that and stay out of its way — two updaters fighting over
one file is worse than a slightly stale app.

Only the AppImage can update itself. A `pip`/`pipx` install belongs to pip, and
the local test build (`packaging/build-appimage.sh`) ships no updater at all.

## All modes

```
linux-prefix-hub                 the window (GTK 4 / libadwaita)
  --gui                          the same thing, explicitly
  --terminal                     the overview in the terminal instead
  --scan [--source X]            list games (steam | lutris | heroic)
  --status                       learned storage locations
  --connect GAME                 install the launch hook
  --disconnect GAME              remove it again
  --redirect GAME [--target P]   move storage into your home folder
  --undo-redirect GAME           move it back
  --check-update / --update      Velopack
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

## Verified on real hardware

Checked against a live Nobara/KDE-Wayland install with 13 games across two
Steam libraries, Heroic (Flatpak) and GearLever (Flatpak):

- ✅ **Steam roots** — 3 of the 5 candidates hit, `realpath` de-duplication
  works. Multi-library discovery finds a library on another disk.
- ✅ **`StateFlags & 4`** — all 21 real manifests read `4`. (The `1026`
  "download running" value is only observable during a download, so that half
  is still unverified.)
- ✅ **Heroic `wrapperOptions`** — present verbatim in a real GamesConfig.
- ✅ **GearLever target folder** — read from its GSettings keyfile
  (`appimages-default-folder`) instead of guessed.
- ✅ **The GUI from inside the AppImage** — hands over to the system
  interpreter, since the bundled CPython has no PyGObject.

Three bugs that only real data exposed, now fixed and covered by regression
tests: a duplicate appid listed twice (same manifest in two libraries), Proton
and the Linux runtimes listed as games (they are identifiable by a
`toolmanifest.vdf`, by nothing inside the manifest), and Heroic's
`download-manager.json` marking an installed game as not installed.

## ⚠️ Still to verify on real hardware (VERIFY-ON-DEVICE)

Marked in the code at the relevant spots:

1. **localconfig.vdf writing** — we keep a `.bak`; try it once on a game whose
   launch options you can afford to lose.
2. **Lutris `prelaunch_wait`** — makes Lutris wait for our pre-hook; the key
   has moved between Lutris releases. Discovery itself is verified against
   Lutris 0.5.23, which keeps the per-game YAMLs under the *data* root
   (`~/.local/share/lutris/games/`) and may not create `~/.config/lutris` at
   all; both layouts are searched.
3. **Registry redirection** — after the first redirected launch, confirm in
   `winecfg` → Desktop Integration that the folder points where you expect.
4. **Desktop notifications from a systemd user service** — need a reachable
   D-Bus (`DBUS_SESSION_BUS_ADDRESS`).
5. **The Velopack build** (`packaging/build-velopack.sh`) — never run end to
   end: the machine it was written on had no working .NET SDK. Check that
   `--mainExe` accepts a shell launcher, what file names `vpk` emits, and
   whether GearLever still accepts a vpk-built AppImage.

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
