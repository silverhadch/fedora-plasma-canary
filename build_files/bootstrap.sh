#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
# SPDX-FileCopyrightText: 2026 Hadi Chokr <hadichokr@icloud.com>
set -oue pipefail

error() { echo -e "\n\033[1;31mERROR: $1\033[0m\n" >&2; }

echo "==> Installing ccache..."
dnf5 install -y --skip-broken --skip-unavailable --allowerasing \
    ccache \
    || error "ccache failed to install"

echo "==> Installing system runtime deps not covered by fedora.yaml..."
dnf5 install -y --skip-broken --skip-unavailable --allowerasing \
    wireplumber \
    pipewire-pulseaudio \
    pipewire-alsa \
    accounts-daemon \
    avahi \
    upower \
    udisks2 \
    pcscd \
    || error "Some system runtime deps failed to install"

echo "==> Installing build dependencies..."
dnf5 install -y --skip-broken --skip-unavailable --allowerasing \
    sudo git ninja-build rsync openssh-clients ccache \
    python3-yaml python3-requests python3-pip python3-setproctitle ruby \
    cmake rpm-build \
    clang-devel kf6-kirigami-devel \
    kf6-kirigami-addons-devel clang-tools-extra git-clang-format jq \
    'dnf-command(repoquery)' \
    || error "Some build deps failed to install"

dnf5 group install development-tools -y || error "development-tools failed to install"

echo "==> Installing kde-builder..."
git clone https://invent.kde.org/sdk/kde-builder.git /usr/share/kde-builder
ln -sf /usr/share/kde-builder/kde-builder /usr/bin/kde-builder
mkdir -p /usr/share/zsh/site-functions
ln -sf /usr/share/kde-builder/data/completions/zsh/_kde-builder \
    /usr/share/zsh/site-functions/_kde-builder
ln -sf /usr/share/kde-builder/data/completions/zsh/_kde-builder_projects_and_groups \
    /usr/share/zsh/site-functions/_kde-builder_projects_and_groups

echo "==> Fetching and installing KDE distro dependencies..."
python3 /ctx/install-kde-deps.py

echo "==> Installing dev tools..."
dnf5 install -y --skip-broken --skip-unavailable --allowerasing \
    neovim zsh flatpak-builder kdevelop kdevelop-devel kdevelop-libs \
    || error "Some dev tools failed to install"
