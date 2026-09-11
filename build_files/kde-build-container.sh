#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
# SPDX-FileCopyrightText: 2026 Hadi Chokr <hadichokr@icloud.com>
#
# Runs inside the transient compile container, started from the same pinned
# Kinoite rawhide the final image is built on. Builds KDE with kde-builder into
# one install tree per project, then package-kde.py turns those into RPMs.
# Nothing from this container ships except /work/rpms.
set -euo pipefail

export HOME=/root
export KDE_MASTER_INSTALL_DESTDIR="/work/tree/install"
LOG_DIR="/work/logs"
# /work outlives the container on local reruns; base-image-digest.txt in it is
# written by the workflow and must survive.
rm -rf /work/tree /work/rpms /work/meta /work/rpmbuild
mkdir -p "$KDE_MASTER_INSTALL_DESTDIR" "$LOG_DIR" /work/meta
rm -rf /root
mkdir -p /root/.config

log()   { echo -e "\n\033[1;34m==> $1\033[0m\n" | tee -a "$LOG_DIR/build.log"; }
error() { echo -e "\n\033[1;31mERROR: $1\033[0m\n" | tee -a "$LOG_DIR/build.log" >&2; }
die()   { error "$1"; exit 1; }

# Block all 32-bit packages globally.
cat >> /etc/dnf/dnf.conf << 'EOF'

[main]
excludepkgs=*.i686
EOF

# rpm-build, redhat-rpm-config and the generator packages are what
# package-kde.py needs: %dist, %_isa, and the cmake()/python dependency
# generators. readelf (binutils) finds Qt private API use, getcap (libcap)
# carries file capabilities into the RPMs.
log "Installing build dependencies..."
dnf5 install -y --skip-broken --skip-unavailable --allowerasing \
    sudo git ninja-build rsync openssh-clients ccache \
    python3-yaml python3-requests python3-pip python3-setproctitle ruby \
    cmake rpm-build redhat-rpm-config python3-rpm-generators cmake-rpm-macros \
    binutils libcap \
    clang-devel kf6-kirigami-devel \
    kf6-kirigami-addons-devel clang-tools-extra git-clang-format jq \
    PackageKit-glib-devel \
    'dnf-command(repoquery)' \
    || error "Some build deps failed to install"

dnf5 group install development-tools -y || error "development-tools failed to install"

for tool in ccache git ruby rpmbuild readelf; do
    command -v "$tool" > /dev/null || die "$tool is missing after installing build dependencies."
done

log "Configuring ccache..."
export CCACHE_DIR=/ccache
export CCACHE_MAXSIZE=5G
ccache --set-config=cache_dir=/ccache
ccache --set-config=max_size=5G
ccache --set-config=compression=true
ccache -z

log "Installing kde-builder..."
git clone https://invent.kde.org/sdk/kde-builder.git /usr/share/kde-builder
ln -sf /usr/share/kde-builder/kde-builder /usr/bin/kde-builder

log "Installing KDE build dependencies and recording what the build replaces..."
python3 /ctx/install-kde-deps.py 2>&1 | tee -a "$LOG_DIR/deps.log" \
    || die "install-kde-deps.py failed, see deps.log."

log "Installing ninja hijack..."
mv /usr/bin/ninja /usr/bin/ninja.orig
cp /ctx/ninja-hijack.rb /usr/bin/ninja
chmod +x /usr/bin/ninja

# The workflow's timeout covers the whole container. Stop kde-builder early
# enough that packaging always fits inside it: a build that finishes with a
# minute to spare is no use if the RPMs never get written.
BUILD_TIMEOUT=()
if [ -n "${BUILD_DEADLINE:-}" ]; then
    RESERVE="${PACKAGE_RESERVE:-1800}"
    BUDGET=$(( BUILD_DEADLINE - $(date +%s) - RESERVE ))
    [ "$BUDGET" -gt 300 ] || die "Only ${BUDGET}s left before the packaging reserve, not starting a build."
    log "Build budget: $(( BUDGET / 60 ))m, keeping $(( RESERVE / 60 ))m for packaging."
    BUILD_TIMEOUT=(timeout --signal=TERM --kill-after=60 "${BUDGET}s")
fi

log "Building KDE..."
set +e
"${BUILD_TIMEOUT[@]}" python3 /ctx/build-kde.py 2>&1 | tee -a "$LOG_DIR/kde-build.log"
rc=${PIPESTATUS[0]}
set -e

# Collect kde-builder per-project logs regardless of outcome
if [ -d /builder/log ]; then
    log "Collecting kde-builder logs..."
    cp -r /builder/log "$LOG_DIR/kde-builder-logs"
fi

if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    error "Build stopped to leave time for packaging. The warm ccache is still saved, rerun to continue from it."
    exit 124
elif [ "$rc" -ne 0 ]; then
    die "build-kde.py failed ($rc). Logs at $LOG_DIR"
fi

# package-kde.py decides what the build replaces by asking dnf which distro
# packages overlap it, so dnf has to see them again.
rm -f /etc/dnf/libdnf5.conf.d/90-kde-selfbuilt.conf

log "Packaging..."
python3 /ctx/package-kde.py 2>&1 | tee -a "$LOG_DIR/package.log" \
    || die "package-kde.py failed, see package.log and $LOG_DIR/rpmbuild/."

log "Packages: $(wc -l < /work/rpms/manifest.txt), $(du -sh /work/rpms | cut -f1)"
log "ccache stats:"
ccache -s

log "Done."
