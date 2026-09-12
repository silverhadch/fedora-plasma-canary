#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
# SPDX-FileCopyrightText: 2026 Hadi Chokr <hadichokr@icloud.com>
"""Turn the per-project install trees kde-builder left behind into RPMs.

Every project becomes kde-canary-<project>. Its Requires and Provides come
from rpmbuild's own dependency generators run over the files themselves:
sonames, pkgconfig(), cmake(), python. Nothing about link-time dependencies
is guessed or hand-maintained.

One more package, kde-canary, carries the policy:

  - It obsoletes every Fedora package the build replaces, so dnf swaps them
    out in the same transaction instead of rpm -e --nodeps leaving a hole.
    Replaced means one thing: the package owns a file this build also ships.
    That is the definition of a file conflict, so the answer is exact rather
    than inferred, and it is read from the rpmdb of the very image the swap
    will happen in. Guessing it from module names or shared capabilities is
    what obsoleted the Qt4 'attica' and KF5 Sonnet in earlier builds.
  - It provides their names, so distro packages that ask for them by name
    (kdevelop wanting kf6-ktexteditor) are still satisfied.
  - It obsoletes Fedora's downstream Plasma configuration without providing it.
  - It re-states what the replaced packages required and recommended that no
    generator can see: dlopen'd Qt plugins, QML modules, daemons reached over
    D-Bus. Whatever the build already provides is dropped from that list, and
    so is whatever only the replaced packages provided.

Two phases, because they need different places:

  package-kde.py projects   End of kde-build-container.sh, in the container
                            that did the build: the Qt pins must name the
                            exact Qt the tree compiled against.
  package-kde.py meta       Release job, inside a container of the pinned
                            base, from the project RPMs alone. Reads only
                            that image's rpmdb, so it can be rerun against an
                            earlier build's RPMs without rebuilding KDE.
"""

import concurrent.futures
import datetime
import logging
import os
import re
import shutil
import subprocess
import sys

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("package-kde")

WORK = os.environ.get("KDE_CANARY_WORK", "/work")
TREE = os.environ.get("KDE_MASTER_INSTALL_DESTDIR", f"{WORK}/tree/install")
SRC = os.environ.get("KDE_BUILDER_SRC", "/builder/src")
META = f"{WORK}/meta"
OUT = f"{WORK}/rpms"
TOPDIR = f"{WORK}/rpmbuild"
LOGS = f"{WORK}/logs/rpmbuild"

# Higher than any Fedora EVR will ever be. The epoch matters: several Gear
# packages carry Epoch: 1, and a plain version would never obsolete them.
REPLACED_EVR = "999:0"

# Never obsoleted, even when they come out of a source package the build
# replaces. Add a name here when dnf refuses the swap because something in
# Kinoite still needs a package the build does not provide.
KEEP = set()

# If any of these is missing the build is broken, whatever kde-builder said.
MUST_SHIP = (
    "/usr/bin/plasmashell",
    "/usr/bin/kwin_wayland",
    "/usr/bin/dolphin",
    "/usr/bin/konsole",
    # dolphin links it. It comes from the packagekit-qt build, not from the
    # distro PackageKit-Qt6, which kde-canary obsoletes.
    "/usr/lib64/libpackagekitqt6.so.2",
)

# Fedora's own Plasma configuration layered over upstream KDE defaults. The
# image ships what KDE ships, so kde-canary obsoletes these without providing
# them, and a requirement on them is never re-stated. sddm is here because
# Plasma Login Manager is built from source and replaces it.
DOWNSTREAM_CONFIG = [re.compile(p) for p in (
    r"kde-settings.*",
    r"plasma-lookandfeel-fedora",
    r"plasma-welcome-fedora",
    r"fedora-chromium-config-kde",
    r"sddm(-.*)?",
)]

# Packages removed even when no file of theirs collides: Fedora's Plasma
# configuration layered over upstream defaults, and projects deliberately not
# built (kde-builder's ignore-projects). Obsoleted without being provided, so
# nothing pulls them back.
CURATED_REMOVALS = [re.compile(p) for p in (
    r"oxygen(-.*)?",
    r"kwin-x11",
    r"plasma-nano",
    r"sddm-kcm",
)]

# Obsoleting any of these would take the image with it. A collision here is a
# packaging bug in the build, never something to resolve by replacing them.
PROTECTED = re.compile(
    r"^(?:filesystem|setup|glibc.*|kernel.*|systemd.*|rpm|rpm-libs|dnf5?(?:-.*)?"
    r"|libdnf5.*|bash|coreutils|util-linux.*|shadow-utils|selinux-policy.*"
    r"|ostree|bootc|rpm-ostree|python3|python3-libs)$")

# A requirement our Provides cannot satisfy. kde-canary provides every
# replaced name at epoch 999, so >= and > are covered; an exact or upper-bound
# requirement on a replaced package is not, and whatever carries it has to go
# in the same transaction. This is what takes plasma-workspace-common along
# with plasma-workspace: subpackages pin their siblings to an exact version.
UNSATISFIABLE = re.compile(r"^(\S+?)(\(\w[\w-]*\))?\s*(?:=|<=|<)\s*\S+$")

VERSIONED = re.compile(r"\s+(?:<=|>=|=|<|>)\s+\S+$")

# Keep what kde-builder installed byte for byte: no stripping, no shebang
# mangling, no rpath checks, no build-id links. The dependency generators run
# during file classification, not in these hooks, so they are unaffected.
# Plugins and QML modules are loaded, never linked, so they provide nothing:
# rpm would advertise a SONAME-less lib*.so under its bare file name, the same
# name the Qt5 build of the same plugin carries.
SPEC_PREAMBLE = """\
%global __provides_exclude_from ^%{_libdir}/qt6/(plugins|qml)/.*\\.so$
%global debug_package %{nil}
%global __os_install_post %{nil}
%global __arch_install_post %{nil}
%global _build_id_links none
%global _missing_build_ids_terminate_build 0
%global _binary_payload w6.zstdio
"""


def read_list(name):
    path = os.path.join(META, name)
    if not os.path.exists(path):
        logger.warning(f"{path} is missing, treating it as empty.")
        return []
    with open(path) as f:
        return [line.strip() for line in f if line.strip() and not line.startswith("#")]


def rpm_eval(expr):
    return subprocess.run(["rpm", "--eval", expr], capture_output=True,
                          text=True, check=True).stdout.strip()


def tree(project):
    return os.path.join(TREE, project)


def projects_in_order():
    """Projects with an install tree, in kde-builder's build order."""
    present = {d for d in os.listdir(TREE) if os.path.isdir(tree(d))}
    order = [p for p in dict.fromkeys(read_list("build-order.txt")) if p in present]
    return order + sorted(present - set(order))


def walk(root):
    """Yield (path, kind) under root. kind is 'file' for files and symlinks,
    'dir' for empty directories, which rpm only keeps if they are listed."""
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        for d in list(dirnames):
            if os.path.islink(os.path.join(dirpath, d)):
                dirnames.remove(d)
                filenames.append(d)
        if rel != "." and not dirnames and not filenames:
            yield "/" + rel, "dir"
        for name in filenames:
            yield "/" + os.path.normpath(os.path.join(rel, name)), "file"


def claim_files(projects):
    """Map every installed path to the project that ships it.

    Two projects installing the same path would be a file conflict inside the
    build. On the build machine the one built later overwrote the other, so
    the later one keeps it here too. Also repairs symlinks that point into the
    DESTDIR rather than at the installed location."""
    owner = {}
    for project in projects:
        root = tree(project)
        for path, kind in walk(root):
            if kind != "file":
                continue
            full = root + path
            if os.path.islink(full):
                target = os.readlink(full)
                if target.startswith(TREE + "/"):
                    rest = target[len(TREE) + 1:]
                    fixed = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
                    logger.warning(f"{project}: {path} pointed into the DESTDIR, "
                                   f"relinking to {fixed}")
                    os.remove(full)
                    os.symlink(fixed, full)
            if path in owner:
                logger.warning(f"{path} is installed by both {owner[path]} and "
                               f"{project}; {project} was built later and keeps it.")
                os.remove(tree(owner[path]) + path)
            owner[path] = project
    return owner


def spec_path(path):
    """Quote a path for a %files list. rpm takes a quoted path literally,
    whitespace and glob characters included, so only % needs escaping."""
    if '"' in path or "\n" in path:
        sys.exit(f"Cannot package a file name containing a quote or newline: {path!r}")
    return '"' + path.replace("%", "%%") + '"'


def file_caps(root):
    """File capabilities set during install (kwin_wayland gets CAP_SYS_NICE).
    rpm does not read xattrs from the buildroot, they must be spelled out."""
    if not shutil.which("getcap"):
        return {}
    out = subprocess.run(["getcap", "-r", root], capture_output=True, text=True).stdout
    caps = {}
    for line in out.splitlines():
        path, _, cap = line.partition(" ")
        cap = cap.strip().lstrip("=").strip()
        if cap:
            caps["/" + os.path.relpath(path, root)] = cap
    return caps


def is_elf(path):
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


def private_qt_libs(elves):
    """Qt libraries whose private API these binaries use.

    Qt tags its private symbols with the Qt_6_PRIVATE_API version, so the
    version-needs section says exactly which library they come from. rpm's
    generator turns that into libQt6Gui.so.6(Qt_6_PRIVATE_API)(64bit), which
    every Qt 6 build satisfies, so it does not help."""
    libs = set()
    for i in range(0, len(elves), 200):
        out = subprocess.run(["readelf", "-V", "-W", *elves[i:i + 200]],
                             capture_output=True, text=True).stdout
        needed = None
        for line in out.splitlines():
            if "File:" in line and "Cnt:" in line:
                needed = line.split("File:", 1)[1].split()[0]
            elif "Name: Qt_6_PRIVATE_API" in line and needed:
                libs.add(needed)
    return libs


def qt_pins(sonames):
    """Exact-version Requires on the packages owning those libraries."""
    pins = set()
    for soname in sorted(sonames):
        path = next((p for p in (f"/usr/lib64/{soname}", f"/usr/lib/{soname}")
                     if os.path.exists(p)), None)
        if path is None:
            logger.warning(f"{soname} uses Qt private API but is not installed, not pinned.")
            continue
        p = subprocess.run(
            ["rpm", "-qf", "--qf",
             "%{NAME} %|EPOCH?{%{EPOCH}:}:{}|%{VERSION}-%{RELEASE}\n", path],
            capture_output=True, text=True)
        if p.returncode != 0:
            logger.warning(f"{path} is not owned by any package, not pinned.")
            continue
        name, evr = p.stdout.splitlines()[0].split()
        pins.add((name, evr))
    return pins


def write_filelist(project, dest):
    root = tree(project)
    caps = file_caps(root)
    lines, files, elves = [], [], []
    for path, kind in walk(root):
        quoted = spec_path(path)
        if kind == "dir":
            lines.append(f"%dir {quoted}")
            continue
        full = root + path
        attrs = ""
        if path.startswith("/etc/") and not os.path.islink(full):
            attrs += "%config(noreplace) "
        if path in caps:
            attrs += f"%caps({caps[path]}) "
        lines.append(attrs + quoted)
        files.append(path)
        if not os.path.islink(full) and os.path.isfile(full) and is_elf(full):
            elves.append(full)
    with open(dest, "w") as f:
        f.write("\n".join(lines) + "\n")
    return files, elves


def git(project, *args):
    p = subprocess.run(["git", "-C", os.path.join(SRC, project), *args],
                       capture_output=True, text=True)
    return p.stdout.strip() if p.returncode == 0 else ""


def upstream_url(project):
    url = git(project, "remote", "get-url", "origin")
    if url.startswith("kde:"):
        url = "https://invent.kde.org/" + url[4:]
    return url.removesuffix(".git") or "https://invent.kde.org"


def licenses(project):
    """REUSE license list, which every KDE repository carries."""
    d = os.path.join(SRC, project, "LICENSES")
    names = []
    if os.path.isdir(d):
        names = sorted(os.path.splitext(f)[0] for f in os.listdir(d) if f.endswith(".txt"))
    return " AND ".join(names) or "LicenseRef-Unknown"


def project_spec(project, version, filelist, pins):
    url = upstream_url(project)
    commit = git(project, "rev-parse", "--short=12", "HEAD") or "unknown"
    lines = [
        SPEC_PREAMBLE,
        f"Name:    kde-canary-{project}",
        f"Version: {version}",
        "Release: 1%{?dist}",
        f"Summary: {project} built from KDE git master",
        f"License: {licenses(project)}",
        f"URL:     {url}",
    ]
    lines += [f"Requires: {name}%{{?_isa}} = {evr}" for name, evr in sorted(pins)]
    lines += [
        "",
        "%description",
        f"{project} from {url} at commit {commit}, built by fedora-plasma-canary.",
        "",
        "%install",
        "mkdir -p %{buildroot}",
        f"cp -a --reflink=auto '{tree(project)}/.' %{{buildroot}}/",
        "",
        f"%files -f {filelist}",
        "",
    ]
    return "\n".join(lines)


def rpmbuild(name, spec_text):
    top = os.path.join(TOPDIR, name)
    os.makedirs(top, exist_ok=True)
    spec = os.path.join(top, f"{name}.spec")
    with open(spec, "w") as f:
        f.write(spec_text)
    log = os.path.join(LOGS, f"{name}.log")
    with open(log, "w") as logf:
        rc = subprocess.run(["rpmbuild", "-bb", "--define", f"_topdir {top}", spec],
                            stdout=logf, stderr=subprocess.STDOUT).returncode
    if rc != 0:
        with open(log) as f:
            tail = "".join(f.readlines()[-30:])
        raise RuntimeError(f"rpmbuild failed for {name}, see {log}:\n{tail}")
    built = [os.path.join(d, f) for d, _, fs in os.walk(os.path.join(top, "RPMS"))
             for f in fs if f.endswith(".rpm")]
    if len(built) != 1:
        raise RuntimeError(f"Expected one RPM for {name}, got {built}")
    # Versionless file name: the release is recreated daily and build.yml
    # installs exactly what manifest.txt lists.
    dest = os.path.join(OUT, f"{name}.rpm")
    shutil.move(built[0], dest)
    shutil.rmtree(top, ignore_errors=True)
    return dest


def provides_of(rpms):
    caps = set()
    for i in range(0, len(rpms), 100):
        out = subprocess.run(["rpm", "-qp", "--provides", *rpms[i:i + 100]],
                             capture_output=True, text=True, check=True).stdout
        caps.update(VERSIONED.sub("", line).strip() for line in out.splitlines() if line.strip())
    return caps


def files_of(rpms):
    """Every non-directory path the built packages ship."""
    files = set()
    for i in range(0, len(rpms), 100):
        out = rpm_query("-qp", "--qf", "[%{FILENAMES}\t%{FILEMODES:perms}\n]", *rpms[i:i + 100])
        for line in out.splitlines():
            path, _, perms = line.rpartition("\t")
            if path.startswith("/") and not perms.startswith("d"):
                files.add(path)
    return files


def rpm_query(*args):
    """rpm against the rpmdb of the image this runs in."""
    return subprocess.run(["rpm", *args], capture_output=True, text=True,
                          check=True).stdout


def installed():
    """Every installed package: its EVR, and every non-directory path it owns.

    One query rather than rpm -qf per file, because rpm -qf needs the file to
    exist on disk and most of these will not: the container is the base image,
    not the built one. Directories are skipped, since sharing /usr/bin with
    the filesystem package is not a conflict."""
    evr, owner = {}, {}
    current = None
    for line in rpm_query("-qa", "--qf",
                          "@@%{NAME}\t%|EPOCH?{%{EPOCH}:}:{}|%{VERSION}-%{RELEASE}\n"
                          "[%{FILENAMES}\t%{FILEMODES:perms}\n]").splitlines():
        if line.startswith("@@"):
            current, _, version = line[2:].partition("\t")
            evr[current] = version
        elif "\t" in line and current:
            path, _, perms = line.rpartition("\t")
            if not perms.startswith("d"):
                owner.setdefault(path, current)
    return evr, owner


def requirements():
    """name -> its requirements, for every installed package."""
    reqs, current = {}, None
    for line in rpm_query("-qa", "--qf", "@@%{NAME}\n[%{REQUIRENEVRS}\n]").splitlines():
        if line.startswith("@@"):
            current = reqs.setdefault(line[2:], set())
        elif current is not None and line.strip():
            current.add(line.strip())
    return reqs


def find_replaced(our_files, evr, owner):
    """Installed packages this build cannot coexist with.

    Every package owning a file the build also ships, plus the curated
    removals, plus the closure of whatever then carries a requirement kde-canary
    cannot satisfy. No inference from names or capabilities: a file collision
    is the only thing that forces a package out, and it is not a judgement
    call."""
    replaced = {owner[path] for path in our_files if path in owner}
    logger.info(f"{len(replaced)} installed package(s) own files this build ships.")

    fatal = {n for n in replaced if PROTECTED.fullmatch(n)}
    if fatal:
        for name in sorted(fatal):
            example = next(p for p in sorted(our_files) if owner.get(p) == name)
            logger.error(f"The build ships {example}, owned by {name}.")
        sys.exit(f"Refusing to obsolete {', '.join(sorted(fatal))}. The build is shipping "
                 f"files it has no business shipping; fix that rather than replacing them.")

    curated = {n for n in evr if any(p.fullmatch(n) for p in CURATED_REMOVALS)} - replaced
    if curated:
        logger.info(f"Also removing {len(curated)} package(s) by policy: "
                    f"{', '.join(sorted(curated))}")
        replaced |= curated

    reqs = requirements()
    while True:
        stranded = set()
        for name, needs in reqs.items():
            if name in replaced:
                continue
            for need in needs:
                match = UNSATISFIABLE.match(need)
                if match and match.group(1) in replaced:
                    stranded.add(name)
                    break
        if not stranded:
            return replaced
        logger.info(f"Also removing {len(stranded)} package(s) pinned to an exact version "
                    f"of something replaced: {', '.join(sorted(stranded))}")
        replaced |= stranded


def restate(caps, *, isa, downstream, satisfied, stale):
    """The requirements of the replaced packages that still need saying.

    Versions are dropped: they were written against the Fedora builds being
    replaced. A requirement the build already satisfies is redundant, one only
    the replaced packages satisfied is stale, and downstream config is out by
    policy. What is left points outside the build, which is exactly the part
    no generator can see."""
    kept, dropped = set(), set()
    for cap in caps:
        if cap.startswith(("rpmlib(", "config(")):
            continue
        if cap.startswith("("):
            # Rich dependency, kept verbatim unless it names downstream config
            if set(re.findall(r"[^\s()]+", cap)) & downstream:
                continue
            kept.add(cap)
            continue
        name = VERSIONED.sub("", cap).strip()
        bare = name[:-len(isa)] if isa and name.endswith(isa) else name
        if bare in downstream or any(p.fullmatch(bare) for p in DOWNSTREAM_CONFIG):
            continue
        if name in satisfied or bare in satisfied:
            continue
        if name in stale:
            dropped.add(name)
            continue
        kept.add(name)
    if dropped:
        logger.info(f"Not re-stating {len(dropped)} requirement(s) that only the replaced "
                    f"packages provided: {', '.join(sorted(dropped))}")
    return kept


def meta_spec(version, release, projects, *, replaced, curated, requires, recommends):
    # Release is written out rather than %{?dist}: this runs in a different
    # container from the project builds, and the exact version-release pairing
    # below must match them to the letter.
    lines = [
        "Name:    kde-canary",
        f"Version: {version}",
        f"Release: {release}",
        "Summary: KDE git master in place of Fedora's KDE packages",
        "License: MIT",
        "URL:     https://invent.kde.org",
        "",
    ]
    lines += [f"Requires: kde-canary-{p}%{{?_isa}} = {version}-{release}" for p in projects]
    lines.append("")
    # Provided as well as obsoleted, so anything left in the image that asks
    # for one of these by name still resolves. Epoch 999 beats every Fedora
    # EVR, including the Epoch: 1 several Gear packages carry.
    for name in sorted(replaced):
        lines += [f"Provides: {name} = {REPLACED_EVR}",
                  f"Provides: {name}%{{?_isa}} = {REPLACED_EVR}",
                  f"Obsoletes: {name} < {REPLACED_EVR}"]
    lines.append("")
    # Obsoleted without being provided: these are meant to be gone.
    lines += [f"Obsoletes: {name} < {REPLACED_EVR}" for name in sorted(curated)]
    lines.append("")
    lines += [f"Requires: {c.replace('%', '%%')}" for c in sorted(requires)]
    lines += [f"Recommends: {c.replace('%', '%%')}" for c in sorted(recommends)]
    lines += [
        "",
        "%description",
        "Every project built from KDE git master by fedora-plasma-canary, obsoleting",
        "the Fedora packages whose files it replaces.",
        "",
        "%files",
        "",
    ]
    return "\n".join(lines)


def phase_projects():
    """Phase one: an RPM per project, in the container that built them."""
    if not os.path.isdir(TREE) or not os.listdir(TREE):
        sys.exit(f"Nothing to package: {TREE} is empty. Did ninja-hijack.rb run?")
    for d in (OUT, TOPDIR):
        shutil.rmtree(d, ignore_errors=True)
    for d in (OUT, TOPDIR, LOGS):
        os.makedirs(d, exist_ok=True)

    version = "0^" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M")
    projects = projects_in_order()
    logger.info(f"Packaging {len(projects)} projects as version {version}.")

    owner = claim_files(projects)
    missing = [p for p in MUST_SHIP if p not in owner]
    if missing:
        sys.exit(f"The build is missing {', '.join(missing)}. Refusing to package it.")

    specs = {}
    for project in projects:
        filelist = os.path.join(TOPDIR, f"{project}.files")
        files, elves = write_filelist(project, filelist)
        if not files:
            logger.info(f"{project} installed no files, skipping it.")
            continue
        pins = qt_pins(private_qt_libs(elves))
        if pins:
            logger.info(f"{project}: pinned to " +
                        ", ".join(f"{n} = {e}" for n, e in sorted(pins)))
        specs[project] = project_spec(project, version, filelist, pins)

    workers = os.cpu_count() or 4
    logger.info(f"Running rpmbuild for {len(specs)} projects, {workers} at a time...")
    rpms = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(rpmbuild, f"kde-canary-{p}", s): p for p, s in specs.items()}
        for future in concurrent.futures.as_completed(futures):
            rpms.append(future.result())

    logger.info(f"Wrote {len(rpms)} project packages to {OUT}. "
                f"package-kde.py meta builds kde-canary from them.")


def phase_meta():
    """Phase two: kde-canary, from the project RPMs and the repos."""
    os.makedirs(LOGS, exist_ok=True)
    os.makedirs(TOPDIR, exist_ok=True)
    os.makedirs(META, exist_ok=True)
    stale_meta = os.path.join(OUT, "kde-canary.rpm")
    if os.path.exists(stale_meta):
        os.remove(stale_meta)
    rpms = sorted(os.path.join(OUT, f) for f in os.listdir(OUT)
                  if f.startswith("kde-canary-") and f.endswith(".rpm"))
    if not rpms:
        sys.exit(f"No kde-canary-*.rpm in {OUT}. Run package-kde.py projects first.")

    info = subprocess.run(["rpm", "-qp", "--qf", "%{NAME} %{VERSION} %{RELEASE}\n", *rpms],
                          capture_output=True, text=True, check=True).stdout.split("\n")
    rows = [line.split() for line in info if line.strip()]
    evrs = {(v, r) for _, v, r in rows}
    if len(evrs) != 1:
        sys.exit(f"The project RPMs come from more than one build: {sorted(evrs)}")
    (version, release), = evrs
    specs = sorted(name.removeprefix("kde-canary-") for name, _, _ in rows)
    isa = rpm_eval("%{?_isa}")
    our_files = files_of(rpms)
    logger.info(f"Building kde-canary {version}-{release} over {len(specs)} project packages.")

    ours = provides_of(rpms)
    evr, owner = installed()
    logger.info(f"The base image has {len(evr)} packages owning {len(owner)} files.")

    found = find_replaced(our_files, evr, owner) - KEEP
    curated = {n for n in found if any(p.fullmatch(n) for p in DOWNSTREAM_CONFIG)
               or any(p.fullmatch(n) for p in CURATED_REMOVALS)}
    replaced = found - curated
    logger.info(f"kde-canary replaces {len(replaced)} package(s) and removes "
                f"{len(curated)} more outright.")

    # Harvested from the packages actually being removed, straight out of the
    # same rpmdb. These are the dependency edges that disappear with them:
    # the dlopen'd plugins, the daemons reached over D-Bus, the weak deps
    # Fedora hangs the desktop off. No generator can see them from the files.
    names = sorted(found)
    harvested = {kind: set(rpm_query("-q", kind, *names).splitlines()) if names else set()
                 for kind in ("--requires", "--recommends", "--provides")}
    for name, lines in (("replaced.txt", sorted(replaced)), ("removed.txt", sorted(curated)),
                        ("harvest-requires.txt", sorted(harvested["--requires"])),
                        ("harvest-recommends.txt", sorted(harvested["--recommends"]))):
        with open(os.path.join(META, name), "w") as f:
            f.write("\n".join(lines) + "\n")

    meta = replaced | {name + isa for name in replaced}
    satisfied = ours | meta | our_files
    # What only the removed packages provided goes stale with them.
    stale = {VERSIONED.sub("", c).strip() for c in harvested["--provides"]} - satisfied

    common = dict(isa=isa, downstream=curated, satisfied=satisfied, stale=stale)
    requires = restate(harvested["--requires"], **common)
    recommends = restate(harvested["--recommends"] | set(read_list("fedora-rundeps.txt")), **common)
    recommends = {c for c in recommends if c not in requires and c + isa not in requires}
    logger.info(f"kde-canary: {len(requires)} requires and {len(recommends)} recommends "
                f"re-stated from the packages it removes.")

    rpms.append(rpmbuild("kde-canary", meta_spec(
        version, release, specs, replaced=replaced, curated=curated,
        requires=requires, recommends=recommends)))

    packages = sorted(os.path.basename(r) for r in rpms)
    with open(os.path.join(OUT, "manifest.txt"), "w") as f:
        f.write("\n".join(packages) + "\n")
    logger.info(f"Wrote kde-canary and manifest.txt ({len(packages)} packages) to {OUT}.")


def main():
    phase = sys.argv[1] if len(sys.argv) > 1 else ""
    if phase == "projects":
        phase_projects()
    elif phase == "meta":
        phase_meta()
    else:
        sys.exit("usage: package-kde.py projects|meta")


if __name__ == "__main__":
    main()
