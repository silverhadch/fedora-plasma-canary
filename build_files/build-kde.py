#!/usr/bin/python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
# SPDX-FileCopyrightText: 2026 Hadi Chokr <hadichokr@icloud.com>

import os
import subprocess
import logging
import shutil
import yaml

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

KDE_BUILDER_TARGETS = [
    "ark",
    "audiocd-kio",
    "auto-chmod",
    "dolphin",
    "dolphin-plugins",
    "ffmpegthumbs",
    "kapsule",
    "kdeconnect-kde",
    "kdegraphics-thumbnailers",
    "kde-inotify-survey",
    "kdenetwork-filesharing",
    "kimageformats",
    "kio-admin",
    "kio-fuse",
    "kio-gdrive",
    "plasma-setup",
    "plasma-wayland-protocols",
    "konlineaccounts",
    "konsole",
    "kpmcore",
    "kunifiedpush",
    "kup",
    "kwalletmanager",
    "package-compatibility-helper",
    "partitionmanager",
    "pulseaudio-qt",
    "selenium-webdriver-at-spi",
    "spectacle",
    "workspace",
]


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

args = ["kde-builder", "--refresh-build"] + KDE_BUILDER_TARGETS
logger.info(f"Running: {' '.join(args)}")
process = subprocess.run(args=args)
if process.returncode != 0:
    raise Exception(f"kde-builder failed ({process.returncode})")
