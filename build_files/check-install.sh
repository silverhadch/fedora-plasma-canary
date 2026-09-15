#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
# SPDX-FileCopyrightText: 2026 Hadi Chokr <hadichokr@icloud.com>
#
# Runs the same dnf transaction as build.sh, but as an rpm test transaction:
# dependencies are resolved and file conflicts checked, nothing is installed.
# kde-build.yml runs it on a pristine copy of the pinned base before
# publishing, so a set of RPMs that cannot install never replaces the last
# one that could, and the image keeps building meanwhile.
#
#   check-install.sh [rpm-dir]
set -euo pipefail

RPM_DIR="${1:-/ctx/rpms}"
[ -s "$RPM_DIR/manifest.txt" ] || { echo "No $RPM_DIR/manifest.txt" >&2; exit 1; }

mapfile -t RPMS < <(sed "s|^|$RPM_DIR/|" "$RPM_DIR/manifest.txt")
mapfile -t EXTRA < <(sed 's/#.*//' /ctx/image-packages.txt | tr -s '[:space:]' '\n' | grep .)

dnf5 install -y --setopt=tsflags=test "${RPMS[@]}" "${EXTRA[@]}"
echo "The image transaction resolves and passes the rpm test transaction."
