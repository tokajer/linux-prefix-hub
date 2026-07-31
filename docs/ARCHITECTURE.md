# Architecture

This file explains the *why* — the decisions behind the structure and the
pitfalls that motivated them. Read it before making changes; it saves you from
re-deriving the same dead ends.

---

## Guiding idea: it should feel like Windows

The user must not need to know about prefixes, `steamuser`, `compatdata`, the
Z: drive or `user.reg`. They see games, a storage location and a "play" button
in the launcher they already use. Every decision below serves that.

Two consequences:

1. The user stays in **their own launcher** (Steam/Lutris/Heroic). We hook in
   invisibly instead of building yet another launcher.
2. **Detection before redirection.** The valuable part is *knowing* where a
   game saves. Moving those saves is optional and builds on top.

---

## The five load-bearing decisions

### 1. Adapter pattern: sources are swappable, the core is shared

```
┌─────────┐  ┌─────────┐  ┌─────────┐
│ Steam   │  │ Lutris  │  │ Heroic  │   ← adapters (discovery + hook)
│ adapter │  │ adapter │  │ adapter │      different config formats
└────┬────┘  └────┬────┘  └────┬────┘
     └────────────┼────────────┘
           ┌──────▼──────┐
           │    Core     │   ← identical for every source
           │  (wrapper)  │
           └──────┬──────┘
        ┌─────────┼─────────┐
    ┌───▼────┐ ┌──▼─────┐ ┌─▼──────┐
    │Snapshot│ │Redirect│ │Prefix  │
    │        │ │        │ │DB      │
    └────────┘ └────────┘ └────────┘
```

A new source (Bottles, say) is **one new adapter**; the core stays untouched.
Every adapter does three things: discovery, installing the hook, and telling
the core which game is starting.

| Source | Config | Hook mechanism | Manual step? |
|--------|--------|----------------|--------------|
| Steam | VDF (`appmanifest`, `libraryfolders`, `localconfig`) | `%command%` wrapper in launch options | only while Steam runs |
| Lutris | YAML (`~/.config/lutris/games/*.yml`) | `prelaunch_command` / `postexit_command` | no |
| Heroic | JSON (`~/.config/heroic/GamesConfig/*.json`) | `wrapperOptions` | no |
| generic | none — the folder itself | the user's own launch command | always |

The generic source is the pattern taken to its end: no config to read, no
config to write, just `base.is_prefix` and the user. It runs last and skips
every folder an adapter above it claims, because a Lutris prefix in
`~/Games/<slug>` is shaped exactly like a hand-made one. Its `connect` can only
hand over a command — see `docs/MODULES.md`.

Steam is the awkward one: it keeps its config in memory and writes it out when
it exits, so anything we write while it runs is lost. With Steam closed we
write `localconfig.vdf` ourselves (with a `.bak`); with Steam running we hand
the user the string and put it on their clipboard. There is no third option.

### 2. Two hook shapes, one code path

Steam and Heroic **wrap** the game command — one process, both snapshots in
memory. Lutris calls a **pre** and a **post** command — two processes, so the
"before" snapshot is written to `~/.config/linux-prefix-hub/snapshots/<fp>.json`
and consumed by the second call. `core/wrapper.py` implements both shapes over
the same `_before()` / `_after()` pair.

### 3. Detecting storage locations by snapshot diff

There is **no API** that says "game X saves to Y". The information is spread
over at least four places: `AppData/Roaming|Local`, `Documents/My Games`,
`Saved Games`, sometimes the install folder itself or Steam Cloud.

Our reliable route: **snapshot before launch, snapshot after exit, diff.**
Whatever changed is a storage location. This finds exotic locations with zero
prior knowledge, which is why the core wraps the launch at all.

Noise is filtered out explicitly (`snapshot.IGNORE_FRAGMENTS`): `Temp`,
`CrashDumps`, shader caches, `INetCache`. Without that, every game "saves" to
half a dozen Windows scratch directories.

It is complemented by **PCGamingWiki** (`core/pcgw.py`) for instant hits
without playing — see below. The diff stays the source of truth: it is the
only one that can see what a game *actually* does on this machine.

**Install-folder special case:** if a game writes into its own
`steamapps/common/<Game>/`, that is *not* a shell folder → not redirectable via
the registry. Important that the app *detects and shows* it even though it
cannot move it.

### 4. Redirection: hybrid (registry + symlink)

- The **registry** (`user.reg`, `Shell Folders` and `User Shell Folders`) is
  the official way — Wine and well-behaved games follow it.
- The **symlink** at the physical location inside the prefix is the safety
  net: plenty of games ignore the registry and write straight to
  `C:\users\steamuser\AppData\...`.

Both point at the **same** target, so they cannot disagree, and the whole
thing is **idempotent and self-healing**: a Proton update that wipes the
symlink does not matter, because the data lives in the home folder and
`redirect.reapply()` relinks it before the next launch.

Granularity is the **shell folder** (Documents, AppData/Roaming, …), because
that is what the registry can express. Since a Proton prefix belongs to one
game, "redirect Documents of this prefix" is exactly "one folder for this
game" — `~/Games/<Game>/Documents`.

Two hazards worth knowing:

- Downloads, Saved Games and LocalLow only have **GUID** value names
  (`{374DE290-…}`), no readable ones. `registry.SHELL_FOLDERS` maps each folder
  to *all* its names and writes them all.
- Wine keeps the registry in memory and flushes it when the last process in
  the prefix exits. Editing `user.reg` while the game runs is silently undone,
  so `registry.prefix_in_use()` refuses.

### 5. Packaging: AppImage with a fixed shim layer

The core problem for *our* app: it hooks in deeply (Steam launch options,
systemd service), but an AppImage is mounted at `/tmp/.mount_XXXXXX` —
ephemeral and differently named on every start. A permanent Steam entry cannot
point there.

**Solution — fixed shims, wandering AppImage:**

```
Steam launch options → ~/.local/bin/linux-prefix-hub-wrapper   (never changes)
                              │
                              ▼
              ~/.local/share/linux-prefix-hub/LinuxPrefixHub.AppImage
```

- The AppImage **copies itself** to the fixed location on first start.
  Downloads/Desktop are only the drop-off point and may be cleaned up after.
- The **shims** are tiny forwarders. They are the only thing that must live
  outside, because something that exists only *inside* the AppImage by
  definition does not exist while the AppImage is not running — and the shim is
  what starts it.
- **Self-heal:** `AppRun` runs `--integrate` on every normal start, so the
  shims and the service come back even if you launched from `/tmp` once.

**GearLever** is *detected and respected* (if it already placed the AppImage at
a fixed location we do not relocate), but **never required** — otherwise users
would need Flatpak + GearLever + AppImage, which contradicts the whole point.

**Why not Flatpak/native?** A Flatpak sandbox would have to be opened up so far
for Steam/Lutris/prefix access that it stops being a sandbox, and reaching host
systemd from inside is fiddly. `pipx` stays the comfortable dev route.

---

## Data flow on launch

```
Steam "Play" → linux-prefix-hub-wrapper %command%
                        │
   ┌────────────────────┼──────────────────────────────┐
   │ 1. which game? (env / adapter)                     │
   │ 2. register in the DB, re-apply redirections       │
   │ 3. snapshot BEFORE                                 │
   │ 4. exec the real game command, wait                │
   │ 5. snapshot AFTER → diff → storage locations       │
   │ 6. merge into the prefix DB (idempotent)           │
   └────────────────────────────────────────────────────┘
```

Step 2 is the only write, and it only touches folders the user already asked
us to move. Everything else is observation. If any of it throws, the game
still launches and its exit code is passed through — a save-game tracker that
stops people from playing has failed at its job.

Step 5 also reads what a lookup already found, **from disk only** — and of
that, only what the user confirmed and only what exists right now. Nothing on
this path waits on a network, and nothing on it promotes a suggestion nobody
has looked at.

---

## The PCGamingWiki lookup

The diff needs a play session before it knows anything. PCGamingWiki knows
already, for thousands of games, so `core/pcgw.py` reads the
`{{Game data/saves|…}}` rows of an article and maps them into the same two
spaces the diff uses. Same shape, same DB, one extra `detected_by` value.

Four constraints follow from it being someone else's server — and from an
article being someone else's writing:

1. **It never runs on its own.** A lookup is a button and a CLI flag. The
   launch hook reads the cache and nothing else — a game that just exited is
   not the moment to wait on a HTTP request, and rule 3 in `CLAUDE.md` covers
   the network too.
2. **The answer is cached**, hits for a month and misses for a day.
   *Unreachable* is not cached at all: "there is no article" is a fact about
   the game, "there is no network" is a fact about us.
3. **A wrong article is worse than no article.** A Steam appid is an exact key
   (their Cargo table); a name is not, so the search fallback refuses anything
   that is not clearly the same game — otherwise we would hand the user
   another game's save folders and call it knowledge.
4. **It suggests, the user decides, the disk has the last word.** `lookup()`
   writes nothing; `confirm()` is the yes, and it is remembered in
   `config.json` where no rescan and no cache expiry can undo it. A folder the
   article names but the machine does not have is never written, created or
   redirected (`on_disk`) — not even after a yes, and not later either unless
   the game itself creates it. The alternative is a storage location invented
   out of an article, which the user then moves real data into.

What the wiki says does not overrule what the diff *saw*: the diff wins on
file counts and provenance, the wiki wins on `type`, which is the one thing a
path heuristic can only guess at.

---

## New-game detection

Steam writes `appmanifest_<appid>.acf` on install; `StateFlags` distinguishes
"still downloading" (e.g. `1026`) from "fully installed" (`& 4`). The watcher
uses **inotify** on every library's `steamapps` (multi-library!) and reports
newly completed installs as a desktop notification. The same loop re-scans
Lutris and Heroic every minute — their configs change rarely enough.

Without `inotify_simple` it degrades to pure polling, so the core stays
dependency-free.

The same watcher does the daily update check, at most one notification per
version.

---

## Idempotency — the most important invariant

Both discovery scans and the wrapper write through `db.upsert_prefix()`. That
function **merges** and preserves user decisions: a rescan must never reset
`redirected`/`redirect_target` (per location) or `managed` (per game). New
user-controlled fields belong in `USER_FIELDS` / `LOCATION_USER_FIELDS` in
`core/db.py`, otherwise the next scan silently throws the user's choice away.

Careful merging is not enough on its own, which is why the prefix DB is
**SQLite** and not the JSON file it started as. Three processes write it — the
launch wrapper, the watcher, the window — and a whole-file rewrite settles two
of them by letting the later one win, one level below where `upsert_prefix`
can see anything. Now every write touches the rows it means, and the read half
of a merge sits inside the same `BEGIN IMMEDIATE` as the write half. Nothing
above `core/db.py` noticed: the signatures and the dicts they pass around are
unchanged. `config.json` is still JSON — one writer, and worth keeping
openable in an editor.

---

## We are not the only one writing there

A launcher with a cloud of its own copies files into the folder we replaced
with a symlink, while the game is not running. Steam's Auto-Cloud is the case
that exists today (`adapters/steam.cloud_paths`, read from `remotecache.vdf`).

The design answer is the same shape as everything else here: `core/` **asks**
(`redirect.cloud_paths` looks for a `cloud_paths` function on the adapter),
the adapter answers, and an adapter without a cloud stays silent. No
`if source == "steam"` below `adapters/`.

The guard warns rather than refuses, because with the link in place both sides
follow it and the arrangement is fine. What is *not* a warning:
`redirect._conflicts` compares the whole tree before anything moves, and a file
present on both sides stops the move with both copies intact. Two versions of
someone's progress is a question only they can answer.

---

## Fingerprint instead of source logic

Every prefix is identified by `sha256(realpath(prefix))[:16]` (`db.fingerprint`).
It therefore does not matter *who* created it — Steam, Lutris, Heroic or a
hand-rolled Wine setup. A prefix is always recognisable by `system.reg` +
`user.reg` + `drive_c/` (`base.is_prefix`), and `adapters/generic.py` is built
on nothing but that: a game folder no launcher knows about is still a game
folder, gets a fingerprint like every other one, and everything downstream
(snapshot diff, redirection, `--open`) works on it unchanged.

---

## Never reformat someone else's config

We write into three foreign config formats, and each one is handled so that a
user's file survives intact:

- **Lutris YAML** — edited line by line inside the `system:` block. Comments,
  ordering and formatting stay. (Our YAML reader is deliberately read-only.)
- **Steam VDF** — parsed and rewritten, but with key order preserved and a
  `.bak` alongside.
- **Heroic JSON** — parsed and rewritten (JSON has no comments to lose), `.bak`
  alongside.

A tool that reformats the config of a program it does not own will eventually
lose someone's settings, and they will never trust it again.

---

## Language

Source strings are English and live in the code. `locales/de.json` maps them to
German. The UI language is the desktop locale unless the config or `--lang`
says otherwise. English can never be "missing", a broken translation falls back
to English instead of crashing, and a test asserts that every German string
keeps the placeholders of its source.
