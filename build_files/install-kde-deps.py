#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
# SPDX-FileCopyrightText: 2026 Hadi Chokr <hadichokr@icloud.com>

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
    return resolved

def load_deps_from_yaml(url, targets):
    logger.info(f"Fetching metadata from {url}")
    with urllib.request.urlopen(url) as f:
        data = yaml.full_load(f)

    targets = set(targets)
    builddeps = set()
    rundeps = set()
    built_packages = set()

    for name, pkg in data.items():
        if name not in targets:
            continue

        builddeps.update(pkg.get("builddeps") or [])
        rundeps.update(pkg.get("rundeps") or [])
        built_packages.update(pkg.get("fedora_package") or [])
        built_packages.add(name)

    return builddeps, rundeps, built_packages

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
    parser.add_argument("--compile", action="store_true", help="Run in compilation mode to build targets and export runtime deps list")
    args = parser.parse_args()

    targets = load_targets()

    if args.compile:
        # Step 1: Run inside the transient compilation container (kde-build-container.sh)
        config_dir = "/root/.config"
        os.makedirs(config_dir, exist_ok=True)
        if os.path.exists("/ctx/kde-builder.yaml"):
            shutil.copy("/ctx/kde-builder.yaml", f"{config_dir}/kde-builder.yaml")

        run_kde_builder(["--metadata-only"])
        all_targets = get_all_build_targets(targets)
        builddeps, rundeps, built_packages = load_deps_from_yaml(KDE_DEPS_YAML, all_targets)

        # Install everything needed to compile
        to_install = (builddeps | rundeps) - built_packages
        install(to_install)

        # Export isolated runtime-only dependencies for the downstream final image
        runtime_final = rundeps - built_packages
        output_path = "/work/kde-runtime-deps.txt"
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as f:
            f.write("\n".join(sorted(runtime_final)))
        logger.info(f"Exported runtime dependencies list to {output_path}")

    else:
        # Step 2: Run inside the final image (build.sh)
        deps_file = "/ctx/kde-runtime-deps.txt"
        if os.path.exists(deps_file):
            with open(deps_file) as f:
                runtime_deps = [line.strip() for line in f if line.strip()]
            logger.info("Installing final image runtime requirements...")
            install(runtime_deps)
        else:
            logger.error(f"Required dependency tracking file missing at {deps_file}")

if __name__ == "__main__":
    main()
