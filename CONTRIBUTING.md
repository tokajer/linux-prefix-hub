# Mitentwickeln

Kurze Konventionen, damit der Code kohärent bleibt.

## Struktur-Regeln

- **Neue Quelle** (Launcher) = neuer Adapter in `src/deinapp/adapters/`. Der Core
  bleibt unangetastet. Ein Adapter macht: Discovery, Hook setzen, Kontext liefern.
- **Persistente Pfade** immer in `core/paths.py` definieren, nie im Code verstreut.
- **Nutzer-Entscheidungen** (`redirected`, `managed`, …) müssen einen Scan
  überleben → in den Bewahr-Mechanismus von `db.upsert_prefix` aufnehmen.
- **Darstellung von Logik trennen** (siehe `gui/welcome.py`), damit die spätere
  GTK-GUI dieselbe Logik nutzt.

## Windows-Gefühl (Produkt-Prinzip)

Keine Wine/Prefix/Proton-Begriffe in nutzersichtbaren Texten. Der Nutzer sieht
Spiele, Speicherorte, „Verbinden"/„Spielen". Technik bleibt unsichtbar.

## Code-Stil

- Zeilenlänge 79 (siehe `.vscode/settings.json`, Ruler).
- `from __future__ import annotations` oben, Typannotationen erwünscht.
- Defensiv gegen kaputte Fremd-Dateien (VDF/YAML/JSON): ein fehlerhaftes Manifest
  darf nie die ganze Discovery sprengen (try/except, weitermachen).
- Lazy imports in `__main__.py`-Zweigen (schneller Start pro Modus).

## Tests

- Gegen **Fake-Umgebungen** unter `tmp_path` testen, kein echtes Steam.
- Neue Adapter: mindestens Discovery + Hook-Injection testen.
- `pytest` lokal grün, bevor committed wird.

## VERIFY-ON-DEVICE

Alles, was echtes Steam/Desktop/Netzwerk braucht, ist im Code mit
`VERIFY-ON-DEVICE` markiert und im README gesammelt. Wenn du so etwas hinzufügst,
markiere es genauso — das hält transparent, was noch auf echter Hardware zu
prüfen ist.

## Commit-Stil (Vorschlag)

Kurz, im Imperativ, mit Bereich: `steam: Multi-Library-Discovery`,
`redirect: Hybrid-Umleitung (Registry + Symlink)`, `docs: Roadmap aktualisiert`.
