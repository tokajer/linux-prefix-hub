"""Single-Binary-Entrypoint mit mehreren Modi.

Passt zum AppImage-Konzept: EIN Artefakt, verschiedene Modi.
  (kein Arg)     -> GUI / Welcome (Erststart erkennt sich selbst)
  --wrapper CMD  -> Spielstart umhuellen (von Steam via Shim aufgerufen)
  --daemon       -> inotify-Watcher (von systemd via Shim aufgerufen)
  --integrate    -> Self-Setup erzwingen (Shims/systemd/Reloc)
  --scan         -> Steam-Discovery ausgeben (Debug/Uebersicht)
  --status       -> zeigt DB-Inhalt (was wurde ueber Spiele gelernt)
"""
from __future__ import annotations

import sys


def _cmd_scan() -> int:
    from .adapters import steam
    games = list(steam.iter_installed_games())
    if not games:
        print("Keine Steam-Spiele gefunden. Steam-Wurzeln pruefen "
              "(adapters/steam.py: STEAM_ROOT_CANDIDATES).")
        return 0
    print(f"{len(games)} Spiel(e) gefunden:\n")
    for g in sorted(games, key=lambda x: x["game_name"].lower()):
        status = "installiert" if g["installed"] else "laedt/teilw."
        pfx = "gestartet" if g["prefix_path"] else "nie gestartet"
        print(f"  {g['game_name']:<32} [{status}] [{pfx}] appid={g['app_id']}")
    return 0


def _cmd_status() -> int:
    from .core import db
    prefixes = db.load_prefixes()
    if not prefixes:
        print("Noch nichts gelernt. Spiele einmal ueber den Wrapper starten,")
        print("damit Speicherorte erkannt werden.")
        return 0
    for fp, entry in prefixes.items():
        print(f"\n{entry['game_name']} ({entry['source']}/{entry['app_id']})")
        print(f"  prefix: {entry['prefix_path']}")
        print(f"  managed: {entry.get('managed', False)}")
        for loc in entry.get("storage_locations", []):
            r = "umgeleitet" if loc.get("redirected") else "am Ort"
            print(f"    [{loc['type']:<7}] {loc['win_path']}  ({r})")
    return 0


def main() -> int:
    args = sys.argv[1:]

    if args and args[0] == "--wrapper":
        from .core import wrapper
        return wrapper.main(args[1:])

    if args and args[0] == "--daemon":
        from .daemon import watcher
        watcher.run()
        return 0

    if args and args[0] == "--integrate":
        from .core import integrate
        result = integrate.full_setup(enable_watcher=True)
        for k, v in result.items():
            print(f"{k:14s} {v}")
        return 0

    if args and args[0] == "--scan":
        return _cmd_scan()

    if args and args[0] == "--status":
        return _cmd_status()

    # Default: Welcome/GUI. Erststart erkennt sich ueber config.
    from .core import db
    from .gui import welcome
    cfg = db.load_config()
    if not cfg.get("setup_done"):
        welcome.run(interactive=sys.stdin.isatty())
    else:
        print("DeinApp ist eingerichtet.")
        print("  --scan    installierte Spiele anzeigen")
        print("  --status  gelernte Speicherorte anzeigen")
        print("(Die grafische Oberflaeche folgt als naechster Baustein.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
