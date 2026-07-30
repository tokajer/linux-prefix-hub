# SPDX-FileCopyrightText: 2026 tokajer
# SPDX-License-Identifier: GPL-3.0-or-later

"""Linux Prefix Hub -- know where your games save, and take that home.

The version is **not** written down here. It comes from the release tag, so a
build can never claim a number nobody tagged. Three sources, in the order
their answer is trustworthy:

  1. `_version.py` -- generated at build time from the tag, by
     `packaging/build-*.sh` for the AppImage and by hatch-vcs for a wheel.
     This is the number that was actually shipped.
  2. the installed distribution's metadata, for a `pip install` that did not
     go through the build hook.
  3. `DEV_VERSION` for a plain checkout: there is no release here, and saying
     otherwise is exactly how a tag and a build start disagreeing.

`_version.py` is generated, git-ignored, and absent from the repository. Do
not add it back by hand -- then we would be right where we started.
"""
from __future__ import annotations

# Sorts below every real release, so `updater.is_newer` treats a checkout as
# older than anything published. That is the honest answer for a working copy.
DEV_VERSION = "0.0.0+dev"


def _detect_version() -> str:
    try:
        from ._version import version  # generated at build time
        return str(version)
    except ImportError:
        pass
    try:
        from importlib.metadata import version as installed_version
        return installed_version("linux-prefix-hub")
    except Exception:      # PackageNotFoundError: a checkout, not an install
        return DEV_VERSION


__version__ = _detect_version()
