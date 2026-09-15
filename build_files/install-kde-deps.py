#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
# SPDX-FileCopyrightText: 2026 Hadi Chokr <hadichokr@icloud.com>
"""Install what KDE needs to compile, and record the build order.

Runs once in kde-build-container.sh, before kde-builder. That is all it does.

It used to also rip the distro copy of every project out of the container, by
a name guessed from the kde-builder module. That is what deleted the library
under qmobipocket-devel's CMake config and stopped a build 56 projects in, and
what removed the Qt4 'attica' because a module happens to share its name.

None of it was needed. The container and the image start from the same pinned
Kinoite, so a library the build links against here is present there too, and
the RPMs are cut from the per-project DESTDIR trees rather than from this
container's /usr, so nothing else installed here can reach the image.
kde-builder installs each project into /usr in dependency order, so by the
time a project is compiled its dependencies are already the fresh ones. The
distro copies underneath are overwritten, not linked against.
"""

import argparse
import logging
import os
import shutil
import subprocess
import urllib.request

import yaml

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

KDE_DEPS_YAML = "https://invent.kde.org/sysadmin/repo-metadata/-/raw/master/distro-dependencies/fedora.yaml"

# Read by package-kde.py
META_DIR = "/work/meta"


def load_targets(path="/ctx/targets.txt"):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def run_kde_builder(args):
    args = ["kde-builder"] + args
    logger.info(f"Running: {' '.join(args)}")
    process = subprocess.run(args=args, capture_output=True, text=True)
    if process.returncode != 0:
        raise Exception(f"kde-builder failed ({process.returncode}): {process.stdout}")
    return process.stdout


def get_all_build_targets(targets):
    """Every project kde-builder will build, in build order."""
    logger.info("Querying kde-builder for true build targets...")
    result = subprocess.run(
        ["kde-builder", "--include-dependencies", "--no-stop-on-failure", "--pretend"] + targets,
        capture_output=True,
        text=True,
    )
    resolved = []
    for line in result.stdout.splitlines():
        if "Building " in line:
            part = line.split("Building ", 1)[1]
            module = part.split()[0].split("/")[-1]
            resolved.append(module)
    return list(dict.fromkeys(resolved))


def fetch_deps_yaml(url):
    logger.info(f"Fetching metadata from {url}")
    with urllib.request.urlopen(url) as f:
        return yaml.full_load(f)


def collect_deps(data, build_modules):
    build_modules = set(build_modules)
    builddeps = set()
    rundeps = set()
    for name, pkg in data.items():
        if name not in build_modules:
            continue
        builddeps.update(pkg.get("builddeps") or [])
        rundeps.update(pkg.get("rundeps") or [])
    return builddeps, rundeps


def write_list(name, items):
    path = os.path.join(META_DIR, name)
    with open(path, "w") as f:
        f.write("\n".join(items) + ("\n" if items else ""))
    logger.info(f"Wrote {len(items)} line(s) to {path}")


def install(packages):
    packages = sorted(set(packages))
    if not packages:
        logger.info("No packages to install.")
        return
    logger.info(f"Installing {len(packages)} packages via dnf5: {', '.join(packages)}")
    process = subprocess.run(
        ["dnf5", "install", "-y", "--skip-broken", "--skip-unavailable", "--allowerasing"] + packages
    )
    if process.returncode != 0:
        raise Exception(f"dnf5 install failed ({process.returncode})")


def main():
    parser = argparse.ArgumentParser()
    # Accepted for compatibility with older callers.
    parser.add_argument("--compile", action="store_true", help=argparse.SUPPRESS)
    parser.parse_args()

    os.makedirs(META_DIR, exist_ok=True)
    config_dir = "/root/.config"
    os.makedirs(config_dir, exist_ok=True)
    if os.path.exists("/ctx/kde-builder.yaml"):
        shutil.copy("/ctx/kde-builder.yaml", f"{config_dir}/kde-builder.yaml")

    run_kde_builder(["--metadata-only"])
    order = get_all_build_targets(load_targets())

    data = fetch_deps_yaml(KDE_DEPS_YAML)
    builddeps, rundeps = collect_deps(data, order)

    write_list("build-order.txt", order)
    # What fedora.yaml says a running KDE needs. package-kde.py offers these
    # as Recommends, minus whatever the build provides itself.
    write_list("fedora-rundeps.txt", sorted(rundeps))

    install(builddeps | rundeps)


if __name__ == "__main__":
    main()
