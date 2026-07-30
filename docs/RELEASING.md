# Releasing

One tag, everything else is automatic.

```bash
# 1. bump the version in both places (they are checked against the tag)
#    src/linux_prefix_hub/__init__.py   __version__ = "0.3.0"
#    pyproject.toml                     version = "0.3.0"

# 2. commit, tag, push
git commit -am "release: 0.3.0"
git tag v0.3.0
git push origin main v0.3.0
```

`.github/workflows/release.yml` then:

1. checks the tag matches `__version__` (and fails loudly if not),
2. installs `zsync` + `desktop-file-utils`,
3. runs `packaging/build-appimage.sh`,
4. smoke-tests the AppImage (`--version`, `--scan`) in a throwaway HOME,
5. publishes `LinuxPrefixHub-<version>-x86_64.AppImage`, the matching
   `.zsync` and `SHA256SUMS` to the GitHub release.

You can also trigger it by hand ("Run workflow" → version) — useful for
re-cutting a release without moving the tag.

## How updating works for users

Three independent routes, all pointing at the same GitHub release:

| Route | Trigger | Mechanism |
|---|---|---|
| GearLever | its own UI | reads the embedded zsync update information |
| AppImageUpdate / `appimageupdatetool` | user runs it | same embedded information, delta download |
| Built in | `--update`, or the daily check in the watcher | GitHub API → download → SHA-256 check → atomic replace |

`core/updater.py` prefers them in that order: if GearLever manages the
AppImage we do nothing (it would fight over placement), if
`appimageupdatetool` exists we let it do a delta update, otherwise we download
the asset ourselves and verify it against `SHA256SUMS` before replacing the
installed binary.

The daily check is cached in `config.json` (`update_check`) and the watcher
notifies at most once per version.

## What makes the update information work

`packaging/build-appimage.sh` passes

```
gh-releases-zsync|tokajer|linux-prefix-hub|latest|LinuxPrefixHub-*-x86_64.AppImage.zsync
```

to appimagetool. The `latest` keyword means every future release is found
without rebuilding the pointer — so the glob in the file name must keep
matching. **If you rename the AppImage, update that pattern too**, otherwise
existing installations stop finding updates.

## Forks

No rebuild needed to point the built-in updater somewhere else — set it in
`~/.config/linux-prefix-hub/config.json`:

```json
{ "github_owner": "you", "github_repo": "your-fork" }
```

The embedded zsync information is baked in at build time, so for GearLever and
AppImageUpdate a fork does need its own build (change `GH_OWNER`/`GH_REPO`,
which the script reads from the environment).

## Checklist before tagging

- [ ] `pytest -q` and `ruff check src tests` are green
- [ ] version bumped in `__init__.py` **and** `pyproject.toml`
- [ ] README/docs mention anything new
- [ ] built locally at least once and run with a throwaway HOME
