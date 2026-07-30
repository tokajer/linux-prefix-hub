#!/usr/bin/env bash
# Build the AppImage. Works locally and on GitHub Actions.
#
# What it does:
#   1. fetch a relocatable CPython AppImage (python-appimage) as the base
#   2. unpack it, drop our package into its site-packages
#   3. replace AppRun / desktop file / icon with ours
#   4. repack with appimagetool, embedding zsync update information
#
# The embedded update information is what makes both GearLever and
# AppImageUpdate able to update the app on their own -- and our own
# `--update` uses the same GitHub release.
#
# Requirements: curl, and either FUSE or (as used here) the extract-and-run
# fallback that needs nothing at all.
#
# Usage:  ./packaging/build-appimage.sh [version]
set -euo pipefail

APP="LinuxPrefixHub"
GH_OWNER="${GH_OWNER:-tokajer}"
GH_REPO="${GH_REPO:-linux-prefix-hub}"
ARCH="${ARCH:-$(uname -m)}"
PY_SERIES="${PY_SERIES:-3.12}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="${ROOT}/build"
APPDIR="${BUILD}/${APP}.AppDir"

VERSION="${1:-}"
if [ -z "${VERSION}" ]; then
  VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' \
    "${ROOT}/src/linux_prefix_hub/__init__.py")"
fi
VERSION="${VERSION#v}"
OUT="${BUILD}/${APP}-${VERSION}-${ARCH}.AppImage"

# AppImages cannot be mounted on most CI runners (no FUSE) -- this makes the
# runtime unpack itself to a temp dir instead. Harmless everywhere else.
export APPIMAGE_EXTRACT_AND_RUN=1

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

rm -rf "${BUILD}"
mkdir -p "${BUILD}"

# --- 1. base interpreter --------------------------------------------------
say "Fetching CPython ${PY_SERIES} base AppImage (${ARCH})"
PY_TAG="python${PY_SERIES}"
PY_PATTERN="manylinux2014_${ARCH}.AppImage"
PY_URL="$(curl -fsSL \
  "https://api.github.com/repos/niess/python-appimage/releases/tags/${PY_TAG}" \
  | grep -o "https://[^\"]*${PY_PATTERN}" | head -n1)"
if [ -z "${PY_URL}" ]; then
  echo "No python-appimage build for ${PY_TAG}/${ARCH}" >&2
  exit 1
fi
curl -fsSL -o "${BUILD}/python.AppImage" "${PY_URL}"
chmod +x "${BUILD}/python.AppImage"

say "Unpacking base"
( cd "${BUILD}" && ./python.AppImage --appimage-extract >/dev/null )
mv "${BUILD}/squashfs-root" "${APPDIR}"
rm -f "${BUILD}/python.AppImage"

# --- 2. our code ----------------------------------------------------------
say "Installing linux_prefix_hub into the bundle"
SITE_PACKAGES="$(find "${APPDIR}/opt" -maxdepth 4 -type d -name site-packages \
  | head -n1)"
if [ -z "${SITE_PACKAGES}" ]; then
  echo "site-packages not found in the base AppImage" >&2
  exit 1
fi
rm -rf "${SITE_PACKAGES}/linux_prefix_hub"
cp -r "${ROOT}/src/linux_prefix_hub" "${SITE_PACKAGES}/linux_prefix_hub"
find "${SITE_PACKAGES}/linux_prefix_hub" -name '__pycache__' -type d \
  -exec rm -rf {} + 2>/dev/null || true
# Keep a copy on PYTHONPATH too, so AppRun still works if the layout changes.
mkdir -p "${APPDIR}/usr/lib/python"
cp -r "${ROOT}/src/linux_prefix_hub" "${APPDIR}/usr/lib/python/"

# --- 3. AppDir metadata ---------------------------------------------------
say "Writing AppRun, desktop entry and icon"
# The base image ships python's own desktop file and icon: appimagetool wants
# exactly one of each, so drop them.
rm -f "${APPDIR}"/*.desktop "${APPDIR}"/*.png "${APPDIR}/.DirIcon"
rm -rf "${APPDIR}/usr/share/applications" \
       "${APPDIR}/usr/share/metainfo"

install -m 755 "${ROOT}/packaging/AppRun" "${APPDIR}/AppRun"

cat > "${APPDIR}/linux-prefix-hub.desktop" << EOF
[Desktop Entry]
Type=Application
Name=Linux Prefix Hub
Comment=Manage where your games store saves
Exec=AppRun
Icon=linux-prefix-hub
Categories=Game;Utility;
Terminal=false
X-AppImage-Version=${VERSION}
EOF
mkdir -p "${APPDIR}/usr/share/applications"
cp "${APPDIR}/linux-prefix-hub.desktop" "${APPDIR}/usr/share/applications/"

ICON="${ROOT}/packaging/linux-prefix-hub.png"
[ -f "${ICON}" ] || python3 "${ROOT}/packaging/make-icon.py" "${ICON}"
cp "${ICON}" "${APPDIR}/linux-prefix-hub.png"
cp "${ICON}" "${APPDIR}/.DirIcon"
mkdir -p "${APPDIR}/usr/share/icons/hicolor/256x256/apps"
cp "${ICON}" "${APPDIR}/usr/share/icons/hicolor/256x256/apps/"

# --- 4. pack --------------------------------------------------------------
say "Fetching appimagetool"
TOOL="${BUILD}/appimagetool"
TOOL_ARCH="${ARCH}"
curl -fsSL -o "${TOOL}" \
  "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${TOOL_ARCH}.AppImage" \
  || curl -fsSL -o "${TOOL}" \
  "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-${TOOL_ARCH}.AppImage"
chmod +x "${TOOL}"

# zsync update info -> GearLever / AppImageUpdate can self-update from the
# GitHub releases of this repo. Generating the .zsync file needs zsyncmake;
# without it we still build, just without delta updates (our own
# `--update` talks to the GitHub API and works either way).
UPDATE_INFO="gh-releases-zsync|${GH_OWNER}|${GH_REPO}|latest|${APP}-*-${ARCH}.AppImage.zsync"

say "Packing ${OUT##*/}"
if command -v zsyncmake >/dev/null 2>&1; then
  ARCH="${ARCH}" VERSION="${VERSION}" "${TOOL}" \
    --updateinformation "${UPDATE_INFO}" \
    "${APPDIR}" "${OUT}"
else
  echo "WARNING: zsyncmake not found -- building without update information."
  echo "         Install the 'zsync' package for a release-grade build."
  ARCH="${ARCH}" VERSION="${VERSION}" "${TOOL}" "${APPDIR}" "${OUT}"
fi

( cd "${BUILD}" && sha256sum "${APP}-${VERSION}-${ARCH}.AppImage"* \
    > SHA256SUMS )

say "Done"
ls -lh "${BUILD}/${APP}-${VERSION}-${ARCH}.AppImage"*
echo
echo "Upload the .AppImage, the .zsync and SHA256SUMS to the GitHub release."
