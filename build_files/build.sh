#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
# SPDX-FileCopyrightText: 2026 Hadi Chokr <hadichokr@icloud.com>
set -oue pipefail

ARTIFACTS_DIR="/var/log/kde-build-artifacts"
mkdir -p "$ARTIFACTS_DIR"

log() { echo -e "\n\033[1;34m==> $1\033[0m\n" | tee -a "$ARTIFACTS_DIR/build.log"; }
error() { echo -e "\n\033[1;31mERROR: $1\033[0m\n" >&2 | tee -a "$ARTIFACTS_DIR/build.log"; }

log "Bootstrapping build dependencies..."
/ctx/bootstrap.sh 2>&1 | tee -a "$ARTIFACTS_DIR/bootstrap.log"

log "Building KDE..."
/ctx/build-kde.py 2>&1 | tee -a "$ARTIFACTS_DIR/kde-build.log"

# Collect kde-builder per-project logs
if [ -d /builder/log ]; then
    log "Collecting kde-builder logs..."
    cp -r /builder/log "$ARTIFACTS_DIR/kde-builder-logs"
fi

log "Enabling systemd units..."
systemctl enable podman.socket               || error "Failed to enable podman.socket"
systemctl enable docker.service              || error "Failed to enable docker.service"

log "Done. Logs at $ARTIFACTS_DIR"
