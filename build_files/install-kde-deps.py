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


def resolved_groups(targets, projects):
    """The groups those projects came from.

    targets.txt names groups as well as projects: workspace is a group, and it
    expands into plasma-workspace, kwin, plasma-desktop and the rest, so it
    never appears as a project itself. Asking which group each project belongs
    to is what tells a group apart from a name kde-builder ignored.

    Only lines whose left-hand side is a project already resolved are read, so
    kde-builder's progress output and its dbus warning, which also carry a
    colon, cannot invent a group name.
    """
    known = set(projects)
    result = subprocess.run(
        ["kde-builder", "--include-dependencies", "--query", "group"] + targets,
        capture_output=True,
        text=True,
    )
    groups = set()
    for line in result.stdout.splitlines():
        name, sep, value = line.partition(": ")
        if sep and name.strip() in known and value.strip():
            groups.add(value.strip())
    return groups


def get_all_build_targets(targets):
    """Every project kde-builder will build, in build order.

    Asked with --query rather than --pretend. --pretend on kde-builder master
    does not pretend: it clones and compiles for real, and dies on the first
    project with

      [Errno 2] No such file or directory: '/builder/src/extra-cmake-modules'

    having printed one "Building ..." line. Parsing those lines then returned a
    build order of one project, so one project's worth of build dependencies
    was installed and karchive failed to configure six projects later for want
    of bzip2-devel.

    --query returns before the build lock is taken, cannot build anything, and
    prints "project: value" once per project in dependency order. source-dir is
    asked for because it is a pure path computation that touches neither the
    network nor the build system.
    """
    logger.info("Querying kde-builder for true build targets...")
    result = subprocess.run(
        ["kde-builder", "--include-dependencies", "--query", "source-dir"] + targets,
        capture_output=True,
        text=True,
    )
    resolved = []
    for line in result.stdout.splitlines():
        name, sep, value = line.partition(": ")
        # Only "name: /absolute/path" lines are answers. kde-builder's progress
        # and its dbus warning also carry a colon, and none of them are a path.
        if sep and value.startswith("/") and name.strip():
            resolved.append(name.strip().split("/")[-1])
    resolved = list(dict.fromkeys(resolved))
    # Every name in targets.txt was asked for by name, so each one has to have
    # contributed something: either a project of that name, or, for a group like
    # workspace, the projects it expands into. A name that contributed neither
    # is a name kde-builder silently ignored, and everything it would have
    # pulled in goes unbuilt and undeclared. That is how a build order of one
    # project passed unnoticed and left karchive without its bzip2-devel.
    groups = resolved_groups(targets, resolved)
    absent = [t for t in targets if t not in resolved and t not in groups]
    if resolved and absent:
        logger.error(f"kde-builder resolved {len(resolved)} projects in "
                     f"{len(groups)} group(s), but {len(absent)} of the {len(targets)} "
                     f"names in targets.txt are neither: {', '.join(absent)}")
        logger.error(f"kde-builder stdout:\n{result.stdout[-4000:]}")
        raise SystemExit(
            "Names in targets.txt resolved to no project and no group. Either they are "
            "misspelled, or the parse of kde-builder's --query output is wrong because "
            "its format changed. Fix that rather than building with a dependency list "
            "that covers only part of the tree.")
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


# A name no package will ever provide, asked for alongside the real ones. If
# rpm does not report it as missing, it is not reporting anything as missing,
# and an empty result means the check is broken rather than that everything is
# installed. package-kde.py's pick_lookup() probes its repo query the same way,
# for the same reason: a silent check reads exactly like a passing one.
SENTINEL = "kde-canary-sentinel-no-package-provides-this"


def not_installed(packages):
    """Which of those names nothing installed provides.

    Capability names work here as well as package names: rpm indexes
    cmake(Qt6Core) and pkgconfig(libzstd) the same way it indexes bzip2-devel.
    """
    p = subprocess.run(["rpm", "-q", "--whatprovides", SENTINEL, *packages],
                       capture_output=True, text=True)
    missing = {line.rsplit(" ", 1)[-1].strip()
               for line in (p.stdout + p.stderr).splitlines()
               if "no package provides" in line}
    if SENTINEL not in missing:
        raise SystemExit(
            f"rpm did not report {SENTINEL} as missing, so this check cannot tell an "
            f"installed package from an absent one and its answer means nothing. rpm "
            f"exited {p.returncode}. Its output was:\n{(p.stdout + p.stderr)[:2000]}")
    return sorted(missing - {SENTINEL})


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
