---
name: arm64-cross-build
description: Use this skill when an AI agent needs to build, clean, or install dependencies in this ARM64 ROS 2 Jazzy cross-compilation workspace, or to diagnose a build failure in it. Triggers include "build the workspace", "rebuild package X", "install rosdep deps", "clean build", "fix the cross compile", "CMake can't find package X", "rebuild still fails after rosdep install", "urdfdom not found", "sysroot path is wrong", "build fails with -Werror", "conversion warning treated as error", "all warnings being treated as errors", "builds for one board but fails for another", or any colcon-related task in this repo.
---

# ARM64 Cross Build

## When to use this skill

Apply this skill for **any** of:

- Compiling ROS 2 packages in this workspace.
- Adding/resolving apt or rosdep dependencies for `src/` packages.
- Investigating cross-compile failures (linker errors, missing
  headers, sysroot issues, `find_package` misses).
- Producing a debug build for GDB.

## When NOT to use this skill

- Running on the target board → use `arm64-deploy-debug` (interactive)
  or `arm64-target-autonomous-test` (agent-driven SSH).
- Editing package metadata / file layout → use
  `arm64-ros2-package-conventions` first, then return here.

## Environment (canonical)

| Item              | Value                                                |
|-------------------|------------------------------------------------------|
| Host              | Ubuntu dev container, `x86_64` or `arm64`            |
| Target            | aarch64, Renesas ARM64, ROS 2 Jazzy                  |
| Toolchain         | `/home/ubuntu/toolchains/cross.cmake` (copy of the `$PRODUCT` variant) |
| Toolchain variants| `/home/ubuntu/toolchains/<product>_cross.cmake`, one per board; they differ in target CPU **and warning strictness** |
| Compiler          | `aarch64-linux-gnu-gcc` / `g++` (C++17)              |
| Sysroot           | `$ARM64_SYSROOT` (must be exported)                    |
| Workspace root    | `$HOME/ros2_ws` (`/home/ubuntu/ros2_ws`)             |
| Source tree       | `src/` (only place ROS packages may live)            |
| Build outputs     | `build/`, `install/`, `log/` at workspace root       |

If `cross-colcon-build` is not on `PATH` or `$ARM64_SYSROOT` is unset, the
agent is outside the dev container — stop and tell the user.

## Build commands (copy-pasteable)

```bash
# Full release build (default).
cross-colcon-build

# Single package + everything it depends on.
cross-colcon-build --packages-up-to <pkg>

# Just one package (its deps must already be built).
cross-colcon-build --packages-select <pkg>

# Debug build (for GDB / launch configs).
bash -i -l -c "export CMAKE_BUILD_TYPE=Debug && cross-colcon-build"

# Clean rebuild.
rm -rf build/ install/ log/ && cross-colcon-build
```

The workspace also defines two VS Code tasks that wrap these:
`ROS2: Build Release` (default) and `ROS2: Build Debug`. When working
through VS Code, prefer the tasks; when scripting, run the commands
above directly.

## Sysroot operations

Use the helper wrappers — never raw `chroot`/`apt`:

```bash
# Run an arbitrary command inside the target sysroot.
arm64-chroot apt update
arm64-chroot apt install -y libfoo-dev   # do NOT prefix with sudo

# Resolve + install rosdep keys for everything under src/ into the sysroot.
sysroot-rosdep-install
```

Rules:

- Do **not** prepend `sudo` to anything after `arm64-chroot`.
- Sysroot-side apt installs are persistent across builds — only re-run
  `sysroot-rosdep-install` when `package.xml` `<depend>` entries change.
- Native (host-side) `apt`/`pip` does **not** affect the cross build.

## Build-failure decision tree

1. `cross-colcon-build: command not found` → not in dev container.
2. `find_package(...) ... not found` → run the discriminator:
   `find "$ARM64_SYSROOT" -name "<pkg>*onfig.cmake"`.
   - Config **exists** in the sysroot but `find_package` still misses it
     → read `references/configure-errors.md` before changing anything.
   - Config **does not exist** → `sysroot-rosdep-install`, then rebuild.
3. Compile error carrying a `[-Werror=<name>]` / `[-W<name>]` bracket
   tag, or `cc1plus: all warnings being treated as errors` → the active
   toolchain variant's warning policy, not a code defect per se. **Stop
   and ask the user** (fix the code vs. relax `CMakeLists.txt`) — read
   `references/strict-flag-errors.md` before touching anything.
4. Linker error referencing a symbol that exists on host → a host
   library leaked in. Re-check `CMakeLists.txt` for absolute `/usr/...`
   paths or non-`ament_*` `find_package` calls.
5. ABI / `GLIBC_x.y not found` at link time → the package is using a
   host toolchain instead of the cross one. Confirm
   `CMAKE_TOOLCHAIN_FILE=/home/ubuntu/toolchains/cross.cmake` is set
   (it is, by `cross-colcon-build`).
6. After source changes seem ignored → `rm -rf build/install/log/` and
   rebuild. Colcon's incremental cache occasionally lies for cross
   builds.

## Toolchain warning strictness

One toolchain file per supported board lives in `/home/ubuntu/toolchains/`
as `<product>_cross.cmake`; the entrypoint copies the one `$PRODUCT`
selects over `cross.cmake`. Each sets its own `ARM_COMPILE_OPTION` —
target CPU plus a warning policy — and **the policies differ in
strictness**. Lax variants add only format-security hardening; strict
ones also enable broad warning sets and promote them to errors
(`-Wall`, `-Wextra`, `-Wconversion`, `-Werror`, `-pedantic-errors`, …).

Consequence: code that compiles clean under one variant can fail under
another with no change to the source, the sysroot, or the cache. A
conversion warning may not even be emitted on one board and be a hard
error on the next.

The variant list grows. Never answer from a remembered table — read the
active policy:

```bash
ls /home/ubuntu/toolchains/*_cross.cmake                       # what exists
grep -m1 ARM_COMPILE_OPTION /home/ubuntu/toolchains/cross.cmake  # what is in force
```

When a strict-flag failure hits, the fix is a **user decision**, not
yours — fixing the code and relaxing the build policy are different
tradeoffs. Present the diagnostics, ask via `AskUserQuestion` (1. fix the
code, 2. relax `CMakeLists.txt`), and implement only what they choose.
Full procedure, scope ladder, and the verified `-Wno-error=` naming
gotcha are in `references/strict-flag-errors.md`.

Never "solve" a warning by pointing `PRODUCT` at a laxer variant — that
retargets the CPU and builds for the wrong board.

## Anti-patterns

- `colcon build` (raw, no cross wrapper) — bypasses the target sysroot and
  may link against the container rootfs instead of the board image ABI.
- Adding `--cmake-args` for typical packages — the toolchain already
  configures everything needed.
- `-DCMAKE_CXX_FLAGS=...` to quiet warnings — the variant's `-mcpu`
  guard then skips its **whole** flag block, silently dropping the
  hardening options too. Scope the change to a target instead.
- Editing `cross.cmake` or any `<product>_cross.cmake` — tracked with the
  toolchain and overwritten on the next container update.
- Putting build artifacts anywhere except `build/`, `install/`, `log/`.
- `pip install` to "fix" a missing ROS dep — use `rosdep` / package.xml.

## How `cross-colcon-build` handles `--cmake-args`

`cross-colcon-build.sh` is a stateful wrapper around `colcon build`.
Know these rules before adding flags:

- It always appends `--cmake-force-configure --cmake-args ...` after
  the user's colcon args, so a user `--cmake-args ...` block does **not**
  need to be the last argument; the wrapper splits / re-merges them.
- **Protected** flags — silently dropped (with a yellow warning) if
  the user tries to set them:
  - `-DCMAKE_TOOLCHAIN_FILE=/home/ubuntu/toolchains/cross.cmake`
  - `-DPython3_EXECUTABLE=/usr/bin/python3`
  - `-DPython3_ROOT_DIR=/usr`
  - `-DPython3_FIND_STRATEGY=LOCATION`
- **Overridable defaults** — honored if the user supplies them:
  - `-DCMAKE_BUILD_TYPE` (defaults to `Release`, or `$CMAKE_BUILD_TYPE`).
- The wrapper exports `CMAKE_PREFIX_PATH`, `AMENT_PREFIX_PATH`,
  `PKG_CONFIG_PATH`, `PKG_CONFIG_SYSROOT_DIR`, and `PYTHONPATH` for the
  sysroot. Do not duplicate those in `CMakeLists.txt`.
- The stderr line about `AMENT_PREFIX_PATH` not containing
  `local_setup.*` is filtered by the wrapper — it's harmless and
  expected.

If you genuinely need to change a protected flag, edit
`cross-colcon-build.sh` itself, don't try to fight the wrapper from
`CMakeLists.txt`.

## Bringing up / refreshing the dev container

If the agent is on the host (not yet inside the dev container), the
workspace ships a one-shot helper to pull / start / shell in:

```bash
./setup_rdk_docker.sh -y --pull --create --prep --shell
```

Defaults:

- Image: `ghcr.io/renesas-rdk/rzv2h_ubuntu_xbuild:latest`
- Container: `ros2_cross_build_container`
- Bind mount: `$HOME/ros2_ws` → `/home/ubuntu/ros2_ws`

The `--prep` step is what installs `cross-colcon-build`, sets
`$ARM64_SYSROOT`, and primes `arm64-chroot`. Re-run with `--pull` to pick
up a new image (e.g. after a sysroot bump).

## How the cross-build actually works (mental model)

From the upstream RDK documentation and the multi-arch container setup:

- The dev container may be **AMD64** or **ARM64**. Inside it,
  `$ARM64_SYSROOT` is always an **ARM64** root filesystem copied from the
  RZ/V2H RDK Linux image.
- `arm64-chroot` enters that sysroot via `chroot`. On AMD64 containers it
  also uses **QEMU user-mode emulation**; on ARM64 containers the sysroot
  commands run natively.
- `cross-colcon-build` does **not** use the chroot — it cross-compiles
  on the host, linking against libraries inside `$ARM64_SYSROOT`.

Consequences the agent must respect:

- **Two filesystem contexts.** A path that is valid in the dev
  container (e.g. `/tmp/foo`) is **not** visible inside `arm64-chroot`.
  To run a script in the chroot, copy it into `$ARM64_SYSROOT/...` first,
  then reference its in-sysroot path:
  ```bash
  cp ./script.sh "$ARM64_SYSROOT/tmp/script.sh"
  arm64-chroot bash /tmp/script.sh
  ```
- **Only one chroot at a time.** Don't try to run two parallel
  `arm64-chroot` commands; the second will fail.
- **QEMU is slow on AMD64.** `apt`/`rosdep` inside the chroot can take
  minutes on AMD64 containers. Native ARM64 containers avoid that emulation
  cost, but still use the separated sysroot for target ABI correctness.
- **ABI must match the board image.** Libraries pulled into the
  sysroot must be the same versions as on the running RDK Linux image,
  or the binary will load on the host sysroot but crash on the device.
  When in doubt, refresh the sysroot from the same image release that's
  flashed on the target.

## Restoring `.vscode/` and `.clang-format`

The `--prep` phase of `setup_rdk_docker.sh` copies these from
`$TOOLCHAINS_WS` (the toolchain directory inside the container,
usually `/home/ubuntu/toolchains/`) into the workspace root. If a user
deleted them or opened a fresh workspace:

```bash
cp -r "$TOOLCHAINS_WS/.vscode" "$ROS2_WS/"
cp    "$TOOLCHAINS_WS/.clang-format" "$ROS2_WS/"
```

Do not hand-write these files — they're versioned with the toolchain.

## Upstream documentation

When the user asks "how does the cross-build / sysroot / chroot
actually work", point them at the canonical RDK docs rather than
paraphrasing:

- Overview: <https://renesas-rdk.github.io/rzv2h_rdk_documentation/latest/chapter-4/development_guide/cross_build_overview.html>
- Setup: <https://renesas-rdk.github.io/rzv2h_rdk_documentation/latest/chapter-4/development_guide/cross_build_setup.html>
- Usage: <https://renesas-rdk.github.io/rzv2h_rdk_documentation/latest/chapter-4/development_guide/cross_build_usage.html>
- ABI mismatch FAQ: <https://renesas-rdk.github.io/rzv2h_rdk_documentation/latest/chapter-4/development_guide/cross_build_faq.html#abi-mismatch>
- Non-relocatable sysroot CMake paths (known issue): <https://renesas-rdk.github.io/rzv2h_rdk_documentation/latest/chapter-4/development_guide/cross_build_know_issue.html>

## Cross-references

- CMake configure errors this tree doesn't resolve →
  `references/configure-errors.md`.
- Compile errors from a strict variant's `-Werror` / `-Wconversion` /
  `-pedantic-errors` → `references/strict-flag-errors.md`.
- Package layout, naming, formatting → `arm64-ros2-package-conventions`.
- Pushing the resulting `install/` to the board → `arm64-deploy-debug`.
- Verifying behavior on the board after a build → `arm64-target-autonomous-test`.
