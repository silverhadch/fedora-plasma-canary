#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
# SPDX-FileCopyrightText: 2026 Hadi Chokr <hadichokr@icloud.com>
#
# Runs inside the final image build, on top of the same pinned Kinoite rawhide
# the KDE RPMs were compiled on. Kinoite already is the desktop platform, so
# this only swaps Fedora's KDE for the master build, adds the development
# layer, and sets services.
set -euo pipefail

log()  { echo -e "\n\033[1;34m==> $1\033[0m\n"; }
fail() { echo -e "\n\033[1;31mERROR: $1\033[0m\n" >&2; exit 1; }

RPM_DIR=/ctx/rpms
[ -s "$RPM_DIR/manifest.txt" ] || fail "No $RPM_DIR/manifest.txt. build.yml downloads \
the RPMs from the kde-nightly release; locally, gh release download them into build_files/rpms."

mapfile -t RPMS < <(sed "s|^|$RPM_DIR/|" "$RPM_DIR/manifest.txt")
mapfile -t EXTRA < <(sed 's/#.*//' /ctx/image-packages.txt | tr -s '[:space:]' '\n' | grep .)

# One transaction, nothing skipped. kde-canary obsoletes Fedora's KDE packages
# and its downstream config, each project package brings the Requires rpmbuild
# generated from its own files, and the Qt pins turn a Qt that moved on since
# the build into a resolver error here instead of a crash at login.
log "Installing KDE master (${#RPMS[@]} packages) and the development layer..."
dnf5 install -y "${RPMS[@]}" "${EXTRA[@]}"

log "Checking the rpm database for unsatisfied dependencies..."
rpm -Va --nofiles --noscripts || fail "Unsatisfied dependencies after the swap, see above."

log "Checking the swap took..."
for f in /usr/bin/plasmashell /usr/bin/kwin_wayland /usr/bin/dolphin /usr/bin/konsole; do
    owner=$(rpm -qf --qf '%{NAME}\n' "$f" | head -1) || fail "$f is not owned by any package."
    [[ "$owner" == kde-canary-* ]] || fail "$f belongs to $owner, not to the master build."
done

# Frameworks keep binary compatibility, so a distro package built against
# released KF6 runs fine on master. Plasma's own libraries promise no such
# thing, so anything here that links them should be built or dropped.
log "Distro KDE packages left in the image, not built from master:"
rpm -qa --qf '%{NAME}\t%{URL}\n' \
    | awk -F'\t' '$2 ~ /kde\.org/ && $1 !~ /^kde-canary/ { print "  " $1 }' | sort

log "Installing kde-builder..."
git clone https://invent.kde.org/sdk/kde-builder.git /usr/share/kde-builder
ln -sf /usr/share/kde-builder/kde-builder /usr/bin/kde-builder
mkdir -p /usr/share/zsh/site-functions
ln -sf /usr/share/kde-builder/data/completions/zsh/_kde-builder \
    /usr/share/zsh/site-functions/_kde-builder
ln -sf /usr/share/kde-builder/data/completions/zsh/_kde-builder_projects_and_groups \
    /usr/share/zsh/site-functions/_kde-builder_projects_and_groups

# Obsoleting a package runs its %preun as a removal, and %systemd_preun then
# disables its units, which the master build ships under the same names.
# Re-apply the presets, but only ever enabling, so nothing Kinoite turned on
# deliberately gets switched off.
log "Applying service policy..."
systemctl preset-all --preset-mode=enable-only

# Kept explicit on top of the presets: podman.socket and pcscd.socket are not
# desktop defaults, and the display-manager alias must end up on the master
# plasmalogin. Enabling an already-enabled unit is a no-op.
rm -f /etc/systemd/system/display-manager.service
for unit in plasmalogin.service plasma-setup.service podman.socket pcscd.socket \
            NetworkManager.service bluetooth.service cups.service \
            avahi-daemon.service accounts-daemon.service; do
    systemctl enable "$unit" || fail "Failed to enable $unit."
done

log "Done."
