
# CMake Configure Errors (known issues)

Triage table for **CMake configure-time** failures in this workspace.
Reference for `arm64-cross-build`; read it when the build-failure
decision tree sends you here.

## Triage: pick the issue by its signature

| Error signature | Issue | Section |
|---|---|---|
| `Could not find a package configuration file provided by "X"` **and** the config file *does* exist in `$ARM64_SYSROOT` | Stale `X_DIR-NOTFOUND` in `CMakeCache.txt` | Issue 1 |
| `Could not find a package configuration file provided by "X"` **and** nothing matching exists in `$ARM64_SYSROOT` | Dependency genuinely missing from the sysroot | Issue 2 |
| `The imported target "..." references a file but this file does not exist` / error path contains a bare `/opt/ros/jazzy/...` or `/usr/lib/...` (no `$ARM64_SYSROOT` prefix) | Non-relocatable hardcoded path in a sysroot CMake export | Issue 3 |

**Always run the discriminator first** — it decides between Issue 1 and
Issue 2, which have opposite fixes:

```bash
# 1. Does the package config actually exist in the sysroot?
find "$ARM64_SYSROOT" \( -name "<pkg>Config.cmake" -o -name "<pkg>-config.cmake" \) 2>/dev/null

# 2. What did the failing build's cache record for it?
grep -i "<pkg>_DIR" build/<failing_package>/CMakeCache.txt
```

- Found on disk **+** `<pkg>_DIR-NOTFOUND` in cache → **Issue 1**.
- Not found on disk → **Issue 2**.

---

## Issue 1 — stale `-NOTFOUND` cached in `CMakeCache.txt`

### Why it happens

`find_package()` results are cached in `<pkg>_DIR` as a `PATH` cache
entry, including the failure value `<pkg>_DIR-NOTFOUND`. If a package
is configured *before* its dependency is installed into the sysroot,
that failure is written to `build/<pkg>/CMakeCache.txt` and **CMake
never re-searches it**.

`cross-colcon-build` always passes `--cmake-force-configure`, which
re-runs the configure step but **keeps the existing cache**. So the
build keeps failing with the identical error long after
`sysroot-rosdep-install` fixed the underlying problem. The timestamps
give it away: `build/<pkg>/CMakeCache.txt` is older than the
dependency's files in `$ARM64_SYSROOT`.

### Fix

```bash
# Preferred: clear the cache for just the affected packages.
cross-colcon-build --packages-select <pkg> [<pkg2> ...] --cmake-clean-cache

# Nuclear option, if several packages are affected.
rm -rf build/ install/ log/ && cross-colcon-build
```

`--cmake-clean-cache` is on `cross-colcon-build`'s colcon-arg allowlist,
so it may be passed directly.

**Do not** "fix" this class of failure by editing `CMakeLists.txt`,
adding `-D<pkg>_DIR=...`, appending to `CMAKE_PREFIX_PATH`, or adding
a `sysroot-fix` rule. The search paths are already correct; only the
cache is wrong.

### Sweep for other victims

One late sysroot install usually poisons every package that was
configured before it. Find them all before rebuilding:

```bash
grep -l "_DIR-NOTFOUND" build/*/CMakeCache.txt
```

Ignore these — they are found in **module** mode, so a `NOTFOUND`
config-mode `_DIR` is normal and harmless:

```
OpenSSL_DIR  PkgConfig_DIR  Python3_DIR  Threads_DIR
```

Anything else in that list is a real stale entry.

---

## Issue 2 — dependency genuinely absent from the sysroot

The config file is nowhere under `$ARM64_SYSROOT`. Install it into the
sysroot, then rebuild:

```bash
# Preferred: driven by <depend> entries in src/**/package.xml.
sysroot-rosdep-install

# If the dep is not expressible as a rosdep key.
arm64-chroot apt update
arm64-chroot apt install -y <libfoo-dev>   # never prefix with sudo

cross-colcon-build --packages-up-to <pkg>
```

Because this install happens *after* other packages configured, it
frequently creates Issue 1 in sibling build dirs. **After any
`sysroot-rosdep-install` or sysroot `apt install`, run the Issue 1
sweep** (`grep -l "_DIR-NOTFOUND" build/*/CMakeCache.txt`) and clear
the caches it reports.

---

## Issue 3 — non-relocatable hardcoded paths in sysroot CMake exports

### Why it happens

The sysroot is a relocated copy of the board rootfs, so CMake export
files that hardcode `/opt/ros/jazzy` or `/usr/lib/aarch64-linux-gnu/...`
point outside `$ARM64_SYSROOT` and resolve to host paths or nothing.

Recognition: the error names a file under `$ARM64_SYSROOT/.../cmake/...`
but the *missing* path it complains about has **no** `$ARM64_SYSROOT`
prefix.

### Fix

`sysroot-rosdep-install` runs `sysroot-fix` automatically, which applies
rules from `/home/ubuntu/toolchains/sysroot-fix.yaml` plus the
gitignored, user-local `sysroot-fix-append.yaml`.

Add new rules to **`sysroot-fix-append.yaml`** — `sysroot-fix.yaml` is
tracked with the toolchain and is overwritten by container updates.

```bash
# 1. Locate the hardcoded path.
grep -RIn "/opt/ros/${ROS_DISTRO}" \
  "$ARM64_SYSROOT/opt/ros/${ROS_DISTRO}/lib/aarch64-linux-gnu/cmake/<pkg>"

# 2. Append a rule to /home/ubuntu/toolchains/sysroot-fix-append.yaml:
#    <pkg>:
#      - file: "${ARM64_SYSROOT}/opt/ros/${ROS_DISTRO}/lib/aarch64-linux-gnu/cmake/<pkg>/<pkg>Targets-none.cmake"
#        find: "/opt/ros/${ROS_DISTRO}"
#        replace: "${_IMPORT_PREFIX}"
#    (${ARM64_SYSROOT} and ${ROS_DISTRO} are expanded by the tool.)

# 3. Preview, then apply. Editing the YAML does NOT auto-apply.
sysroot-fix <pkg> --dry-run
sysroot-fix

# 4. Rebuild.
cross-colcon-build --packages-up-to <pkg>
```

Replacement is normally `${_IMPORT_PREFIX}`; use
`${_IMPORT_PREFIX}/../../..` when the file sits deeper than the prefix
root. `sysroot-fix --list` shows which packages already have rules.

Upstream examples: `pinocchio`, `hardware_interface`, `flann`.

---

## Verification

A fix is done only when the package configures **and** compiles:

```bash
cross-colcon-build --packages-up-to <pkg>
```

Then confirm nothing else was left poisoned:

```bash
grep -l "_DIR-NOTFOUND" build/*/CMakeCache.txt   # only the harmless four
```

## Cross-references

- Normal builds, sysroot model, `cross-colcon-build` flag rules →
  `SKILL.md`.
- Compile/link failures (missing headers, GLIBC, host-lib leakage) →
  `SKILL.md`'s decision tree, not this file.
- Package metadata / `package.xml` `<depend>` entries →
  `arm64-ros2-package-conventions`.