# Roadmap

Die nächsten Bausteine in sinnvoller Reihenfolge, jeweils mit dem Kontext aus den
Design-Entscheidungen (damit du nicht neu überlegen musst, *warum* etwas so
gedacht ist).

Legende: ✅ fertig · 🔨 als Nächstes · 🔭 später

---

## ✅ Fundament (diese Iteration)

- Steam-Discovery (Multi-Library, ACF, Prefix + user_dir)
- Snapshot-Diff-Speicherorterkennung
- Prefix-DB (idempotent)
- Neu-Spiel-Watcher (inotify + Poll-Fallback)
- Self-Integration (Reloc + Shims + systemd, GearLever-aware)
- Terminal-Welcome, AppImage-Build-Vorlage

---

## 🔨 Lutris-Adapter

**Warum zuerst:** Lutris ist technisch *einfacher* als alles andere, weil es
echte Hooks in YAML hat — du musst keinen manuellen Nutzer-Schritt einbauen wie
bei Steam.

- Discovery: `~/.config/lutris/games/*.yml` lesen → `game.prefix` ist der echte
  Prefix-Pfad (nicht raten!), plus Spielname.
- Hook: `system.prelaunch_command` / `system.postexit_command` in die YAML
  schreiben, die auf `deinapp-wrapper` zeigen.
- `user_dir` ist bei Lutris meist `$USER` (nicht `steamuser`) — die vorhandene
  `steam.user_dir_for`-Logik (auflisten statt raten) taugt als Vorlage.
- Wrapper: `_steam_context()` verallgemeinern, damit auch ein Lutris-Kontext
  (anderer ENV/Übergabeweg) aufgelöst wird.

**Dateien:** neu `adapters/lutris.py`; kleine Änderung in `core/wrapper.py`.

---

## 🔭 Heroic-Adapter

- Discovery: `~/.config/heroic/GamesConfig/<appName>.json` (JSON, nicht YAML!).
  Felder: `prefixInstallPath` (Prefix-Anker), Spielname aus Store-Cache.
- Hook: Heroic unterstützt Wrapper + pre-launch-Scripts → in die JSON schreiben.
- Prefixe liegen standardmäßig unter `~/Games/Heroic/Prefixes/`, aber der echte
  Pfad steht pro Spiel in der JSON.

**Verifizieren:** Heroic ändert sein Config-Layout gelegentlich zwischen Major-
Versionen — gegen eine echte Installation prüfen, nicht auf Annahmen verlassen.

**Dateien:** neu `adapters/heroic.py`.

---

## 🔭 Optionale Hybrid-Umleitung

Das Konzept steht (siehe ARCHITECTURE.md, Abschnitt 3). Die DB ist mit
`redirected`-Flag pro Speicherort schon vorbereitet.

- `core/redirect.py`: `redirect_hybrid(pfx, user_dir, win_folder, target)`:
  1. Registry-Key setzen (`Z:` + Zielpfad) in `user.reg`.
  2. Symlink am physischen Ort als Fallback (vorhandene Daten erst rüberziehen).
- **Wichtig:** Prefix-Prozesse müssen aus sein, sonst überschreibt Wine die
  `user.reg`. Der Watcher kann First-Launch (`compatdata/*/pfx` erscheint) als
  sicheren Moment nutzen.
- Downloads-GUID (`{374DE290-…}`) gesondert behandeln — dort trägt oft nur der
  Symlink.
- Ein-Ordner-pro-Spiel-Prinzip: `~/AppData/<Spiel>/{Roaming,Local}` bzw.
  `~/Games/<Spiel>/` — *besser* als Windows, weil zentral.
- Umleitung im Wrapper *vor* dem Spielstart einklinken (idempotent, self-healing).

**Sonderfall:** Schreibt ein Spiel in den Installationsordner
(`steamapps/common/...`), ist das kein Shell-Folder → nur anzeigen, nicht
umleiten (oder Symlink, mit Warnung).

---

## 🔭 Grafische Oberfläche (GTK4 / libadwaita)

Auf der bestehenden Logik — die Trennung Logik/Darstellung in `gui/welcome.py`
ist genau dafür angelegt.

- Spieleliste („Cyberpunk 2077 · Speicherstände: ~/Games/… · [Verbinden]").
- **Keine** Wine/Prefix-Begriffe in der UI (Windows-Gefühl!).
- Welcome-Dialog: `choose_install_dir` durch einen Ordner-Dialog ersetzen, Rest
  wiederverwenden.
- „Verbinden"-Knopf für Steam: den Launch-Options-String setzen (bei
  geschlossenem Steam direkt in `localconfig.vdf`, sonst in Zwischenablage +
  Anleitung).
- PyGObject als Dependency; im AppImage bündeln oder System-GTK nutzen.

---

## 🔭 Install-Erlebnis-Layer

Baut auf Watcher + Discovery auf (fällt größtenteils „geschenkt" an):

- „Neu installiert"-Begrüßung mit Verbinden-Angebot (nutzt den Watcher).
- First-Launch-Vorbereitung: Prefix kontrolliert erzeugen (`wineboot`-artig),
  Umleitung setzen, *bevor* der Nutzer zum ersten Mal „richtig" spielt.
- Optional: Proton-Version pro Spiel setzen / ProtonDB-Tweaks vorschlagen.

**Grenze (ehrlich):** Steams eigenen Install-Dialog fasst man nicht an; VDF-
Schreiben braucht *geschlossenes* Steam, sonst überschreibt Steam beim Beenden.

---

## 🔭 Weitere Ideen (Backlog)

- **PCGamingWiki-Anbindung**: Speicherort-Sofort-Treffer ohne Spielen; verfeinert
  `snapshot._guess_type`.
- **Bottles-Adapter**: nur „erkannt, nicht verwaltet" anzeigen; volle Verwaltung
  nur bei Nachfrage (Gaming-Schnittmenge klein).
- **Generischer Prefix-Scan**: alles mit `user.reg` + `drive_c` als Prefix werten
  (custom Wine).
- **Steam-Cloud-Kollisionsschutz**: warnen, wenn umgeleitete Saves mit Cloud-Sync
  kollidieren könnten.
- **SQLite** statt JSON, wenn das Schema wächst (Signaturen in `db.py` beibehalten).
