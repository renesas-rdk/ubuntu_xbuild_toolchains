#!/bin/bash

# Align the container's `ubuntu` to the host owner of the bind-mounted ros2_ws
# so it's editable from both sides without chmod (root-owned -> hand to ubuntu;
# other UID -> re-map ubuntu to it). Done in one sudo that ends by dropping back
# to ubuntu via setpriv and re-running this script, so `docker exec` still lands
# as ubuntu (a usermod + second sudo would crash on the now-stale caller UID).
ROS2_WS_DIR="${ROS2_WS:-/home/ubuntu/ros2_ws}"
if [ -d "$ROS2_WS_DIR" ]; then
    ws_uid="$(stat -c %u "$ROS2_WS_DIR" 2>/dev/null || echo -1)"
    ws_gid="$(stat -c %g "$ROS2_WS_DIR" 2>/dev/null || echo -1)"
    if [ "$ws_uid" != "-1" ] && { [ "$ws_uid" != "$(id -u)" ] || [ "$ws_gid" != "$(id -g)" ]; }; then
        echo "[INFO] Reconciling 'ubuntu' with workspace owner ${ws_uid}:${ws_gid}..."
        exec sudo bash -c '
            set -u
            ws_uid=$1; ws_gid=$2; cur_uid=$3; cur_gid=$4; ws=$5; self=$6; shift 6
            if [ "$ws_uid" = 0 ]; then
                chown "$cur_uid:$cur_gid" "$ws" 2>/dev/null || true
                drop_uid=$cur_uid; drop_gid=$cur_gid
            else
                # Re-map ubuntu to the workspace owner (or create a new user if needed)
                sed -i -E "s/^(ubuntu:[^:]*:)[0-9]+:[0-9]+:/\1${ws_uid}:${ws_gid}:/" /etc/passwd 2>/dev/null || true
                sed -i -E "s/^(ubuntu:[^:]*:)[0-9]+:/\1${ws_gid}:/"                   /etc/group  2>/dev/null || true
                # -xdev keeps re-owning on the home filesystem and never descends
                # into the ros2_ws bind mount (a separate device).
                find /home/ubuntu -xdev \( -uid "$cur_uid" -o -gid "$cur_gid" \) \
                    -exec chown -h "$ws_uid:$ws_gid" {} + 2>/dev/null || true
                drop_uid=$ws_uid; drop_gid=$ws_gid
            fi
            exec setpriv --reuid "$drop_uid" --regid "$drop_gid" --init-groups -- "$self" "$@"
        ' bash "$ws_uid" "$ws_gid" "$(id -u)" "$(id -g)" "$ROS2_WS_DIR" "$0" "$@"
    fi
fi

# Require ARM64_SYSROOT to be set (prevents accidental writes)
: "${ARM64_SYSROOT:?ARM64_SYSROOT is not set}"

echo "Updating DNS configuration in sysroot..."
mkdir -p "${ARM64_SYSROOT}/etc"
sudo cp /etc/resolv.conf "${ARM64_SYSROOT}/etc/resolv.conf" 2>/dev/null || echo "[WARN] Could not update DNS in sysroot."
echo "DNS updated in sysroot."

TOOLCHAIN_DIR="${TOOLCHAINS_WS:-/home/ubuntu/toolchains}"

# Absolute path to THIS script, resolved before the auto-update block `cd`s
# away, so the post-update re-exec below can run the freshly checked-out copy of
# itself. self_sum fingerprints that file to tell "the release rewrote it" from
# "it is unchanged"; with no md5sum it returns a constant, so the two readings
# always compare equal and the re-exec simply never fires.
SELF="$0"
case "$SELF" in /*) ;; *) SELF="$PWD/$SELF";; esac
self_sum() { md5sum "$SELF" 2>/dev/null || echo "no-md5sum"; }

# === Symlink helper scripts (always, before auto-update) ===
for pair in \
  "arm64-chroot.sh:arm64-chroot" \
  "sysroot-rosdep-install.sh:sysroot-rosdep-install" \
  "sysroot-fix.py:sysroot-fix" \
  "cross-colcon-build.sh:cross-colcon-build"; do
  src="$TOOLCHAIN_DIR/${pair%:*}"
  dst="/usr/local/bin/${pair#*:}"
  [ -f "$src" ] && sudo ln -sf "$src" "$dst"
done

# === Auto-update: check out the latest PUBLISHED release; never block container ===
# Users track vetted releases, not the moving `main` branch, and not raw tags:
# the release is resolved from the GitHub Releases API, so a pushed-but-failing
# tag (which has no release yet) never reaches containers — only a release the CI
# publishes after its tests pass does. Offline / no-release / checkout failure is
# non-fatal: the container keeps the toolchain baked into the image (a valid release).
if [ -d "$TOOLCHAIN_DIR/.git" ]; then
  cd "$TOOLCHAIN_DIR" || { echo "ERROR: cannot cd into $TOOLCHAIN_DIR" >&2; exit 1; }
  git config user.email "container@local" 2>/dev/null || true
  git config user.name "Container" 2>/dev/null || true

  updated=0
  self_sum_before="$(self_sum)"

  if [ -n "${TOOLCHAIN_REEXEC_RELEASE:-}" ]; then
    # We are the second pass: an earlier pass checked this release out and
    # re-exec'd us because the release rewrote entrypoint.sh itself (see the
    # re-exec below). The tree already sits on the release, so skip resolving
    # and fetching it a second time and drop straight into the post-update
    # steps — this script's versions of them.
    latest="$TOOLCHAIN_REEXEC_RELEASE"
    unset TOOLCHAIN_REEXEC_RELEASE
    updated=1
  else
    # Resolve the latest PUBLISHED release from the GitHub API (excludes drafts
    # and pre-releases). OWNER/REPO is derived from origin, stripping any
    # embedded credentials and the .git suffix. Parsed with grep/sed so no jq is
    # needed. TOOLCHAIN_API_BASE overrides the API host (a GitHub Enterprise
    # mirror, or a local mock in the integration test); it defaults to the
    # public API.
    api_base="${TOOLCHAIN_API_BASE:-https://api.github.com}"
    slug="$(git remote get-url origin 2>/dev/null | sed -E 's#^.*github\.com[:/]##; s#\.git$##')"
    latest=""
    if [ -n "$slug" ]; then
      latest="$(timeout 15 curl -fsSL -H 'Accept: application/vnd.github+json' \
        "$api_base/repos/$slug/releases/latest" 2>/dev/null \
        | grep -m1 '"tag_name"' | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/')"
    fi
  fi

  if [ "$updated" -eq 1 ]; then
    : # already checked out by the pass that re-exec'd us — nothing to resolve
  elif [ -z "$latest" ]; then
    echo "[WARN] No published release resolved (offline / none) — using local toolchain."
  elif ! timeout 15 git fetch --force origin "refs/tags/$latest:refs/tags/$latest" >/dev/null 2>&1; then
    echo "[WARN] Could not fetch release $latest — using local toolchain."
  elif [ -z "$(git status --porcelain 2>/dev/null)" ]; then
    # Fully clean toolchain tree (no tracked edits, no untracked files) —
    # nothing user-made can be lost, safe to force straight to the release.
    if git checkout -q -f "$latest" >/dev/null 2>&1; then
      echo "[INFO] Toolchain checked out release $latest."
      updated=1
    else
      echo "[WARN] Checkout of $latest failed — using local toolchain."
    fi
  else
    # The user has edited tracked files and/or added untracked ones. NEVER
    # discard either: stash tracked edits, move to the release with a NON-force
    # checkout (git refuses to overwrite untracked files, so a collision fails
    # safe), then re-apply the stash on top. A clean re-apply keeps the edits;
    # a conflicting one leaves standard git conflict markers in the tree for
    # the user to resolve by hand (README: "Resolving toolchain update
    # conflicts") and retains the stash as a safety copy. Either way the
    # container still finishes starting.
    echo "[INFO] Local toolchain changes detected — preserving them across update to $latest..."
    stashed=0
    if ! git diff --quiet HEAD 2>/dev/null; then
      if git stash push -q >/dev/null 2>&1; then
        stashed=1
      else
        echo "[WARN] Could not stash local edits — kept your local toolchain unchanged."
      fi
    fi
    if [ "$stashed" -eq 1 ] || git diff --quiet HEAD 2>/dev/null; then
      if git checkout -q "$latest" >/dev/null 2>&1; then
        if [ "$stashed" -eq 0 ]; then
          echo "[INFO] Toolchain checked out release $latest (untracked files kept)."
          updated=1
        elif git stash pop >/dev/null 2>&1; then
          echo "[INFO] Toolchain updated to $latest; your local edits re-applied cleanly."
          updated=1
        else
          # stash pop hit a conflict: HEAD is on the release, conflict markers
          # are in the tree, and the stash is kept so nothing is lost.
          echo "[WARN] Your local toolchain edits CONFLICT with release $latest."
          echo "[WARN] Conflict markers are in the affected files. Resolve them, then run:"
          echo "[WARN]     cd $TOOLCHAIN_DIR && git stash drop"
          echo "[WARN] See README 'Resolving toolchain update conflicts'. Update left unfinished."
        fi
      else
        # Could not reach the release (e.g. an untracked file would be
        # overwritten by it) — re-apply the stash so the toolchain is exactly
        # as before.
        [ "$stashed" -eq 1 ] && { git stash pop >/dev/null 2>&1 || true; }
        echo "[WARN] Could not check out $latest (an untracked file may be in the way) — kept your local toolchain unchanged."
      fi
    fi
  fi

  if [ "$updated" -eq 1 ]; then
    # The release just checked out may have rewritten entrypoint.sh ITSELF,
    # while this process still runs the OLD script — so every step below, and
    # the wrapper and skill symlink lists, would be the PREVIOUS release's, and
    # anything a release adds there would land one restart late. Re-exec the
    # fresh copy, passing the resolved tag in TOOLCHAIN_REEXEC_RELEASE so it
    # knows the checkout is done and does not resolve, fetch or apply again.
    #
    # It fires at most once per start: on the second pass both fingerprints read
    # the new file, so they match and it falls through. If $0 is not the file
    # git updated (a copy baked elsewhere in the image), the fingerprint is
    # unchanged too and startup continues here.
    if [ "$self_sum_before" != "$(self_sum)" ]; then
      echo "[INFO] entrypoint.sh changed in $latest — restarting startup with the new script."
      export TOOLCHAIN_REEXEC_RELEASE="$latest"
      exec bash "$SELF" "$@"
    fi

    # Normalize product cmake files
    product="${PRODUCT:-V2H}"
    case "$product" in
      V2H) [ -f v2h_cross.cmake ] && cp v2h_cross.cmake cross.cmake;;
      V4H) [ -f v4h_cross.cmake ] && cp v4h_cross.cmake cross.cmake;;
    esac

    echo "[INFO] Toolchain synchronized."
    /usr/local/bin/sysroot-fix || echo "[WARN] sysroot-fix failed, skipping."
  fi
fi

# === Seed / merge per-user VS Code settings from the tracked template ===
# settings.json is gitignored so auto-update is never blocked by user edits and
# never clobbers them. The template is the source of truth for structure + new
# keys; we overlay the user-owned values back on top so target/project config
# survives every update.
VSCODE_DIR="$TOOLCHAIN_DIR/.vscode"
TEMPLATE="$VSCODE_DIR/settings.template.json"
SETTINGS="$VSCODE_DIR/settings.json"
if [ -f "$TEMPLATE" ]; then
  if command -v python3 >/dev/null 2>&1; then
    if python3 - "$TEMPLATE" "$SETTINGS" <<'PYMERGE'; then
import json, os, re, sys

USER_KEYS = ["TARGET_IP", "TARGET_GDB_PORT", "TARGET_USER", "TARGET_PASSWORD",
             "TARGET_ROS2_WS", "NODE_PACKAGE_NAME", "NODE_EXECUTABLE_NAME",
             "LAUNCH_PACKAGE_NAME", "LAUNCH_FILE_NAME"]

def load_jsonc(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    # tolerate VS Code JSONC: // line comments and trailing commas
    text = re.sub(r"(^|\s)//[^\n]*", r"\1", text)
    text = re.sub(r",(\s*[}\]])", r"\1", text)
    return json.loads(text)

template, settings = sys.argv[1], sys.argv[2]

# Pull over values the user already set, so a re-run never clobbers them.
overrides = {}
if os.path.exists(settings):
    user = load_jsonc(settings)
    overrides = {k: user[k] for k in USER_KEYS if k in user}

# Transform the template TEXT (not its parsed data) so blank-line grouping,
# key order, and any comments survive verbatim. We only ever: drop the
# template-only "DO NOT EDIT" banner, and swap in the user's override values.
key_re = re.compile(r'^(\s*"([^"]+)"\s*:\s*)("(?:[^"\\]|\\.)*"|[^,]*?)(,?\s*)$')
out = []
with open(template, encoding="utf-8") as f:
    for line in f:
        if line.lstrip().startswith('"#"'):
            continue
        m = key_re.match(line.rstrip("\n"))
        if m and m.group(2) in overrides:
            line = m.group(1) + json.dumps(overrides[m.group(2)], ensure_ascii=False) + m.group(4) + "\n"
        out.append(line)

with open(settings + ".tmp", "w", encoding="utf-8") as f:
    f.writelines(out)
PYMERGE
      mv "$SETTINGS.tmp" "$SETTINGS"
      echo "[INFO] Seeded/merged .vscode/settings.json from template (user values preserved)."
    else
      rm -f "$SETTINGS.tmp"
      echo "[WARN] settings.json merge skipped (invalid JSON?) — kept user file as-is."
    fi
  elif [ ! -f "$SETTINGS" ]; then
    cp "$TEMPLATE" "$SETTINGS"
    echo "[INFO] Seeded .vscode/settings.json from template (python3 unavailable; banner not stripped)."
  else
    echo "[WARN] python3 not found — skipping settings.json template merge."
  fi
fi

# === Seed the user-local sysroot fixups file from its template (if missing) ===
# sysroot-fix-append.yaml is gitignored: auto-update never clobbers it and never
# conflicts with it, so it is the frictionless place for users to add their OWN
# package fixups (the tracked sysroot-fix.yaml can conflict on update). We seed it
# only when absent so a user's edits are never overwritten. Editing it does NOT
# auto-apply — run `sysroot-fix` to patch the sysroot with the new rules.
APPEND_TEMPLATE="$TOOLCHAIN_DIR/sysroot-fix-append.template.yaml"
APPEND_FILE="$TOOLCHAIN_DIR/sysroot-fix-append.yaml"
if [ -f "$APPEND_TEMPLATE" ] && [ ! -f "$APPEND_FILE" ]; then
  cp "$APPEND_TEMPLATE" "$APPEND_FILE"
  echo "[INFO] Seeded sysroot-fix-append.yaml from template."
fi

# === Symlink agent skill files ===
# Guarded so a mounted workspace that already ships its own .vscode/.github/
# AGENTS.md/etc. is never clobbered: refresh our own symlink, but if a real
# file/dir is already there, leave it and warn. Plain `ln -sfn` would either
# delete the user's file (file dest) or nest a bogus .vscode/.vscode inside it
# (directory dest) — both silently.
link_skill() {  # $1=toolchain target, $2=link path in the workspace
  if [ -L "$2" ] || [ ! -e "$2" ]; then
    ln -sfn "$1" "$2"
  else
    echo "[WARN] $2 already exists and is not a symlink — keeping it, not linking $(basename "$1") from the toolchain."
  fi
}

if [ -d "$ROS2_WS_DIR" ] && [ "$ROS2_WS_DIR" != "$TOOLCHAIN_DIR" ]; then
  link_skill "$TOOLCHAIN_DIR/.vscode"       "$ROS2_WS_DIR/.vscode"
  link_skill "$TOOLCHAIN_DIR/.github"       "$ROS2_WS_DIR/.github"
  link_skill "$TOOLCHAIN_DIR/.claude"       "$ROS2_WS_DIR/.claude"
  link_skill "$TOOLCHAIN_DIR/.agents"       "$ROS2_WS_DIR/.agents"
  link_skill "$TOOLCHAIN_DIR/AGENTS.md"     "$ROS2_WS_DIR/AGENTS.md"
  link_skill "$TOOLCHAIN_DIR/.clang-format" "$ROS2_WS_DIR/.clang-format"
  echo "[INFO] Agent skill files symlinked to $ROS2_WS_DIR."
fi

echo "--- Startup Complete ---"
exec "$@"
