
# Strict-Warning Build Errors

Compile errors that come from the **active toolchain's warning flags**, not
from a broken dependency. Reference for `arm64-cross-build`; read it when the
build-failure decision tree sends you here.

The same source tree can build clean under one board toolchain and fail under
another. Nothing is wrong with the sysroot, the cache, or `find_package` — the
active toolchain simply promotes warnings to errors.

## Recognition

The build reaches the **compile** step and fails with a message shaped like:

```
error: conversion from 'double' to 'int' may change value [-Werror=float-conversion]
cc1plus: all warnings being treated as errors
```

Signature, all three together:

- The failing step is compilation of a `.cpp`/`.c` file in `src/`, not
  configure and not link.
- The message text is a **warning** phrased as an error.
- A `[-Werror=<name>]` or `[-W<name>]` tag appears in brackets, and/or the
  trailing `cc1plus: all warnings being treated as errors` line.

If there is no bracket tag and no `all warnings being treated as errors`
line, this is a genuine compile error — fix the code, do not read on.

## How toolchain variants work

One toolchain file per supported board lives in `/home/ubuntu/toolchains/` as
`<product>_cross.cmake`. The container entrypoint reads the `PRODUCT`
environment variable and **copies** the matching file over `cross.cmake`,
which is what `cross-colcon-build` passes to CMake.

Each variant sets its own `ARM_COMPILE_OPTION` string — target CPU plus a
warning policy. **Variants differ in how strict that policy is.** Some add
only format-security hardening; others additionally turn on broad warning
sets and promote them to errors (`-Wall`, `-Wextra`, `-Wconversion`,
`-Werror`, `-pedantic-errors`, and similar). A diagnostic that is not even
emitted under a lax variant can be a hard error under a strict one, with no
change to the source.

Do not assume which variants exist or what any of them enables — the set of
boards grows. **Read the active flags at runtime.**

### Discover the active variant and its flags

```bash
# Which variants ship in this container?
ls /home/ubuntu/toolchains/*_cross.cmake

# Which one is currently active? (cross.cmake is a copy, not a symlink)
for f in /home/ubuntu/toolchains/*_cross.cmake; do
  cmp -s "$f" /home/ubuntu/toolchains/cross.cmake && echo "active: $f"
done

# What warning policy is actually in force?
grep -m1 ARM_COMPILE_OPTION /home/ubuntu/toolchains/cross.cmake
echo "PRODUCT=${PRODUCT:-<unset, entrypoint default applies>}"
```

Read the flag string before explaining anything to the user — quote the real
flags, never a remembered table.

If the loop prints **no** `active:` line, `cross.cmake` matches no shipped
variant: it is stale or hand-edited, and that mismatch is itself the bug.
Re-sync it from the variant that matches the hardware, then rebuild clean:

```bash
cp /home/ubuntu/toolchains/<product>_cross.cmake /home/ubuntu/toolchains/cross.cmake
rm -rf build/ install/ log/ && cross-colcon-build
```

To compare two variants' policies directly:

```bash
diff <(grep ARM_COMPILE_OPTION /home/ubuntu/toolchains/<a>_cross.cmake) \
     <(grep ARM_COMPILE_OPTION /home/ubuntu/toolchains/<b>_cross.cmake)
```

## Required: ask the user before fixing

There are two legitimate fixes and they are not interchangeable — one changes
program behavior, the other changes build policy. **Do not pick for the
user.** Present the failure, then ask with `AskUserQuestion`:

> The active toolchain (`<variant file>`) compiles with `<the -Werror-ish
> flags you actually found>`, which turned `<N>` warning(s) into errors in
> `<package>`. How should I fix it?
>
> 1. **Fix the code** — change the source so the warning goes away
>    (explicit casts, correct types, remove the unused parameter). Keeps the
>    strict policy intact; may alter behavior at the fixed sites.
> 2. **Relax in CMakeLists.txt** — scope a `-Wno-error=<name>` to the
>    affected target. Source untouched; the underlying warning stays real
>    and unaddressed.

Give them the concrete diagnostic list first — how many warnings, which
categories (`float-conversion`, `unused-parameter`, …), and which files — so
the choice is informed. Then implement **only** the option they picked.

If the warnings split across categories (e.g. some are real bugs, some are
noise from a vendored header), say so and let them choose per category.

## Option 1 — fix the code

Work per diagnostic. Common categories a strict variant surfaces, and their
honest fixes:

| Bracket tag | Cause | Fix |
|---|---|---|
| `-Werror=float-conversion`, `-Werror=conversion`, `-Werror=sign-conversion` | implicit narrowing | explicit `static_cast<T>(...)`, or widen the destination type |
| `-Werror=unused-parameter` | unused function argument | drop the parameter name, or `(void)param;` |
| `-Werror=unused-variable` | dead local | delete it |
| `-Werror=maybe-uninitialized` | possibly-unread init | initialize at declaration |
| `-Werror=sign-compare` | signed/unsigned `<` | match the types, or cast the loop index |
| `-Wpedantic` | ISO non-conformance (zero-size array, stray `;`, extra qualification) | rewrite to standard C++17 |

Rules:

- A cast is **not** automatically the right fix. `-Wconversion` on a value
  that can genuinely exceed the destination range is a real bug — say so
  rather than silencing it with `static_cast`.
- Stay inside `src/`. Never edit a header under `$ARM64_SYSROOT` to quiet a
  warning; the sysroot is regenerated and the edit is lost.
- Fix only the diagnostics the build reported. Do not sweep the file for
  other warnings that did not fail the build (see CLAUDE.md §3).

## Option 2 — relax in `CMakeLists.txt`

### The one rule that matters

**Copy the exact name from the bracket in GCC's output.** The umbrella name
is not the name GCC reports, and `-Wno-error=<umbrella>` silently does
nothing. Verified:

```
error: conversion from 'double' to 'int' may change value [-Werror=float-conversion]

target_compile_options(foo PRIVATE -Wno-error=conversion)         # STILL FAILS
target_compile_options(foo PRIVATE -Wno-error=float-conversion)   # works
```

Strip the leading `-W` / `-Werror=` from the bracket tag and rebuild it as
`-Wno-error=<name>`. For `[-Wpedantic]` that is `-Wno-error=pedantic`.

### Why target-level options win

The toolchain's flags land in `CMAKE_CXX_FLAGS`, which CMake emits **first**
on the compile line; `target_compile_options` are appended **after**. GCC
applies warning flags last-wins, so a target-scoped `-Wno-error=...`
overrides the toolchain's `-Werror` for that target only:

```
c++ <toolchain flags> ... -Wconversion -Werror -Wno-error=float-conversion -c foo.cpp
                                       ^^^^^^^ toolchain      ^^^^^^^^^^^^ target — wins
```

### Scope ladder — always take the narrowest rung that works

```cmake
# 1. Best: demote one category on one target. Warning still prints.
target_compile_options(my_node PRIVATE -Wno-error=float-conversion)

# 2. If the category is genuinely not applicable to this target, silence it.
target_compile_options(my_node PRIVATE -Wno-conversion)

# 3. Single file (vendored / generated source you do not own).
set_source_files_properties(third_party/blob.cpp
  PROPERTIES COMPILE_OPTIONS "-Wno-error=conversion;-Wno-error=sign-compare")
```

Prefer rung 1: `-Wno-error=` keeps the warning visible in the log, so the
debt stays discoverable. `-Wno-` hides it entirely.

### Gate on the policy, not on the board

The flags are harmless no-ops under a lax variant, so a bare
`target_compile_options` line is already portable. When the intent should be
explicit in the file, gate on the flag that actually caused the problem —
**never** on a board or `PRODUCT` name, which goes stale the moment a variant
is added:

```cmake
# Good — tracks the policy, works for any current or future strict variant.
if(CMAKE_CXX_FLAGS MATCHES "-Werror")
  target_compile_options(my_node PRIVATE -Wno-error=float-conversion)
endif()

# Bad — pins to one board; breaks silently when a new strict variant ships.
if(CMAKE_CXX_FLAGS MATCHES "-mcpu=<some-cpu>")
  ...
endif()
```

Leave a comment naming *why* the target cannot satisfy the warning. A bare
`-Wno-error=` line with no rationale becomes permanent.

## Anti-patterns

- **Editing `cross.cmake` or any `<product>_cross.cmake` to drop `-Werror`.**
  They are tracked with the toolchain and overwritten on the next container
  update, and the change silently weakens every package in the workspace. If
  the *policy* is genuinely wrong, raise it upstream (see below) — do not
  patch locally.
- **`-DCMAKE_CXX_FLAGS=...` on the `cross-colcon-build` command line.** The
  wrapper does not protect this key, so it goes through — and each variant
  guards its flag block with `if(NOT CMAKE_CXX_FLAGS MATCHES "<its -mcpu=>")`.
  Supplying a `CMAKE_CXX_FLAGS` that contains that `-mcpu=` string makes the
  toolchain skip its **entire** block. Verified: the resulting flags are
  exactly what you passed — the hardening options (`-fstack-protector-strong`,
  `-Werror=format-security`, and any section/GC flags the variant adds) are
  all gone. You lose the hardening, not just the warnings.
- **Switching `PRODUCT` to a laxer variant to make a build pass.** That
  retargets the CPU and produces a binary tuned for the wrong board.
  `PRODUCT` follows the hardware, never the error count.
- **`add_compile_options()` / `-w` at directory or workspace scope.** Blankets
  every target including ones that were clean.
- **`#pragma GCC diagnostic ignored` sprinkled in sources** as a substitute
  for option 2. Acceptable for one genuinely unavoidable line with a comment;
  not as a bulk fix.

## Verification

A fix is done only when the package compiles under the **same** toolchain
that failed:

```bash
cross-colcon-build --packages-select <pkg>
```

Then confirm you did not weaken more than intended — the compile line must
still carry everything the variant's `ARM_COMPILE_OPTION` specifies:

```bash
grep -m1 'CXX_FLAGS' build/<pkg>/CMakeFiles/<target>.dir/flags.make
grep -m1 ARM_COMPILE_OPTION /home/ubuntu/toolchains/cross.cmake
```

Every option in the second output must appear in the first, with your
`-Wno-error=<name>` appended after it. If hardening options are missing,
someone overrode `CMAKE_CXX_FLAGS` — back that out.

For option 1, also confirm no behavior changed beyond the reported sites:
re-read the diff and check every cast you added is range-safe.

## Report a policy problem upstream

If a diagnostic fires on code that is correct and cannot reasonably be
rewritten — most often `-Wconversion` inside a third-party ROS header pulled
from the sysroot — the variant's flag set, not the workspace, is the problem.

Draft a report and **hand it to the user; do not file it.** Include the
package, the full `error:` block with its bracket tag, the offending header
path, the active toolchain file, and which of its flags produced the error.

Repo: <https://github.com/renesas-rdk/ubuntu_xbuild_toolchains>

## Cross-references

- Normal builds, `cross-colcon-build` flag rules, sysroot model → `SKILL.md`.
- Configure-time `find_package` / imported-target failures →
  `references/configure-errors.md`.
- Where source files and `CMakeLists.txt` belong →
  `arm64-ros2-package-conventions`.
