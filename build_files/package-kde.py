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
    What counts as replaced is decided from evidence, after the build: a
    Fedora package that provides the same sonames, cmake(), pkgconfig() or
    .desktop IDs as the build, or owns the same files in /usr/bin or /etc.
    Module names are not trusted: in rawhide, 'attica' is the Qt4 library
    from 2014, not the KF6 framework.
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
  package-kde.py meta       Release job, on a pristine copy of the base, from
                            the project RPMs alone. Needs dnf to see the
                            distro packages, and can be rerun against an
                            earlier build's RPMs without rebuilding KDE.
"""

import concurrent.futures
import datetime
import logging
import os
import platform
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

# Source packages that are never replaced whatever the evidence says. If the
# build overlaps Qt, that is a file conflict to fix, not a reason to obsolete
# the toolkit the whole desktop runs on.
NEVER_REPLACE = re.compile(r"qt[456](-.*)?")

# Qt4/Qt5/kdelibs4-era packages. Never pulled in as siblings of a replaced
# source package: qca builds qca-qt5 next to qca-qt6, and only the latter
# overlaps the build. The Qt5 one conflicts with nothing and may be needed.
LEGACY_NAME = re.compile(r"(^|-)(qt4|qt5|kf5|kdelibs4?)($|-)", re.IGNORECASE)

# Capabilities that identify content: two packages providing one of these
# ship the same thing. mimehandler() and bare application() are excluded,
# every file manager handles inode/directory. Only versioned library names
# count: rpm provides a lib*.so without a SONAME under its bare file name,
# which is what QML plugins look like, and KF5 Sonnet's
# libsonnetquickplugin.so is not KF6 Sonnet's.
EVIDENCE = re.compile(
    r"^(?:lib[^\s/()]*\.so\.\d[\w.]*\(\)(?:\(64bit\))?"
    r"|cmake\(.+\)|pkgconfig\(.+\)|application\(.+\.desktop\)"
    r"|metainfo\(.+\)|qt6qmlimport\(.+\)|python3(?:\.\d+)?dist\(.+\))$")

# Paths the primary repo metadata carries, so dnf can match them without
# downloading filelists.
PRIMARY_PATHS = ("/usr/bin/", "/usr/sbin/", "/etc/")

# One capability in repoquery --qf output, however the list is separated
CAP_TOKEN = re.compile(r"[^\s,()]+(?:\([^\s,()]*\))+")

ARCHES = f"{platform.machine()},noarch"

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
    files = set()
    for i in range(0, len(rpms), 100):
        out = subprocess.run(["rpm", "-qpl", *rpms[i:i + 100]],
                             capture_output=True, text=True, check=True).stdout
        files.update(line for line in out.splitlines() if line.startswith("/"))
    return files


def repoquery_raw(*args):
    process = subprocess.run(["dnf5", "repoquery", f"--arch={ARCHES}", *args],
                             capture_output=True, text=True)
    if process.returncode != 0:
        raise RuntimeError(f"dnf5 repoquery {' '.join(args[:2])} failed "
                           f"({process.returncode}): {process.stderr.strip()}")
    return process.stdout


def repoquery(*args):
    return sorted({line.strip() for line in repoquery_raw(*args).splitlines() if line.strip()})


def chunks(items, size=100):
    items = sorted(items)
    return [items[i:i + size] for i in range(0, len(items), size)]


def source_map():
    """Binary package name -> source package name, for everything in the repos."""
    sources = {}
    # A literal newline: dnf5 does not terminate --qf output on its own
    for line in repoquery("--qf", "%{name} %{sourcerpm}\n"):
        name, _, srpm = line.partition(" ")
        if srpm and srpm != "(none)":
            sources[name] = srpm.rsplit("-", 2)[0]
    return sources


def evidence_of(names):
    """name -> the identifying capabilities each package provides, in one query."""
    caps, current = {}, None
    for line in repoquery_raw("--qf", "@@%{name}@@\n%{provides}\n", *sorted(names)).splitlines():
        header = re.fullmatch(r"@@(.+)@@", line.strip())
        if header:
            current = caps.setdefault(header.group(1), set())
        elif current is not None:
            current.update(t for t in CAP_TOKEN.findall(line) if EVIDENCE.match(t))
    return caps


def find_replaced(ours, our_files, sources):
    """The Fedora packages this build replaces, from evidence rather than names.

    Direct: a package that provides an identifying capability the build also
    provides, or owns a file the build also ships. Those would conflict, so
    they must go. Siblings: the other binary packages of the same source
    package, when everything identifying they provide is covered by the build
    too. That takes plasma-workspace-common and kf6-kio-doc along, and leaves
    qca-qt5 or appstream-compose alone when the build does not ship them.

    Returns (replaced, left): the siblings left in place matter too, see
    main()."""
    direct = set()
    evidence = [c for c in ours if EVIDENCE.match(c) and "," not in c]
    for chunk in chunks(evidence):
        direct.update(repoquery("--qf", "%{name}\n", "--whatprovides=" + ",".join(chunk)))
    paths = [p for p in our_files if p.startswith(PRIMARY_PATHS) and "," not in p]
    try:
        for chunk in chunks(paths):
            direct.update(repoquery("--qf", "%{name}\n", "--file=" + ",".join(chunk)))
    except RuntimeError as e:
        logger.warning(f"File ownership query failed, going by capabilities only: {e}")

    guarded = {n for n in direct if NEVER_REPLACE.fullmatch(sources.get(n, n))}
    if guarded:
        logger.error(f"The build overlaps {', '.join(sorted(guarded))}. Not obsoleting "
                     f"those; the install check will report the conflicting files.")
        direct -= guarded
    # The build is Qt6 only, and Fedora co-installs KF5 and KF6 by design, so
    # an overlap with a Qt4/Qt5/KDE 4 package is a false match. If it is a real
    # file conflict after all, the install check names the files.
    legacy = {n for n in direct if LEGACY_NAME.search(n)}
    if legacy:
        logger.warning(f"Not replacing Qt4/Qt5/KDE 4 package(s) that matched the build: "
                       f"{', '.join(sorted(legacy))}")
        direct -= legacy

    srpms = {sources[n] for n in direct if n in sources}
    siblings = {n for n, s in sources.items() if s in srpms} - direct
    candidates = {n for n in siblings if not LEGACY_NAME.search(n)}
    sibling_caps = evidence_of(candidates) if candidates else {}
    covered = {n for n in candidates if sibling_caps.get(n, set()) <= ours}
    # Every sibling not replaced stays, legacy ones included, and whatever
    # only they provide must not be re-stated as a requirement.
    left = siblings - covered
    if left:
        logger.info(f"Leaving {len(left)} sibling package(s) in place, they provide things "
                    f"the build does not: {', '.join(sorted(left))}")
    return direct | covered, left


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


def meta_spec(version, release, projects, *, replaced, downstream, requires, recommends):
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
    for name in sorted(replaced):
        lines += [f"Provides: {name} = {REPLACED_EVR}",
                  f"Provides: {name}%{{?_isa}} = {REPLACED_EVR}",
                  f"Obsoletes: {name} < {REPLACED_EVR}"]
    lines.append("")
    lines += [f"Obsoletes: {name} < {REPLACED_EVR}" for name in sorted(downstream)]
    lines.append("")
    lines += [f"Requires: {c.replace('%', '%%')}" for c in sorted(requires)]
    lines += [f"Recommends: {c.replace('%', '%%')}" for c in sorted(recommends)]
    lines += [
        "",
        "%description",
        "Every project built from KDE git master by fedora-plasma-canary, obsoleting",
        "the Fedora packages it replaces and Fedora's downstream Plasma configuration.",
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
    sources = source_map()
    downstream = {n for n in sources if any(p.fullmatch(n) for p in DOWNSTREAM_CONFIG)} - KEEP
    found, left = find_replaced(ours, our_files, sources)
    found -= KEEP
    replaced = found - downstream
    logger.info(f"The build replaces {len(replaced)} Fedora package(s), and "
                f"{len(found & downstream)} downstream config package(s) overlap it.")

    # Harvested only now, from the packages actually replaced. The distro
    # packages' own requirements are what no generator can see. What they and
    # the siblings left behind provided tells which of those requirements went
    # stale: a sibling left in place is usually tied to its replaced main
    # package by an exact-version Requires, so pointing kde-canary at it would
    # ask for a transaction that cannot exist.
    names = sorted(replaced)
    harvested = {kind: repoquery(kind, *names) if names else []
                 for kind in ("--requires", "--recommends")}
    from_replaced_sources = sorted(replaced | left)
    harvested["--provides"] = (repoquery("--provides", *from_replaced_sources)
                               if from_replaced_sources else [])
    for name, lines in (("replaced.txt", names), ("downstream.txt", sorted(downstream)),
                        ("left-siblings.txt", sorted(left)),
                        ("harvest-requires.txt", harvested["--requires"]),
                        ("harvest-recommends.txt", harvested["--recommends"])):
        with open(os.path.join(META, name), "w") as f:
            f.write("\n".join(lines) + "\n")

    meta = replaced | {name + isa for name in replaced}
    satisfied = ours | meta | our_files
    stale = {VERSIONED.sub("", c).strip() for c in harvested["--provides"]} - satisfied

    common = dict(isa=isa, downstream=downstream, satisfied=satisfied, stale=stale)
    requires = restate(harvested["--requires"], **common)
    recommends = restate(harvested["--recommends"] + read_list("fedora-rundeps.txt"), **common)
    recommends = {c for c in recommends if c not in requires and c + isa not in requires}
    logger.info(f"kde-canary: {len(replaced)} replaced, {len(downstream)} downstream obsoleted, "
                f"{len(requires)} requires and {len(recommends)} recommends re-stated.")

    rpms.append(rpmbuild("kde-canary", meta_spec(
        version, release, specs, replaced=replaced, downstream=downstream,
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
