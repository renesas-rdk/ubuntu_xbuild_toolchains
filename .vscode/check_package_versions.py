#!/usr/bin/env python3
# -----------------------------------------------------------------------------
# Description:
#   Check (and optionally synchronise) the versions of the ROS 2 "community"
#   packages that this workspace depends on, between two independent places that
#   must stay ABI-compatible:
#
#     1. TARGET_LOCAL_SYSROOT  - the ARM64 cross-compile sysroot
#        (default /opt/arm64_sysroot), queried through `arm64-chroot`.
#        This is what the host links against at build time.
#     2. The target board      - Ubuntu 24.04 ARM64 running ROS 2 Jazzy natively
#        as `ros-jazzy-*` debs, queried over SSH.
#        This is what the binaries actually run against at runtime.
#
#   Two families of packages are checked:
#
#     a) "Community" packages - every dependency declared in src/*/package.xml
#        (third-party deps installed as debs), EXCLUDING the workspace's own
#        packages (which are built from source).
#     b) Vendor packages - everything published by the extra APT repositories
#        (default https://apt.uxpai.dev), i.e. the board support packages that
#        are not referenced from any package.xml but are still linked against
#        or loaded at runtime (libcamera/libpvr/librcar-xos, kernel images...).
#        Their names are read from each environment's own APT index, so the
#        list follows the repository instead of being hardcoded here.
#
#   The check expands both families through each environment's installed Debian
#   Depends/Pre-Depends graph. This catches ABI providers that are only
#   transitive dependencies.
#
#   Only packages installed on BOTH sides are version-checked, and those must
#   match exactly. A package installed on one side only is reported but is NOT
#   an error, because the two environments are deliberately not identical:
#
#     * the sysroot carries build-time packages the board never needs
#       (-dev, -dbg, headers, static libs);
#     * the board carries runtime-only packages the sysroot never needs
#       (kernel images, firmware, services).
#
#   Such packages are also kept out of the update set, so the script never
#   pushes a -dev package onto the board. Pass --require-both to treat a
#   one-sided install as an error instead (and to let the sync install it).
#   On a first confirmation, the script refreshes APT metadata and selects the
#   newest exact version available to BOTH environments. It then prints the
#   plan (current -> target on each side), refuses any downgrade unless
#   --allow-downgrades is given, and dry-runs the install on both sides. Only
#   after a second confirmation does it install that exact package=version on
#   each side. It never independently upgrades each side to a moving "latest"
#   candidate.
#
# Usage:
#   check_package_versions.py IP USER PASSWORD SYSROOT [SRC_DIR] [--check-only] [--yes]
#
#   --check-only   Report only; never modify either side (always safe / read-only).
#   --yes          Skip both interactive [y/N] gates and apply updates directly.
#   --allow-downgrades  Accept a plan that downgrades a package on either side.
#   --repo-uri U   Extra APT repository to pull package names from (repeatable).
#                  Defaults to https://apt.uxpai.dev.
#   --no-repo-check  Skip the extra-repository check entirely.
#   --require-both Treat a package installed on one side only as an error, and
#                  let the sync install it on the side that lacks it.
#
# Notes:
#   * All sysroot reads are batched into a single `arm64-chroot` call because the
#     wrapper holds a global lock and runs under QEMU emulation. The sysroot APT
#     index files are the exception: the sysroot is a plain directory on the
#     host, so they are read directly (no QEMU, no lock).
#   * The target board is shared lab hardware: reads are always safe, but writes
#     (apt upgrades) only ever happen after an explicit confirmation.
# -----------------------------------------------------------------------------

import argparse
import bz2
from functools import cmp_to_key
import glob
import gzip
import lzma
import os
import re
import shlex
import subprocess
import sys
import xml.etree.ElementTree as ET

# SSH options mirror the rest of the workspace tooling (deploy.sh / run_program.sh).
# The board is shared lab hardware whose first (cold) connection can be slow, so
# allow a slightly longer connect timeout and retry transient failures.
SSH_OPTS = [
    "-o", "ConnectTimeout=8",
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
]
SSH_RETRIES = 3

# Every dependency tag we treat as a "community" dependency declaration.
DEP_TAGS = (
    "depend",
    "build_depend",
    "build_export_depend",
    "exec_depend",
    "test_depend",
    "buildtool_depend",
    "run_depend",  # package.xml format 1 spelling
)

# Fallback hints for abstract rosdep keys whose apt package name is NOT
# ros-jazzy-<name> and is NOT already an apt name. Most community deps either
# follow the ros-jazzy-<name> convention or are already written as apt names in
# package.xml, so only these few abstract system keys need an explicit hint.
SYSTEM_KEY_MAP = {
    "eigen": ["libeigen3-dev"],
    "opencv": ["libopencv-dev"],
    "boost": ["libboost-dev"],
    "fmt": ["libfmt-dev"],
    "spdlog": ["libspdlog-dev"],
    "yaml-cpp": ["libyaml-cpp-dev"],
    "yaml_cpp": ["libyaml-cpp-dev"],
}

# Extra (vendor) APT repositories whose ENTIRE published package set must stay
# consistent between the sysroot and the board, regardless of whether any
# package.xml mentions them. These hold the board support packages
# (libcamera/libpvr/librcar-xos, kernel images, ...) that the workspace links
# against or loads at runtime.
DEFAULT_REPO_URIS = ("https://apt.uxpai.dev",)

# apt stores each downloaded index under <root>/var/lib/apt/lists/ using a file
# name derived from the source URI: the scheme is dropped and every "/" becomes
# "_". A repository's binary indexes therefore always start with its host name
# and end with "_Packages" plus an optional compression suffix.
APT_LISTS_DIR = "var/lib/apt/lists"
APT_INDEX_SUFFIXES = ("", ".gz", ".xz", ".bz2", ".lz4", ".zst")

# `arm64-chroot` runs its command through `sudo chroot`, which resets the
# environment, so ROS_DISTRO never reaches rosdep inside the sysroot. Pass the
# distribution explicitly instead.
ROS_DISTRO = os.environ.get("ROS_DISTRO") or "jazzy"

# Printed by `arm64-chroot` when another instance holds its (non-blocking) lock.
CHROOT_BUSY_MARKER = "Another arm64-chroot instance is running"

# Keep the locally modified version of any conffile without prompting. dpkg's
# conffile prompt ignores DEBIAN_FRONTEND and would otherwise fail with
# "EOF on stdin at conffile prompt", leaving a half-configured package behind.
APT_INSTALL_OPTS = (
    "-y", "--no-remove",
    "-o", "Dpkg::Options::=--force-confdef",
    "-o", "Dpkg::Options::=--force-confold",
)

_USE_COLOR = sys.stdout.isatty()


def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def info(msg):
    print(f"{_c('0;34', '[INFO]')} {msg}")


def warn(msg):
    print(f"{_c('1;33', '[WARN]')} {msg}")


def err(msg):
    print(f"{_c('0;31', '[ERROR]')} {msg}", file=sys.stderr)


def ok(msg):
    print(f"{_c('0;32', '[OK]')} {msg}")


def step(msg):
    print(f"\n{_c('1;36', '=== ' + msg + ' ===')}")


def die(msg, code=1):
    err(msg)
    sys.exit(code)


# -----------------------------------------------------------------------------
# 1. Enumerate community dependencies from src/*/package.xml
# -----------------------------------------------------------------------------
def enumerate_community(src_dir):
    """Return (own_names, community_deps) parsed from all package.xml files under src_dir."""
    xml_files = sorted(
        glob.glob(os.path.join(src_dir, "**", "package.xml"), recursive=True)
    )

    if not xml_files:
        die(f"No package.xml found under {src_dir}")

    own_names = set()
    dep_names = set()

    for path in xml_files:
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as exc:
            warn(f"Skipping unparseable {path}: {exc}")
            continue

        name_el = root.find("name")
        if name_el is not None and name_el.text:
            own_names.add(name_el.text.strip())

        for tag in DEP_TAGS:
            for el in root.iter(tag):
                if el.text and el.text.strip():
                    dep_names.add(el.text.strip())

    community = dep_names - own_names

    info(
        f"Parsed {len(xml_files)} package.xml file(s): "
        f"{len(own_names)} workspace package(s), "
        f"{len(community)} community dependency name(s)."
    )

    return own_names, community


# -----------------------------------------------------------------------------
# 2. Read installed package metadata from the sysroot and the board
# -----------------------------------------------------------------------------
DPKG_QUERY_FORMAT = (
    r"${db:Status-Abbrev}\t${Package}\t${Version}\t${Depends}\t"
    r"${Pre-Depends}\t${Provides}\n"
)


def _parse_dump(text):
    """Parse the tab-separated package records emitted by ``dpkg-query``.

    The chroot wrapper writes banner/cleanup messages to stdout, so only lines
    with all six fields are accepted. Debian dependency fields do not contain
    tabs.
    """
    packages = {}
    for line in text.splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        status, name, version, depends, pre_depends, provides = (
            field.strip() for field in fields
        )
        if status == "ii" and name and version and " " not in name:
            packages[name] = {
                "version": version,
                "depends": depends,
                "pre_depends": pre_depends,
                "provides": provides,
            }
    return packages


def _chroot(sysroot, snippet):
    """Run ``bash -c snippet`` inside the sysroot and capture its output.

    `arm64-chroot` does not wait for its global lock: when another instance
    (typically a build) holds it, it exits at once. Report that explicitly
    instead of letting callers misread the empty output.
    """
    env = dict(os.environ, ARM64_SYSROOT=sysroot)
    res = subprocess.run(
        ["arm64-chroot", "bash", "-c", snippet],
        capture_output=True, text=True, env=env,
    )
    if res.returncode != 0 and CHROOT_BUSY_MARKER in res.stdout:
        die("Another arm64-chroot instance is holding the sysroot lock "
            "(a build or another sysroot task?). Wait for it and retry.")
    return res


def sysroot_dump(sysroot):
    """Query all installed packages inside the sysroot via a single chroot call."""
    snippet = f"dpkg-query -W -f={shlex.quote(DPKG_QUERY_FORMAT)}"
    res = _chroot(sysroot, snippet)
    packages = _parse_dump(res.stdout)
    if not packages:
        die("Could not read any package versions from the sysroot via "
            f"arm64-chroot.\n{res.stderr.strip()}")
    info(f"Sysroot: {len(packages)} installed package(s).")
    return packages


def _ssh(ip, user, password, remote, retries=SSH_RETRIES):
    """Run a remote command over SSH, retrying transient connection failures.

    Returns the last CompletedProcess. Read-only callers should treat a non-zero
    return code as "unreachable"; the board is shared hardware whose first
    connection can time out.
    """
    res = None
    for _ in range(retries):
        res = subprocess.run(
            ["sshpass", "-p", password, "ssh", *SSH_OPTS, f"{user}@{ip}", remote],
            capture_output=True, text=True,
        )
        if res.returncode == 0:
            return res
    return res


def board_probe(ip, user, password):
    res = _ssh(ip, user, password, "echo OK")
    return res is not None and res.returncode == 0 and "OK" in res.stdout


def board_dump(ip, user, password):
    """Query all installed packages on the board over SSH (read-only)."""
    snippet = f"dpkg-query -W -f={shlex.quote(DPKG_QUERY_FORMAT)}"
    res = _ssh(ip, user, password, snippet)
    packages = _parse_dump(res.stdout if res else "")
    if not packages:
        die("Could not read any package versions from the board.\n"
            f"{res.stderr.strip() if res else ''}")
    info(f"Board {ip}: {len(packages)} installed package(s).")
    return packages


# -----------------------------------------------------------------------------
# 2b. Enumerate every package published by the extra APT repositories
# -----------------------------------------------------------------------------
def repo_host(uri):
    """Host part of a repository URI ("https://apt.uxpai.dev/x" -> "apt.uxpai.dev")."""
    return uri.split("://", 1)[-1].split("/", 1)[0].strip()


def _is_packages_index(path):
    """True for an apt binary index file (optionally compressed)."""
    base = os.path.basename(path)
    return any(base.endswith("_Packages" + suffix) for suffix in APT_INDEX_SUFFIXES)


def _parse_packages_index(text):
    """Return {package: {version, ...}} from the text of a Debian Packages index.

    Continuation lines in a Packages stanza are indented, so a line starting at
    column 0 with a known field name is always that stanza's own field.
    """
    available, name = {}, None
    for line in text.splitlines():
        if line.startswith("Package: "):
            name = line[len("Package: "):].strip()
        elif line.startswith("Version: ") and name:
            available.setdefault(name, set()).add(line[len("Version: "):].strip())
        elif not line.strip():
            name = None
    return available


_INDEX_OPENERS = {"": open, ".gz": gzip.open, ".xz": lzma.open, ".bz2": bz2.open}


def _read_index_file(path):
    """Read one apt index file, transparently decompressing known suffixes.

    apt can be told to keep its lists in a codec Python has no module for
    (``.lz4`` in particular), so an unknown suffix is handed to the matching
    command line tool instead of being parsed as text.
    """
    suffix = os.path.splitext(path)[1]
    opener = _INDEX_OPENERS.get(suffix)
    if opener is None:
        tool = {".lz4": "lz4", ".zst": "zstd"}.get(suffix)
        if tool is None:
            warn(f"Unsupported APT index compression for {path}; skipped.")
            return ""
        try:
            res = subprocess.run([tool, "-cd", path],
                                 capture_output=True, text=True)
        except FileNotFoundError:
            warn(f"`{tool}` not found on PATH; cannot read APT index {path}.")
            return ""
        if res.returncode != 0:
            warn(f"Could not decompress APT index {path}: {res.stderr.strip()}")
            return ""
        return res.stdout
    try:
        with opener(path, "rt", errors="replace") as handle:
            return handle.read()
    except (OSError, EOFError, lzma.LZMAError) as exc:
        warn(f"Could not read APT index {path}: {exc}")
        return ""


def _uncommented(text):
    """Drop comment lines so a commented-out source is not read as configured."""
    return "\n".join(line for line in text.splitlines()
                      if not line.lstrip().startswith("#"))


def sysroot_repo_index(sysroot, hosts):
    """Packages published by ``hosts``, read from the sysroot's APT cache.

    The sysroot is an ordinary directory on the host, so its index and sources
    files are read directly rather than through `arm64-chroot`: no QEMU, no
    global lock. Returns ({package: {version, ...}}, {configured host, ...}).
    """
    available, configured = {}, set()
    if not hosts:
        return available, configured

    sources = ""
    source_files = [os.path.join(sysroot, "etc/apt/sources.list")]
    source_files += sorted(glob.glob(
        os.path.join(sysroot, "etc/apt/sources.list.d", "*")))
    for path in source_files:
        try:
            with open(path, "r", errors="replace") as handle:
                sources += _uncommented(handle.read()) + "\n"
        except OSError:
            continue

    lists_dir = os.path.join(sysroot, APT_LISTS_DIR)
    for host in hosts:
        if host in sources:
            configured.add(host)
        for path in sorted(glob.glob(os.path.join(lists_dir, host + "_*"))):
            if not _is_packages_index(path):
                continue
            for name, versions in _parse_packages_index(
                    _read_index_file(path)).items():
                available.setdefault(name, set()).update(versions)
    return available, configured


def _board_repo_snippet(hosts):
    """Shell snippet that dumps the board's index for each host, marker-tagged."""
    parts = []
    for host in hosts:
        quoted = shlex.quote(host)
        parts.append(
            f"if grep -rhs -- {quoted} /etc/apt/sources.list "
            f"/etc/apt/sources.list.d/ 2>/dev/null "
            f"| grep -qvE '^[[:space:]]*#'; then "
            f"printf 'REPO_CONFIGURED\\t%s\\n' {quoted}; fi"
        )
        parts.append(
            f"for f in /var/lib/apt/lists/{host}_*; do "
            f"[ -f \"$f\" ] || continue; "
            f"case \"$f\" in "
            f"*_Packages) cat \"$f\" ;; "
            f"*_Packages.gz) gzip -cd \"$f\" ;; "
            f"*_Packages.xz) xz -cd \"$f\" ;; "
            f"*_Packages.bz2) bzip2 -cd \"$f\" ;; "
            f"*_Packages.lz4) lz4 -cd \"$f\" ;; "
            f"*_Packages.zst) zstd -cd \"$f\" ;; "
            f"*) continue ;; "
            f"esac; done 2>/dev/null "
            f"| awk '/^Package: /{{p=$2}} "
            f"/^Version: /{{if (p != \"\") print \"REPO_PKG\\t\" p \"\\t\" $2}}'"
        )
    return "; ".join(parts)


def board_repo_index(ip, user, password, hosts):
    """Same information as :func:`sysroot_repo_index`, read from the board."""
    available, configured = {}, set()
    if not hosts:
        return available, configured
    res = _ssh(ip, user, password, _board_repo_snippet(hosts))
    if res is None or res.returncode != 0:
        warn("Could not read the extra APT repository index from the board.\n"
             f"{res.stderr.strip() if res else ''}")
        return available, configured
    for line in res.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 2 and fields[0] == "REPO_CONFIGURED":
            configured.add(fields[1].strip())
        elif len(fields) == 3 and fields[0] == "REPO_PKG":
            name, version = fields[1].strip(), fields[2].strip()
            if name and version:
                available.setdefault(name, set()).add(version)
    return available, configured


def collect_repo_packages(sysroot, ip, user, password, hosts,
                          sysroot_packages, board_packages):
    """Union of the package names the repositories publish to either side.

    Each side is asked separately: a repository may legitimately be pinned to a
    different snapshot on the board, and a package that only one side knows
    about is exactly the kind of drift this check exists to surface. Returns
    (published names, names installed on at least one side).
    """
    sysroot_available, sysroot_configured = sysroot_repo_index(sysroot, hosts)
    board_available, board_configured = board_repo_index(ip, user, password, hosts)

    for host in hosts:
        if host not in sysroot_configured:
            warn(f"{host} is not an enabled APT source in the sysroot.")
        if host not in board_configured:
            warn(f"{host} is not an enabled APT source on the board.")

    published = set(sysroot_available) | set(board_available)
    if not published:
        warn("No APT index found for " + ", ".join(hosts) +
             "; run `apt-get update` on both sides so their package lists can "
             "be compared (use --no-repo-check to skip this check).")
        return published, set()

    installed = {name for name in published
                 if name in sysroot_packages or name in board_packages}
    only_sysroot = sorted(set(sysroot_available) - set(board_available))
    only_board = sorted(set(board_available) - set(sysroot_available))
    info(f"{', '.join(hosts)}: {len(published)} published package(s), "
         f"{len(installed)} installed on at least one side.")
    if only_sysroot:
        warn("Published to the sysroot only (repository snapshots differ): "
             + ", ".join(only_sysroot))
    if only_board:
        warn("Published to the board only (repository snapshots differ): "
             + ", ".join(only_board))
    return published, installed


# -----------------------------------------------------------------------------
# 3. Map a package.xml dependency name to its apt package name
# -----------------------------------------------------------------------------
def candidate_apt_names(key):
    """Ordered candidate apt names for a package.xml dependency key."""
    dashed = key.replace("_", "-")
    cands = list(SYSTEM_KEY_MAP.get(key, []))
    cands += [f"ros-jazzy-{dashed}", key, dashed]
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def resolve_key(key, known):
    """Pick the first candidate apt name that is actually installed somewhere."""
    for cand in candidate_apt_names(key):
        if cand in known:
            return cand
    return None


def rosdep_resolve(keys, sysroot):
    """Best-effort fallback: resolve keys via rosdep inside the sysroot.

    Only invoked for keys that the cheap transform could not place. Uses a
    labelled loop so each key's output is unambiguous; a failing key's error
    text is tagged so it is reported instead of being read as package names.
    Only names under rosdep's ``#apt`` installer are accepted (``#pip`` and
    ``#source`` rules do not name Debian packages). Returns {key: [apt,...]}.
    """
    if not keys:
        return {}
    key_list = " ".join(shlex.quote(k) for k in keys)
    distro = shlex.quote(ROS_DISTRO)
    snippet = (
        f'for k in {key_list}; do '
        f'echo "ROSDEP_KEY=$k"; '
        f'if out=$(rosdep resolve --rosdistro={distro} "$k" 2>&1); then '
        f'printf "%s\\n" "$out"; '
        f'else printf "%s\\n" "$out" | sed "s/^/ROSDEP_ERR=/"; fi; '
        f'echo "ROSDEP_END=$k"; '
        f'done'
    )
    res = _chroot(sysroot, snippet)
    mapping, errors, current, installer = {}, {}, None, None
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.startswith("ROSDEP_KEY="):
            current, installer = line[len("ROSDEP_KEY="):], None
            mapping[current] = []
        elif line.startswith("ROSDEP_END="):
            current = None
        elif not current or not line:
            continue
        elif line.startswith("ROSDEP_ERR="):
            errors.setdefault(current, line[len("ROSDEP_ERR="):].strip())
        elif line.startswith("#ROSDEP["):
            continue
        elif line.startswith("#"):
            installer = line[1:].strip()
        elif installer == "apt":
            mapping[current].extend(line.split())

    if not mapping:
        warn("rosdep could not be run inside the sysroot; unmapped keys stay "
             f"unresolved.\n{(res.stderr or res.stdout).strip()}")
    for key, message in sorted(errors.items()):
        warn(f"rosdep resolve {key}: {message}")
    return {k: v for k, v in mapping.items() if v}


# -----------------------------------------------------------------------------
# 4. Compare versions (Debian version semantics, via host dpkg)
# -----------------------------------------------------------------------------
def deb_compare(a, b):
    """-1 if a < b, 0 if equal, 1 if a > b, using `dpkg --compare-versions`."""
    if a == b:
        return 0
    if subprocess.run(["dpkg", "--compare-versions", a, "eq", b]).returncode == 0:
        return 0
    if subprocess.run(["dpkg", "--compare-versions", a, "lt", b]).returncode == 0:
        return -1
    return 1


def resolve_dependencies(community, sysroot_packages, board_packages, sysroot):
    """Resolve package.xml dependency keys to Debian package names.

    Cheap conventional-name resolution is attempted first. Unresolved keys are
    then sent through rosdep automatically; otherwise a checker intended to be
    an ABI gate can silently omit system keys such as ``pcl`` or ``yaml-cpp``.
    One rosdep key may resolve to more than one Debian package.
    """
    known = set(sysroot_packages) | set(board_packages)
    resolved = {}
    fallback = []
    for key in sorted(community):
        apt = resolve_key(key, known)
        if apt is None:
            fallback.append(key)
        else:
            resolved[key] = {apt}

    if fallback:
        info(f"Resolving {len(fallback)} unmapped dependency key(s) via rosdep "
             "(slow under QEMU)...")
        rosdep_mapping = rosdep_resolve(fallback, sysroot)
        unresolved = []
        for key in fallback:
            apt_names = set(rosdep_mapping.get(key, []))
            if apt_names:
                resolved[key] = apt_names
            else:
                unresolved.append(key)
    else:
        unresolved = []

    return resolved, unresolved


_DEPENDENCY_DECORATION = re.compile(r"\s*(?:\([^)]*\)|\[[^]]*\]|<[^>]*>)")


def _dependency_name(value):
    """Return the Debian package name from one dependency/provides term."""
    value = _DEPENDENCY_DECORATION.sub("", value).strip()
    if not value:
        return ""
    name = value.split()[0]
    if ":" in name:
        base, qualifier = name.rsplit(":", 1)
        if qualifier in ("any", "native") or re.fullmatch(
                r"(?:arm64|amd64|armhf|i386|ppc64el|s390x)", qualifier):
            name = base
    return name


def _provider_index(packages):
    providers = {}
    for package, record in packages.items():
        for term in record["provides"].split(","):
            virtual = _dependency_name(term)
            if virtual:
                providers.setdefault(virtual, set()).add(package)
    return providers


def dependency_closure(seeds, packages):
    """Expand seeds through installed Depends and Pre-Depends relationships."""
    providers = _provider_index(packages)
    closure = set()
    pending = [name for name in seeds if name in packages]

    while pending:
        package = pending.pop()
        if package in closure:
            continue
        closure.add(package)
        record = packages[package]
        dependency_text = ",".join(
            part for part in (record["pre_depends"], record["depends"]) if part
        )
        for group in dependency_text.split(","):
            selected = set()
            for alternative in group.split("|"):
                name = _dependency_name(alternative)
                if not name:
                    continue
                if name in packages:
                    selected.add(name)
                selected.update(providers.get(name, ()))
            pending.extend(selected - closure)

    return closure


def build_report(scope, tracked, sysroot_packages, board_packages):
    """Compare versions in ``scope`` and enforce direct dependencies.

    ``tracked`` holds the direct seeds (package.xml dependencies and vendor
    repository packages). A one-sided install is collected into ``missing`` for
    those seeds and into ``absent`` for anything reached only transitively; the
    caller decides whether ``missing`` is an error (see --require-both). A
    one-sided package is not automatically an ABI problem: Debian alternatives,
    build-only -dev packages and environment-specific runtime helpers are valid.
    """
    matched, mismatches, missing, absent = [], [], [], []
    for apt in sorted(scope):
        sysroot_record = sysroot_packages.get(apt)
        board_record = board_packages.get(apt)
        sv = sysroot_record["version"] if sysroot_record else None
        bv = board_record["version"] if board_record else None
        if sv and bv:
            cmp = deb_compare(sv, bv)
            if cmp == 0:
                matched.append((apt, sv))
            else:
                mismatches.append({
                    "apt": apt,
                    "sysroot": sv,
                    "board": bv,
                    "outdated": "sysroot" if cmp < 0 else "board",
                })
        elif (sv or bv) and apt in tracked:
            missing.append({
                "apt": apt,
                "sysroot": sv,
                "board": bv,
                "missing_from": "board" if sv else "sysroot",
            })
        else:
            absent.append(apt)
    return matched, mismatches, missing, absent


def repo_status_rows(published, sysroot_packages, board_packages):
    """Per-package status of everything the extra repositories publish."""
    rows = []
    for apt in sorted(published):
        sysroot_record = sysroot_packages.get(apt)
        board_record = board_packages.get(apt)
        sv = sysroot_record["version"] if sysroot_record else None
        bv = board_record["version"] if board_record else None
        if sv and bv:
            status = "IN SYNC" if deb_compare(sv, bv) == 0 else "MISMATCH"
        elif sv:
            status = "SYSROOT ONLY"
        elif bv:
            status = "BOARD ONLY"
        else:
            status = "NOT INSTALLED"
        rows.append({"apt": apt, "sysroot": sv, "board": bv, "status": status})
    return rows


# -----------------------------------------------------------------------------
# 5. Reporting
# -----------------------------------------------------------------------------
_REPO_STATUS_COLOR = {
    "IN SYNC": "0;32",
    "MISMATCH": "1;33",
    "SYSROOT ONLY": "1;33",
    "BOARD ONLY": "1;33",
}


def print_repo_report(rows, hosts, strict):
    """Print the status of every package published by the extra repositories."""
    step("Extra APT repository packages (" + ", ".join(hosts) + ")")

    installed = [row for row in rows if row["status"] != "NOT INSTALLED"]
    if installed:
        name_w = max([len(r["apt"]) for r in installed] + [len("PACKAGE")])
        sv_w = max([len(r["sysroot"] or "-") for r in installed] + [len("SYSROOT")])
        bv_w = max([len(r["board"] or "-") for r in installed] + [len("BOARD")])
        st_w = max(len(r["status"]) for r in installed)
        print()
        print("    " + _c("1", f"{'PACKAGE':<{name_w}}  {'SYSROOT':<{sv_w}}  "
                                f"{'BOARD':<{bv_w}}  STATUS"))
        print("    " + "  ".join(["-" * name_w, "-" * sv_w, "-" * bv_w, "-" * st_w]))
        for row in installed:
            colour = _REPO_STATUS_COLOR.get(row["status"], "0")
            print(f"    {row['apt']:<{name_w}}  {(row['sysroot'] or '-'):<{sv_w}}  "
                  f"{(row['board'] or '-'):<{bv_w}}  {_c(colour, row['status'])}")
        print()

    one_sided = [r for r in installed if r["status"].endswith("ONLY")]
    if one_sided and not strict:
        info(f"{len(one_sided)} vendor package(s) are installed on one side only; "
             "not treated as errors (pass --require-both to require both sides).")

    not_installed = [row["apt"] for row in rows if row["status"] == "NOT INSTALLED"]
    if not_installed:
        info(f"{len(not_installed)} published package(s) are installed on neither "
             "side: " + ", ".join(not_installed))

    if installed and not any(r["status"] != "IN SYNC" for r in installed):
        ok("Every installed vendor repository package is at the same version on "
           "both sides.")


def print_report(matched, mismatches, missing, absent, unresolved, strict):
    step("Version comparison")

    if mismatches:
        name_w = max([len(m["apt"]) for m in mismatches] + [len("PACKAGE")])
        sv_w = max([len(m["sysroot"]) for m in mismatches] + [len("SYSROOT")])
        bv_w = max([len(m["board"]) for m in mismatches] + [len("BOARD")])
        vd_w = len("SYSROOT OUTDATED")
        warn(f"{len(mismatches)} version mismatch(es) found:")
        print()
        print("    " + _c("1", f"{'PACKAGE':<{name_w}}  {'SYSROOT':<{sv_w}}  "
                                f"{'BOARD':<{bv_w}}  VERDICT"))
        print("    " + "  ".join(["-" * name_w, "-" * sv_w, "-" * bv_w, "-" * vd_w]))
        for m in mismatches:
            verdict = ("SYSROOT OUTDATED" if m["outdated"] == "sysroot"
                       else "BOARD OUTDATED")
            print(f"    {m['apt']:<{name_w}}  {m['sysroot']:<{sv_w}}  "
                  f"{m['board']:<{bv_w}}  {_c('1;33', verdict)}")
        print()
    elif not (missing and strict):
        ok("No version mismatches between the sysroot and the board.")

    if missing:
        name_w = max([len(m["apt"]) for m in missing] + [len("PACKAGE")])
        sv_w = max([len(m["sysroot"] or "-") for m in missing] + [len("SYSROOT")])
        bv_w = max([len(m["board"] or "-") for m in missing] + [len("BOARD")])
        if strict:
            warn(f"{len(missing)} required package(s) are installed on one side "
                 "only (--require-both):")
        else:
            info(f"{len(missing)} dependency package(s) are installed on one "
                 "side only; not an error, and not part of any update:")
        verdict_colour = "1;33" if strict else "0;34"
        print()
        print("    " + _c("1", f"{'PACKAGE':<{name_w}}  {'SYSROOT':<{sv_w}}  "
                                f"{'BOARD':<{bv_w}}  VERDICT"))
        for item in missing:
            sv = item["sysroot"] or "-"
            bv = item["board"] or "-"
            verdict = f"MISSING FROM {item['missing_from'].upper()}"
            print(f"    {item['apt']:<{name_w}}  {sv:<{sv_w}}  {bv:<{bv_w}}  "
                  f"{_c(verdict_colour, verdict)}")
        print()
        if not strict:
            print("    The sysroot carries build-time packages the board never "
                  "needs (-dev,\n    headers) and the board carries runtime-only "
                  "packages the sysroot never\n    needs (kernel, firmware). Pass "
                  "--require-both to treat these as errors.")
            print()

    if unresolved:
        warn(f"Could not map {len(unresolved)} package.xml dependency key(s); "
             "they are not part of the ABI comparison:")
        print("    " + ", ".join(sorted(unresolved)))

    not_comparable = len(absent) + len(unresolved)

    step("Summary")
    print(f"  {_c('0;32', 'in sync')}        : {len(matched)}")
    print(f"  {_c('1;33', 'mismatched')}     : {len(mismatches)}")
    one_sided = _c("1;33", "one side only") if strict or not missing \
        else _c("0;34", "one side only")
    suffix = "" if strict or not missing else "  (ignored)"
    print(f"  {one_sided}  : {len(missing)}{suffix}")
    print(f"  not comparable : {not_comparable}")


# -----------------------------------------------------------------------------
# 6. Confirmation + update
# -----------------------------------------------------------------------------
def confirm(prompt):
    """Read a [y/N] answer from the controlling terminal."""
    try:
        with open("/dev/tty", "r") as tty:
            sys.stdout.write(prompt)
            sys.stdout.flush()
            return tty.readline().strip().lower() in ("y", "yes")
    except OSError:
        warn("No interactive terminal available; assuming 'no'. "
             "Re-run with --yes to apply updates non-interactively.")
        return False


def refresh_sysroot_apt(sysroot):
    env = dict(os.environ, ARM64_SYSROOT=sysroot)
    info("Sysroot command: arm64-chroot apt-get update")
    res = subprocess.run(["arm64-chroot", "apt-get", "update"], env=env)
    return res.returncode == 0


def refresh_board_apt(ip, user, password):
    q = shlex.quote(password)
    remote = f"echo {q} | sudo -S -p '' apt-get update"
    info(f"Board command: ssh {user}@{ip} '<sudo apt-get update>'")
    res = subprocess.run(
        ["sshpass", "-p", password, "ssh", *SSH_OPTS, f"{user}@{ip}", remote]
    )
    return res.returncode == 0


def _parse_madison(text, names):
    available = {name: set() for name in names}
    for line in text.splitlines():
        fields = [field.strip() for field in line.split("|")]
        if len(fields) >= 2 and fields[0] in available and fields[1]:
            available[fields[0]].add(fields[1])
    return available


def sysroot_available_versions(sysroot, names):
    joined = " ".join(shlex.quote(name) for name in names)
    res = _chroot(sysroot, f"apt-cache madison {joined}")
    if res.returncode != 0:
        die(f"Could not query sysroot APT versions:\n{res.stderr.strip()}")
    return _parse_madison(res.stdout, names)


def board_available_versions(ip, user, password, names):
    joined = " ".join(shlex.quote(name) for name in names)
    res = _ssh(ip, user, password, f"apt-cache madison {joined}")
    if res is None or res.returncode != 0:
        die("Could not query board APT versions.\n"
            f"{res.stderr.strip() if res else ''}")
    return _parse_madison(res.stdout, names)


def select_common_versions(names, sysroot_packages, board_packages,
                           sysroot_available, board_available):
    """Pick the newest version usable on both sides for every package.

    An already-installed version is usable even if it has aged out of that
    side's repository. The other side must either already have it or still be
    able to download it.
    """
    targets, unavailable = {}, {}
    for name in names:
        sysroot_versions = set(sysroot_available.get(name, ()))
        board_versions = set(board_available.get(name, ()))
        if name in sysroot_packages:
            sysroot_versions.add(sysroot_packages[name]["version"])
        if name in board_packages:
            board_versions.add(board_packages[name]["version"])
        common = sysroot_versions & board_versions
        if not common:
            unavailable[name] = (sysroot_versions, board_versions)
            continue
        targets[name] = sorted(common, key=cmp_to_key(deb_compare))[-1]
    return targets, unavailable


def install_specs(targets, installed):
    """``name=version`` specs for the targets not already installed as such."""
    return [f"{name}={version}" for name, version in sorted(targets.items())
            if name not in installed or installed[name]["version"] != version]


def _apt_install_cmd(specs, allow_downgrades, simulate=False):
    """Shell-quoted `apt-get install` command line for exact-version specs."""
    argv = ["apt-get", "install", *APT_INSTALL_OPTS]
    if simulate:
        argv.append("--simulate")
    if allow_downgrades:
        argv.append("--allow-downgrades")
    return " ".join(shlex.quote(arg) for arg in argv + list(specs))


def plan_rows(targets, sysroot_packages, board_packages):
    """Per-package current -> target versions on each side, with downgrades."""
    rows = []
    for name, target in sorted(targets.items()):
        row = {"apt": name, "target": target, "downgrade": []}
        for side, packages in (("sysroot", sysroot_packages),
                               ("board", board_packages)):
            record = packages.get(name)
            current = record["version"] if record else None
            row[side] = current
            if current and deb_compare(target, current) < 0:
                row["downgrade"].append(side)
        rows.append(row)
    return rows


def print_plan(rows, repo_published, label):
    """Print exactly what each side will be moved to, before any install."""
    def cell(current, target):
        if current == target:
            return f"{current} (unchanged)"
        return f"{current or '(not installed)'} -> {target}"

    cells = [(r["apt"], cell(r["sysroot"], r["target"]),
              cell(r["board"], r["target"])) for r in rows]
    name_w = max([len(c[0]) for c in cells] + [len("PACKAGE")])
    sv_w = max([len(c[1]) for c in cells] + [len("SYSROOT")])
    bv_w = max([len(c[2]) for c in cells] + [len("BOARD")])
    print()
    print("    " + _c("1", f"{'PACKAGE':<{name_w}}  {'SYSROOT':<{sv_w}}  "
                            f"{'BOARD':<{bv_w}}  NOTE"))
    for row, (name, sysroot_cell, board_cell) in zip(rows, cells):
        notes = []
        if row["downgrade"]:
            notes.append(_c("0;31", "DOWNGRADE " + "+".join(
                side.upper() for side in row["downgrade"])))
        if name in repo_published:
            notes.append(_c("1;35", label))
        print(f"    {name:<{name_w}}  {sysroot_cell:<{sv_w}}  "
              f"{board_cell:<{bv_w}}  {' '.join(notes)}")
    print()


_SIMULATED_INST = re.compile(r"^Inst (\S+)(?: \[([^\]]+)\])? \((\S+)")


def _parse_simulation(text):
    """{package: (old version or None, new version)} from `apt-get -s` output."""
    changes = {}
    for line in text.splitlines():
        match = _SIMULATED_INST.match(line.strip())
        if match:
            changes[match.group(1)] = (match.group(2), match.group(3))
    return changes


def simulate_sysroot(sysroot, specs, allow_downgrades):
    """Dry-run the sysroot install. Returns (ok, {package: (old, new)}, output)."""
    if not specs:
        return True, {}, ""
    res = _chroot(sysroot, _apt_install_cmd(specs, allow_downgrades,
                                            simulate=True))
    output = res.stdout + res.stderr
    return res.returncode == 0, _parse_simulation(res.stdout), output


def simulate_board(ip, user, password, specs, allow_downgrades):
    """Dry-run the board install (no sudo: a simulation needs no root)."""
    if not specs:
        return True, {}, ""
    res = _ssh(ip, user, password,
               _apt_install_cmd(specs, allow_downgrades, simulate=True))
    if res is None:
        return False, {}, ""
    output = res.stdout + res.stderr
    return res.returncode == 0, _parse_simulation(res.stdout), output


def install_sysroot_exact(sysroot, specs, allow_downgrades):
    if not specs:
        ok("Sysroot already has every selected exact version.")
        return True
    env = dict(os.environ, ARM64_SYSROOT=sysroot)
    snippet = ("DEBIAN_FRONTEND=noninteractive "
               + _apt_install_cmd(specs, allow_downgrades))
    info("Sysroot command: arm64-chroot apt-get install "
         + " ".join(specs))
    res = subprocess.run(["arm64-chroot", "bash", "-c", snippet], env=env)
    return res.returncode == 0


def fix_sysroot(sysroot):
    """Re-run `sysroot-fix` after a successful apt upgrade in the sysroot.

    An apt upgrade reinstalls each package's exported CMake target files with
    hardcoded absolute paths, so the relativisation applied at image-build time
    (see sysroot-rosdep-install.sh) must be re-applied or cross builds resolve
    the wrong prefixes. Runs the same `sysroot-fix` wrapper with ARM64_SYSROOT
    pointed at this sysroot; a missing wrapper is a warning, not a hard failure.
    """
    env = dict(os.environ, ARM64_SYSROOT=sysroot)
    info("Sysroot command: sysroot-fix")
    try:
        res = subprocess.run(["sysroot-fix"], env=env)
    except FileNotFoundError:
        warn("`sysroot-fix` not found on PATH; skipping CMake path fixups. "
             "Cross builds may resolve absolute paths from the sysroot.")
        return False
    return res.returncode == 0


def install_board_exact(ip, user, password, specs, allow_downgrades):
    if not specs:
        ok("Board already has every selected exact version.")
        return True
    q = shlex.quote(password)
    remote = (f"echo {q} | sudo -S -p '' env DEBIAN_FRONTEND=noninteractive "
              + _apt_install_cmd(specs, allow_downgrades))
    info(f"Board command: ssh {user}@{ip} "
         f"'<sudo apt-get install {' '.join(specs)}>'")
    res = subprocess.run(
        ["sshpass", "-p", password, "ssh", *SSH_OPTS, f"{user}@{ip}", remote]
    )
    return res.returncode == 0


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Check/sync ROS 2 community-package versions between the "
                    "ARM64 sysroot and the target board.")
    ap.add_argument("ip")
    ap.add_argument("user")
    ap.add_argument("password")
    ap.add_argument("sysroot", nargs="?", default="")
    ap.add_argument("src_dir", nargs="?", default="")
    ap.add_argument("--check-only", action="store_true",
                    help="Report only; never modify either side.")
    ap.add_argument("--yes", action="store_true",
                    help="Apply updates without the interactive [y/N] gate.")
    ap.add_argument("--repo-uri", action="append", metavar="URI",
                    help="Extra APT repository whose entire published package "
                         "set is checked, on top of the package.xml "
                         "dependencies. Repeatable. Defaults to "
                         + ", ".join(DEFAULT_REPO_URIS) + ".")
    ap.add_argument("--no-repo-check", action="store_true",
                    help="Skip the extra APT repository check.")
    ap.add_argument("--require-both", action="store_true",
                    help="Treat a package installed on one side only as an "
                         "error and let the sync install it on the side that "
                         "lacks it. Off by default: the sysroot legitimately "
                         "carries build-only packages (-dev, headers) and the "
                         "board legitimately carries runtime-only ones "
                         "(kernel, firmware).")
    ap.add_argument("--allow-downgrades", action="store_true",
                    help="Allow the sync to downgrade a package on either "
                         "side. Off by default: a plan that needs a downgrade "
                         "is shown and refused.")
    ap.add_argument("--rosdep", action="store_true",
                    help="Deprecated compatibility option; unresolved keys are "
                         "now always passed through rosdep so the ABI check cannot "
                         "silently omit abstract system dependencies.")
    args = ap.parse_args()

    repo_uris = () if args.no_repo_check else tuple(
        args.repo_uri or DEFAULT_REPO_URIS)
    repo_hosts = []
    for uri in repo_uris:
        host = repo_host(uri)
        if not host:
            die(f"Cannot derive a host name from repository URI {uri!r}.")
        if host not in repo_hosts:
            repo_hosts.append(host)

    sysroot = args.sysroot
    if not sysroot or sysroot.startswith("${"):
        sysroot = os.environ.get("ARM64_SYSROOT", "")
    if not sysroot or not os.path.isdir(sysroot):
        die(f"Sysroot directory not found: {sysroot!r} "
            "(pass it as arg 4 or export ARM64_SYSROOT).")

    src_dir = args.src_dir
    if not src_dir:
        # Default to <workspace>/src relative to this script (.vscode/..).
        src_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    if not os.path.isdir(src_dir):
        die(f"Source directory not found: {src_dir}")

    step("Enumerating community dependencies")
    info(f"Sysroot : {sysroot}")
    info(f"Board   : {args.user}@{args.ip}")
    info(f"Source  : {src_dir}")
    _own, community = enumerate_community(src_dir)

    step("Probing target board")
    if not board_probe(args.ip, args.user, args.password):
        die(f"Cannot reach board {args.user}@{args.ip} (SSH probe failed).")
    ok("Board reachable.")

    step("Reading installed package versions")
    board_packages = board_dump(args.ip, args.user, args.password)
    sysroot_packages = sysroot_dump(sysroot)

    resolved, unresolved = resolve_dependencies(
        community, sysroot_packages, board_packages, sysroot
    )
    direct = set().union(*resolved.values()) if resolved else set()

    repo_published, repo_installed = set(), set()
    if repo_hosts:
        step("Enumerating extra APT repository packages")
        repo_published, repo_installed = collect_repo_packages(
            sysroot, args.ip, args.user, args.password, repo_hosts,
            sysroot_packages, board_packages,
        )

    # Vendor packages are seeds like any direct dependency, so their own
    # Depends/Pre-Depends are compared too. Every seed is tracked so a one-sided
    # install stays visible in the report; --require-both decides whether that
    # is an error.
    seeds = direct | repo_installed
    sysroot_closure = dependency_closure(seeds, sysroot_packages)
    board_closure = dependency_closure(seeds, board_packages)
    scope = seeds | sysroot_closure | board_closure
    info(f"Dependency scope: {len(direct)} direct package.xml Debian "
         f"package(s), {len(repo_installed)} vendor repository package(s), "
         f"{len(scope)} package(s) including transitive Depends/Pre-Depends.")

    matched, mismatches, missing, absent = build_report(
        scope, seeds, sysroot_packages, board_packages
    )
    if repo_published:
        print_repo_report(
            repo_status_rows(repo_published, sysroot_packages, board_packages),
            repo_hosts, args.require_both,
        )
    print_report(matched, mismatches, missing, absent, unresolved,
                 args.require_both)

    # Without --require-both a one-sided package is reported but never
    # installed: pushing a -dev package onto the board (shared lab hardware)
    # would be wrong, and so would installing the board's kernel into the
    # sysroot.
    drift = mismatches + (missing if args.require_both else [])
    if not drift:
        ok("All comparable direct and transitive dependency packages are "
           "installed at identical versions in the sysroot and on the board.")
        return 0

    if args.check_only:
        warn(f"{len(drift)} dependency consistency error(s) found "
             "(--check-only: no changes made).")
        return 2

    update_pkgs = sorted({item["apt"] for item in drift})

    # Two gates: the first only allows the APT metadata refresh needed to
    # build an exact-version plan; the second shows that plan (with every
    # downgrade and every extra package apt would touch) before anything is
    # installed.
    step("Inconsistent packages")
    warn(f"{len(update_pkgs)} inconsistent package(s) need an exact version "
         "that is available to BOTH environments:")
    label = "[" + ", ".join(repo_hosts) + "]" if repo_hosts else ""
    for package in update_pkgs:
        suffix = f"  {_c('1;35', label)}" if package in repo_published else ""
        print(f"    {package}{suffix}")

    if not args.yes and not confirm(_c(
            "1;33", "\nRefresh APT metadata on both environments to build an "
                    "update plan? No package is installed yet. [y/N] ")):
        warn("Aborted by user. No changes made.")
        return 2

    step("Refreshing APT metadata")
    if not refresh_sysroot_apt(sysroot):
        err("Sysroot apt-get update failed; no packages were installed.")
        return 1
    if not refresh_board_apt(args.ip, args.user, args.password):
        err("Board apt-get update failed; no packages were installed.")
        return 1

    step("Selecting exact common versions")
    sysroot_available = sysroot_available_versions(sysroot, update_pkgs)
    board_available = board_available_versions(
        args.ip, args.user, args.password, update_pkgs
    )
    targets, unavailable = select_common_versions(
        update_pkgs, sysroot_packages, board_packages,
        sysroot_available, board_available,
    )
    if unavailable:
        err("No common installable version exists for:")
        for package, (sysroot_versions, board_versions) in unavailable.items():
            print(f"    {package}: sysroot={sorted(sysroot_versions)} "
                  f"board={sorted(board_versions)}", file=sys.stderr)
        err("Use the same RDK/ROS APT snapshot on both environments, then retry.")
        return 1
    step("Update plan")
    rows = plan_rows(targets, sysroot_packages, board_packages)
    print_plan(rows, repo_published, label)

    downgrades = [r for r in rows if r["downgrade"]]
    if downgrades and not args.allow_downgrades:
        err(f"{len(downgrades)} package(s) would be DOWNGRADED; nothing was "
            "installed. The newest version both environments can install is "
            "older than what one side already has, usually because their APT "
            "sources differ. Align the sources, or re-run with "
            "--allow-downgrades to accept the plan above.")
        return 1

    vendor_updates = [r["apt"] for r in rows if r["apt"] in repo_published]
    if vendor_updates:
        warn(f"{len(vendor_updates)} of those come from the vendor repository "
             "and may include kernel or firmware packages. Review the plan "
             "before confirming: the board reboots into whatever kernel is "
             "installed here.")

    # Dry-run both sides before touching either, so a conflict on the board
    # cannot leave the sysroot already modified.
    step("Simulating the update on both environments")
    sysroot_specs = install_specs(targets, sysroot_packages)
    board_specs = install_specs(targets, board_packages)
    simulations = (
        ("sysroot", sysroot_specs,
         simulate_sysroot(sysroot, sysroot_specs, args.allow_downgrades)),
        ("board", board_specs,
         simulate_board(args.ip, args.user, args.password, board_specs,
                        args.allow_downgrades)),
    )
    failed = False
    for side, specs, (sim_ok, changes, output) in simulations:
        if not sim_ok:
            failed = True
            err(f"Simulated install failed on the {side}:")
            tail = output.strip().splitlines()[-15:]
            print("\n".join("    " + line for line in tail), file=sys.stderr)
            continue
        requested = {spec.split("=", 1)[0] for spec in specs}
        extra = sorted(set(changes) - requested)
        ok(f"{side}: {len(specs)} requested package(s) install cleanly.")
        if extra:
            warn(f"{side}: apt would also change {len(extra)} other "
                 "package(s):")
            for name in extra:
                old, new = changes[name]
                print(f"    {name}  {old or '(not installed)'} -> {new}")
    if failed:
        err("Nothing was installed on either environment.")
        return 1

    if not args.yes and not confirm(
            _c("1;33", "\nApply exactly this plan? [y/N] ")):
        warn("Aborted by user. APT metadata was refreshed; no package was "
             "installed.")
        return 2

    step("Updating sysroot")
    if install_sysroot_exact(sysroot, sysroot_specs, args.allow_downgrades):
        step("Fixing sysroot (sysroot-fix)")
        if not fix_sysroot(sysroot):
            warn("sysroot-fix reported a failure; check CMake paths manually.")
    else:
        err("Sysroot update reported a failure.")
        return 1

    step("Updating board")
    if not install_board_exact(
            args.ip, args.user, args.password, board_specs,
            args.allow_downgrades):
        err("Board update reported a failure.")
        return 1

    step("Re-checking after update")
    board_packages2 = board_dump(args.ip, args.user, args.password)
    sysroot_packages2 = sysroot_dump(sysroot)
    sysroot_closure2 = dependency_closure(seeds, sysroot_packages2)
    board_closure2 = dependency_closure(seeds, board_packages2)
    scope2 = seeds | sysroot_closure2 | board_closure2
    matched2, mismatches2, missing2, absent2 = build_report(
        scope2, seeds, sysroot_packages2, board_packages2
    )
    if repo_published:
        print_repo_report(
            repo_status_rows(repo_published, sysroot_packages2, board_packages2),
            repo_hosts, args.require_both,
        )
    print_report(matched2, mismatches2, missing2, absent2, unresolved,
                 args.require_both)
    if mismatches2 or (missing2 and args.require_both):
        warn("Dependency versions are still inconsistent after the update. "
             "No successful consistency result will be reported.")
        return 1
    ok("All comparable direct and transitive dependency packages are now "
       "installed at identical versions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
