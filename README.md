# DeinApp — Prefix- & Speicherstand-Verwaltung mit Windows-Gefühl

Findet deine Steam/Proton-Spiele, lernt beim Spielen **wo sie speichern**, und
kann diese Orte optional zentral ins Home umleiten — ohne dass du dich mit
Prefixen, `steamuser` oder `%appdata%` herumschlagen musst.

Dies ist das **Fundament** (erste Iteration): Steam-Discovery, Speicherort-
Erkennung, Prefix-DB, Neu-Spiel-Watcher und das AppImage-Integrations-Konzept.
Lutris/Heroic-Adapter, die optionale Umleitung und die grafische Oberfläche
setzen darauf auf.

## Was schon funktioniert

- **Steam-Discovery** über alle Libraries (`libraryfolders.vdf`), inkl.
  installiert-vs-lädt (`StateFlags`) und Prefix/`user_dir`-Erkennung.
- **Speicherort-Lernen** per Snapshot-Diff: Der Wrapper schnappt vor/nach dem
  Spiel und erkennt, welche Ordner sich geändert haben.
- **Prefix-DB** (`~/.config/deinapp/prefixes.json`), idempotent — ein erneuter
  Scan überschreibt deine `redirected`/`managed`-Entscheidungen nicht.
- **Neu-Spiel-Watcher** (inotify, mit Poll-Fallback) → Desktop-Notification.
- **Self-Integration**: AppImage kopiert sich an einen festen Ort, legt feste
  Shims (`~/.local/bin`) und eine systemd-Unit an. GearLever wird erkannt und
  respektiert, aber **nicht vorausgesetzt**.

## Modi (ein Binary, wie beim AppImage)

```
deinapp              # Welcome/Einrichtung (Erststart), sonst Übersicht
deinapp --scan       # installierte Steam-Spiele anzeigen
deinapp --status     # gelernte Speicherorte anzeigen
deinapp --wrapper …  # Spielstart umhüllen  (Steam ruft das via Shim)
deinapp --daemon     # Neu-Spiel-Watcher    (systemd ruft das via Shim)
deinapp --integrate  # Shims/systemd/Reloc erzwingen (idempotent)
```

## Steam-Spiel verbinden

In den **Launch-Options** des Spiels eintragen:

```
"$HOME/.local/bin/deinapp-wrapper" %command%
```

(Später übernimmt das der „Verbinden"-Knopf in der GUI — bei geschlossenem
Steam direkt in die `localconfig.vdf` geschrieben.)

## Schnellstart (Entwicklung)

```bash
pipx install .            # oder: pip install -e .
deinapp --scan
```

## Layout

```
~/.local/share/deinapp/DeineApp.AppImage   # das Binary (fester Ort)
~/.local/bin/deinapp-wrapper               # Shim für Steam
~/.local/bin/deinapp-daemon                # Shim für systemd
~/.config/deinapp/                         # config, prefixes.json, snapshots
~/.config/systemd/user/deinapp-watcher.service
```

## ⚠️ Auf deinem Gerät noch prüfen (VERIFY-ON-DEVICE)

Diese Punkte konnte ich ohne Netzwerk/echtes Steam nicht abschließend testen —
sie sind im Code an den jeweiligen Stellen kommentiert:

1. **Steam-Wurzeln** (`adapters/steam.py: STEAM_ROOT_CANDIDATES`) — je nach
   Distro/Flatpak ergänzen.
2. **`StateFlags`-Semantik** — `& 4 = installiert` an echten `appmanifest_*.acf`
   gegenprüfen (Valve dokumentiert das nicht offiziell).
3. **VDF-Parser** — gegen echte `libraryfolders.vdf` mit mehreren Platten testen;
   für den Produktivbetrieb das `vdf`-PyPI-Paket als robusteren Parser nutzen
   (`pip install .[full]`).
4. **Desktop-Notifications aus systemd-user** — brauchen erreichbaren D-Bus
   (`DBUS_SESSION_BUS_ADDRESS`). `notify-send` auf deinem Desktop testen.
5. **GearLever-Zielordner** (`core/integrate.py: detect_gearlever`) — ist
   konfigurierbar; ggf. an deine Installation anpassen.
6. **AppImage-Build** (`packaging/build-appimage.sh`) — `appimagetool`,
   Python-Bundling und die GitHub-Release-URL an dein echtes Repo anpassen.

## Dokumentation

- **`docs/ARCHITECTURE.md`** — das *Warum*: Design-Entscheidungen, Fallstricke,
  Datenfluss. Zuerst lesen.
- **`docs/MODULES.md`** — jede Datei, ihre Funktionen, worauf beim Weiterbauen zu
  achten ist.
- **`docs/DEVELOPING.md`** — Setup in VSCodium, venv, Debuggen, Tests.
- **`docs/ROADMAP.md`** — die nächsten Bausteine mit Kontext.
- **`CONTRIBUTING.md`** — Konventionen.

## Nächste Bausteine (in Reihenfolge)

1. Lutris-Adapter (YAML-Discovery + pre/post-Hook-Injection).
2. Heroic-Adapter (JSON-Discovery unter `~/.config/heroic/GamesConfig/`).
3. Optionale **Hybrid-Umleitung** (Registry + Symlink-Fallback), pro Ort
   aktivierbar.
4. Grafische Oberfläche (GTK4/libadwaita) auf der bestehenden Logik.
5. Install-Erlebnis-Layer (Verbinden-Knopf, First-Launch-Vorbereitung).
