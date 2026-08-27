#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# entrypoint.sh — auto-update / restart resilience tests.
#
# entrypoint.sh checks out the latest PUBLISHED GitHub release on every container
# start: it reads the release tag from the Releases API, fetches that tag, and
# checks it out, so a restart picks up a new release without an image rebuild.
# The contract these tests lock in is: that auto-update must NEVER wedge the
# container. Offline, an unresolvable release, a remote whose tag cannot be
# fetched, or a failing post-update step must all fall through to a warning and
# let startup complete with the toolchain wrappers on PATH. A second contract:
# local changes — edits to tracked toolchain files AND untracked user files —
# are NEVER silently discarded. Tracked edits are stashed and re-applied on top
# of the release, and a conflicting re-apply leaves git conflict markers for the
# user (cases 6 & 7); an untracked file that a new release would overwrite makes
# the update fail safe instead (cases 9 & 10). Case 8 covers seeding the
# gitignored sysroot-fix-append.yaml from its template without clobbering edits.
# A third contract: a release that rewrites entrypoint.sh itself must take effect
# on the start that installs it, via a single re-exec of the fresh script, and
# must not re-exec when the release leaves the entrypoint alone (cases 11 & 12).
#
# The entrypoint is driven DIRECTLY (not through a built image) against a
# throwaway toolchain checkout wired to a local bare git remote, with the
# Releases API mocked via TOOLCHAIN_API_BASE=file://... — so "which release is
# latest", offline, and fetch-failure outcomes are reproduced deterministically
# with real git and no network. The happy path on the real released image is
# covered by tests/integration_image_test.sh.
#
# Runs as root inside a container (see .github/workflows/test.yml) so the
# entrypoint's `sudo` calls and its `/usr/local/bin` symlinks work without a
# password. `sudo` is shimmed to a plain exec so the test needs no real
# privilege escalation.
# -----------------------------------------------------------------------------
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
# shellcheck source=tests/lib/assert.sh
source "$HERE/lib/assert.sh"

if [ ! -w /usr/local/bin ]; then
    echo "SKIP: /usr/local/bin is not writable — run this test as root (e.g. inside the CI container)." >&2
    exit 0
fi

# The entrypoint resolves releases with curl; without it every auto-update case
# would silently see "no release" and fail confusingly.
if ! command -v curl >/dev/null 2>&1; then
    echo "SKIP: curl not found — install it (e.g. apt-get install curl) to run these tests." >&2
    exit 0
fi

# --- sudo shim: run the command directly (we are already root) ----------------
SHIM_DIR="$(mktemp -d)"
cat >"$SHIM_DIR/sudo" <<'SHIM'
#!/bin/sh
# drop any leading `-E`/options the callers pass, then exec the real command
while [ "$#" -gt 0 ]; do case "$1" in -*) shift;; *) break;; esac; done
exec "$@"
SHIM
chmod +x "$SHIM_DIR/sudo"

SANDBOXES=()
cleanup() { rm -rf "$SHIM_DIR" "${SANDBOXES[@]}"; }
trap cleanup EXIT

GIT_ID=(-c user.email=ci@test -c user.name=CI)

# make_sandbox — create a throwaway toolchain checkout tracking a local bare
# remote, seeded with the entrypoint + wrappers from this repo. Prints the base
# dir (which holds ./toolchains and ./remote.git).
make_sandbox() {
    local base tc remote
    base="$(mktemp -d)"; SANDBOXES+=("$base")
    tc="$base/toolchains"; remote="$base/remote.git"

    git init -q --bare --initial-branch=main "$remote"
    git clone -q "$remote" "$tc" 2>/dev/null || true
    ( cd "$tc" && git checkout -q -b main 2>/dev/null || true )

    cp "$REPO/entrypoint.sh" "$tc/"
    cp "$REPO/arm64-chroot.sh" "$REPO/sysroot-rosdep-install.sh" \
       "$REPO/cross-colcon-build.sh" "$tc/" 2>/dev/null || true
    # sysroot-fix runs only on the success path; stub it to exit 0 so the test
    # does not depend on PyYAML or a populated sysroot. Its failure-is-tolerated
    # guard (`|| echo WARN`) is covered explicitly by the "failing sub-step" case.
    printf '#!/bin/sh\nexit 0\n' >"$tc/sysroot-fix.py"
    chmod +x "$tc/sysroot-fix.py"
    mkdir -p "$tc/.vscode"
    cp "$REPO/.vscode/settings.template.json" "$tc/.vscode/" 2>/dev/null || true

    ( cd "$tc" || exit 1
      git add -A >/dev/null
      git "${GIT_ID[@]}" commit -qm init
      git push -q -u origin main >/dev/null 2>&1 )
    echo "$base"
}

# tag_remote BASE TAG MSG — push a new commit AND a release tag onto the bare
# remote (via a second clone) so the sandbox's next fetch sees a release to
# check out.
tag_remote() {
    local base="$1" tag="$2" msg="$3" work
    work="$(mktemp -d)"; SANDBOXES+=("$work")
    git clone -q --branch main "$base/remote.git" "$work/c"
    ( cd "$work/c" || exit 1
      printf '%s\n' "$msg" >>RELEASE_CHANGE
      git add -A >/dev/null
      git "${GIT_ID[@]}" commit -qm "$msg"
      git tag "$tag"
      git push -q origin HEAD:main >/dev/null 2>&1
      git push -q origin "refs/tags/$tag" >/dev/null 2>&1 )
}

# set_latest_release BASE TAG — point the mocked Releases API (served from
# file://BASE/ghapi) at TAG, written to the exact path the entrypoint derives
# from its current origin URL. This is what makes the entrypoint pick TAG as the
# latest published release; the tag object must already exist on the remote.
# Call it AFTER any `remote set-url`, so the derived slug matches.
set_latest_release() {
    local base="$1" tag="$2" slug
    slug="$(git -C "$base/toolchains" remote get-url origin | sed -E 's#^.*github\.com[:/]##; s#\.git$##')"
    mkdir -p "$base/ghapi/repos/$slug/releases"
    printf '{"tag_name":"%s"}' "$tag" > "$base/ghapi/repos/$slug/releases/latest"
}

# run_entry BASE [CMD...] — run the entrypoint from the sandbox with a hermetic
# env, ending in CMD (default: print a startup sentinel). Captures stdout+stderr.
# The timeout is a backstop: the entrypoint re-execs itself when a release
# rewrites it (case 11), so a regression that made that loop would otherwise
# hang the whole suite instead of failing the case.
run_entry() {
    local base="$1"; shift
    local tc="$base/toolchains"
    [ "$#" -gt 0 ] || set -- /bin/echo "CMD_SENTINEL"
    env -i \
        HOME=/root \
        PATH="$SHIM_DIR:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
        ARM64_SYSROOT="$base/sysroot" \
        TOOLCHAINS_WS="$tc" \
        TOOLCHAIN_API_BASE="file://$base/ghapi" \
        ROS2_WS="$base/no_ros2_ws" \
        PRODUCT="V2H" \
        timeout 120 bash "$tc/entrypoint.sh" "$@" 2>&1
}

# NOTE: plain first-start / restart / wrapper-symlink cases used to live here;
# they are covered on the REAL released image by tests/integration_image_test.sh
# (and every case below asserts "Startup Complete" in the sandbox anyway).
# Only the git edge cases that need a controllable local remote remain.

# =============================================================================
# 1. The latest published release is checked out on start
# =============================================================================
base="$(make_sandbox)"
tag_remote "$base" "v1.0.0" "release-one"
set_latest_release "$base" "v1.0.0"
before="$(git -C "$base/toolchains" rev-parse HEAD)"

it "1.1 latest release tag is checked out and startup completes"
out="$(run_entry "$base")"
assert_contains "$out" "checked out release v1.0.0"
assert_contains "$out" "--- Startup Complete ---"
after="$(git -C "$base/toolchains" rev-parse HEAD)"
it "1.2 local HEAD actually advanced to the tagged commit"
[ "$before" != "$after" ] && _pass "$CURRENT_TEST" \
    || _fail "$CURRENT_TEST" "HEAD did not move: still $after"

# =============================================================================
# 2. The entrypoint obeys the PUBLISHED release, not the highest tag present
# =============================================================================
base="$(make_sandbox)"
tag_remote "$base" "v1.9.0"  "release-1.9"
tag_remote "$base" "v1.10.0" "release-1.10"      # a higher tag also exists on the remote
set_latest_release "$base" "v1.9.0"              # but the API publishes only v1.9.0

it "2.1 checks out the published v1.9.0 even though a higher v1.10.0 tag exists"
out="$(run_entry "$base")"
assert_contains "$out" "checked out release v1.9.0"
assert_not_contains "$out" "v1.10.0"
assert_contains "$out" "--- Startup Complete ---"

# =============================================================================
# 3. A release is published but its tag cannot be fetched -> graceful skip
# =============================================================================
base="$(make_sandbox)"
# Point origin at a missing repo FIRST, then key the mock to that same origin, so
# the release resolves but the tag fetch is what fails.
git -C "$base/toolchains" remote set-url origin "$base/gone.git"
set_latest_release "$base" "v1.0.0"

it "3.1 unfetchable release tag -> auto-update skipped, container still starts"
out="$(run_entry "$base" /bin/echo STILL_ALIVE)"
assert_contains "$out" "Could not fetch release v1.0.0"
assert_contains "$out" "--- Startup Complete ---"
assert_contains "$out" "STILL_ALIVE"

# =============================================================================
# 4. No published release (API resolves nothing) falls back to local toolchain
# =============================================================================
base="$(make_sandbox)"                           # no release mocked for this sandbox
before="$(git -C "$base/toolchains" rev-parse HEAD)"

it "4.1 no published release -> nothing checked out, container starts"
out="$(run_entry "$base" /bin/echo NO_TAG_OK)"
assert_contains "$out" "No published release resolved"
assert_contains "$out" "--- Startup Complete ---"
assert_contains "$out" "NO_TAG_OK"
after="$(git -C "$base/toolchains" rev-parse HEAD)"
it "4.2 HEAD stays put when there is no release to check out"
[ "$before" = "$after" ] && _pass "$CURRENT_TEST" \
    || _fail "$CURRENT_TEST" "HEAD moved with no release: $after"

# =============================================================================
# 5. A failing post-update step (sysroot-fix) degrades gracefully
# =============================================================================
base="$(make_sandbox)"
# Publish a release whose sysroot-fix fails, so the success path runs it and
# must survive the non-zero exit.
work="$(mktemp -d)"; SANDBOXES+=("$work")
git clone -q --branch main "$base/remote.git" "$work/c"
( cd "$work/c" || exit 1
  printf '#!/bin/sh\nexit 1\n' >sysroot-fix.py           # break it in the release
  git add -A >/dev/null
  git "${GIT_ID[@]}" commit -qm break-sysroot-fix
  git tag v1.0.0
  git push -q origin HEAD:main >/dev/null 2>&1
  git push -q origin refs/tags/v1.0.0 >/dev/null 2>&1 )
set_latest_release "$base" "v1.0.0"

it "5.1 failing sysroot-fix is warned about but does not abort startup"
out="$(run_entry "$base" /bin/echo SURVIVED)"
assert_contains "$out" "sysroot-fix failed"
assert_contains "$out" "--- Startup Complete ---"
assert_contains "$out" "SURVIVED"

# =============================================================================
# 6. A NON-conflicting local edit is preserved across the update
# =============================================================================
base="$(make_sandbox)"
tag_remote "$base" "v1.0.0" "release-one"        # release only touches RELEASE_CHANGE
set_latest_release "$base" "v1.0.0"
# User edits a different tracked file locally, uncommitted.
printf '\n# user local tweak\n' >>"$base/toolchains/arm64-chroot.sh"

it "6.1 non-conflicting local edit -> re-applied cleanly, startup completes"
out="$(run_entry "$base")"
assert_contains "$out" "re-applied cleanly"
assert_contains "$out" "--- Startup Complete ---"

it "6.2 HEAD advanced to the release tag"
[ "$(git -C "$base/toolchains" rev-parse HEAD)" = "$(git -C "$base/toolchains" rev-parse v1.0.0)" ] \
    && _pass "$CURRENT_TEST" || _fail "$CURRENT_TEST" "HEAD is not at v1.0.0"

it "6.3 the user's local edit survived the update"
grep -q "user local tweak" "$base/toolchains/arm64-chroot.sh" \
    && _pass "$CURRENT_TEST" || _fail "$CURRENT_TEST" "edit was discarded"

# =============================================================================
# 7. A CONFLICTING local edit is NOT discarded — conflict markers are left
# =============================================================================
base="$(make_sandbox)"
tc="$base/toolchains"
# Share a tracked file on main so both the release and the user can diverge it.
printf 'line-base\n' >"$tc/SHARED.txt"
( cd "$tc" || exit 1
  git add SHARED.txt >/dev/null
  git "${GIT_ID[@]}" commit -qm add-shared
  git push -q origin main >/dev/null 2>&1 )
# Cut a release that changes SHARED.txt.
work="$(mktemp -d)"; SANDBOXES+=("$work")
git clone -q --branch main "$base/remote.git" "$work/c"
( cd "$work/c" || exit 1
  printf 'line-from-release\n' >SHARED.txt
  git add -A >/dev/null
  git "${GIT_ID[@]}" commit -qm release-change-shared
  git tag v1.0.0
  git push -q origin HEAD:main >/dev/null 2>&1
  git push -q origin refs/tags/v1.0.0 >/dev/null 2>&1 )
set_latest_release "$base" "v1.0.0"
# User edits the same file, conflictingly, uncommitted.
printf 'line-from-user\n' >"$tc/SHARED.txt"

it "7.1 conflicting edit -> warned as a CONFLICT, never wedges startup"
out="$(run_entry "$base")"
assert_contains "$out" "CONFLICT with release v1.0.0"
assert_contains "$out" "git stash drop"
assert_contains "$out" "--- Startup Complete ---"

it "7.2 conflict markers are left in the affected file for the user to resolve"
grep -q '^<<<<<<<' "$tc/SHARED.txt" \
    && _pass "$CURRENT_TEST" || _fail "$CURRENT_TEST" "no conflict markers in SHARED.txt"

it "7.3 the stashed edit is retained as a safety copy"
git -C "$tc" rev-parse -q --verify refs/stash >/dev/null 2>&1 \
    && _pass "$CURRENT_TEST" || _fail "$CURRENT_TEST" "stash was not kept"

# =============================================================================
# 8. sysroot-fix-append.yaml is seeded from its template, and a user-edited
#    one is never overwritten on restart.
# =============================================================================
base="$(make_sandbox)"
tc="$base/toolchains"
cp "$REPO/sysroot-fix-append.template.yaml" "$tc/"      # ship the template in the checkout

it "8.1 first start seeds sysroot-fix-append.yaml from the template"
out="$(run_entry "$base")"
assert_contains "$out" "Seeded sysroot-fix-append.yaml from template"
[ -f "$tc/sysroot-fix-append.yaml" ] && _pass "$CURRENT_TEST" \
    || _fail "$CURRENT_TEST" "append file was not created"

it "8.2 a user-edited append file is NOT overwritten on the next start"
printf 'my_pkg:\n  - {file: x, find: a, replace: b}\n' >"$tc/sysroot-fix-append.yaml"
out="$(run_entry "$base")"
assert_not_contains "$out" "Seeded sysroot-fix-append.yaml from template"
grep -q "my_pkg" "$tc/sysroot-fix-append.yaml" \
    && _pass "$CURRENT_TEST" || _fail "$CURRENT_TEST" "user edit was overwritten"

# =============================================================================
# 9. An untracked user file that the release would OVERWRITE is never clobbered
#    — the update is skipped instead (fail safe).
# =============================================================================
base="$(make_sandbox)"
tc="$base/toolchains"
tag_remote "$base" "v1.0.0" "release-one"        # the release adds tracked RELEASE_CHANGE
set_latest_release "$base" "v1.0.0"
# The user created a file with the same name, untracked, BEFORE the update.
printf 'precious user data\n' >"$tc/RELEASE_CHANGE"
before="$(git -C "$tc" rev-parse HEAD)"

it "9.1 colliding untracked file -> update skipped with a warning, startup completes"
out="$(run_entry "$base")"
assert_contains "$out" "Could not check out v1.0.0"
assert_not_contains "$out" "checked out release v1.0.0"
assert_contains "$out" "--- Startup Complete ---"

it "9.2 the untracked user file's content is untouched"
grep -q "precious user data" "$tc/RELEASE_CHANGE" \
    && _pass "$CURRENT_TEST" || _fail "$CURRENT_TEST" "untracked file was clobbered by the release"

it "9.3 HEAD stayed put — the toolchain is exactly as before"
[ "$(git -C "$tc" rev-parse HEAD)" = "$before" ] \
    && _pass "$CURRENT_TEST" || _fail "$CURRENT_TEST" "HEAD moved despite the skipped update"

# =============================================================================
# 10. A NON-colliding untracked user file does not block the update and survives
# =============================================================================
base="$(make_sandbox)"
tc="$base/toolchains"
tag_remote "$base" "v1.0.0" "release-one"
set_latest_release "$base" "v1.0.0"
printf 'my scratch notes\n' >"$tc/my-notes.txt"   # untracked, not in the release

it "10.1 non-colliding untracked file -> release checked out, startup completes"
out="$(run_entry "$base")"
assert_contains "$out" "checked out release v1.0.0"
assert_contains "$out" "--- Startup Complete ---"

it "10.2 HEAD advanced to the release tag"
[ "$(git -C "$tc" rev-parse HEAD)" = "$(git -C "$tc" rev-parse v1.0.0)" ] \
    && _pass "$CURRENT_TEST" || _fail "$CURRENT_TEST" "HEAD is not at v1.0.0"

it "10.3 the untracked user file survived the update"
grep -q "my scratch notes" "$tc/my-notes.txt" \
    && _pass "$CURRENT_TEST" || _fail "$CURRENT_TEST" "untracked file was lost"

# =============================================================================
# 11. A release that rewrites entrypoint.sh ITSELF takes effect on the SAME start
#     The running shell holds the OLD script, so without a re-exec every step
#     after the checkout — the wrapper symlinks, the seeding, the workspace
#     skill symlinks — stays the PREVIOUS release's, and anything a release ADDS
#     there lands one restart late. The entrypoint must re-exec the fresh copy,
#     and must do the update work exactly once: one checkout, one re-exec, no
#     loop, and the container's CMD still reached at the end.
# =============================================================================
base="$(make_sandbox)"
tc="$base/toolchains"
work="$(mktemp -d)"; SANDBOXES+=("$work")
git clone -q --branch main "$base/remote.git" "$work/c"
( cd "$work/c" || exit 1
  # This release adds a startup step that exists ONLY in its entrypoint.sh, so
  # the sentinel below can only be printed by the new script — proving startup
  # continued on the checked-out copy rather than the one that began the start.
  sed -i 's|^echo "--- Startup Complete ---"|echo "NEW_STEP_FROM_RELEASE"\necho "--- Startup Complete ---"|' entrypoint.sh
  git add -A >/dev/null
  git "${GIT_ID[@]}" commit -qm release-changes-entrypoint
  git tag v1.0.0
  git push -q origin HEAD:main >/dev/null 2>&1
  git push -q origin refs/tags/v1.0.0 >/dev/null 2>&1 )
set_latest_release "$base" "v1.0.0"

out="$(run_entry "$base")"

it "11.1 the changed entrypoint is detected and startup is restarted on it"
assert_contains "$out" "entrypoint.sh changed in v1.0.0"

it "11.2 the release's NEW startup step runs on this very start"
assert_contains "$out" "NEW_STEP_FROM_RELEASE"

it "11.3 startup completes exactly once (the re-exec replaces the old pass)"
n="$(printf '%s\n' "$out" | grep -c -- "--- Startup Complete ---")"
[ "$n" = "1" ] && _pass "$CURRENT_TEST" || _fail "$CURRENT_TEST" "expected 1 completion, got $n"

it "11.4 the CMD still runs after the re-exec"
assert_contains "$out" "CMD_SENTINEL"

it "11.5 the update work is not repeated — one checkout, one re-exec"
n="$(printf '%s\n' "$out" | grep -c "checked out release v1.0.0")"
m="$(printf '%s\n' "$out" | grep -c "restarting startup with the new script")"
[ "$n" = "1" ] && [ "$m" = "1" ] && _pass "$CURRENT_TEST" \
    || _fail "$CURRENT_TEST" "checkouts=$n re-execs=$m (expected 1 and 1)"

it "11.6 HEAD is at the release tag after the re-exec"
[ "$(git -C "$tc" rev-parse HEAD)" = "$(git -C "$tc" rev-parse v1.0.0)" ] \
    && _pass "$CURRENT_TEST" || _fail "$CURRENT_TEST" "HEAD is not at v1.0.0"

# =============================================================================
# 12. A release that leaves entrypoint.sh alone does NOT re-exec — the common
#     case must not pay for a second startup pass, and the post-update steps
#     must still run exactly once.
# =============================================================================
base="$(make_sandbox)"
tag_remote "$base" "v1.0.0" "release-one"        # touches RELEASE_CHANGE only
set_latest_release "$base" "v1.0.0"

out="$(run_entry "$base")"

it "12.1 unchanged entrypoint -> no re-exec"
assert_not_contains "$out" "restarting startup with the new script"

it "12.2 startup still runs the post-update steps, exactly once"
assert_contains "$out" "checked out release v1.0.0"
n="$(printf '%s\n' "$out" | grep -c "Updating DNS configuration")"
[ "$n" = "1" ] && _pass "$CURRENT_TEST" || _fail "$CURRENT_TEST" "startup ran $n times, expected 1"

finish
