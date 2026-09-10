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
    # --no-overwrite-dir keeps tar from restoring mode/mtime on directories that
    # already exist (/usr/lib/systemd and friends), and --no-delay-directory-restore
    # drops the deferred re-stat pass. Without these, overlayfs copy-up changes the
    # inode identity of a pre-existing directory between creation and the final
    # set-stat, tar reports "Directory renamed before its status could be
    # extracted", and exits 2 even though every member extracted fine.
    if ! tar -C / -xf /ctx/kde-master.tar.zst \
            --use-compress-program=unzstd \
            --no-overwrite-dir \
            --no-delay-directory-restore 2>&1 | tee -a "$ARTIFACTS_DIR/extract.log"; then
        error "Failed to extract kde-master.tar.zst"
        FAILED=1
    fi
fi

if [ "$FAILED" -eq 0 ]; then
    log "Verifying extracted tree..."
    for f in /usr/bin/plasmashell /usr/bin/dolphin /usr/bin/konsole; do
        if [ ! -x "$f" ]; then
            error "Expected $f after extraction, not found. Archive may be truncated."
            FAILED=1
        fi
    done
    # dolphin links this and will not start without it. It comes from the tar
    # now that packagekit-qt is built rather than ignored, so check the file
    # rather than the rpm: the distro PackageKit-Qt6 stays excluded.
    for lib in /usr/lib64/libpackagekitqt6.so.2; do
        if [ ! -e "$lib" ]; then
            error "Expected $lib after extraction, not found. Did packagekit-qt build?"
            FAILED=1
        fi
    done
fi

if [ "$FAILED" -eq 0 ]; then
    log "Verifying desktop platform..."
    # Every install in this path runs with --skip-unavailable, so a rename
    # upstream is silent. These are the ones whose absence would only show up
    # on real hardware after a rebase. bluez is not in any comps group at all:
    # it arrives as an rpm require of bluedevil, which this image builds
    # itself, so it is the canary for the harvested runtime deps.
    for p in NetworkManager-wifi alsa-sof-firmware default-fonts-core-sans \
             glibc-all-langpacks cups mesa-dri-drivers bluez \
             redhat-systemd-presets-desktop qt6-qtwayland libddcutil; do
        if ! rpm -q "$p" > /dev/null 2>&1; then
            error "$p not installed. See the unresolved list in bootstrap.log."
            FAILED=1
        fi
    done
fi

if [ "$FAILED" -eq 0 ]; then
    log "Updating linker cache..."
    ldconfig

    log "Removing SDDM if present..."
    rm -f /etc/systemd/system/display-manager.service
    dnf5 remove -y sddm || true

    # Fedora keeps its enable policy in preset files rather than in the
    # packages, and the base image only carries the server set. The desktop
    # presets are installed by bootstrap.sh, but the units they cover arrive
    # later in the KDE tar, so the policy has to be applied here rather than
    # by the rpm scriptlets. This is what the base image would have done on
    # first boot if it were a desktop.
    log "Applying Fedora desktop presets..."
    systemctl preset-all || error "systemctl preset-all failed"

    # Kept explicit on top of the presets: podman.socket is not a desktop
    # default, plasmalogin is upstream KDE with no Fedora preset, and the rest
    # are cheap to assert. Enabling an already-enabled unit is a no-op.
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
