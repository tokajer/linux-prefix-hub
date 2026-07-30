# Entwickeln in VSCodium

Schritt-für-Schritt, um das Projekt in VSCodium lauffähig zu haben und
weiterzuentwickeln.

---

## 1. Repo klonen / entpacken

Wenn du das ZIP bekommen hast: entpacken und den Ordnerinhalt in dein (leeres)
git-Repo kopieren. Das `.git` initialisierst du dann einfach:

```bash
cd deinapp
git init
git add .
git commit -m "Fundament: Steam-Discovery, Snapshot-Lernen, DB, Watcher, AppImage-Integration"
```

## 2. Python-Umgebung

Empfohlen: virtuelles Environment, damit die Extras isoliert bleiben.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[full]"     # editable install + inotify_simple + vdf
```

`-e` (editable) heißt: Code-Änderungen wirken sofort, ohne neu zu installieren.

Der Kern läuft auch **ohne** `[full]` (dependency-frei, Poll-Fallback + eigener
VDF-Parser) — praktisch, wenn du nur schnell was testen willst:

```bash
pip install -e .
```

## 3. Empfohlene VSCodium-Extensions

- **Python** (ms-python.python — im Open-VSX-Marketplace verfügbar)
- **Pylance** oder **Jedi** für Autovervollständigung
- **Ruff** (charliermarsh.ruff) für Linting/Formatting — optional, aber angenehm

Interpreter auf `.venv/bin/python` setzen: `Ctrl+Shift+P` →
„Python: Select Interpreter" → `.venv` auswählen.

## 4. Ausprobieren (ohne echtes Steam)

Die Module sind gegen Fake-Umgebungen testbar. Schnelltest der Discovery:

```bash
python -m deinapp --scan      # zeigt gefundene Steam-Spiele (echt, auf deinem System)
python -m deinapp --status    # zeigt bisher gelernte Speicherorte
python -m deinapp --integrate # legt Shims + systemd-Unit an (idempotent)
```

Für isolierte Tests kannst du HOME umbiegen (so haben wir entwickelt):

```bash
HOME=/tmp/test_home \
XDG_DATA_HOME=/tmp/test_home/.local/share \
XDG_CONFIG_HOME=/tmp/test_home/.config \
python -m deinapp --scan
```

## 5. Debuggen in VSCodium

Es liegt eine `.vscode/launch.json` bei mit fertigen Konfigurationen:
- **deinapp: scan** — Discovery
- **deinapp: status** — DB-Inhalt
- **deinapp: integrate** — Self-Setup
- **deinapp: welcome** — Einrichtungs-Flow

Auswählen im „Run and Debug"-Panel (`Ctrl+Shift+D`), Breakpoints setzen, F5.

> Hinweis: VSCodium nutzt den Open-VSX-Marketplace, nicht den von MS-VSCode. Die
> genannten Extensions sind dort vorhanden. Der Debugger (`debugpy`) kommt mit der
> Python-Extension.

## 6. Projekt-Layout

```
deinapp/
├── src/deinapp/          # der Code (src-Layout, sauber importierbar)
│   ├── __main__.py       # Entrypoint & Modus-Dispatch
│   ├── core/             # paths, db, vdf, snapshot, wrapper, integrate
│   ├── adapters/         # steam  (später: lutris, heroic)
│   ├── daemon/           # watcher
│   └── gui/              # welcome  (später: GTK4-Oberfläche)
├── packaging/            # AppRun + build-appimage.sh
├── docs/                 # ARCHITECTURE, MODULES, ROADMAP, dieses File
├── tests/                # Beispiel-Tests gegen Fake-Umgebungen
├── pyproject.toml
└── README.md
```

## 7. Tests laufen lassen

```bash
pip install pytest
pytest                    # oder: python -m pytest tests/
```

Die mitgelieferten Tests bauen Fake-Steam-/Prefix-Umgebungen unter `/tmp` und
prüfen Discovery, Snapshot-Diff und DB-Idempotenz — kein echtes Steam nötig.

## 8. Wenn du am AppImage baust

Das AppImage baust du auf deinem Rechner (braucht `appimagetool` + Netzwerk):

```bash
./packaging/build-appimage.sh
```

Vorher in dem Skript `GH_OWNER`/`GH_REPO` auf dein Repo setzen und ein echtes
Icon (`deinapp.png`) hinterlegen. Details in `docs/MODULES.md`.
