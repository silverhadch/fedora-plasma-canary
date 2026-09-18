#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
# SPDX-FileCopyrightText: 2026 Hadi Chokr <hadichokr@icloud.com>
"""Install what KDE needs to compile, and record the build order.

Runs once in kde-build-container.sh, before kde-builder. That is all it does.

It does not remove the distro copy of each project, and must not start doing
so again: the container and the image come from the same pinned Kinoite, the
RPMs are cut from the per-project DESTDIR trees rather than this container's
/usr, and kde-builder overwrites its dependencies in build order anyway.
Guessing those package names from module names deleted qmobipocket's library
and the Qt4 attica in earlier builds.
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
    resolved = list(dict.fromkeys(resolved))
    if not resolved:
        # An empty build order is never a real answer, and it used to pass in
        # silence: no build dependencies were installed, kde-builder then built
        # nothing, and the run failed half an hour later in the packaging step
        # with an empty tree, which says nothing about the cause. kde-builder's
        # own output is the only thing that explains it, so print that.
        logger.error(f"kde-builder resolved none of the {len(targets)} targets in "
                     f"targets.txt (exit {result.returncode}).")
        for stream, text in (("stdout", result.stdout), ("stderr", result.stderr)):
            text = text.strip()
            if text:
                logger.error(f"kde-builder {stream}:\n{text[-4000:]}")
        raise SystemExit(
            "Refusing to continue with an empty build order. If kde-builder is "
            "reporting a problem with repo-metadata rather than with these targets, "
            "it has broken upstream: pin a working commit in "
            "build_files/kde-builder-ref.txt, taking the last good one from "
            "meta/kde-builder-commit.txt in a successful build's artifact.")
    logger.info(f"kde-builder resolved {len(resolved)} projects to build.")
    return resolved


def fetch_deps_yaml(url):
    logger.info(f"Fetching metadata from {url}")
    with urllib.request.urlopen(url) as f:
        return yaml.full_load(f)


def collect_deps(data, build_modules):
    build_modules = set(build_modules)
    builddeps = set()
    rundeps = set()
    matched = set()
    for name, pkg in data.items():
        if name not in build_modules:
            continue
        matched.add(name)
        builddeps.update(pkg.get("builddeps") or [])
        rundeps.update(pkg.get("rundeps") or [])
    # A project with no entry contributes no build dependencies at all and will
    # fail to configure if it needs anything the rest of the order did not
    # already pull in. That is a gap in repo-metadata, so name them: the fix is
    # an entry upstream, not another package added by hand here.
    missing_entries = sorted(build_modules - matched)
    logger.info(f"{len(matched)} of {len(build_modules)} projects have a fedora.yaml entry.")
    if missing_entries:
        logger.warning(f"{len(missing_entries)} project(s) have no entry in fedora.yaml, so "
                       f"none of their build dependencies are installed: "
                       f"{', '.join(missing_entries)}")
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


def not_installed(packages):
    """Which of those names nothing installed provides.

    Capability names work here as well as package names: rpm indexes
    cmake(Qt6Core) and pkgconfig(libzstd) the same way it indexes bzip2-devel.
    """
    p = subprocess.run(["rpm", "-q", "--whatprovides", *packages],
                       capture_output=True, text=True)
    return sorted({line.rsplit(" ", 1)[-1].strip()
                   for line in (p.stdout + p.stderr).splitlines()
                   if "no package provides" in line})


def ensure_builddeps(builddeps):
    """Install whatever the lenient pass dropped, or stop the build here.

    install() passes --skip-broken and --skip-unavailable so one bad name
    cannot keep the build from starting at all. The cost is silence.
    bzip2-devel is in the fedora.yaml builddeps for karchive, it went into the
    same 700-package transaction as everything else, it did not get installed,
    and nothing said so: the build found out seven projects later, as
    "Could NOT find BZip2" in a cmake log.

    Asking again for only the missing names, without the skip flags, either
    installs them or makes dnf5 say why it cannot. --skip-broken prunes
    whatever it cannot fit into one large transaction, which is not the same
    thing as a package being unavailable, so the retry usually succeeds.
    """
    missing = not_installed(sorted(builddeps))
    if not missing:
        logger.info(f"All {len(builddeps)} build dependencies are installed.")
        return

    logger.warning(f"dnf5 skipped {len(missing)} build dependency(ies) silently: "
                   f"{', '.join(missing)}")
    logger.warning("Retrying them on their own, without --skip-broken.")
    subprocess.run(["dnf5", "install", "-y", "--allowerasing"] + missing)

    still = not_installed(missing)
    if still:
        raise SystemExit(
            f"{len(still)} build dependency(ies) could not be installed: "
            f"{', '.join(still)}. dnf5's reason is above. Every project needing one "
            f"would fail to configure, so this stops here rather than hours into the "
            f"build. If the name is simply gone from Fedora, that is a fedora.yaml "
            f"entry to fix in repo-metadata.")
    logger.info(f"The retry installed all {len(missing)}.")


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
    ensure_builddeps(builddeps)


if __name__ == "__main__":
    main()
