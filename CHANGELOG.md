# Change Log

## [1.3.0] - 2026-08-26

### Added

- `.agents/skills` symlink to `.github/skills/` so Codex CLI detects all skills; the entrypoint now links `.agents` into the workspace too.
- Two `arm64-cross-build` references: `configure-errors.md` (stale `-NOTFOUND` cache, missing sysroot dependency, non-relocatable CMake export paths) and `strict-flag-errors.md` (`-Werror` / `-Wconversion` / `-pedantic-errors` failures).

### Changed

- The `arm64-cross-build` skill now covers the per-board `<product>_cross.cmake` variants, whose warning strictness differs - the same source can build for one board and fail on another. On a strict-flag failure the agent asks the user before fixing the code or relaxing `CMakeLists.txt`.
- `check_package_versions.py` now compares the full Debian dependency closure of each side, requires every direct dependency on both sides, and syncs to the newest version available to both instead of upgrading each side to its own latest.

## [1.2.0] - 2026-07-11

### Upgrade notice

> **Before running this release, remove the old toolchain files from your mount workspace.**
>
> This release wires the toolchain's skill files (`.vscode/`, `.github/`, `.claude/`, `AGENTS.md`, `.clang-format`) into the workspace as **symlinks** to the decoupled toolchain directory. To avoid destroying anything you own, the entrypoint now **refuses to overwrite a real file or directory already sitting at those paths** - it keeps it and prints a `[WARN]`. Anyone upgrading from the older in-place layout still has real copies of those files in the workspace, so the new symlinks are skipped and the toolchain integration (VS Code tasks, agent skills) never activates.
>
> From your workspace root, delete the stale copies so the entrypoint can recreate them as symlinks on the next container start:
>
> ```sh
> rm -rf .vscode .github .claude AGENTS.md .clang-format
> ```
>
> If you customized `.vscode/settings.json` (e.g. `TARGET_IP`, `TARGET_PASSWORD`), **back up those values first** - after removal, `settings.json` is re-seeded from the template and you re-enter them. Stale *symlinks* left by a prior pre-release are refreshed automatically and need no action.

### Added

- Multi-arch Docker image builds for `amd64` and `arm64` (Apple M1/M2 hosts).
- Host OS support for Ubuntu, Windows, and macOS via Docker Desktop.
- `sysroot-fix-append.template.yaml`, a tracked template seeded on container start to the gitignored `sysroot-fix-append.yaml` when it is missing. This gives users a discoverable, frictionless place to add their own sysroot fixups that auto-update never overwrites or conflicts with (see README "Adding your own sysroot fixups").

### Changed

- On start, `entrypoint.sh` now updates the container to the latest `vX.Y.Z` release tag, so containers track vetted releases. Falls back to the toolchain baked into the image when offline or when no tag is available.
- The auto-update no longer silently discards local edits to tracked toolchain files. It now stashes and re-applies them on top of the release; a clean re-apply is kept, and a conflicting one leaves git conflict markers for the user to resolve (see README "Resolving toolchain update conflicts"). The container still starts either way.
- The released image now bakes in the latest release tag at build time; set the `TOOLCHAIN_REF` build-arg to pin a specific ref.

### Fixed

- Auto-update no longer overwrites *untracked* user files in the toolchain directory. If a new release would add a tracked file with the same name as an untracked local file, the update is skipped with a warning instead of force-checking out over it; non-colliding untracked files never block the update. The entrypoint-generated `cross.cmake` is now gitignored so a pristine container keeps taking the fast clean-tree update path.

## [1.1.0] - 2026-05-11

### Added

- Added support for Claude Code and Codex skills for building, deployment, debugging, and organization management.
- Enhanced deployment and debugging skills.

### Updated

- Updated the `cross-colcon-build` script to enhance the build process and improve error handling.
- Changed the `rzv2h-chroot` command to `arm64-chroot` for better generality and consistency with other architectures.

### Fixed

- Fixed an issue where the `cross-colcon-build` script did not properly handle certain build configurations, which caused all packages to rebuild instead of only the changed ones.

## [1.0.1] - 2026-04-16

### Added

- Added Copilot AI agent skills for building, deployment, debugging, and organization management.

### Fixed

- Fixed a build issue for the `controller_interface` package caused by newer package versions in `sysroot-fix.yaml`.
- Fixed an issue where remote debugging did not work if no ROS environment variable (such as `ROS_DOMAIN_ID`) was set in `~/.bashrc`.

## [1.0.0] - 2026-03-31

### Added

- Initial release of the RZ/V2H Ubuntu XBuild system, providing a robust and efficient build environment for ROS 2-based projects.