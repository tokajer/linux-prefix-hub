# Architektur

Diese Datei erklärt das *Warum* hinter dem Aufbau — die Entscheidungen, die wir
getroffen haben, und die Fallstricke, die dahinterstehen. Wenn du in VSCodium
weiterbaust, lies das zuerst; es erspart dir, dieselben Sackgassen nochmal zu
durchdenken.

---

## Leitidee: „Windows-Gefühl"

Der Nutzer soll **nichts** von Prefixen, `steamuser`, `compatdata`, Z:-Laufwerken
oder `user.reg` wissen müssen. Er sieht Spiele, einen Speicherort und einen
„Spielen"-Knopf (in seinem gewohnten Launcher). Alle Linux/Wine-Begriffe werden
versteckt. Jede Design-Entscheidung unten dient diesem Ziel.

Zwei Konsequenzen daraus:

1. Der Nutzer bleibt in **seinem gewohnten Launcher** (Steam/Lutris/Heroic). Wir
   klinken uns unsichtbar dazwischen statt einen eigenen Launcher zu bauen.
2. **Erkennung vor Umleitung.** Der wertvollste Teil ist zu *wissen*, wo ein
   Spiel speichert. Das Umleiten ist optional und setzt darauf auf.

---

## Die vier tragenden Entscheidungen

### 1. Adapter-Muster: Quellen sind austauschbar, der Kern ist geteilt

```
┌─────────┐  ┌─────────┐  ┌─────────┐
│ Steam   │  │ Lutris  │  │ Heroic  │   ← Adapter (Discovery + Hook setzen)
│ Adapter │  │ Adapter │  │ Adapter │      unterschiedliche Config-Formate
└────┬────┘  └────┬────┘  └────┬────┘
     └───────────┼───────────┘
          ┌──────▼──────┐
          │  Core        │   ← identisch für alle Quellen
          │  (Wrapper)   │
          └──────┬───────┘
       ┌─────────┼─────────┐
   ┌───▼───┐ ┌───▼────┐ ┌──▼─────┐
   │Snapshot│ │Redirect│ │Prefix- │
   │        │ │(später)│ │DB      │
   └────────┘ └────────┘ └────────┘
```

Eine neue Quelle (z. B. Bottles) = **nur ein neuer Adapter**, der Core bleibt
unangetastet. Der Adapter macht immer dreierlei:
- **Discovery**: Spiele + Prefix + `user_dir` finden.
- **Hook setzen**: den Wrapper in die Launch-Mechanik der Quelle einklinken.
- **Kontext liefern**: beim Spielstart Prefix/Name an den Core übergeben.

Config-Formate pro Quelle:

| Quelle | Config | Hook-Mechanik | Manueller Schritt? |
|--------|--------|---------------|--------------------|
| Steam  | VDF (`appmanifest`, `libraryfolders`) | `%command%`-Wrapper in Launch-Options | **ja, einmalig** |
| Lutris | YAML (`~/.config/lutris/games/*.yml`) | `prelaunch_command` / `postexit_command` | nein |
| Heroic | JSON (`~/.config/heroic/GamesConfig/*.json`) | Wrapper / pre-launch-Script | nein |

Steam ist der Sonderfall: den einen Launch-Options-Schritt kann man nicht
wegzaubern (Steams Install-/Config-UI ist eine Blackbox). Lutris und Heroic
klinken sich selbst ein.

### 2. Speicherort-Erkennung per Snapshot-Diff

Es gibt **keine API**, die sagt „Spiel X speichert in Y". Die Info ist über
mind. vier Orte verstreut: `AppData/Roaming|Local`, `Documents/My Games`,
`Saved Games`, teils im Installationsordner selbst oder in Steam Cloud.

Unser zuverlässigster Weg: **Snapshot vor Spielstart, Snapshot nach Spielende,
Diff.** Was sich geändert hat, ist ein Speicherort. Das findet auch exotische
Orte ohne Vorwissen („Learn"-Modus). Deshalb wrappt der Core den Spielstart —
so bekommt er die Snapshots automatisch, ohne dass der Nutzer „Snapshot"-Knöpfe
drückt.

Ergänzbar (später) durch **PCGamingWiki**-Daten für Sofort-Treffer ohne Spielen,
und eine Heuristik (`*.sav`, `My Games`, kürzlich geänderte Ordner).

**Sonderfall Installationsordner:** Schreibt ein Spiel in seinen eigenen
`steamapps/common/<Spiel>/`-Ordner, ist das *kein* Shell-Folder → per Registry
nicht umleitbar. Wichtig, dass die App das *erkennt und anzeigt*, auch wenn sie
es nicht umleiten kann.

### 3. Umleitung: Hybrid (Registry + Symlink) — optional, kommt später

Noch nicht implementiert, aber das Konzept steht fest, damit die DB schon darauf
vorbereitet ist (`redirected`-Flag pro Speicherort):

- **Registry** (`user.reg`, Keys unter `Shell Folders` / `User Shell Folders`)
  ist der „offizielle" Weg — Wine/Proton wissen dann, wo die Ordner liegen.
- **Symlink** am physischen Ort im Prefix ist das **Sicherheitsnetz**: manche
  Spiele ignorieren die Registry und schreiben stur nach
  `C:\users\steamuser\AppData\...`. Der Symlink fängt das ab.

Beide zeigen auf **dasselbe Ziel** im Home → kein Konflikt, und das Ganze ist
**idempotent + selbstheilend**: Ein Proton-Update, das den Symlink killt, ist
egal, weil die Daten im Home liegen und beim nächsten Start neu verlinkt werden.

Downloads hat als Registry-Key eine **GUID**
(`{374DE290-123F-4565-9164-39C4925E467B}`), keinen Klartext-Namen — hier ist der
Symlink-Fallback doppelt wertvoll.

### 4. Verpackung: AppImage mit fester Shim-Schicht

Das Kernproblem von AppImage für *unsere* App: Es klinkt sich tief ein (Steam-
Launch-Options, systemd-Daemon), aber ein AppImage wird nach `/tmp/.mount_XXXXXX`
gemountet — **flüchtig und bei jedem Start anders benannt**. Ein fester
Steam-Eintrag kann da nicht draufzeigen.

**Lösung — fester Shim, wanderndes AppImage:**

```
Steam Launch-Options → ~/.local/bin/deinapp-wrapper   (fester Pfad, nie geändert)
                              │
                              ▼
                       ~/.local/share/deinapp/DeineApp.AppImage  (fester Ort)
```

- Das AppImage **kopiert sich beim ersten Start** an den festen Ort
  (`~/.local/share/deinapp/`) und läuft von dort. Downloads/Desktop sind nur der
  Ablageort für den allerersten Start und dürfen danach weg.
- Die **Shims** (`~/.local/bin/deinapp-{wrapper,daemon}`) sind winzige
  Weiterleitungen an den festen Ort. Nur sie müssen persistent draußen liegen —
  weil etwas, das *nur im AppImage* lebt, per Definition nicht existiert, wenn
  das AppImage nicht läuft (und der Shim ist ja der, der es startet).
- **Self-Heal:** `AppRun` ruft bei jedem Normalstart `--integrate` auf, damit
  Shims/Unit auch dann wiederhergestellt werden, wenn der Nutzer mal aus `/tmp`
  gestartet hat.

**GearLever:** wird *erkannt und respektiert* (wenn es das AppImage schon an
einen festen Ort integriert hat, relozieren wir nicht selbst), aber **nicht
vorausgesetzt** — sonst bräuchte der Nutzer Flatpak + GearLever + AppImage, was
dem Einfachheits-Ziel widerspricht. Wer GearLever hat, profitiert von dessen
Updates (wir betten zsync-Update-Info ein); wer nicht, wird von der App selbst
integriert.

**Warum nicht Flatpak/nativ?** Flatpak-Sandbox müsste man für Steam-/Lutris-/
Prefix-Zugriff so weit aufbohren, dass die Sandbox kaum noch Sinn ergibt, und
Host-systemd aus Flatpak ist fummelig. AppImage mit Shim-Schicht passt besser.
`pipx` bleibt der bequeme Dev-Weg.

---

## Datenfluss beim Spielstart (der Wrapper)

```
Steam "Spielen" → deinapp-wrapper %command%
                        │
   ┌────────────────────┼─────────────────────────┐
   │ 1. SteamAppId aus ENV lesen (Steam setzt sie!)│
   │ 2. Prefix + user_dir über Discovery finden    │
   │ 3. Snapshot VORHER                             │
   │ 4. exec echtes Spiel-Command, warten           │
   │ 5. Snapshot NACHHER → Diff → Speicherorte      │
   │ 6. in Prefix-DB schreiben (merge, idempotent)  │
   └────────────────────────────────────────────────┘
```

Der Wrapper ist **read-only fürs Spiel** — er beobachtet nur, verändert nichts
am Spielverhalten. Risikofrei. Umleitung wäre ein *zusätzlicher* Schritt vor 4.

---

## Neu-Spiel-Erkennung (der Watcher)

Steam legt beim Installieren `appmanifest_<appid>.acf` an. Das Feld
`StateFlags` unterscheidet „lädt noch" (z. B. `1026`) von „fertig installiert"
(`& 4`). Der Watcher lauscht per **inotify** auf alle `steamapps`-Ordner (Multi-
Library!) und meldet neu fertiggestellte Spiele per Desktop-Notification.

Fällt inotify (Paket `inotify_simple`) aus, gibt es einen **Poll-Fallback**, damit
das Fundament auch dependency-frei läuft.

Derselbe Watcher kann später auch das Auftauchen von `compatdata/<appid>/pfx`
erkennen (= zum ersten Mal gestartet) — der Moment für die optionale Umleitung.

---

## Idempotenz — die wichtigste Invariante

Sowohl Discovery-Scans als auch der Wrapper schreiben über `db.upsert_prefix()`.
Diese Funktion **merged** und bewahrt Nutzer-Entscheidungen: ein erneuter Scan
darf `redirected` (pro Speicherort) und `managed` (pro Prefix) **nie**
zurücksetzen. Wenn du neue Felder hinzufügst, die der Nutzer steuert, ziehe sie
in denselben Bewahr-Mechanismus (siehe `core/db.py`).

---

## Fingerprint statt Quellen-Logik

Jeder Prefix wird über `sha256(realpath(prefix))[:16]` identifiziert
(`db.fingerprint`). Dadurch ist es egal, *wer* den Prefix erzeugt hat — Steam,
Lutris, Heroic oder custom. Ein Prefix ist immer an `system.reg` + `user.reg` +
`drive_c/` erkennbar; das ist der universelle Anker für einen späteren
generischen „finde alle Prefixe"-Scan.
