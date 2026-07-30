#!/usr/bin/env bash
# Baut das AppImage. Setzt appimagetool voraus.
#
# Update-Info (zsync) wird eingebettet -> GearLever UND AppImageUpdate koennen
# selbststaendig aus deinem GitHub-Repo aktualisieren. Passe GH_OWNER/GH_REPO an.
#
# VERIFY-ON-DEVICE: appimagetool-Version, Python-Bundling-Strategie und die
# GitHub-Release-URL an dein echtes Repo anpassen. Ohne Netzwerk hier nicht
# ausfuehrbar -- dieses Skript ist die Vorlage fuer deinen Build-Rechner/CI.
set -euo pipefail

GH_OWNER="dein-user"
GH_REPO="deinapp"
APP="DeinApp"
ARCH="x86_64"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="${ROOT}/build/${APP}.AppDir"

rm -rf "${BUILD}"
mkdir -p "${BUILD}/usr/lib/python" "${BUILD}/usr/bin"

# App-Code hineinlegen
cp -r "${ROOT}/src/deinapp" "${BUILD}/usr/lib/python/deinapp"

# AppRun
cp "${ROOT}/packaging/AppRun" "${BUILD}/AppRun"
chmod +x "${BUILD}/AppRun"

# Desktop-Datei + Icon (Icon-Platzhalter -> durch echtes ersetzen)
cat > "${BUILD}/${APP}.desktop" << EOF
[Desktop Entry]
Type=Application
Name=${APP}
Exec=AppRun
Icon=deinapp
Categories=Game;Utility;
Terminal=false
EOF
# Minimaler Icon-Platzhalter (durch echtes PNG ersetzen)
touch "${BUILD}/deinapp.png"

# Optional: gebuendeltes Python hier hineinkopieren (python-appimage o.ae.).
# Sonst nutzt AppRun das System-python3.

# Update-Info fuer Self-Update / GearLever
UPDATE_INFO="gh-releases-zsync|${GH_OWNER}|${GH_REPO}|latest|${APP}-*${ARCH}.AppImage.zsync"

echo "Baue ${APP}-${ARCH}.AppImage ..."
ARCH="${ARCH}" appimagetool \
  --updateinformation "${UPDATE_INFO}" \
  "${BUILD}" \
  "${ROOT}/build/${APP}-${ARCH}.AppImage"

echo "Fertig: build/${APP}-${ARCH}.AppImage"
echo "Die .zsync-Datei danebenlegen und beide als GitHub-Release hochladen."
