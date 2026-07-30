# Releasing

One tag, everything else is automatic. Releases are built and updated by
**Velopack** — it owns the packaging *and* the update feed, so the two can
never drift apart.

```bash
git tag v0.1.0
git push origin main v0.1.0
```

That is the whole procedure. **The tag is the version** — nothing in the
source carries one, so there is nothing to bump and nothing that could
disagree with the tag:

| Where | How it learns the version |
|---|---|
| the AppImage | `packaging/build-*.sh` writes `_version.py` into the bundle |
| a wheel (`pip install .`) | hatch-vcs, from `git describe` |
| a plain checkout | neither exists → `0.0.0+dev` (see `__init__.py`) |

`.github/workflows/release.yml` then:

1. takes the version from the tag (`v1.2.3` → `1.2.3`),
2. installs the .NET SDK and the `vpk` tool,
3. runs `packaging/build-velopack.sh` with that version,
4. smoke-tests the AppImage in a throwaway HOME — including that its
   `--version` really reports the tag, because that number is what `--update`
   compares against,
5. publishes **everything** in `build/release/` to the GitHub release.

Step 5 matters: the directory *is* the update feed. Uploading only the
`.AppImage` leaves `--update` unable to find anything.

You can also trigger it by hand ("Run workflow" → version) — useful for
re-cutting a release without moving the tag.

## Build requirements

`vpk` is a .NET tool, so a release build needs the .NET SDK:

```bash
sudo dnf install dotnet-sdk-10.0     # Fedora/Nobara
dotnet tool install -g vpk
```

Keep the `vpk` version and the `velopack` wheel version (pyproject, `update`
extra) aligned — Velopack asks for that explicitly, and a mismatch shows up as
a feed the client cannot read.

Without a .NET SDK you can still get a **local test build**:

```bash
./packaging/build-appimage.sh
```

That one deliberately ships no `velopack` wheel, so it cannot self-update.
Never publish it.

## How updating works for users

| Route | Trigger | Mechanism |
|---|---|---|
| GearLever | its own UI | it manages the file, we stay out of the way |
| Built in | `--update`, or the daily check in the watcher | Velopack: `check_for_updates` → `download_updates` → `apply_updates_and_restart` |

`core/updater.py` still checks GearLever first: if GearLever manages the
AppImage we do nothing, because two updaters fighting over one file is worse
than a slightly stale app.

`updater.app_hook()` runs once at startup (from `__main__.main`, deliberately
*after* the `--wrapper`/`--hook`/`--daemon` fast paths so a game launch never
pays for it). Velopack uses it to finish a pending update. It is skipped
entirely outside the AppImage: the native layer logs a `NotInstalled`
complaint straight to stderr that no Python `except` can swallow.

The daily check is cached in `config.json` (`update_check`) and the watcher
notifies at most once per version.

## Forks

Point the updater somewhere else in `~/.config/linux-prefix-hub/config.json`:

```json
{ "github_owner": "you", "github_repo": "your-fork" }
```

or override the whole feed with `"update_url"`. No rebuild needed — unlike the
old zsync setup, nothing about the feed is baked into the binary.

## Checklist before tagging

- [ ] `pytest -q` and `ruff check src tests` are green
- [ ] README/docs mention anything new
- [ ] built locally at least once and run with a throwaway HOME
- [ ] the tag is the number you actually want — it is the only place the
      version exists, so a wrong tag is a wrong release (delete it, tag
      again; the workflow re-runs on the new one)
