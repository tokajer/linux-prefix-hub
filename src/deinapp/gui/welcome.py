"""Welcome-/Einrichtungs-Flow.

Fuers Fundament als Terminal-Version. Die grafische Version (GTK4/libadwaita)
setzt spaeter auf derselben Logik auf -- daher ist die Entscheidungslogik hier
von der Darstellung getrennt (choose_install_dir gibt nur den Pfad zurueck).

Design (mit dir abgestimmt):
  - GearLever erkannt? -> nichts fragen, nur bestaetigen.
  - Sonst: Default ~/.local/share/deinapp vorschlagen, "Aendern" optional.
  - Instabile Orte (Downloads/Desktop/tmp/Wechselmedien) -> warnen, nicht
    verbieten.
"""
from __future__ import annotations

import os
from pathlib import Path

from ..core import db, integrate, paths

UNSTABLE_HINTS = ("downloads", "desktop", "/tmp", "/media/", "/run/media/",
                  "/mnt/")


def is_unstable(path: Path) -> bool:
    p = str(path).lower()
    return any(h in p for h in UNSTABLE_HINTS)


def choose_install_dir(interactive: bool = True) -> Path:
    """Ermittelt den Installationsort. Default gewinnt bei Enter."""
    default = paths.DEFAULT_INSTALL_DIR
    if not interactive:
        return default

    print(f"\nInstallationsort [{default}]")
    print("  Enter = Standard uebernehmen, oder eigenen Pfad eingeben:")
    raw = input("> ").strip()
    if not raw:
        return default
    chosen = Path(os.path.expanduser(raw))
    if is_unstable(chosen):
        print(f"\n  Hinweis: '{chosen}' wird oft aufgeraeumt oder ist nicht "
              "immer eingehaengt.\n  Empfohlen ist der Standard, damit Steam "
              "deine Spiele zuverlaessig findet.")
        again = input("  Trotzdem verwenden? [j/N] ").strip().lower()
        if again != "j":
            return choose_install_dir(interactive=True)
    return chosen


def run(interactive: bool = True) -> dict[str, str]:
    print("=" * 52)
    print("  DeinApp - Einrichtung")
    print("=" * 52)

    gl = integrate.detect_gearlever()
    if gl:
        print(f"\nGearLever-Verwaltung erkannt ({gl}).")
        print("Ablage & Updates uebernimmt GearLever -- ich richte nur die")
        print("Steam-/systemd-Anbindung ein.")
        cfg = db.load_config()
        cfg["setup_done"] = True
        cfg["managed_by"] = "gearlever"
        db.save_config(cfg)
        result = integrate.full_setup(enable_watcher=True)
        _print_result(result)
        return result

    install_dir = choose_install_dir(interactive=interactive)
    cfg = db.load_config()
    cfg["install_dir"] = str(install_dir)
    cfg["setup_done"] = True
    cfg["managed_by"] = "self"
    db.save_config(cfg)

    print(f"\nRichte ein in: {install_dir}")
    result = integrate.full_setup(enable_watcher=True)
    _print_result(result)
    return result


def _print_result(result: dict[str, str]) -> None:
    print("\nEingerichtet:")
    for k, v in result.items():
        print(f"  {k:14s} {v}")
    print("\nNaechster Schritt fuer Steam-Spiele:")
    print("  Trage in den Launch-Options des Spiels ein:")
    print(f'    "{paths.WRAPPER_SHIM}" %command%')
    print("  (Spaeter macht das der 'Verbinden'-Knopf in der GUI fuer dich.)")
