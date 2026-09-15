#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
# SPDX-FileCopyrightText: 2026 Hadi Chokr <hadichokr@icloud.com>
#
# Runs on the developer's machine rather than in the build container, so it
# uses env rather than a hardcoded /bin/bash: NixOS has no /bin/bash.
#
# Print every package the Kinoite base has that this image does not, other
# than the ones kde-canary obsoletes on purpose. The image is its base with
# KDE swapped, so the expected output is empty: anything listed was lost as a
# side effect of the swap. It reads both installed package sets directly.
#
#   ./compare-with-kinoite.sh [image]
#
# Compares against the mirror the image is built from. Set KINOITE_IMAGE to
# compare against a different Kinoite.
set -euo pipefail

IMAGE="${1:-localhost/fedora-plasma-canary:latest}"
KINOITE="${KINOITE_IMAGE:-ghcr.io/silverhadch/fedora-plasma-canary-kinoite:latest}"

WORK=$(mktemp -d)
trap 'rm -rf "${WORK}"' EXIT

rpm_in() {
    local image="$1"
    shift
    podman run --rm --entrypoint /usr/bin/rpm "${image}" "$@"
}

echo "Reading ${KINOITE}..." >&2
rpm_in "${KINOITE}" -qa --qf '%{NAME}\n' | sort -u > "${WORK}/kinoite"

echo "Reading ${IMAGE}..." >&2
rpm_in "${IMAGE}" -qa --qf '%{NAME}\n' | sort -u > "${WORK}/canary"
rpm_in "${IMAGE}" -q --obsoletes kde-canary | awk '{ print $1 }' | sort -u > "${WORK}/obsoleted"

comm -23 "${WORK}/kinoite" "${WORK}/canary" \
    | comm -23 - "${WORK}/obsoleted"
