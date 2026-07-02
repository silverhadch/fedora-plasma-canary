#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
# SPDX-FileCopyrightText: 2026 Hadi Chokr <hadichokr@icloud.com>
set -oue pipefail

export HOME=/root
DESTDIR="/work/tree/install"
LOG_DIR="/work/logs"
mkdir -p "$DESTDIR" "$LOG_DIR"
rm -rf /root
mkdir -p /root/.config

# Block all 32-bit packages globally
# Safely append the excludepkgs configuration to the main section
sudo bash -c 'cat << EOF >> /etc/dnf/dnf.conf

[main]
excludepkgs=*.i686
EOF'

FAILED=0

log()   { echo -e "\n\033[1;34m==> $1\033[0m\n" | tee -a "$LOG_DIR/build.log"; }
error() { echo -e "\n\033[1;31mERROR: $1\033[0m\n" | tee -a "$LOG_DIR/build.log" >&2; }

echo "==> Installing ccache..."
dnf5 install -y --skip-broken --skip-unavailable --allowerasing \
    ccache \
    || error "ccache failed to install"

log "Configuring ccache..."
export CCACHE_DIR=/ccache
export CCACHE_MAXSIZE=5G
ccache --set-config=cache_dir=/ccache
ccache --set-config=max_size=5G
ccache --set-config=compression=true
ccache -z

log "Installing build dependencies..."
dnf5 install -y --skip-broken --skip-unavailable --allowerasing \
    sudo git ninja-build rsync openssh-clients ccache \
    python3-yaml python3-requests python3-pip python3-setproctitle ruby \
    cmake rpm-build \
    clang-devel kf6-kirigami-devel \
    kf6-kirigami-addons-devel clang-tools-extra git-clang-format jq \
    'dnf-command(repoquery)' \
    || error "Some build deps failed to install"

dnf5 group install development-tools -y || error "development-tools failed to install"

log "Installing kde-builder..."
git clone https://invent.kde.org/sdk/kde-builder.git /usr/share/kde-builder
ln -sf /usr/share/kde-builder/kde-builder /usr/bin/kde-builder

log "Fetching and installing KDE distro dependencies..."
python3 /ctx/install-kde-deps.py --compile 2>&1 | tee -a "$LOG_DIR/deps.log"

log "Installing ninja hijack..."
mv /usr/bin/ninja /usr/bin/ninja.orig
cp /ctx/ninja-hijack.rb /usr/bin/ninja
chmod +x /usr/bin/ninja

log "Building KDE..."
export KDE_MASTER_INSTALL_DESTDIR="$DESTDIR"
if ! python3 /ctx/build-kde.py 2>&1 | tee -a "$LOG_DIR/kde-build.log"; then
    error "build-kde.py failed"
    FAILED=1
fi

# Collect kde-builder per-project logs regardless of outcome
if [ -d /builder/log ]; then
    log "Collecting kde-builder logs..."
    cp -r /builder/log "$LOG_DIR/kde-builder-logs"
fi

if [ "$FAILED" -ne 0 ]; then
    error "Build failed. Logs at $LOG_DIR"
    exit "$FAILED"
fi

log "Creating tar archive..."
tar -C "$DESTDIR" -c . | zstd -T0 -19 -o /work/kde-master.tar.zst

log "Archive size: $(du -sh /work/kde-master.tar.zst | cut -f1)"
log "ccache stats:"
ccache -s

log "Done."
