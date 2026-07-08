#!/bin/bash

# This script deploy compiled program to remote target and run.

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NONE='\033[0m'

function print_usage() {
  echo -e "\n${GREEN}Usage${NONE}:"
  echo -e "  ${BOLD}./run_program.sh${NONE} run TARGET_IP TARGET_USER SSHPASS PACKAGE EXECUTABLE_NAME [ARGS...]     # for ros2 run"
  echo -e "  ${BOLD}./run_program.sh${NONE} launch TARGET_IP TARGET_USER SSHPASS PACKAGE LAUNCH_FILE [ARGS...]      # for ros2 launch"
  echo -e "\n${GREEN}TARGET_IP${NONE}:"
  echo -e "  ${NONE}IP address of remote target board."
  echo -e "\n${GREEN}TARGET_USER${NONE}:"
  echo -e "  ${NONE}User name."
  echo -e "\n${GREEN}SSHPASS${NONE}:"
  echo -e "  ${NONE}Password for ${TARGET_USER} user."
  echo -e "\n${GREEN}PACKAGE${NONE}:"
  echo -e "  ${NONE}Run a ROS2 node. Requires PACKAGE and EXECUTABLE. Use with ros2 run/launch."
  echo -e "\n${GREEN}EXECUTABLE_NAME${NONE}:"
  echo -e "  ${NONE}Launch a ROS2 launch file. Requires LAUNCH_FILE. Use with ros2 run."
  echo -e "\n${GREEN}LAUNCH_FILE${NONE}:"
  echo -e "  ${NONE}Launch a ROS2 launch file. Requires LAUNCH_FILE. Use with ros2 launch."
  echo -e "\n${GREEN}ARGS${NONE}:"
  echo -e "  ${NONE}Additional arguments to pass to the ROS2 application."
}

# Function to get ROS-related environment variables from target's bashrc
function get_target_ros_env() {
  local target_ip=$1
  local target_user=$2
  local sshpass=$3

  sshpass -p "${sshpass}" ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new ${target_user}@${target_ip} \
    "grep -E '^export (ROS_|RMW_|FASTRTPS_|CYCLONEDDS_|DDS_|RCUTILS_|SPDLOG_)' ~/.bashrc 2>/dev/null" 2>/dev/null | \
    sort
}

# Check if there are at least 6 arguments
if [ $# -lt 6 ]; then
  echo -e "${RED}[Host] Not enough arguments.${NONE}"
  print_usage
  exit 1
fi

# Determine mode based on the first argument
if [ "$1" = "run" ]; then
  MODE="run"
elif [ "$1" = "launch" ]; then
  MODE="launch"
else
  print_usage
  exit 1
fi

ROS2_MODE_CMD="ros2 $MODE"

TARGET_IP=$2
TARGET_USER=$3
SSHPASS=$4
PACKAGE=$5
EXECUTABLE=$6
shift 6
ARGS=$@
ROS2_APP_CMD="$ROS2_MODE_CMD $PACKAGE $EXECUTABLE $ARGS"

# Check ssh connection
if ! sshpass -p "${SSHPASS}" ssh -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new ${TARGET_USER}@${TARGET_IP} "exit"; then
  echo -e "${RED}[Host] SSH connection to ${TARGET_USER}@${TARGET_IP} failed. Please check the IP address and password.${NONE}"
  exit 1
fi

# Check if install folder exists relative to the script location and copy it to remote /tmp if it does
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Strip the trailing "/.vscode" by string, not via kernel "..", so a symlinked
# .vscode (e.g. ~/ros2_ws/.vscode -> ~/toolchains/.vscode) still resolves
# install/ next to the *logical* workspace instead of the symlink's real parent.
INSTALL_DIR="${SCRIPT_DIR%/*}/install"

if [ -d "$INSTALL_DIR" ]; then
  echo -e "${GREEN}[Host] '$INSTALL_DIR' folder found. Synchronizing install folder${NONE}"
  sudo rsync -avz --delete-during -e "sshpass -p "${SSHPASS}" ssh -o StrictHostKeyChecking=no" \
    "$INSTALL_DIR/" \
    "${TARGET_USER}@${TARGET_IP}:/tmp/install/"
else
  echo -e "${RED}[Host] '$INSTALL_DIR' folder not found. Skipping synchronization.${NONE}"
  exit 1
fi

# Get ROS environment from target
echo -e "${GREEN}[Host] Fetching ROS environment from target...${NONE}"
TARGET_ENV_EXPORTS=$(get_target_ros_env ${TARGET_IP} ${TARGET_USER} ${SSHPASS})
# Convert multiline exports to single line
TARGET_ENV_SINGLE_LINE=$(echo "${TARGET_ENV_EXPORTS}" | tr '\n' ';' | sed 's/;$//')

if [ -n "${TARGET_ENV_EXPORTS}" ]; then
  echo -e "${GREEN}[Host] Detected environment on target:${NONE}"
  echo "${TARGET_ENV_EXPORTS}"
else
  echo -e "${BLUE}[Host] No custom ROS environment found on target${NONE}"
fi

# Connect to target board (Use option -tt to force tty allocation)
echo -e "${GREEN}\n[Host] Trying to login ${TARGET_USER}@${TARGET_IP}...${NONE}"
sshpass -p "${SSHPASS}" ssh -tt ${TARGET_USER}@${TARGET_IP} " \
  stty -echo; \
  ${TARGET_ENV_SINGLE_LINE}; \
  echo -e \"\n${BLUE}[Target] Welcome to ${TARGET_IP}!${NONE}\"; \
  echo -e \"\n${BLUE}[Target] Running app: ${BOLD}${ROS2_APP_CMD}${NONE}${BLUE}...${NONE}\n\"; \
  # Check if /opt/ros/jazzy/setup.bash exists
  if [ ! -f /opt/ros/jazzy/setup.bash ]; then \
    echo -e \"${RED}[Target] /opt/ros/jazzy/setup.bash not found. Exiting.${NONE}\"; \
    exit 1; \
  fi; \
  source /opt/ros/jazzy/setup.bash; \

  # Check if /tmp/install/setup.bash exists
  if [ ! -f /tmp/install/setup.bash ]; then \
    echo -e \"${RED}[Target] /tmp/install/setup.bash not found. Exiting.${NONE}\"; \
    exit 1; \
  fi; \
  source /tmp/install/setup.bash; \
  ${ROS2_APP_CMD}; \
"