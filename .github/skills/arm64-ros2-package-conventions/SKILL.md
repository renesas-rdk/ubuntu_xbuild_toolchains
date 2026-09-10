---
name: arm64-ros2-package-conventions
description: Use this skill when an AI agent creates, edits, reviews, or organizes ROS 2 packages in this workspace — package layout, naming, formatting, dependency declaration, ros2_control hardware interfaces, or any structural decision affecting `src/`. Triggers include "create a new package", "where should this file live", "review my package.xml", "format this code", "convert to ament_cmake/ament_python".
---

# ARM64 ROS 2 Package Conventions

## When to use this skill

- Creating a new package (cpp or python).
- Splitting / merging existing packages.
- Reviewing `package.xml`, `CMakeLists.txt`, `setup.py`.
- Picking a build type (`ament_cmake` vs `ament_python`).
- Deciding whether a file belongs in description, control, or
  bringup.
- Formatting C/C++ code in this workspace.

## When NOT to use this skill

- Compile errors → `arm64-cross-build` decision tree.
- On-device behavior → deploy/debug skills.

## Build types

| Code            | `<build_type>` in `package.xml` | Layout           |
|-----------------|---------------------------------|------------------|
| C++             | `ament_cmake`                   | `CMakeLists.txt` |
| Python          | `ament_python`                  | `setup.py`       |
| Mixed           | split into two packages         | —                |

Do not mix C++ and Python build systems in the same package.

## Naming

| Item                    | Style          | Example                              |
|-------------------------|----------------|--------------------------------------|
| Package directory       | `snake_case`   | `inspire_rh56e2_hand_description`    |
| File names              | `snake_case`   | `bridge_node.cpp`                    |
| Topics / services       | `snake_case`   | `/joint_states`                      |
| C++ class               | `CamelCase`    | `BridgeNode`                         |
| ROS parameters          | `snake_case`   | `publish_rate_hz`                    |

Suffixes used in this workspace:

- `*_description` — URDF / xacro / meshes only.
- `*_ros2_control` — `ros2_control` hardware interfaces & controllers.
- `*_bringup` — launch files + config that wire description + control.

Keep these concerns in separate packages.

## Formatting

- C / C++: `clang-format` using the workspace root `.clang-format`
  (Google base, 100-col, custom brace wrapping).
- The clang-format binary used by VS Code lives at:
  `~/.vscode-server/extensions/ms-vscode.cpptools-*/LLVM/bin/clang-format`.
  Agents should call it via that path or via the C/C++ extension's
  format-document command, not via a system-installed binary that may
  use a different config.
- Python: PEP 8 + the package's existing style; no enforced formatter
  in this workspace.

## ros2_control hardware interfaces

When implementing a hardware interface:

- Inherit from `hardware_interface::SystemInterface` (or the right
  subclass for your component type).
- Declare via the `pluginlib` macro at the bottom of the .cpp file.
- Export the plugin XML in `CMakeLists.txt` with
  `pluginlib_export_plugin_description_file(hardware_interface plugin.xml)`.
- Keep the plugin XML and the YAML controller config in the
  `*_ros2_control` package; never in `*_description`.

## Anti-patterns

- Putting URDF and `ros2_control` plugin code in the same package.
- Naming a package `<thing>-driver` (kebab-case is invalid).
- Adding `add_compile_options(-march=...)` — the cross toolchain
  controls flags.
- Hardcoded absolute paths in `CMakeLists.txt`.

## IntelliSense (`.vscode/c_cpp_properties.json`)

IntelliSense in this workspace is configured for **target** headers,
not host headers. The single configuration `ARM64 Cross` keys off
`${config:TARGET_LOCAL_SYSROOT}` (defaults to `$ARM64_SYSROOT`) and
includes:

- `${workspaceFolder}/src/*/include/**`
- `${TARGET_LOCAL_SYSROOT}/opt/ros/jazzy/{include,share,lib}/**`
- `${TARGET_LOCAL_SYSROOT}/usr/include/**`,
  `usr/include/c++/13/**`, `usr/include/opencv4/**`
- `compilerPath: /usr/bin/aarch64-linux-gnu-gcc`,
  `intelliSenseMode: gcc-arm64`, `cppStandard: c++17`.

When IntelliSense reports "cannot open source file ...":

1. Check the header is actually in `$ARM64_SYSROOT/usr/include/...`.
2. If it's in a non-standard sysroot subfolder (e.g.
   `usr/include/aarch64-linux-gnu/...`), add a glob to the `includePath`
   in `c_cpp_properties.json`. Do **not** "fix" this by editing
   `CMakeLists.txt` — the build was already finding it.
3. Never point IntelliSense at host `/usr/include` — you'll get
   x86_64 prototypes that compile but mislead the editor.

## Dependency hygiene (`package.xml`)

Dependencies are resolved twice in this workspace:

1. At **build time** by `sysroot-rosdep-install`, which reads every
   `package.xml` under `src/` and installs the rosdep keys into
   `$ARM64_SYSROOT` via the QEMU chroot.
2. At **runtime** on the board, where the same keys must already be
   installed in the RDK Linux image (or via a post-deploy
   `rosdep install --from-paths ./install/*/share`).

Guidance:

- Every runtime dep that triggers `find_package(...)` must appear as
  `<depend>` (not `<build_depend>` only).
- For Python packages, declare deps with `<exec_depend>` for libraries
  imported at runtime.
- Board-specific deps carry `condition="$PRODUCT == 'V4H'"` or
  `condition="$PRODUCT == 'V2H'"` on the dependency tag; those are the
  only two forms used in this tree.
- Use the **canonical rosdep key** (e.g. `libopencv-dev`,
  `boost`) — not a Debian-only package name. Check
  <https://github.com/ros/rosdistro/blob/master/rosdep/base.yaml>
  before inventing one.
- Re-run `sysroot-rosdep-install` whenever you add a `<depend>`,
  `<build_depend>`, or `<exec_depend>`. The cross build does not
  re-trigger it automatically.
- **ABI must match the board image.** If a package pulls a different
  major version of a system library than what's on the deployed ARM64 Ubuntu
  image, the binary will link in the sysroot but crash at runtime.
  Prefer libraries already in the base image; bump the sysroot
  alongside the board image, not independently. See
  <https://renesas-rdk.github.io/rzv2h_rdk_documentation/latest/chapter-4/development_guide/cross_build_faq.html#abi-mismatch>.

## Cross-references

- Building once a package compiles → `arm64-cross-build`.
- Running it on the board → `arm64-deploy-debug` /
  `arm64-target-autonomous-test`.
