#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
# SPDX-FileCopyrightText: 2026 Hadi Chokr <hadichokr@icloud.com>

import os
import subprocess
import logging
import shutil

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def load_targets(path="/ctx/targets.txt"):
    with open(path) as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


KDE_BUILDER_TARGETS = load_targets()


def run_kde_builder(args):
    args = ["kde-builder"] + args
    logger.info(f"Running: {' '.join(args)}")
    process = subprocess.run(args=args, capture_output=True, text=True)
    if process.returncode != 0:
        raise Exception(
            f"kde-builder failed ({process.returncode}): {process.stdout}"
        )
    return process.stdout


# --- ccache ---
os.environ["CCACHE_DIR"] = "/ccache"

# --- Setup config ---
config_dir = "/root/.config"
os.makedirs(config_dir, exist_ok=True)
shutil.copy("/ctx/kde-builder.yaml", f"{config_dir}/kde-builder.yaml")

run_kde_builder(["--metadata-only"])

# --- TODO: Hotfix — remove Rust CMake file that conflicts with KDE's Rust builds ---
subprocess.run(["dnf5", "install", "-y", "qt6-qtwebengine-devel"])
rust_cmake = "/usr/lib64/cmake/Qt6/FindRust.cmake"
if os.path.exists(rust_cmake):
    os.remove(rust_cmake)

# --- Build ---
os.environ["CXXFLAGS"] = "-ffile-prefix-map=/builder/src/=/usr/src/debug/"

DESTDIR = os.environ.get("KDE_MASTER_INSTALL_DESTDIR", "/work/tree/install")

# --clean-build since kde-builder deprecated --refresh-build for it, and this
# runs against a fresh clone of kde-builder master on every build.
args = ["kde-builder", "--clean-build"] + KDE_BUILDER_TARGETS
logger.info(f"Running: {' '.join(args)}")
process = subprocess.run(args=args)
if process.returncode != 0:
    raise Exception(f"kde-builder failed ({process.returncode})")

# kde-builder can exit 0 having built nothing whatsoever: when it cannot
# resolve the project names, every target is quietly skipped and the run looks
# like a success. Catch that here, where kde-builder's own output is still on
# screen, rather than thirty minutes later in the packaging step.
installed = sorted(os.listdir(DESTDIR)) if os.path.isdir(DESTDIR) else []
if not installed:
    raise Exception(
        f"kde-builder exited 0 but installed nothing into {DESTDIR}. Either it "
        f"resolved no projects, in which case its messages above say why, or the "
        f"ninja hijack did not run."
    )
logger.info(f"{len(installed)} projects installed into {DESTDIR}.")
