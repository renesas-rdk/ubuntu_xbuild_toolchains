---
name: arm64-target-autonomous-test
description: Use this skill when an AI agent must drive the ARM64 target board itself, non-interactively, from inside the dev container, for any kind of on-device validation. Triggers include "test on the board", "verify the deploy", "smoke test the node", "tail the journal on the device", "check what's running on the target", "reproduce the issue on hardware", or any unattended SSH-driven verification. Complements arm64-deploy-debug (which is for human-in-the-loop VS Code flows).
---

# ARM64 Autonomous Target Testing

General-purpose, agent-driven validation of anything running on the
ARM64 board. The skill is **deployment-agnostic**: it covers the
mechanics of connecting, inspecting, mutating, and cleaning up. The
r365_rdk_ota agent / fluentbit bridge / `ros2-app` unit are mentioned
only as **concrete examples** of stacks you might verify with it.

## When to use this skill

Apply when **all** of these hold:

- The work needs hands-on shell access to the board (not just deploy).
- The agent — not a human — is driving the SSH session.
- The user has asked for, or implied, on-device validation
  ("check it works", "verify the deploy", "run a smoke test",
  "reproduce on hardware").

Typical jobs:

- Smoke-testing a freshly deployed ROS 2 node or launch file.
- Confirming a `systemd` unit's state / log / restart count.
- Tailing a bounded `journalctl` window to debug an in-flight task.
- Pushing/pulling a single file with `rsync` to reproduce an issue.
- Running an end-to-end OTA / installer test (one **example** stack).
- Cleaning up after a test (rollback).

## When NOT to use this skill

- Build is failing → `arm64-cross-build`.
- A human is in VS Code and just wants to deploy/run/debug
  → `arm64-deploy-debug`.
- You just need to look at code, not the device → no skill needed.

## 0. Confirm intent before connecting

The board is shared lab hardware. Before the **first** SSH command of
a session:

- Confirm the user actually wants on-device action; do not hop on the
  board for unrelated work.
- State, in one sentence, what you are about to do.
- Treat any persistent change (apt install, `systemctl enable`, file
  drop under `/opt`/`/etc`, `docker rmi`) as something that warrants
  a heads-up first.

## 1. Connection parameters

Read from `.vscode/settings.json` — never hard-code:

| Setting              | Source                              |
|----------------------|-------------------------------------|
| `TARGET_IP`          | `.vscode/settings.json`             |
| `TARGET_USER`        | `.vscode/settings.json`             |
| `TARGET_PASSWORD`    | `.vscode/settings.json`             |
| `TARGET_ROS2_WS`     | `.vscode/settings.json`             |

Read them from the file (grep or a small JSON read). If a value is
missing, stop and ask.

## 2. Non-interactive SSH/rsync via sshpass

Password auth is the supported flow on this lab board. Standard form:

```bash
SSH_OPTS=(-o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new
          -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

sshpass -p "$TARGET_PASSWORD" ssh "${SSH_OPTS[@]}" \
    "$TARGET_USER@$TARGET_IP" '<command>'
```

Notes:

- `accept-new` + `UserKnownHostsFile=/dev/null` keeps the dev
  container's `known_hosts` clean across rebuilds.
- `LogLevel=ERROR` silences the "added to known hosts" banner so
  output is parseable.
- For multi-line work prefer a quoted heredoc:

  ```bash
  sshpass -p "$TARGET_PASSWORD" ssh "${SSH_OPTS[@]}" \
      "$TARGET_USER@$TARGET_IP" bash -s <<'REMOTE'
  set -euo pipefail
  uname -a
  systemctl --no-pager --failed | head
  df -h /
  REMOTE
  ```

- For files: `sshpass -p "$TARGET_PASSWORD" rsync -az -e \
  "ssh ${SSH_OPTS[*]}" SRC "$TARGET_USER@$TARGET_IP:DEST"`.
  Never build install trees on the device — ship them.

## 3. First-contact health probe

Before any mutation, run a small probe and stop on anything unexpected:

```bash
uname -a                 # expect aarch64 with a Renesas kernel
date -u                  # clock sanity (TLS / log timestamps depend on it)
systemctl --no-pager --failed | head
df -h /                  # need >1G typically
free -m
docker version --format '{{.Server.Version}}' 2>/dev/null || echo "(no docker)"
```

If `--failed` lists units that are *expected* to be failing for the
test you are about to run, note it explicitly; do not silently mask
state.

## 4. Standard ROS 2 / system inspection

These work on any ARM64 board, regardless of which app stack is
installed:

| What                       | Command                                                              |
|----------------------------|----------------------------------------------------------------------|
| Sourced ROS distro         | `ls /opt/ros/`                                                       |
| Workspace install present  | `ls "$TARGET_ROS2_WS/install" 2>/dev/null \| head`                   |
| Node listing               | `source /opt/ros/jazzy/setup.bash && ros2 node list`                 |
| Topic listing              | `... && ros2 topic list`                                             |
| One-shot topic sample      | `... && timeout 3 ros2 topic echo --once <topic>`                    |
| Param dump for a node      | `... && ros2 param dump <node>`                                      |
| Process tree of a unit     | `systemctl status <unit> --no-pager -n 0` + `ps -ef \| grep <bin>`   |
| Bounded log slice          | `journalctl -u <unit> --since "2 min ago" --no-pager`                |
| Last boot of a unit        | `journalctl -u <unit> -b --no-pager \| tail -100`                    |
| Recent unit failures       | `systemctl --no-pager --failed`                                      |

Always pass `--no-pager` for `systemctl`/`journalctl`. Use `-n N`,
`--since`, or `timeout N` to bound output. Never `journalctl -f` or
`ros2 topic echo` (without a timeout) in autonomous mode — they will
hang the SSH session.

When sourcing ROS on the board, also pick up the deployed workspace
overlay if the test depends on a freshly-deployed package:

```bash
source /opt/ros/jazzy/setup.bash
[ -f "$TARGET_ROS2_WS/install/setup.bash" ] && \
    source "$TARGET_ROS2_WS/install/setup.bash"
```

(The VS Code run/launch tasks instead source `/tmp/install/setup.bash`
because they rsync to `/tmp/install/` — see `arm64-deploy-debug`.)

## 5. Staging files for tests

Two patterns:

- **One-shots** (tarballs, scripts, sample data) → `rsync -az` to a
  `mktemp -d` on the device, run, then delete the directory.
- **Long-lived artifacts** (installer `.run` payloads, compose files,
  unit overrides) → drop into the location the consumer expects only
  after a corresponding `is-active` / diff confirms you understand the
  current state.

If a system component dispatches files in filename order (e.g. an
inbox queue), use a monotonic name (`$(date +%s%N).json`) so the
ordering is deterministic.

## 6. Rollback / cleanup

Every autonomous test must leave the device no worse than it found
it, unless the user explicitly asked for a destructive change. Capture
state you'll need to restore *before* mutating:

```bash
# Generic: snapshot every unit you might touch.
sshpass ... ssh ... \
    'systemctl show -p ActiveState,SubState,UnitFileState <unit> ...' \
    > /tmp/before-state.txt

# Generic: snapshot any config file you intend to swap.
sshpass ... ssh ... 'sha256sum /etc/<file> /opt/<file> 2>/dev/null' \
    > /tmp/before-hashes.txt
```

After the test: restart any unit you stopped, restore any file you
swapped (or note clearly that you didn't), and report a one-line diff
of `systemctl --failed`.

## 7. Reporting

Surface, in order:

1. What you ran (single sentence).
2. Exit code / unit state of the thing under test.
3. The relevant `journalctl` excerpt — a grep'd slice around the lines
   that decide the result, not the whole log.
4. Any state left behind on the device (files, running containers,
   modified units).

Every claim in the report must trace to a command output from this
session. If something was not checked, say so rather than inferring it
from a green exit code elsewhere.

## 8. Anti-patterns

- `sshpass` calls without `StrictHostKeyChecking=accept-new`.
- Running `apt-get`, `pip install`, `docker pull` on the device for
  speculative reasons. Build artifacts in the dev container, ship them.
- `docker system prune`, `rm -rf /var/lib/...`,
  `journalctl --vacuum-*`, or `disable --now <unit>` without explicit
  user consent.
- Long-lived background processes started over SSH (`ros2 launch ... &`,
  `journalctl -f`) that don't have a matching kill in the same session.
- Using `expect` / interactive shells when a one-shot
  `ssh ... bash -s <<EOF` would do.
- Reusing host paths inside `ros2 run` invocations — the on-device
  filesystem layout is independent of the dev container.

---

## Examples: stack-specific recipes

The recipes below are **examples**, not part of the core skill. Use
them when a session involves these specific stacks; otherwise adapt
the patterns above to whatever unit / executable the user named.

### Example A: smoke-testing a freshly deployed ROS 2 node

```bash
sshpass -p "$TARGET_PASSWORD" ssh "${SSH_OPTS[@]}" \
    "$TARGET_USER@$TARGET_IP" bash -s <<REMOTE
set -euo pipefail
source /opt/ros/jazzy/setup.bash
source "$TARGET_ROS2_WS/install/setup.bash"
timeout 5 ros2 run <pkg> <exe> --ros-args -p use_sim_time:=false &
APP=\$!
sleep 2
ros2 node list
ros2 topic list
kill \$APP 2>/dev/null || true
wait \$APP 2>/dev/null || true
REMOTE
```

### Example B: capturing a bag for offline analysis

```bash
sshpass ... ssh ... bash -s <<REMOTE
source /opt/ros/jazzy/setup.bash
DIR=\$(mktemp -d)
cd "\$DIR"
timeout 10 ros2 bag record -a -o capture
ls -la "\$DIR/capture"
echo "BAG_DIR=\$DIR"
REMOTE
# then rsync the capture/ directory back to the dev container,
# then ssh ... "rm -rf \$BAG_DIR"
```

## Cross-references

- Building the artifacts you'll ship → `arm64-cross-build`.
- VS Code-driven flows (build → deploy → run/launch → GDB attach)
  → `arm64-deploy-debug`.
- Package layout and conventions for what you're testing →
  `arm64-ros2-package-conventions`.
