#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
# SPDX-FileCopyrightText: 2026 Hadi Chokr <hadichokr@icloud.com>
"""Prepare the compile container and record what the build replaces.

Runs once in kde-build-container.sh, before kde-builder, and does two things.

It gets the throwaway compile container into shape: what fedora.yaml says KDE
needs to build goes in, and no distro copy of anything being built is left to
shadow the fresh one at link time. The container is discarded afterwards, so
the blunt tools are fine here: an exclude drop-in and rpm -e --nodeps.

And while dnf can still see the distro packages, it writes down what
package-kde.py needs for the kde-canary metapackage: which Fedora packages the
build replaces, expanded to whole source packages, what those provide, and
what they require and recommend. Nothing from here ships; the image gets RPMs.
"""

import argparse
import logging
import os
import platform
import re
import shutil
import subprocess
import urllib.request

import yaml

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

KDE_DEPS_YAML = "https://invent.kde.org/sysadmin/repo-metadata/-/raw/master/distro-dependencies/fedora.yaml"

# dnf5 drop-in directory (overrides /etc/dnf/dnf.conf, last one wins per key)
DNF_DROPIN = "/etc/dnf/libdnf5.conf.d/90-kde-selfbuilt.conf"

# Read by package-kde.py
META_DIR = "/work/meta"

ARCHES = f"{platform.machine()},noarch"

# Fedora's own Plasma configuration, which overrides upstream KDE defaults.
# The image ships what KDE ships, so kde-canary obsoletes these without
# providing them, and a requirement on them is never re-stated. sddm is here
# because Plasma Login Manager is built from source and replaces it.
DOWNSTREAM_CONFIG = [re.compile(p) for p in (
    r"kde-settings.*",
    r"plasma-lookandfeel-fedora",
    r"plasma-welcome-fedora",
    r"fedora-chromium-config-kde",
    r"sddm(-.*)?",
)]

# Source packages never expanded into the replaced set, whatever fedora.yaml
# maps a module to. Obsoleting Qt would take the whole desktop with it.
NEVER_EXPAND = re.compile(r"qt[56](-.*)?")


def load_targets(path="/ctx/targets.txt"):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def load_ignored_projects(path="/ctx/kde-builder.yaml"):
    """Projects listed in ignore-projects are deliberately not part of the image,
    so their distro packages are obsoleted too (e.g. oxygen, kwin-x11)."""
    if not os.path.exists(path):
        return []
    with open(path) as f:
        cfg = yaml.safe_load(f)
    return list((cfg.get("global") or {}).get("ignore-projects") or [])


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


def fedora_names(data, modules):
    """Fedora package names for kde-builder modules: fedora.yaml's mapping,
    plus the module name itself, which usually matches."""
    names = set()
    for mod in modules:
        names.add(mod)
        entry = data.get(mod) or {}
        names.update(entry.get("fedora_package") or [])
    return names


def repoquery(*args):
    process = subprocess.run(
        ["dnf5", "repoquery", f"--arch={ARCHES}", *args],
        capture_output=True,
        text=True,
    )
    if process.returncode != 0:
        raise RuntimeError(f"dnf5 repoquery {' '.join(args[:2])} failed "
                           f"({process.returncode}): {process.stderr.strip()}")
    return sorted({line.strip() for line in process.stdout.splitlines() if line.strip()})


def source_map():
    """Binary package name -> source package name, for everything in the repos."""
    sources = {}
    # A literal newline: dnf5 does not terminate --qf output on its own
    for line in repoquery("--qf", "%{name} %{sourcerpm}\n"):
        name, _, srpm = line.partition(" ")
        if srpm and srpm != "(none)":
            sources[name] = srpm.rsplit("-", 2)[0]
    return sources


def expand(names, sources):
    """Every binary package built from the same source packages as names.

    A project built from source replaces the whole Fedora source package, not
    just the binary package fedora.yaml happens to name: plasma-workspace
    comes with a dozen subpackages, and one left behind would conflict on
    files with the build."""
    srpms = {sources[n] for n in names if n in sources}
    skipped = {s for s in srpms if NEVER_EXPAND.fullmatch(s)}
    if skipped:
        logger.warning(f"Not replacing source package(s) {', '.join(sorted(skipped))}: "
                       f"fedora.yaml maps a built module to them, which is wrong.")
    srpms -= skipped
    return {n for n, s in sources.items() if s in srpms}


def write_list(name, items):
    path = os.path.join(META_DIR, name)
    with open(path, "w") as f:
        f.write("\n".join(items) + ("\n" if items else ""))
    logger.info(f"Wrote {len(items)} line(s) to {path}")


def write_dnf_dropin(excluded):
    """Make every later dnf5 invocation in this container (hotfix installs,
    group installs) refuse to pull the replaced packages back in.

    NOTE: excludepkgs is replaced, not merged, and this drop-in overrides
    /etc/dnf/dnf.conf, so the *.i686 exclude must be repeated here or it
    would silently be re-enabled."""
    os.makedirs(os.path.dirname(DNF_DROPIN), exist_ok=True)
    value = ",".join(["*.i686"] + sorted(excluded))
    with open(DNF_DROPIN, "w") as f:
        f.write("# Generated by install-kde-deps.py: packages the KDE build replaces.\n")
        f.write("# Compile container only, the image gets kde-canary's Obsoletes instead.\n")
        f.write(f"[main]\nexcludepkgs={value}\n")
    logger.info(f"Wrote dnf exclude drop-in with {len(excluded)} packages to {DNF_DROPIN}")


def remove_installed(excluded):
    """Purge every replaced package that is present (the Kinoite base ships
    most of them) so no stale distro lib shadows the fresh build at link time.
    rpm --nodeps --noscripts, because this container is thrown away."""
    installed = []
    for pkg in sorted(excluded):
        rc = subprocess.run(
            ["rpm", "-q", pkg],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        if rc == 0:
            installed.append(pkg)
    if not installed:
        logger.info("No replaced packages present, nothing to remove.")
        return
    logger.info(f"Removing {len(installed)} replaced package(s): {', '.join(installed)}")
    subprocess.run(["rpm", "-e", "--nodeps", "--noscripts"] + installed, check=True)


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
    # Accepted for compatibility; compile mode is the only mode now.
    parser.add_argument("--compile", action="store_true", help=argparse.SUPPRESS)
    parser.parse_args()

    os.makedirs(META_DIR, exist_ok=True)
    config_dir = "/root/.config"
    os.makedirs(config_dir, exist_ok=True)
    if os.path.exists("/ctx/kde-builder.yaml"):
        shutil.copy("/ctx/kde-builder.yaml", f"{config_dir}/kde-builder.yaml")

    run_kde_builder(["--metadata-only"])
    order = get_all_build_targets(load_targets())
    ignored = load_ignored_projects()

    data = fetch_deps_yaml(KDE_DEPS_YAML)
    builddeps, rundeps = collect_deps(data, order)

    # Everything below queries the repos for the distro packages, so it has to
    # happen before the drop-in hides them.
    sources = source_map()
    built = expand(fedora_names(data, order), sources)
    ignored_pkgs = expand(fedora_names(data, ignored), sources) - built
    replaced = built | ignored_pkgs
    downstream = {n for n in sources if any(p.fullmatch(n) for p in DOWNSTREAM_CONFIG)}
    logger.info(f"The build replaces {len(built)} Fedora package(s), and ignore-projects "
                f"drops {len(ignored_pkgs)} more.")

    write_list("build-order.txt", order)
    write_list("replaced.txt", sorted(replaced))
    write_list("downstream.txt", sorted(downstream))
    write_list("fedora-rundeps.txt", sorted(rundeps - replaced))
    # repoquery with no package arguments lists the whole repo, hence the guards.
    write_list("replaced-provides.txt",
               repoquery("--provides", *sorted(replaced)) if replaced else [])
    # Only what the built packages needed. The ignored ones are gone on
    # purpose, so what they pulled in is not wanted either.
    write_list("harvest-requires.txt",
               repoquery("--requires", *sorted(built)) if built else [])
    write_list("harvest-recommends.txt",
               repoquery("--recommends", *sorted(built)) if built else [])

    write_dnf_dropin(replaced)
    install((builddeps | rundeps) - replaced)
    remove_installed(replaced)


if __name__ == "__main__":
    main()
