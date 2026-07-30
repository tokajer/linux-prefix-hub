#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

# Build the release with Velopack. This is the official pipeline.
#
# What it does:
#   1. fetch a relocatable CPython AppImage (python-appimage) as the base
#   2. unpack it into a plain directory and drop our package + the velopack
#      wheel into its site-packages
#   3. write the launcher: a **compiled** main executable plus the shell
#      script it hands over to -- see step 3 for why it cannot be the script
#   4. hand the directory to `vpk pack`, which produces the .AppImage and the
#      release feed that `core/updater.py` talks to
#
# Velopack packages a *directory* of files (like PyInstaller's --onedir), not a
# single file -- that is why step 2 builds one instead of repacking an AppImage.
#
# Requirements:
#   - curl and a C compiler (see step 3 -- ~30 lines, no libraries)
#   - the .NET SDK and the `vpk` tool:
#       sudo dnf install dotnet-sdk-10.0        # Fedora/Nobara
#       dotnet tool install -g vpk
#     or, without installing vpk globally:  dnx vpk@<version>
#   - keep the vpk version and the `velopack` wheel version in step (see
#     pyproject `update` extra) aligned; Velopack asks for that explicitly.
#
# VERIFY-ON-DEVICE: still worth checking on a real release run:
#   - the exact output file names vpk writes, for the release upload glob
#   - whether GearLever accepts a vpk-built AppImage (it carries no zsync
#     update information any more)
#
# Usage:  ./packaging/build-velopack.sh [version]
set -euo pipefail

APP="LinuxPrefixHub"
PACK_ID="${PACK_ID:-io.github.tokajer.LinuxPrefixHub}"
GH_OWNER="${GH_OWNER:-tokajer}"
GH_REPO="${GH_REPO:-linux-prefix-hub}"
# ARCH picks the CPython base *and* has to match what $CC emits: vpk reads the
# machine out of the main executable, so a cross-build needs a cross compiler.
ARCH="${ARCH:-$(uname -m)}"
PY_SERIES="${PY_SERIES:-3.12}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="${ROOT}/build"
PACKDIR="${BUILD}/pack"
OUTDIR="${BUILD}/release"

# The version comes from the release tag, never from the source. Without an
# argument we ask git; without a tag there is nothing to release.
VERSION="${1:-}"
if [ -z "${VERSION}" ]; then
  VERSION="$(git -C "${ROOT}" describe --tags --abbrev=0 2>/dev/null || true)"
fi
VERSION="${VERSION#v}"
if [ -z "${VERSION}" ]; then
  echo "No version given and no tag found. Pass one, or tag first." >&2
  exit 1
fi

export APPIMAGE_EXTRACT_AND_RUN=1

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# --- 0. tools -------------------------------------------------------------
VPK="${VPK:-}"
if [ -z "${VPK}" ]; then
  if command -v vpk >/dev/null 2>&1; then
    VPK="vpk"
  elif command -v dnx >/dev/null 2>&1; then
    VPK="dnx vpk"
  else
    echo "vpk not found. Install the .NET SDK, then:" >&2
    echo "  dotnet tool install -g vpk" >&2
    echo "(or set VPK=... to point at it)" >&2
    exit 1
  fi
fi

rm -rf "${BUILD}"
mkdir -p "${PACKDIR}" "${OUTDIR}"

# --- 1. base interpreter --------------------------------------------------
say "Fetching CPython ${PY_SERIES} base (${ARCH})"
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

say "Unpacking base into the pack directory"
( cd "${BUILD}" && ./python.AppImage --appimage-extract >/dev/null )
rm -rf "${PACKDIR}"
mv "${BUILD}/squashfs-root" "${PACKDIR}"
rm -f "${BUILD}/python.AppImage"

# The base ships python's own desktop file and icon; vpk writes its own.
rm -f "${PACKDIR}"/*.desktop "${PACKDIR}"/*.png "${PACKDIR}/.DirIcon" \
      "${PACKDIR}/AppRun"
rm -rf "${PACKDIR}/usr/share/applications" "${PACKDIR}/usr/share/metainfo"

# --- 2. our code and the update SDK ---------------------------------------
say "Installing linux_prefix_hub and velopack into the bundle"
SITE_PACKAGES="$(find "${PACKDIR}/opt" -maxdepth 4 -type d -name site-packages \
  | head -n1)"
if [ -z "${SITE_PACKAGES}" ]; then
  echo "site-packages not found in the base" >&2
  exit 1
fi
rm -rf "${SITE_PACKAGES}/linux_prefix_hub"
cp -r "${ROOT}/src/linux_prefix_hub" "${SITE_PACKAGES}/linux_prefix_hub"
find "${SITE_PACKAGES}/linux_prefix_hub" -name '__pycache__' -type d \
  -exec rm -rf {} + 2>/dev/null || true

# Stamp the version in. The package carries none of its own (see its
# __init__), so this is where the shipped build learns what vpk called it --
# and `updater.check` compares against exactly this number.
printf 'version = "%s"\n__version__ = version\n' "${VERSION}" \
  > "${SITE_PACKAGES}/linux_prefix_hub/_version.py"

# The bundle's *own* pip, not the host's. It resolves the wheel against the
# interpreter that will actually run it, so none of --platform/--abi/
# --python-version has to be guessed and kept in step with PY_SERIES -- and
# the build machine no longer needs a pip at all (Fedora's python3 ships
# without one).
BUNDLED_PY="$(find "${PACKDIR}/opt" -maxdepth 3 -type f -name 'python3.*' \
  -perm -u+x | head -n1)"
if [ -z "${BUNDLED_PY}" ]; then
  echo "No interpreter found in the base" >&2
  exit 1
fi
"${BUNDLED_PY}" -m pip install --quiet --upgrade \
  --target "${SITE_PACKAGES}" "velopack>=1.2"

# --- 3. the launcher vpk calls --------------------------------------------
# Two files, because vpk insists on the first one being a real binary: it
# reads `--mainExe` with an ELF parser to work out the target architecture
# (LinuxPackCommandRunner.GetMachineForBinary). Handing it the shell script
# this used to be fails the build with "Given stream is not a proper ELF
# file" -- that is what a shebang looks like to ELFSharp.
#
# So the main executable is a compiled shim whose whole job is to exec the
# script next to it, and every bit of actual logic stays in the script.
say "Writing the launcher"
SH_NAME="${APP}.sh"
LAUNCHER="${PACKDIR}/${SH_NAME}"
cat > "${LAUNCHER}" << 'EOF'
#!/usr/bin/env bash
# The Velopack package's launcher, reached through the compiled shim.
#
# Velopack replaces the whole directory on update, so this file is always in
# step with the interpreter next to it. $APPIMAGE is set by the AppImage
# runtime; core/integrate.py uses it, and updater.app_hook() keys off it.
set -u
HERE="$(dirname "$(readlink -f "${0}")")"
export APPDIR="${HERE}"

PYTHON=""
for candidate in "${HERE}"/opt/python*/bin/python3.*; do
  if [ -x "${candidate}" ]; then
    PYTHON="${candidate}"
    break
  fi
done
if [ -n "${PYTHON}" ] && [ -d "$(dirname "$(dirname "${PYTHON}")")/lib" ]; then
  export PYTHONHOME="$(dirname "$(dirname "${PYTHON}")")"
fi
[ -x "${PYTHON}" ] || PYTHON="$(command -v python3)"
export PYTHONDONTWRITEBYTECODE=1

# Shim modes run on every game launch: straight through, no self-heal.
case "${1:-}" in
  --wrapper|--hook|--daemon)
    exec "${PYTHON}" -m linux_prefix_hub "$@"
    ;;
esac

"${PYTHON}" -m linux_prefix_hub --integrate >/dev/null 2>&1 || true
exec "${PYTHON}" -m linux_prefix_hub "$@"
EOF
chmod 755 "${LAUNCHER}"

CC="${CC:-$(command -v cc || command -v gcc || true)}"
if [ -z "${CC}" ]; then
  echo "No C compiler found; vpk needs an ELF main executable." >&2
  echo "Install gcc/clang, or set CC=..." >&2
  exit 1
fi

# The name is written into the source rather than repeated in it, so the two
# cannot drift apart.
CSRC="${BUILD}/launcher.c"
printf '#define SHELL_LAUNCHER "%s"\n' "${SH_NAME}" > "${CSRC}"
cat >> "${CSRC}" << 'EOF'
/* Main executable of the Velopack package: exec the script next to me.
 *
 * It exists because vpk parses this file as ELF (see the build script). Keep
 * it free of logic -- everything a user could ever need to debug belongs in
 * the shell script, where they can read it.
 *
 * /proc/self/exe rather than argv[0]: the AppImage mounts itself somewhere
 * new on every start, and only the kernel knows where that is.
 */
#include <libgen.h>
#include <limits.h>
#include <stdio.h>
#include <unistd.h>

int main(int argc, char **argv)
{
    char self[PATH_MAX];
    char script[PATH_MAX];
    ssize_t len;

    (void) argc;
    len = readlink("/proc/self/exe", self, sizeof(self) - 1);
    if (len < 0) {
        perror("LinuxPrefixHub: cannot locate myself");
        return 127;
    }
    self[len] = '\0';

    if (snprintf(script, sizeof(script), "%s/%s", dirname(self),
                 SHELL_LAUNCHER) >= (int) sizeof(script)) {
        fprintf(stderr, "LinuxPrefixHub: path too long\n");
        return 127;
    }

    argv[0] = script;
    execv(script, argv);
    perror(script);
    return 127;
}
EOF

MAIN_EXE="${PACKDIR}/${APP}"
"${CC}" -O2 -s -o "${MAIN_EXE}" "${CSRC}"
chmod 755 "${MAIN_EXE}"
# Fail here, not three minutes later inside vpk, if this is not what it wants.
head -c 4 "${MAIN_EXE}" | grep -q 'ELF' || {
  echo "The main executable did not come out as ELF." >&2
  exit 1
}

ICON="${ROOT}/packaging/linux-prefix-hub.png"
[ -f "${ICON}" ] || python3 "${ROOT}/packaging/make-icon.py" "${ICON}"

# --- 4. pack --------------------------------------------------------------
say "Packing ${PACK_ID} ${VERSION}"
# shellcheck disable=SC2086  # VPK may be "dnx vpk", two words on purpose
${VPK} pack \
  --packId "${PACK_ID}" \
  --packVersion "${VERSION}" \
  --packDir "${PACKDIR}" \
  --mainExe "${APP}" \
  --packTitle "Linux Prefix Hub" \
  --packAuthors "${GH_OWNER}" \
  --icon "${ICON}" \
  --categories "Game;Utility;" \
  --outputDir "${OUTDIR}"

# One stable download name, so
#   .../releases/latest/download/LinuxPrefixHub-x86_64.AppImage
# keeps working across releases. vpk names the file after the packId, which
# we must not change -- that is Velopack's app identity and renaming it would
# orphan every installation. Renaming the *file* is safe: the update feed
# points at the .nupkg, never at this one.
ASSET="${APP}-${ARCH}.AppImage"
BUILT="$(find "${OUTDIR}" -maxdepth 1 -name '*.AppImage' | head -n1)"
if [ -z "${BUILT}" ]; then
  echo "vpk produced no AppImage in ${OUTDIR}" >&2
  exit 1
fi
[ "$(basename "${BUILT}")" = "${ASSET}" ] || mv "${BUILT}" "${OUTDIR}/${ASSET}"

# After the rename, so the sums describe the names that get uploaded.
( cd "${OUTDIR}" && sha256sum ./* > SHA256SUMS 2>/dev/null || true )

say "Done"
ls -lh "${OUTDIR}"
echo
echo "Upload everything in ${OUTDIR#"${ROOT}/"} to the GitHub release --"
echo "the whole directory is the feed core/updater.py reads."
