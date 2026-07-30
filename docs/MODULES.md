# Modul-Referenz

Jede Datei im Projekt, was sie tut, ihre öffentlichen Funktionen und worauf beim
Weiterbauen zu achten ist.

---

## `src/deinapp/__main__.py` — Entrypoint & Modus-Dispatch

Das Single-Binary-Konzept: **ein** Einstieg, mehrere Modi. Passt zum AppImage
(ein Artefakt) und zu `pipx` (ein `deinapp`-Command).

| Aufruf | Funktion | Wer ruft es auf |
|--------|----------|-----------------|
| `deinapp` | Welcome (Erststart) / Übersicht | Nutzer / Desktop-Icon |
| `deinapp --wrapper CMD…` | `core.wrapper.main` | Steam via Shim |
| `deinapp --daemon` | `daemon.watcher.run` | systemd via Shim |
| `deinapp --integrate` | `core.integrate.full_setup` | AppRun (Self-Heal) |
| `deinapp --scan` | Steam-Discovery ausgeben | Nutzer (Debug) |
| `deinapp --status` | Prefix-DB ausgeben | Nutzer (Debug) |

**Weiterbauen:** Neue Modi hier als weiteres `if args[0] == "--…"` ergänzen.
Imports bewusst *innerhalb* der Zweige (lazy), damit z. B. der Wrapper-Pfad nicht
GUI-Abhängigkeiten lädt und schnell startet.

---

## `src/deinapp/core/paths.py` — zentrale Pfade

Alle festen Orte an **einer** Stelle. XDG-konform:
- `DEFAULT_INSTALL_DIR` = `~/.local/share/deinapp/` (AppImage + Daten)
- `CONFIG_DIR` = `~/.config/deinapp/` (config, DB, Snapshots)
- `LOCAL_BIN` = `~/.local/bin/` (Shims — absolut referenziert, nicht via PATH)
- `WATCHER_UNIT` = systemd-user-Unit

`ensure_dirs()` legt alles an. `installed_appimage_path(install_dir)` liefert den
festen AppImage-Ort (Default oder aus config gewählt).

**Weiterbauen:** Neue persistente Pfade *immer* hier definieren, nie hart im Code
verstreuen. Das hält das Layout überschaubar und die XDG-Trennung sauber.

---

## `src/deinapp/core/vdf.py` — VDF/ACF-Parser

Dependency-freier Parser für Valves KeyValues-Textformat (`appmanifest_*.acf`,
`libraryfolders.vdf`). `loads(text) -> dict`.

**Grenzen:** Nur Text-KeyValues; binäre VDFs (manche `localconfig`-Varianten)
kann er nicht. Für Produktiv das PyPI-Paket `vdf` als robusteren Ersatz nutzen
(`pip install .[full]`) — API ist kompatibel genug, um im Adapter umzuschalten.

**Verifizieren:** Gegen echte Manifeste mit Verschachtelung/Escapes testen.

---

## `src/deinapp/core/db.py` — Prefix-DB & Config

JSON-Persistenz in `~/.config/deinapp/`. Atomare Writes (tmp + replace).

Öffentliche Funktionen:
- `fingerprint(prefix_path)` — stabiler Prefix-Identifier.
- `load_config()` / `save_config(cfg)` / `install_dir()`.
- `load_prefixes()` / `save_prefixes(db)`.
- `upsert_prefix(entry) -> fingerprint` — **der wichtige Teil.** Merged und
  bewahrt `redirected`/`managed` (Idempotenz-Invariante, siehe ARCHITECTURE.md).
- `get_prefix(fp)`.

**Weiterbauen:** Wenn das Schema wächst und die JSON-Struktur unhandlich wird, ist
SQLite der nächste Schritt — aber die Funktionssignaturen oben so lassen, dann
bleibt der Rest des Codes unberührt. Nutzer-gesteuerte Felder in den
Bewahr-Mechanismus von `upsert_prefix` aufnehmen.

---

## `src/deinapp/core/snapshot.py` — Speicherort-Erkennung

- `snapshot(prefix_path, user_dir) -> {rel_path: mtime}` — scannt nur die
  relevanten Zweige (`INTERESTING_SUBTREES`: AppData/*, Documents, Saved Games,
  Downloads) unter `drive_c/users/<user_dir>/`.
- `diff(before, after) -> [rel_path]` — neu/geänderte Dateien.
- `classify_locations(changed) -> [location_dict]` — aggregiert auf Verzeichnis-
  ebene und rät den `type` (saves/config/unknown).

**Weiterbauen:**
- `INTERESTING_SUBTREES` erweitern, falls Spiele woanders schreiben.
- `_guess_type` ist grob — mit PCGamingWiki-Daten verfeinerbar.
- Für sehr große Prefixe könnte der `rglob` langsam werden; ggf. auf mtime der
  Verzeichnisse vorfiltern.

---

## `src/deinapp/core/wrapper.py` — Spielstart-Wrapper

`main(argv) -> exit_code`. `argv` = das echte Spiel-Command (hinter `%command%`).
Ablauf: Kontext aus `SteamAppId`-ENV → Snapshot vorher → Spiel starten & warten →
Snapshot nachher → Diff → `db.upsert_prefix`.

**Read-only fürs Spiel** — verändert das Spielverhalten nicht.

**Weiterbauen:**
- `_steam_context()` liest `SteamAppId`/`STEAM_COMPAT_APP_ID`. Für Lutris/Heroic
  einen analogen Kontext-Resolver ergänzen (die setzen andere ENV/übergeben den
  Kontext anders) und `main` quellenagnostisch machen.
- Hier käme später der **optionale Umleitungs-Schritt** *vor* dem Spielstart rein.

---

## `src/deinapp/adapters/steam.py` — Steam-Discovery

- `find_steam_roots()` — häufige Steam-Wurzeln (nativ + Flatpak), dedupliziert.
- `find_library_dirs()` — **alle** `steamapps` über alle Platten
  (`libraryfolders.vdf`). Der Multi-Library-Teil ist entscheidend, sonst werden
  Spiele auf der zweiten Platte übersehen.
- `iter_installed_games()` — yield dict pro Spiel (appid, name, installed,
  state_flags, prefix_path, game_dir).
- `_prefix_for(steamapps, appid)` — Proton-Prefix, `None` wenn nie gestartet.
- `user_dir_for(prefix_path)` — listet `drive_c/users` auf (nicht raten),
  bevorzugt `steamuser`.

**Verifizieren (VERIFY-ON-DEVICE):**
- `STEAM_ROOT_CANDIDATES` je Distro/Flatpak ergänzen.
- `StateFlags & 4 = installiert` an echten Manifesten prüfen.

---

## `src/deinapp/daemon/watcher.py` — Neu-Spiel-Watcher

- `run()` — inotify auf alle `steamapps`, meldet neu installierte Spiele.
- `run_poll(interval)` — Fallback ohne `inotify_simple`.
- `_notify(title, body)` — via `notify-send` (best effort).
- `_scan_once(known)` / `_load_known()` / `_save_known()`.

Beim allerersten Lauf werden bestehende Spiele als „bekannt" markiert (nicht
gemeldet), damit nicht die ganze Bibliothek als „neu" aufpoppt.

**Verifizieren:** `notify-send`/D-Bus aus systemd-user-Kontext auf dem Zielsystem.

**Weiterbauen:** Zusätzlich auf `compatdata/*/pfx`-Erscheinen lauschen
(= First-Launch) → Trigger für optionale Umleitung.

---

## `src/deinapp/core/integrate.py` — Self-Integration

- `running_as_appimage()` — liest `$APPIMAGE` (von AppRun gesetzt).
- `detect_gearlever()` — erkennt GearLever-Verwaltung (Ablageort-Heuristik).
- `relocate_appimage()` — kopiert das AppImage an den festen Ort (Dev/GearLever:
  no-op).
- `install_shims()` — schreibt `deinapp-wrapper` + `deinapp-daemon` (zeigen fest
  aufs AppImage bzw. im Dev-Modus auf `python -m deinapp`).
- `install_systemd_unit(enable)` — schreibt + aktiviert die Watcher-Unit.
- `full_setup(enable_watcher)` — alles zusammen, **idempotent**.

**Verifizieren:** GearLever-Zielordner (konfigurierbar), systemd-`--user`-
Verfügbarkeit.

**Weiterbauen:** Die Shims sind bewusst dumm. Wenn du den Einstieg änderst
(z. B. anderes Modul), nur `_shim_body` anpassen — die festen Pfade bleiben.

---

## `src/deinapp/gui/welcome.py` — Einrichtungs-Flow (Terminal)

Trennt **Entscheidungslogik** von Darstellung, damit die spätere GTK-GUI
dieselbe Logik nutzt:
- `choose_install_dir(interactive)` — Default `~/.local/share/deinapp`, „Ändern"
  optional, **warnt bei instabilen Orten** (`is_unstable`: Downloads/Desktop/
  tmp/Wechselmedien).
- `run(interactive)` — GearLever-Erkennung → sonst Pfadwahl → `full_setup`.

**Weiterbauen:** Für die grafische Version `choose_install_dir` durch einen
Dialog ersetzen, `run` als Kontroller wiederverwenden. `is_unstable` und der
Default-Pfad bleiben gleich.

---

## `packaging/AppRun` — AppImage-Einstieg

Setzt `PYTHONPATH`, reicht Sondermodi direkt durch (schnell), und ruft bei
Normalstart leise `--integrate` auf (Self-Heal). Nutzt gebündeltes Python, sonst
System-`python3`.

## `packaging/build-appimage.sh` — Build-Vorlage

Baut die AppDir, bettet **zsync-Update-Info** ein (GitHub-Releases → GearLever &
AppImageUpdate). `GH_OWNER`/`GH_REPO`/Icon/Python-Bundling an dein Repo anpassen.
Ohne Netzwerk hier nicht ausführbar — Vorlage für deinen Build-Rechner/CI.
