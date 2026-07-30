# Linux Prefix Hub

Finds your Steam/Lutris/Heroic games, learns **where they put their data**
while you play, and can move that data into your home folder — without you
ever having to think about prefixes, `steamuser` or `%appdata%`.

The interface speaks your language: **German on a German desktop, English
everywhere else.** Nothing to configure.

```
linux-prefix-hub --scan                  # your games, from every launcher
linux-prefix-hub --connect "Cyberpunk"   # let us learn from it
linux-prefix-hub --status                # what we learned
linux-prefix-hub --redirect "Cyberpunk"  # data -> ~/Games/linux-prefix-hub/
```

## What it does

- **Discovery** across Steam (all libraries), Lutris (pga.db + YAML) and
  Heroic (GamesConfig JSON) — with the real game names, not app ids. Games you
  set up by hand, without any launcher, are found too (see below).
- **Learning where a game puts its data**, by snapshotting the game folder
  before and after a session and diffing. No database of known games
  required; it works for obscure titles too.
- **Or asking PCGamingWiki**, if you would rather not play first: one lookup
  and the known storage locations are there. Optional, cached, and never done
  behind your back — see below.
- **Connecting** a game installs the launch hook *in the launcher's own
  config*. Lutris and Heroic do it silently; Steam needs one click while Steam
  is closed (it overwrites its config on exit, so there is no way around that).
- **Moving game data home**, optionally: a registry entry plus a symlink
  pointing at the same folder in `~/Games/linux-prefix-hub/<Game>/`
  (configurable in Settings). Games that respect Windows folders follow the
  registry, stubborn ones hit the symlink. Idempotent and self-healing — a
  Proton update that eats the symlink is repaired before the next launch.
- **Showing you the folder**, whether or not it can be moved. Source-engine
  games like Portal 2 save into their own install directory; those are found
  too, listed, and opened in your file manager on request — just not moved.
  The game's own folder is listed the same way, with its full path, from the
  moment the game has been started once — in the window and in `--status`,
  and `--open` takes you there when nothing else is known yet.
- **Noticing new games** via a small background service, and telling you when
  a new version of this app is out.
- **A window** (GTK 4 / libadwaita): your games as a list, one switch to
  connect a game, one per storage location to move it home. Everything the CLI
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

### When something shows up that is not worth keeping

A session changes more than your progress: shader caches, crash dumps and log
files churn on every launch. The known ones are filtered out already — Aim Lab
writing its DXVK pipeline cache to `AppData/Local/dxvk` used to be listed as a
storage location, and no longer is. Every engine invents its own, though, so
you can add your own filter:

```bash
linux-prefix-hub --ignore-path "AppData/Local/SomeEngine/Cache"
linux-prefix-hub --unignore-path "AppData/Local/SomeEngine/Cache"
```

Any part of a path works (`ShaderCache`, `.trace`), matched case-insensitively.
Adding one also forgets what has already been recorded under it — except
folders you have moved into your home folder, which are yours to undo with
`--undo-redirect`.

## Looking a game up instead of playing it

Not everything has to be learned the slow way — [PCGamingWiki][pcgw] already
knows where thousands of games keep their data:

```bash
linux-prefix-hub --lookup "Cyberpunk 2077"
```

```
PCGamingWiki knows 2 storage location(s) for Cyberpunk 2077.
    [saves  ] Saved Games/CD Projekt Red/Cyberpunk 2077
    [config ] AppData/Local/CD Projekt Red/Cyberpunk 2077
    https://www.pcgamingwiki.com/wiki/Cyberpunk_2077
```

In the window it is the search button next to a game. What comes back is
treated exactly like a location we found ourselves: you can open it, and move
it into your home folder.

Two things worth knowing:

- **It only happens when you ask.** Nothing is looked up in the background,
  and never while a game is starting or stopping. The switch in Settings
  ("Allow looking games up online") turns it off entirely.
- **Answers are cached** for a month, so asking twice does not bother anyone's
  server. If the game has never been started, the answer waits until it has —
  we key everything by the game folder, and there is not one yet.

The wiki is a starting point, not the last word: it describes Windows, and
your machine is the one that decides. Play once and the diff confirms — or
corrects — what was looked up.

[pcgw]: https://www.pcgamingwiki.com/

## Games without a launcher

A game folder you made yourself is found by its shape alone — we look in
`~/.wine*`, `~/Games`, `~/.local/share/wineprefixes` and the other usual
places, and skip everything a launcher already manages. If yours lives
somewhere else:

```bash
linux-prefix-hub --add-game-folder /mnt/ssd/wine
linux-prefix-hub --forget-game-folder /mnt/ssd/wine
```

There is no launcher config to hook here, so `--connect` hands you the line to
put in front of your own launch command instead:

```bash
WINEPREFIX="$HOME/.wine-osu" "$HOME/.local/bin/linux-prefix-hub-wrapper" wine osu.exe
```

Everything after that is the same as for any other game: play once, `--status`
shows what it touched, `--redirect` moves the data into your home folder.

## Moving game data into your home folder

```bash
linux-prefix-hub --redirect "Elden Ring"              # -> the data folder
linux-prefix-hub --redirect "Elden Ring" --target /mnt/ssd/Saves/Elden
linux-prefix-hub --undo-redirect "Elden Ring"         # back into the game folder
linux-prefix-hub --open "Elden Ring"                  # show it in the file manager
```

Existing files are **never overwritten** — if both sides have a `save0.sav`,
the one in the target wins and nothing is lost. The game must be closed.

By default everything lands in `~/Games/linux-prefix-hub/<Game>/`. Change it in
the window under **Settings**, or:

```bash
linux-prefix-hub --set-data-folder /mnt/ssd/Saves
```

Folders you already moved keep the path they were moved to; only later moves
follow the new setting.

Locations outside a standard Windows folder (a game writing into its own
install directory) are reported but not moved: there is no safe way to do it,
and pretending otherwise would risk your data. `--open` still takes you there.

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
  --scan [--source X]            list games (steam | lutris | heroic | generic)
  --status                       learned storage locations
  --connect GAME                 install the launch hook
  --disconnect GAME              remove it again
  --lookup GAME                  ask PCGamingWiki where it stores things
  --redirect GAME [--target P]   move storage into your home folder
  --undo-redirect GAME           move it back
  --open GAME                    show its data folder (or the game folder)
  --set-data-folder PATH         where moved game data is kept
  --add-game-folder PATH         also look for games there
  --forget-game-folder PATH      stop looking there
  --ignore-path PATH             never report that path as a storage location
  --unignore-path PATH           report it again
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
~/.config/linux-prefix-hub/pcgamingwiki/                  cached lookups
~/.config/systemd/user/linux-prefix-hub-watcher.service
~/Games/linux-prefix-hub/<Game>/                          moved game data
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
- ✅ **The PCGamingWiki lookup against the live wiki** — Steam appid, exact
  title and search resolution, plus a name that has no article. Portal 2's
  install-folder saves, Cyberpunk's `Saved Games`, Skyrim's `My Games` and
  Hollow Knight's `LocalLow` all map to the paths the diff would produce.

Five bugs that only real use exposed, now fixed and covered by regression
tests: a duplicate appid listed twice (same manifest in two libraries), Proton
and the Linux runtimes listed as games (they are identifiable by a
`toolmanifest.vdf`, by nothing inside the manifest), Heroic's
`download-manager.json` marking an installed game as not installed, every
lookup reporting "could not reach PCGamingWiki" **from inside the AppImage**
while working fine outside it (the bundled CPython carries no CA certificates,
so we hand it the host's — `pcgw.ssl_context`), and the app silently refusing
to open a window when started from a file manager that we had started
ourselves earlier — it had inherited our GUI hand-over guard and passed it on
(`__main__._reexec_gui`, `desktop._child_env`).

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
5. **Hand-made game folders** — `DEFAULT_ROOTS` in `adapters/generic.py` is a
   "where do people keep these" list and cannot be complete. Check it against
   your own setup; anything missing is one `--add-game-folder` away.
6. **The Velopack build** (`packaging/build-velopack.sh`) — never run end to
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

Copyright © 2026 tokajer.

GNU General Public License, version 3 or later — see [LICENSE](LICENSE). This
program comes with **absolutely no warranty**; you are free to redistribute it
and to change it under those terms.

The AppImage bundles a CPython interpreter (PSF licence) and, in the release
build, Velopack (MIT); the window uses the system's PyGObject/GTK (LGPL). All
of those may be combined with GPLv3 code.
