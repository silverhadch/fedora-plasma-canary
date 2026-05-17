#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
# SPDX-FileCopyrightText: 2026 Hadi Chokr <hadichokr@icloud.com>
set -uo pipefail

export HOME=/root
ARTIFACTS_DIR="/usr/lib/kde-build-logs"
mkdir -p "$ARTIFACTS_DIR"
rm -rf /root
mkdir -p /root/.config

FAILED=0

log() { echo -e "\n\033[1;34m==> $1\033[0m\n" | tee -a "$ARTIFACTS_DIR/build.log"; }
error() { echo -e "\n\033[1;31mERROR: $1\033[0m\n" | tee -a "$ARTIFACTS_DIR/build.log" >&2; }

log "Bootstrapping build dependencies..."
if ! /ctx/bootstrap.sh 2>&1 | tee -a "$ARTIFACTS_DIR/bootstrap.log"; then
    error "bootstrap.sh failed"
    FAILED=1
fi

if [ "$FAILED" -eq 0 ]; then
    log "Building KDE..."
    if ! /ctx/build-kde.py 2>&1 | tee -a "$ARTIFACTS_DIR/kde-build.log"; then
        error "build-kde.py failed"
        FAILED=1
    fi
fi

# Collect kde-builder per-project logs regardless of build outcome
if [ -d /builder/log ]; then
    log "Collecting kde-builder logs..."
    cp -r /builder/log "$ARTIFACTS_DIR/kde-builder-logs"
fi

if [ "$FAILED" -eq 0 ]; then
    log "Enabling systemd units..."
    systemctl enable podman.socket || error "Failed to enable podman.socket"
fi

rm -rf /root
rm -rf /builder
log "Done. Logs at $ARTIFACTS_DIR"

exit "$FAILED"
