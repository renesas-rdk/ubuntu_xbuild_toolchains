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
#   "Community" packages are every dependency declared in src/*/package.xml
#   (third-party deps installed as debs), EXCLUDING the workspace's own packages
#   (which are built from source). The check expands those direct dependencies
#   through each environment's installed Debian Depends/Pre-Depends graph. This
#   catches ABI providers that are only transitive dependencies.
#
#   Every direct dependency must be installed on both sides, and every package
#   present in both transitive closures must have the exact same Debian version.
#   A transitive package used only by one environment is reported as not
#   comparable; it is often a legitimate alternative/runtime-only dependency.
#   On confirmation, the script refreshes APT metadata, selects the newest exact
#   version available to BOTH environments, and installs that exact
#   package=version on each side. It never independently upgrades each side to a
#   moving "latest" candidate.
#
# Usage:
#   check_package_versions.py IP USER PASSWORD SYSROOT [SRC_DIR] [--check-only] [--yes]
#
#   --check-only  Report only; never modify either side (always safe / read-only).
#   --yes         Skip the interactive [y/N] gate and apply updates directly.
#
# Notes:
#   * All sysroot reads are batched into a single `arm64-chroot` call because the
#     wrapper holds a global lock and runs under QEMU emulation.
#   * The target board is shared lab hardware: reads are always safe, but writes
#     (apt upgrades) only ever happen after an explicit confirmation.
# -----------------------------------------------------------------------------

import argparse
from functools import cmp_to_key
import glob
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


def sysroot_dump(sysroot):
    """Query all installed packages inside the sysroot via a single chroot call."""
    env = dict(os.environ, ARM64_SYSROOT=sysroot)
    snippet = f"dpkg-query -W -f={shlex.quote(DPKG_QUERY_FORMAT)}"
    res = subprocess.run(
        ["arm64-chroot", "bash", "-c", snippet],
        capture_output=True, text=True, env=env,
    )
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
    labelled loop so each key's output is unambiguous. Returns {key: [apt,...]}.
    """
    if not keys:
        return {}
    env = dict(os.environ, ARM64_SYSROOT=sysroot)
    key_list = " ".join(shlex.quote(k) for k in keys)
    snippet = (
        f'for k in {key_list}; do '
        f'echo "ROSDEP_KEY=$k"; '
        f'rosdep resolve "$k" 2>/dev/null || true; '
        f'echo "ROSDEP_END=$k"; '
        f'done'
    )
    res = subprocess.run(
        ["arm64-chroot", "bash", "-c", snippet],
        capture_output=True, text=True, env=env,
    )
    mapping, current = {}, None
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.startswith("ROSDEP_KEY="):
            current = line[len("ROSDEP_KEY="):]
            mapping[current] = []
        elif line.startswith("ROSDEP_END="):
            current = None
        elif current and line and not line.startswith("#"):
            mapping[current].extend(line.split())
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


def build_report(scope, required_both, sysroot_packages, board_packages):
    """Compare versions in ``scope`` and enforce direct dependencies.

    ``required_both`` contains direct package.xml dependency seeds. A one-sided
    transitive package is not automatically an ABI problem because Debian
    alternatives and environment-specific runtime helpers are valid.
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
        elif (sv or bv) and apt in required_both:
            missing.append({
                "apt": apt,
                "sysroot": sv,
                "board": bv,
                "missing_from": "board" if sv else "sysroot",
            })
        else:
            absent.append(apt)
    return matched, mismatches, missing, absent


# -----------------------------------------------------------------------------
# 5. Reporting
# -----------------------------------------------------------------------------
def print_report(matched, mismatches, missing, absent, unresolved):
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
    elif not missing:
        ok("No version mismatches between the sysroot and the board.")

    if missing:
        name_w = max([len(m["apt"]) for m in missing] + [len("PACKAGE")])
        sv_w = max([len(m["sysroot"] or "-") for m in missing] + [len("SYSROOT")])
        bv_w = max([len(m["board"] or "-") for m in missing] + [len("BOARD")])
        warn(f"{len(missing)} required package(s) are installed on one side only:")
        print()
        print("    " + _c("1", f"{'PACKAGE':<{name_w}}  {'SYSROOT':<{sv_w}}  "
                                f"{'BOARD':<{bv_w}}  VERDICT"))
        for item in missing:
            sv = item["sysroot"] or "-"
            bv = item["board"] or "-"
            verdict = f"MISSING FROM {item['missing_from'].upper()}"
            print(f"    {item['apt']:<{name_w}}  {sv:<{sv_w}}  {bv:<{bv_w}}  "
                  f"{_c('1;33', verdict)}")
        print()

    if unresolved:
        warn(f"Could not map {len(unresolved)} package.xml dependency key(s); "
             "they are not part of the ABI comparison:")
        print("    " + ", ".join(sorted(unresolved)))

    not_comparable = len(absent) + len(unresolved)

    step("Summary")
    print(f"  {_c('0;32', 'in sync')}        : {len(matched)}")
    print(f"  {_c('1;33', 'mismatched')}     : {len(mismatches)}")
    print(f"  {_c('1;33', 'one side only')}  : {len(missing)}")
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
    env = dict(os.environ, ARM64_SYSROOT=sysroot)
    joined = " ".join(shlex.quote(name) for name in names)
    snippet = f"apt-cache madison {joined}"
    res = subprocess.run(
        ["arm64-chroot", "bash", "-c", snippet],
        capture_output=True, text=True, env=env,
    )
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


def install_sysroot_exact(sysroot, targets, installed):
    specs = [f"{name}={version}" for name, version in sorted(targets.items())
             if name not in installed or installed[name]["version"] != version]
    if not specs:
        ok("Sysroot already has every selected exact version.")
        return True
    env = dict(os.environ, ARM64_SYSROOT=sysroot)
    joined = " ".join(shlex.quote(spec) for spec in specs)
    snippet = ("DEBIAN_FRONTEND=noninteractive apt-get install -y "
               f"--allow-downgrades --no-remove {joined}")
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


def install_board_exact(ip, user, password, targets, installed):
    specs = [f"{name}={version}" for name, version in sorted(targets.items())
             if name not in installed or installed[name]["version"] != version]
    if not specs:
        ok("Board already has every selected exact version.")
        return True
    q = shlex.quote(password)
    joined = " ".join(shlex.quote(spec) for spec in specs)
    remote = (f"echo {q} | sudo -S -p '' env DEBIAN_FRONTEND=noninteractive "
              f"apt-get install -y --allow-downgrades --no-remove {joined}")
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
    ap.add_argument("--rosdep", action="store_true",
                    help="Deprecated compatibility option; unresolved keys are "
                         "now always passed through rosdep so the ABI check cannot "
                         "silently omit abstract system dependencies.")
    args = ap.parse_args()

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
    seeds = set().union(*resolved.values()) if resolved else set()
    sysroot_closure = dependency_closure(seeds, sysroot_packages)
    board_closure = dependency_closure(seeds, board_packages)
    scope = seeds | sysroot_closure | board_closure
    info(f"Dependency scope: {len(seeds)} direct Debian package(s), "
         f"{len(scope)} package(s) including transitive Depends/Pre-Depends.")

    matched, mismatches, missing, absent = build_report(
        scope, seeds, sysroot_packages, board_packages
    )
    print_report(matched, mismatches, missing, absent, unresolved)

    drift = mismatches + missing
    if not drift:
        ok("All comparable direct and transitive dependency packages are "
           "installed at identical versions in the sysroot and on the board.")
        return 0

    if args.check_only:
        warn(f"{len(drift)} dependency consistency error(s) found "
             "(--check-only: no changes made).")
        return 2

    update_pkgs = sorted({item["apt"] for item in drift})

    step("Proposed updates")
    warn(f"APT metadata will be refreshed on both environments. Then "
         f"{len(update_pkgs)} inconsistent package(s) will be installed at the "
         "newest EXACT version available to BOTH environments (board is "
         "SHARED LAB HARDWARE):")
    for package in update_pkgs:
        print(f"    {package}")

    if not args.yes and not confirm(
            _c("1;33", "\nApply these updates? [y/N] ")):
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
    for package, version in sorted(targets.items()):
        info(f"Selected {package}={version}")

    step("Updating sysroot")
    if install_sysroot_exact(sysroot, targets, sysroot_packages):
        step("Fixing sysroot (sysroot-fix)")
        if not fix_sysroot(sysroot):
            warn("sysroot-fix reported a failure; check CMake paths manually.")
    else:
        err("Sysroot update reported a failure.")
        return 1

    step("Updating board")
    if not install_board_exact(
            args.ip, args.user, args.password, targets, board_packages):
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
    print_report(matched2, mismatches2, missing2, absent2, unresolved)
    if mismatches2 or missing2:
        warn("Dependency versions are still inconsistent after the update. "
             "No successful consistency result will be reported.")
        return 1
    ok("All comparable direct and transitive dependency packages are now "
       "installed at identical versions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
