#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# Integration test — run THIS commit's toolchain on the released image, the last
# gate before a release ships. Delivers the candidate through the entrypoint's
# own auto-update path, then drives the real execution paths (chroot, a real
# cross build), not just the guard branches:
#   1. start the released image, wait for first startup;
#   2. clone $TOOLCHAINS_WS in-container, replace its tree with this checkout,
#      commit, and publish that commit both as `main` and as a synthetic release
#      tag that outranks any real vX.Y.Z (so either entrypoint mechanism —
#      `pull --ff-only main` or latest-tag checkout — lands on it);
#   3. point origin at the clone and restart to pull the candidate in;
#   4. restart again so the CANDIDATE entrypoint itself runs end-to-end;
#   5. drive the wrappers: arm64-chroot must report aarch64 and the cross build
#      must produce an ARM aarch64 shared object.
#
# Usage: integration_image_test.sh [IMAGE] [PRODUCT]
#   IMAGE    default ghcr.io/renesas-rdk/rzv2h_ubuntu_xbuild:latest
#   PRODUCT  V2H (default) or V4H — must match the image
#
# Needs docker with privileged containers and (on non-arm64 hosts) QEMU binfmt
# handlers registered. A locally present image is reused; the pull is skipped.
# -----------------------------------------------------------------------------
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
# shellcheck source=tests/lib/assert.sh
source "$HERE/lib/assert.sh"

IMAGE="${1:-ghcr.io/renesas-rdk/rzv2h_ubuntu_xbuild:latest}"
PRODUCT="${2:-V2H}"
case "$PRODUCT" in
    V2H) PRODUCT_CMAKE="v2h_cross.cmake" ;;
    V4H) PRODUCT_CMAKE="v4h_cross.cmake" ;;
    *) echo "ERROR: unknown PRODUCT '$PRODUCT' (expected V2H or V4H)" >&2; exit 2 ;;
esac

if ! command -v docker >/dev/null 2>&1; then
    echo "SKIP: docker is not available — this test needs the released image." >&2
    exit 0
fi
if ! git -C "$REPO" rev-parse -q --verify HEAD >/dev/null 2>&1; then
    echo "ERROR: $REPO is not a git checkout with a HEAD — nothing to deliver." >&2
    exit 2
fi

# Reuse a local image when present, pull otherwise.
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "[INFO] Image $IMAGE already present locally — skipping pull."
else
    echo "[INFO] Pulling $IMAGE ..."
    docker pull "$IMAGE" || { echo "ERROR: cannot pull $IMAGE" >&2; exit 1; }
fi

CTN="xbuild_integ_$$"
TARBALL="$(mktemp)"
cleanup() { docker rm -f "$CTN" >/dev/null 2>&1 || true; rm -f "$TARBALL"; }
trap cleanup EXIT

# dexec CMD — login-shell command in the container (login shell so the
# entrypoint-symlinked wrappers are on PATH).
dexec() { docker exec "$CTN" bash -lc "$1" 2>&1; }

# wait_startup N — block until "Startup Complete" has been logged N times (logs
# persist across restarts), or fail after ~2 minutes.
wait_startup() {
    local want="$1" tries=120 n
    while [ "$tries" -gt 0 ]; do
        n="$(docker logs "$CTN" 2>&1 | grep -c -- '--- Startup Complete ---')"
        [ "$n" -ge "$want" ] && return 0
        tries=$((tries - 1)); sleep 1
    done
    return 1
}

# put_file <container_path> <<<contents — write a file into the container as its
# default user, via a base64 round-trip to avoid quoting hazards.
put_file() {
    local path="$1" b64
    b64="$(base64 -w0)"
    dexec "mkdir -p \"\$(dirname '$path')\" && printf '%s' '$b64' | base64 -d > '$path'" >/dev/null
}

# =============================================================================
# 1. First start of the released image
# =============================================================================
# TOOLCHAIN_API_BASE points the entrypoint's "latest release" lookup at a local
# mock (populated in step 2), so this test controls which release is delivered
# without touching the real GitHub API.
docker run -dt --privileged -e TOOLCHAIN_API_BASE=file:///tmp/ghapi \
    --name "$CTN" "$IMAGE" >/dev/null

it "1.1 released image completes its first startup"
if wait_startup 1; then _pass "$CURRENT_TEST"
else _fail "$CURRENT_TEST" "no startup-complete marker: $(docker logs "$CTN" 2>&1 | tail -5)"; exit 1; fi

TC_WS="$(docker exec "$CTN" bash -lc 'printf %s "$TOOLCHAINS_WS"')"
[ -n "$TC_WS" ] || { echo "ERROR: TOOLCHAINS_WS not set in image" >&2; exit 1; }

# =============================================================================
# 2. Deliver this commit through the entrypoint's own auto-update path
# =============================================================================
# The delivery is set up for EVERY entrypoint generation the released image
# might still carry, since the image ships with the previously-released
# entrypoint until this candidate becomes a release:
#   - branch `main`  — an old `git pull --ff-only origin main` entrypoint;
#   - tag $CAND_TAG  — an old `git tag | sort -V | tail -1` entrypoint;
#   - mock release   — the current entrypoint, which reads the latest published
#                      release from $TOOLCHAIN_API_BASE (the file:// mock).
git -C "$REPO" archive --format=tar HEAD >"$TARBALL"
ORIG_SHA="$(dexec "git -C '$TC_WS' rev-parse HEAD" | tr -d '[:space:]')"

# Outranks any real vX.Y.Z for the old sort -V | tail -1 entrypoint.
CAND_TAG="v99.99.99"

it "2.1 candidate commit staged as a fast-forward of the container checkout"
# Stream the tar over stdin (docker cp would land it root-owned). checkout -B
# main gives a branch to advance even if the image left HEAD detached at a tag.
out="$(dexec "set -e
    git clone -q '$TC_WS' /tmp/incoming
    git -C /tmp/incoming checkout -q -B main
    git -C /tmp/incoming rm -rq . >/dev/null
    echo CLONED")"
case "$out" in *CLONED*) ;; *) _fail "$CURRENT_TEST" "clone/clean failed: $out"; finish ;; esac
docker exec -i "$CTN" tar -xf - -C /tmp/incoming <"$TARBALL" || {
    _fail "$CURRENT_TEST" "tar stream into /tmp/incoming failed"; finish; }
# --allow-empty: even when the candidate tree matches the container's checkout,
# an empty commit yields a NEW sha to prove delivery with. Published on both
# main and $CAND_TAG so either entrypoint mechanism delivers it.
out="$(dexec "set -e
    cd /tmp/incoming
    git add -A
    git -c user.email=ci@test -c user.name=CI commit -q --allow-empty -m 'ci: candidate'
    git tag -f '$CAND_TAG'
    git -C '$TC_WS' remote set-url origin /tmp/incoming
    echo STAGED")"
CAND_SHA="$(dexec 'git -C /tmp/incoming rev-parse HEAD' | tr -d '[:space:]')"
# Must be a NEW commit: a silent no-op here would let the restart validate
# upstream instead of this checkout.
case "$out" in *STAGED*) ;; *) _fail "$CURRENT_TEST" "staging failed: $out"; finish ;; esac
if [ -z "$CAND_SHA" ] || [ "$CAND_SHA" = "$ORIG_SHA" ]; then
    _fail "$CURRENT_TEST" "no new candidate commit (HEAD=$CAND_SHA, orig=$ORIG_SHA)"; finish
fi
_pass "$CURRENT_TEST"

# Seed the mock release the current entrypoint reads: derive the slug from origin
# exactly as the entrypoint does, so the mock lands at the path it will curl
# ($TOOLCHAIN_API_BASE/repos/<slug>/releases/latest).
slug="$(dexec "git -C '$TC_WS' remote get-url origin | sed -E 's#^.*github[.]com[:/]##; s#[.]git\$##'")"
put_file "/tmp/ghapi/repos/$slug/releases/latest" <<EOF
{"tag_name":"$CAND_TAG"}
EOF

it "2.2 restart completes startup with the update pending"
docker restart "$CTN" >/dev/null
if wait_startup 2; then _pass "$CURRENT_TEST"
else
    _fail "$CURRENT_TEST" "startup did not complete after the update restart: $(docker logs "$CTN" 2>&1 | tail -8)"
    exit 1
fi

# Only the entrypoint's auto-update can move HEAD here, so this proves delivery
# went through it.
it "2.3 auto-update moved the toolchain to the candidate commit"
head="$(dexec "git -C '$TC_WS' rev-parse HEAD" | tr -d '[:space:]')"
[ "$head" = "$CAND_SHA" ] && _pass "$CURRENT_TEST" \
    || _fail "$CURRENT_TEST" "HEAD=$head, expected $CAND_SHA"

# =============================================================================
# 3. Candidate entrypoint runs end-to-end (restart again: the previous start
#    still ran the PRE-update entrypoint file it was exec'ed from)
# =============================================================================
it "3.1 candidate entrypoint completes startup on the real image"
docker restart "$CTN" >/dev/null
if wait_startup 3; then _pass "$CURRENT_TEST"
else _fail "$CURRENT_TEST" "candidate entrypoint wedged: $(docker logs "$CTN" 2>&1 | tail -8)"; exit 1; fi

it "3.2 sysroot-fix succeeded against the real sysroot"
assert_not_contains "$(docker logs "$CTN" 2>&1)" "sysroot-fix failed"

it "3.3 all wrappers resolve on PATH"
out="$(dexec 'command -v arm64-chroot sysroot-rosdep-install sysroot-fix cross-colcon-build')"
assert_contains "$out" "/usr/local/bin/cross-colcon-build"

it "3.4 cross.cmake was normalized from $PRODUCT_CMAKE"
out="$(dexec "cmp -s '$TC_WS/$PRODUCT_CMAKE' '$TC_WS/cross.cmake' && echo CMAKE_OK")"
assert_contains "$out" "CMAKE_OK"

# =============================================================================
# 4. Real execution: chroot + cross build with the candidate scripts
# =============================================================================
it "4.1 arm64-chroot enters the sysroot: uname -m == aarch64"
out="$(dexec 'arm64-chroot uname -m')"
assert_contains "$out" "aarch64"

WS="$(docker exec "$CTN" bash -lc 'printf %s "$ROS2_WS"')"
PKG="$WS/src/smoke_pkg"

put_file "$PKG/package.xml" <<'XML'
<?xml version="1.0"?>
<package format="3">
  <name>smoke_pkg</name>
  <version>0.0.1</version>
  <description>minimal cross-compile smoke package</description>
  <maintainer email="ci@example.com">ci</maintainer>
  <license>Apache-2.0</license>
  <buildtool_depend>ament_cmake</buildtool_depend>
  <export><build_type>ament_cmake</build_type></export>
</package>
XML
put_file "$PKG/CMakeLists.txt" <<'CM'
cmake_minimum_required(VERSION 3.8)
project(smoke_pkg)
find_package(ament_cmake REQUIRED)
add_library(smoke SHARED src/smoke.cpp)
install(TARGETS smoke DESTINATION lib)
ament_package()
CM
put_file "$PKG/src/smoke.cpp" <<'CPP'
extern "C" int smoke_answer() { return 42; }
CPP

it "4.2 cross-colcon-build builds a minimal ament_cmake package"
out="$(dexec "cd '$WS' && cross-colcon-build --packages-select smoke_pkg")"
if echo "$out" | grep -qiE 'Finished.*smoke_pkg|Summary:.*1 package'; then
    _pass "$CURRENT_TEST"
else
    _fail "$CURRENT_TEST" "build did not report success: ${out: -400}"
fi

it "4.3 produced shared object is ARM aarch64 (not x86-64)"
out="$(dexec "file '$WS'/install/smoke_pkg/lib/libsmoke.so 2>/dev/null || file '$WS'/build/smoke_pkg/libsmoke.so 2>/dev/null")"
assert_contains "$out" "ARM aarch64"
assert_not_contains "$out" "x86-64"

finish
