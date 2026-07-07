# ubuntu-xbuild-toolchains

## Overview

Ubuntu XBuild is a Docker-based cross-build environment for ROS 2 applications
targeting ARM64 (aarch64) boards. It is designed to simplify the workflow for
building, deploying, and debugging software from an Ubuntu host system.

This repository holds the toolchain files that drive that environment. They run
inside the cross-build Docker container, where this repo is checked out at
`/home/ubuntu/toolchains` and updated to the latest tagged release on container start.

The project is intended for developers who need a practical and reproducible
environment for ARM64 application development, especially for robotics and edge
AI use cases.

### Supported boards

| Board | Core | Toolchain file | `PRODUCT` |
| --- | --- | --- | --- |
| **RZ/V2H RDK** | Cortex-A55 | `v2h_cross.cmake` | `V2H` (default) |
| **RCar/V4H Sparrow Hawk** | Cortex-A76 | `v4h_cross.cmake` | `V4H` |

The container selects the active board from the `PRODUCT` environment variable
(defaults to `V2H`). On start, the entrypoint copies the matching
`*_cross.cmake` to `cross.cmake`, which the build wrapper then uses.

## Quick Start

To get started with Ubuntu XBuild:

- Run the setup script on an Ubuntu 24.04 x86_64 host machine to create the
  Docker-based cross-compilation environment.

  ```bash
  wget https://github.com/renesas-rdk/ros2_demo_workspace/raw/refs/heads/main/common_utils/setup_rdk_docker.sh
  chmod +x setup_rdk_docker.sh
  ./setup_rdk_docker.sh
  ```

- Follow the instructions provided by the script to build the Docker image and
  set up the workspace.

After setup is complete, you can use the environment to cross-build and develop
applications for the target board.

## Contents

| File | Purpose |
| --- | --- |
| `v2h_cross.cmake`, `v4h_cross.cmake` | CMake toolchain files (per-board compiler flags, sysroot, `rpath-link` fixups). One is copied to `cross.cmake` based on `PRODUCT`. |
| `cross-colcon-build.sh` | `colcon build` wrapper that injects the toolchain + Python paths into `--cmake-args`. Installed as `cross-colcon-build`. |
| `arm64-chroot.sh` | Enters the ARM64 sysroot via QEMU + `chroot` (used to run `rosdep`/`apt` against the target). |
| `sysroot-rosdep-install.sh` | Copies the ROS 2 workspace into the sysroot and installs build-time dependencies with `rosdep`. |
| `sysroot-fix.py` + `sysroot-fix.yaml` | Relocate hardcoded absolute paths in the sysroot's exported CMake target files so cross builds resolve correctly. |
| `sysroot-fix-append.template.yaml` | Tracked template for the user-local fixups file. Seeded to `sysroot-fix-append.yaml` on container start when that file is missing. |
| `sysroot-fix-append.yaml` | User-local sysroot fixups; gitignored, seeded from the template, never overwritten by or in conflict with auto-update. |
| `entrypoint.sh` | Container entrypoint: refreshes sysroot DNS, selects the board toolchain from `PRODUCT`, and auto-updates the toolchain to the latest `vX.Y.Z` release tag on start. |
| `env.conf` | Bash tab-completion for `colcon` and `cross-colcon-build`. |

## Documentation

For full setup and development instructions, see the
[Application Development Guide](https://renesas-rdk.github.io/rzv2h_rdk_documentation/latest/chapter-4/development_guide/development_guide.html).

> Note: the hosted guides above are written for the RZ/V2H RDK. The
> cross-build workflow is the same for the RCar/V4H Sparrow Hawk; set
> `PRODUCT=V4H` and use the `v4h_cross.cmake` toolchain.

## Troubleshooting

For common cross-compilation issues, see the
[Cross-compilation FAQ](https://renesas-rdk.github.io/rzv2h_rdk_documentation/latest/chapter-4/development_guide/cross_build_faq.html).

### Adding your own sysroot fixups

If a cross build fails with an error like *"references the file … but this file
does not exist"*, a sysroot CMake export file has a hardcoded absolute path that
needs relocating. Add the fixup in **`sysroot-fix-append.yaml`**, not in the
tracked `sysroot-fix.yaml`:

- `sysroot-fix-append.yaml` is gitignored and seeded from
  `sysroot-fix-append.template.yaml` on container start. Because git never tracks
  it, auto-update **never overwrites it and never conflicts with it** — your
  rules survive every update and every restart, no manual merge ever needed.
- The `sysroot-fix` tool loads `sysroot-fix.yaml` first, then merges your append
  rules on top. Use the append file to **add** fixups for packages the shipped
  file does not cover.

Editing the append file does **not** apply the rules by itself — the patch runs
against the sysroot, which persists across restarts. After adding a rule, apply
it explicitly and rebuild:

```bash
sysroot-fix        # patches the sysroot now with main + append rules
```

Only edit the tracked `sysroot-fix.yaml` directly when you need to **change or
override** an existing shipped rule (the append file can only add). Such edits go
through the update-conflict handling below.

### Resolving toolchain update conflicts

On start the container updates `/home/ubuntu/toolchains` to the latest `vX.Y.Z`
release tag. If you have edited tracked toolchain files, the entrypoint no longer
discards your edits — it stashes them, checks out the release, and re-applies them
on top:

- **Clean re-apply** — your edits sit on top of the new release. You see
  `your local edits re-applied cleanly` and nothing else is needed.
- **Conflict** — your edits touch the same lines the release changed. The
  entrypoint stops mid-update and prints:

  ```
  [WARN] Your local toolchain edits CONFLICT with release vX.Y.Z.
  [WARN] Conflict markers are in the affected files. Resolve them, then run:
  [WARN]     cd /home/ubuntu/toolchains && git stash drop
  ```

  The container still finishes starting, but the conflicted files contain
  standard git conflict markers (`<<<<<<<` / `=======` / `>>>>>>>`) and must be
  fixed by hand. From a shell inside the container:

  ```bash
  cd /home/ubuntu/toolchains
  git status                 # list the conflicted files
  # edit each file, keeping the changes you want and removing the markers
  git stash drop             # discard the safety copy once you are satisfied
  ```

  Your stashed edits are kept as a safety copy until you run `git stash drop`, so
  nothing is lost. To instead throw away your local edits and take the release
  as-is, run `git checkout -f <tag> && git stash drop`.

To avoid conflicts entirely, contribute lasting changes as a release rather than
editing files in the container: edits to tracked files that overlap a future
release will need to be resolved on every affected update.

## Limitations

For known limitations, see
[Non-Relocatable Sysroot CMake Paths](https://renesas-rdk.github.io/rzv2h_rdk_documentation/latest/chapter-4/development_guide/cross_build_know_issue.html).

## Change Log

See [CHANGELOG](./CHANGELOG.md).

## License

See [LICENSE](LICENSE).
