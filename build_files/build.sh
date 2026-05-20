#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
# SPDX-FileCopyrightText: 2026 Hadi Chokr <hadichokr@icloud.com>
set -uo pipefail

ARTIFACTS_DIR="/usr/lib/kde-build-logs"
mkdir -p "$ARTIFACTS_DIR"

FAILED=0

log()   { echo -e "\n\033[1;34m==> $1\033[0m\n" | tee -a "$ARTIFACTS_DIR/build.log"; }
error() { echo -e "\n\033[1;31mERROR: $1\033[0m\n" | tee -a "$ARTIFACTS_DIR/build.log" >&2; }

log "Installing system deps and dev tools..."
if ! /ctx/bootstrap.sh 2>&1 | tee -a "$ARTIFACTS_DIR/bootstrap.log"; then
    error "bootstrap.sh failed"
    FAILED=1
fi

if [ "$FAILED" -eq 0 ]; then
    log "Extracting KDE master tar..."
    if ! tar -C / -xf /ctx/kde-master.tar.zst --use-compress-program=unzstd 2>&1 | tee -a "$ARTIFACTS_DIR/extract.log"; then
        error "Failed to extract kde-master.tar.zst"
        FAILED=1
    fi
fi

if [ "$FAILED" -eq 0 ]; then
    log "Updating linker cache..."
    ldconfig

    log "Removing SDDM if present..."
    rm -f /etc/systemd/system/display-manager.service
    dnf5 remove -y sddm || true

    log "Enabling systemd units..."
    systemctl enable accounts-daemon.service  || error "Failed to enable accounts-daemon.service"
    systemctl enable avahi-daemon.service     || error "Failed to enable avahi-daemon.service"
    systemctl enable avahi-daemon.socket      || error "Failed to enable avahi-daemon.socket"
    systemctl enable bluetooth.service        || error "Failed to enable bluetooth.service"
    systemctl enable cups.service             || error "Failed to enable cups.service"
    systemctl enable NetworkManager.service   || error "Failed to enable NetworkManager.service"
    systemctl enable pcscd.socket             || error "Failed to enable pcscd.socket"
    systemctl enable plasma-setup.service     || error "Failed to enable plasma-setup.service"
    systemctl enable plasmalogin.service      || error "Failed to enable plasmalogin.service"
    systemctl enable podman.socket            || error "Failed to enable podman.socket"
fi

log "Done. Logs at $ARTIFACTS_DIR"
exit "$FAILED"
