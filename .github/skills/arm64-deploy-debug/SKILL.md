---
name: arm64-deploy-debug
description: Use this skill when an AI agent is asked to deploy build artifacts to the ARM64 board, run a node or launch file remotely, or set up a remote GDB debugging session — through the workspace's existing VS Code tasks and launch configurations. Triggers include "deploy to target", "run on the board", "launch on target", "debug with GDB", "ssh into the board" (interactive), or any flow that the user expects to drive from VS Code.
---

# ARM64 Deploy & Debug (VS Code task-driven)

## When to use this skill

Apply this skill when the user's intent is **interactive** and routes
through VS Code:

- "Deploy what I just built."
- "Run / launch node X on the target."
- "Debug node Y with GDB."
- "Open an SSH shell to the board."

## When NOT to use this skill

- Autonomous / unattended testing where the agent itself drives SSH
  (no VS Code in the loop) → use `arm64-target-autonomous-test`.
- Building / cross-compiling → use `arm64-cross-build`.

## Connection settings (single source of truth)

All target parameters come from `.vscode/settings.json`:

| Key                       | Used by                                          |
|---------------------------|--------------------------------------------------|
| `TARGET_IP`               | every task / launch                              |
| `TARGET_USER`             | every task / launch                              |
| `TARGET_PASSWORD`         | every task / launch (sshpass)                    |
| `TARGET_ROS2_WS`          | `ROS2: Deploy to Target`, `ROS2: Install Deps on Target (rosdep)` |
| `TARGET_LOCAL_SYSROOT`    | GDB launch configs, `ROS2: Check Package Versions (Sysroot and Board)` |
| `TARGET_GDB_PORT`         | GDB tasks + launch configs                       |
| `NODE_PACKAGE_NAME`       | `ROS2: Run Package Executable`, GDB Run          |
| `NODE_EXECUTABLE_NAME`    | `ROS2: Run Package Executable`, GDB              |
| `LAUNCH_PACKAGE_NAME`     | `ROS2: Launch Package LaunchFile`, GDB Launch    |
| `LAUNCH_FILE_NAME`        | `ROS2: Launch Package LaunchFile`, GDB Launch    |

Never hard-code these. If a value is missing or stale, surface it to
the user — do not guess.

## Standard tasks (preferred entry points)

Run via the workspace's VS Code tasks (**Tasks: Run Task**, or the
harness's task-runner tool if it has one). If the harness cannot run VS
Code tasks, read the task's `command` from `.vscode/tasks.json` and run
it from the workspace root with the `${config:*}` values substituted
from `.vscode/settings.json` — reuse the task's exact command rather
than reinventing it.

| Task label                           | Purpose                                              |
|--------------------------------------|------------------------------------------------------|
| `ROS2: Build Release`                | default build (release)                              |
| `ROS2: Build Debug`                  | debug build, prerequisite for GDB launches           |
| `ROS2: Deploy to Target`             | rsync `install/` → `$TARGET_ROS2_WS`                 |
| `ROS2: Run Package Executable`       | run a single executable on the board                 |
| `ROS2: Launch Package LaunchFile`    | run a launch file on the board                       |
| `ROS2: Debug Run (GDB)`              | starts gdbserver under the executable                |
| `ROS2: Debug Launch (GDB)`           | starts gdbserver under a launch file                 |
| `ROS2: SSH to Target`                | interactive SSH shell                                |
| `ROS2: Clean All`                    | wipe `build/`, `install/`, `log/`                    |
| `ROS2: Install Deps on Target (rosdep)` | install runtime rosdep keys on the board from the deployed `install/` |
| `ROS2: Check Package Versions (Sysroot and Board)` | compare library versions in the sysroot vs. on the board (ABI check) |

GDB attach is then handled by the launch configurations
`GDB for ROS2 Run` and `GDB for ROS2 Launch`. Prefer launch configs
that auto-deploy via `dependsOn`.

## Decision tree

1. User says "deploy" only → `ROS2: Deploy to Target`.
2. User says "run X" → settings already have `NODE_*` set?
   - yes: `ROS2: Run Package Executable`.
   - no: ask which package/executable, then update `.vscode/settings.json`.
3. User says "launch X" → same as #2 but with `LAUNCH_*`.
4. User says "debug" or "GDB" → debug-build task is automatic via
   `dependsOn`; the agent only needs to start `GDB for ROS2 Run` or
   `GDB for ROS2 Launch`.
5. User says "ssh / shell" → `ROS2: SSH to Target` (this opens an
   interactive terminal — the agent should not try to script it; if
   scripting is needed, switch to `arm64-target-autonomous-test`).
6. A run fails on the board with a missing shared library or Python
   module → `ROS2: Install Deps on Target (rosdep)`. If the library is
   present but the wrong version → `ROS2: Check Package Versions
   (Sysroot and Board)` to confirm the sysroot/board ABI mismatch.

## Anti-patterns

- Inventing alternative `scp`/`rsync` commands when the deploy task
  already exists.
- Editing `.vscode/launch.json` to fix a problem better solved by
  updating `.vscode/settings.json`.
- Starting `gdbserver` by hand when the GDB tasks already wrap it.
- Suggesting generic ROS 2 tutorials (`ros2 run` from a host shell)
  instead of the workspace's task wrappers.

## Behavior gotchas

The scripts under `.vscode/` make a few non-obvious choices the agent
must know to debug task failures or unexpected on-target state:

- **Two different on-target install paths.** `ROS2: Deploy to Target`
  rsyncs `install/` to `$TARGET_ROS2_WS/install/`. The run/launch/GDB
  tasks rsync `install/` to **`/tmp/install/`** instead and source
  that. So a successful "Run" does **not** update what's in
  `$TARGET_ROS2_WS`. If the user expects the deployed copy to change,
  run `ROS2: Deploy to Target` explicitly.
- **Target ROS env is scraped from `~/.bashrc`.** `run_program.sh`
  greps the target's `~/.bashrc` for lines matching
  `^export (ROS_|RMW_|FASTRTPS_|CYCLONEDDS_|DDS_|RCUTILS_|SPDLOG_)` and
  re-exports them before the `ros2 run` / `ros2 launch`. If a node
  needs `ROS_DOMAIN_ID` or `RMW_IMPLEMENTATION`, set it in the user's
  `~/.bashrc` on the board, not in `.vscode/settings.json`.
- **GDB launch internals.** `start_target_gdbserver.sh` runs
  `gdbserver :$TARGET_GDB_PORT`. The host side uses
  `gdb-multiarch` (`HOST_GDB_PATH` in settings) with
  `set sysroot $ARM64_SYSROOT`,
  `substitute-path /usr/src → $ARM64_SYSROOT/usr/src`, and
  `targetArchitecture: arm` (this is the cppdbg-extension string for
  64-bit ARM — do not "correct" it to `arm64`).
- **Ad-hoc args via `${input:appArgs}`.** All run/launch/debug tasks
  prompt for an `appArgs` string. Pass `--ros-args -p foo:=bar` here;
  it's forwarded verbatim to `ros2 run`/`ros2 launch`.

## Target-side prerequisites

These are the **board's** responsibility, not the workspace's. If a
run/debug task fails immediately, check these before debugging task
scripts:

- **Runtime rosdep keys.** After the very first deploy of a new
  package set, run `ROS2: Install Deps on Target (rosdep)`. It executes
  this on the board over SSH:
  ```bash
  source /opt/ros/jazzy/setup.bash
  cd "$TARGET_ROS2_WS"
  rosdep install --from-paths ./install/*/share -y -r --ignore-src
  ```
  Sysroot rosdep (`sysroot-rosdep-install` on the host) covers
  **build** deps; this covers **runtime** deps that aren't part of the
  base RDK image.
- **`gdbserver` must be installed on the board** for the Debug tasks:
  ```bash
  sudo apt-get update && sudo apt-get install -y gdbserver
  ```
  The workspace's GDB tasks invoke it; they don't install it.
- **`appArgs` syntax.** ROS 2 parameter form is `name:=value`.
  Examples:
  - `video_device:=/dev/video0`
  - `--ros-args -p use_sim_time:=true -r __ns:=/robot`
  Whitespace-separated; passed verbatim into the underlying
  `ros2 run` / `ros2 launch` invocation.

## Underlying GDB command shape

For reference when reading `start_target_gdbserver.sh` or diagnosing
attach failures, the upstream-documented commands the script ends up
running on the board are:

```bash
# ROS2: Debug Run (GDB)
ros2 run --prefix 'gdbserver localhost:<TARGET_GDB_PORT>' \
         <NODE_PACKAGE_NAME> <NODE_EXECUTABLE_NAME>

# ROS2: Debug Launch (GDB) — attaches only to the matching node
ros2 launch --launch-prefix 'gdbserver localhost:<TARGET_GDB_PORT>' \
            --launch-prefix-filter '<NODE_EXECUTABLE_NAME>' \
            <LAUNCH_PACKAGE_NAME> <LAUNCH_FILE_NAME>
```

The `--launch-prefix-filter` is why you must set both
`LAUNCH_*` and `NODE_EXECUTABLE_NAME` for Debug Launch — the latter
selects which node inside the launch file gets `gdbserver`.

## Upstream documentation

For settings semantics and the official walkthrough, defer to:

- VS Code workspace config & settings keys: <https://renesas-rdk.github.io/rzv2h_rdk_documentation/latest/chapter-4/development_guide/vs_code_ws.html>
- Deployment workflow: <https://renesas-rdk.github.io/rzv2h_rdk_documentation/latest/chapter-4/development_guide/deploy_application.html>
- Remote debugging workflow: <https://renesas-rdk.github.io/rzv2h_rdk_documentation/latest/chapter-4/development_guide/remote_debug.html>

## Cross-references

- Build first → `arm64-cross-build`.
- Non-VS-Code SSH automation, journal inspection, OTA / agent install
  testing → `arm64-target-autonomous-test`.
