"""Self-Integration: der einmalige Einrichtungs-Schritt.

Loest das wandernde-Pfad-Problem: das AppImage kopiert sich an einen festen
Ort, und Steam/systemd zeigen nur auf feste Shims -- nie direkt aufs
(evtl. wandernde) AppImage.

Ablauf (idempotent -- bei jedem Start pruefbar):
  1. GearLever erkennen. Wenn GearLever die App schon an einen festen Ort
     integriert hat, respektieren wir das und relozieren NICHT selbst.
  2. Sonst: AppImage nach install_dir kopieren (falls nicht schon dort).
  3. Shims in ~/.local/bin anlegen (zeigen fest aufs AppImage am festen Ort).
  4. systemd-user-Unit fuer den Watcher anlegen + aktivieren.

Wir laufen evtl. NICHT als AppImage (z.B. via pipx im Dev). Dann ueberspringen
wir die AppImage-Reloc und legen Shims an, die den Dev-Entrypoint aufrufen.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from . import db, paths


def running_as_appimage() -> str | None:
    """Gibt den AppImage-Pfad zurueck, wenn wir als AppImage laufen."""
    return os.environ.get("APPIMAGE")  # von AppRun gesetzt


def detect_gearlever() -> Path | None:
    """Erkennt, ob GearLever die App bereits integriert hat.

    Heuristik: GearLever legt AppImages typischerweise unter
    ~/.local/share/AppImages/ (oder ~/AppImages/) ab. Wenn unser AppImage
    von dort laeuft, gehen wir von GearLever-Verwaltung aus und relozieren
    nicht selbst.

    VERIFY-ON-DEVICE: GearLevers Zielordner ist konfigurierbar; ggf. an die
    tatsaechliche Installation anpassen.
    """
    appimg = running_as_appimage()
    if not appimg:
        return None
    real = Path(os.path.realpath(appimg))
    gearlever_dirs = [
        Path.home() / ".local" / "share" / "AppImages",
        Path.home() / "AppImages",
        Path.home() / "Applications",
    ]
    for gd in gearlever_dirs:
        try:
            if gd in real.parents:
                return real
        except (OSError, ValueError):
            continue
    return None


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    st = path.stat().st_mode
    path.chmod(st | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _target_appimage() -> Path:
    """Wo das maßgebliche AppImage liegt (oder liegen soll)."""
    gl = detect_gearlever()
    if gl:
        return gl
    return paths.installed_appimage_path(db.install_dir())


def relocate_appimage() -> Path | None:
    """Kopiert das laufende AppImage an den festen install_dir-Ort.

    Rueckgabe: der feste Zielpfad, oder None wenn wir nicht als AppImage
    laufen (Dev-Modus) oder GearLever das uebernimmt.
    """
    if detect_gearlever():
        # GearLever verwaltet Ablage & Updates -> nichts kopieren.
        return _target_appimage()

    src = running_as_appimage()
    if not src:
        return None  # Dev-Modus (pipx): keine Reloc noetig

    target = paths.installed_appimage_path(db.install_dir())
    target.parent.mkdir(parents=True, exist_ok=True)

    src_real = os.path.realpath(src)
    if os.path.realpath(target) == src_real:
        return target  # laeuft bereits vom festen Ort

    shutil.copy2(src_real, target)
    st = target.stat().st_mode
    target.chmod(st | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def _shim_body(mode: str) -> str:
    """Shim-Inhalt. Ruft AppImage (fester Ort) im gegebenen Modus auf,
    oder im Dev-Modus den Python-Entrypoint."""
    appimg = _target_appimage()
    if running_as_appimage() or appimg.exists():
        return (
            "#!/usr/bin/env bash\n"
            f'exec "{appimg}" --{mode} "$@"\n'
        )
    # Dev-Modus: auf aktuellen Python-Interpreter + Modul zeigen
    py = sys.executable
    return (
        "#!/usr/bin/env bash\n"
        f'exec "{py}" -m deinapp --{mode} "$@"\n'
    )


def install_shims() -> tuple[Path, Path]:
    _write_executable(paths.WRAPPER_SHIM, _shim_body("wrapper"))
    _write_executable(paths.DAEMON_SHIM, _shim_body("daemon"))
    return paths.WRAPPER_SHIM, paths.DAEMON_SHIM


def install_systemd_unit(enable: bool = True) -> Path:
    unit = (
        "[Unit]\n"
        "Description=DeinApp Steam-Watcher (neue Spiele erkennen)\n"
        "After=graphical-session.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={paths.DAEMON_SHIM}\n"
        "Restart=on-failure\n"
        "RestartSec=10\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    paths.WATCHER_UNIT.parent.mkdir(parents=True, exist_ok=True)
    paths.WATCHER_UNIT.write_text(unit, encoding="utf-8")

    if enable:
        try:
            subprocess.run(["systemctl", "--user", "daemon-reload"],
                           check=False, timeout=10)
            subprocess.run(["systemctl", "--user", "enable", "--now",
                            paths.WATCHER_UNIT.name],
                           check=False, timeout=10)
        except (FileNotFoundError, subprocess.SubprocessError):
            # Kein systemd (--user) verfuegbar -> Unit-Datei liegt bereit,
            # Nutzer kann manuell aktivieren. Auf Zielsystem pruefen.
            pass
    return paths.WATCHER_UNIT


def full_setup(enable_watcher: bool = True) -> dict[str, str]:
    """Kompletter Einrichtungslauf. Idempotent."""
    paths.ensure_dirs()
    appimg = relocate_appimage()
    wrapper, daemon = install_shims()
    unit = install_systemd_unit(enable=enable_watcher)
    return {
        "appimage": str(appimg) if appimg else "(dev-mode, keine reloc)",
        "gearlever": str(detect_gearlever() or "nicht erkannt"),
        "wrapper_shim": str(wrapper),
        "daemon_shim": str(daemon),
        "systemd_unit": str(unit),
    }
