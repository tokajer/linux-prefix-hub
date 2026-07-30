# Developing

Getting the project running and working on it.

---

## 1. Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[full,dev]"     # editable + optional extras + pytest/ruff
```

`-e` (editable) means code changes take effect immediately. The core also runs
**without** `[full]` (dependency-free: own VDF/YAML readers, poll fallback) —
and CI tests both paths, so do not accidentally make an extra mandatory.

## 2. Running it safely

```bash
python -m linux_prefix_hub --scan      # read-only, safe on your real system
python -m linux_prefix_hub --status
```

Anything that *sets up* (the default start, `--integrate`, `--connect`) writes
shims, a systemd unit, a desktop entry and launcher config. Use a throwaway
HOME for that:

```bash
HOME=/tmp/lph-test python -m linux_prefix_hub
HOME=/tmp/lph-test LPH_LANG=de python -m linux_prefix_hub --scan
```

`LPH_LANG` overrides the language for one run, which is also how the tests keep
their assertions deterministic.

## 3. Tests

```bash
pytest -q          # ~66 tests, well under a second
```

They build fake Steam/Lutris/Heroic installs and fake prefixes under `tmp_path`
and redirect `HOME` (see `tests/conftest.py`) — no real launcher needed, and
nothing touches your own config.

New adapter? At minimum test discovery *and* hook injection, including that the
user's own settings survive the injection.

## 4. Lint

```bash
ruff check src tests
```

The rule set is pinned in `pyproject.toml` so a global ruff config cannot make
your run disagree with CI. Line length is 79.

## 5. VSCodium / VS Code

`.vscode/launch.json` ships with ready-made configurations: **scan**,
**status**, **integrate**, **welcome (throwaway HOME)** and **welcome in
German**. Pick one in "Run and Debug" (`Ctrl+Shift+D`), set breakpoints, F5.

Recommended extensions (all on Open VSX, so VSCodium works):
**Python** (`ms-python.python`), **Pylance** or **Jedi**, **Ruff**
(`charliermarsh.ruff`).

Set the interpreter to `.venv/bin/python`: `Ctrl+Shift+P` → "Python: Select
Interpreter".

## 6. Project layout

```
linux-prefix-hub/
├── src/linux_prefix_hub/     # the code (src layout)
│   ├── __main__.py           # CLI & mode dispatch
│   ├── core/                 # paths, i18n, db, vdf, yamlite, snapshot,
│   │                         # wrapper, registry, redirect, integrate, updater
│   ├── adapters/             # base, steam, lutris, heroic
│   ├── daemon/               # watcher
│   ├── gui/                  # welcome (later: GTK4)
│   └── locales/              # de.json (English is the source language)
├── packaging/                # AppRun, build-appimage.sh, make-icon.py, icon
├── .github/workflows/        # ci.yml, release.yml
├── docs/                     # ARCHITECTURE, MODULES, ROADMAP, RELEASING, this
├── tests/                    # against fake environments
├── CLAUDE.md                 # compact orientation map
└── pyproject.toml
```

## 7. Building the AppImage locally

```bash
sudo dnf install zsync            # or: apt install zsync   (optional but
                                  # needed for update information)
./packaging/build-appimage.sh
```

Needs network (it fetches a relocatable CPython and appimagetool). Output lands
in `build/`. Without `zsyncmake` it still builds, just without the delta-update
information — fine for testing, not for a release.

Test the result in a throwaway HOME:

```bash
HOME=/tmp/lph-test ./build/LinuxPrefixHub-*-x86_64.AppImage --scan
```

## 8. Working on translations

Source strings are English and live in the code inside `_()`. To translate:

1. `src/linux_prefix_hub/locales/de.json` — key is the exact English string.
2. Keep every `{placeholder}` identical; `test_core.py` fails otherwise.
3. New language: copy `de.json` to `<code>.json`. It is picked up
   automatically (`i18n.available_languages`).

## 9. Conventions in short

`from __future__ import annotations`, type annotations, 79 columns, defensive
parsing of foreign files, lazy imports in CLI branches, and anything that needs
real hardware marked `VERIFY-ON-DEVICE`. `CONTRIBUTING.md` has the full list.
