#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only OR GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
# SPDX-FileCopyrightText: 2026 Hadi Chokr <hadichokr@icloud.com>
"""Print the most recently built Fedora Kinoite rawhide image reference.

Kinoite rawhide is published in more than one place, and any one of them can
go stale or lose its moving tag on a given day: quay expires tags, and
rebuilds get paused. So no single name is trusted. Every candidate is
inspected and the newest by build date wins. The mirror's own :latest takes
part too and wins ties, so the mirror never moves backwards, is not re-copied
when nothing changed, and a day where every upstream is broken reuses it.

  resolve-base.py --mirror ghcr.io/owner/fedora-plasma-canary-kinoite:latest

Prints the winning reference on stdout, the comparison on stderr. Run it as
the same user that logged skopeo in to ghcr.io.
"""

import argparse
import datetime
import json
import os
import re
import subprocess
import sys

UPSTREAMS = os.environ.get("KINOITE_UPSTREAMS", " ".join((
    # Fedora release engineering
    "quay.io/fedora/fedora-kinoite",
    # Fedora Atomic Desktops SIG builds
    "quay.io/fedora-ostree-desktops/kinoite",
))).split()

STALE_AFTER = datetime.timedelta(days=7)

# rawhide, rawhide-20260909, 46, 46.20260909.n.0, ...
TAG = re.compile(r"^(?:rawhide(?:[-.][0-9][0-9.n]*)?|[0-9]+(?:\.[0-9n]+)*)$")


def note(msg):
    print(msg, file=sys.stderr)


def skopeo_json(*args):
    p = subprocess.run(["skopeo", *args], capture_output=True, text=True)
    if p.returncode != 0:
        return None
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return None


def built_at(ref):
    info = skopeo_json("inspect", "--no-tags", f"docker://{ref}")
    stamp = (info or {}).get("Created")
    if not stamp:
        return None
    # Go timestamps carry nanoseconds, fromisoformat takes at most micro.
    stamp = re.sub(r"(\.\d{6})\d+", r"\1", stamp).replace("Z", "+00:00")
    try:
        return datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return None


def numbers(tag):
    return tuple(int(n) for n in re.findall(r"\d+", tag))


def fedora_version(tag):
    """The Fedora release a version tag belongs to, None for other tags."""
    if not tag[0].isdigit():
        return None
    major = numbers(tag)[0]
    # Bare date tags look like versions too
    return major if major < 1000 else None


def upstream_candidates():
    listed = {}
    for repo in UPSTREAMS:
        data = skopeo_json("list-tags", f"docker://{repo}")
        if not data:
            note(f"::warning::Could not list tags on {repo}.")
            continue
        listed[repo] = [t for t in data.get("Tags") or [] if TAG.match(t)]

    # The highest Fedora release published anywhere is rawhide. Branched and
    # stable are below it and never considered, whatever their dates.
    top = max((v for tags in listed.values() for t in tags
               if (v := fedora_version(t)) is not None), default=None)

    for repo, tags in listed.items():
        moving = [t for t in tags if t == "rawhide"]
        dated = sorted((t for t in tags if t.startswith("rawhide-") or t.startswith("rawhide.")),
                       key=numbers)[-2:]
        versioned = sorted((t for t in tags if top is not None and fedora_version(t) == top),
                           key=numbers)[-2:]
        for t in moving + dated + versioned:
            yield f"{repo}:{t}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mirror", help="The mirror's current reference, wins ties")
    args = parser.parse_args()

    refs = list(dict.fromkeys(upstream_candidates()))
    if args.mirror:
        refs.append(args.mirror)

    found = []
    for ref in refs:
        when = built_at(ref)
        if when is None:
            note(f"  unresolvable      {ref}")
            continue
        note(f"  {when:%Y-%m-%d %H:%M}  {ref}")
        found.append((when, ref == args.mirror, ref))

    if not found:
        note("::error::No Kinoite rawhide image resolved anywhere, and there is no "
             "mirror to fall back to.")
        return 1

    when, _, ref = max(found)
    age = datetime.datetime.now(datetime.timezone.utc) - when
    if age > STALE_AFTER:
        note(f"::warning::The newest Kinoite rawhide image anywhere is {age.days} days "
             f"old ({ref}). Every upstream may have stalled.")
    note(f"Using {ref}")
    print(ref)
    return 0


if __name__ == "__main__":
    sys.exit(main())
