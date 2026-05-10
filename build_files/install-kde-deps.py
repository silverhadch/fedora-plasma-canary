#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
# SPDX-FileCopyrightText: 2026 Hadi Chokr <hadichokr@icloud.com>
import subprocess
import urllib.request
import yaml

KDE_DEPS_YAML = "https://invent.kde.org/sysadmin/repo-metadata/-/raw/master/distro-dependencies/fedora.yaml"


def load_deps_from_yaml(url):
    with urllib.request.urlopen(url) as f:
        data = yaml.full_load(f)
    deps = []
    for pkg in data.values():
        deps.extend(pkg.get("builddeps", []))
    return list(set(deps))


def install(packages):
    packages = list(set(packages))
    print(f"Installing {len(packages)} packages via dnf5")
    process = subprocess.run(
        [
            "dnf5", "install", "-y",
            "--best",
            "--allowerasing",
            "--skip-unavailable",
            "--skip-broken",
        ] + packages,
    )
    if process.returncode != 0:
        raise Exception(f"dnf5 install failed ({process.returncode})")


deps = load_deps_from_yaml(KDE_DEPS_YAML)
install(deps)
