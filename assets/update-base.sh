#!/usr/bin/env bash
# update-base.sh — helper for updating the ccbox-base image.
#
# Usage:
#   assets/update-base.sh launch   — cleanup old temp, launch from ccbox-base, apply idmap
#   assets/update-base.sh publish  — stop, republish as ccbox-base, cleanup
#
# Between launch and publish, run `lxc exec ccbox-update-temp -- ...` to make changes.

set -euo pipefail

TEMP="ccbox-update-temp"
IMAGE="ccbox-base"
LXC="/snap/bin/lxc"

# Read storage pool from state.json if available
pool() {
  local state="$HOME/.config/ccbox/state.json"
  if [ -f "$state" ]; then
    python3 -c "import json; d=json.load(open('$state')); print(d.get('storage_pool',''))" 2>/dev/null || true
  fi
}

cmd_launch() {
  # Cleanup leftover
  if $LXC info "$TEMP" &>/dev/null; then
    echo "Cleaning up leftover $TEMP..."
    $LXC delete "$TEMP" --force
  fi

  # Check base image exists
  if ! $LXC image alias list --format csv | grep -q "^${IMAGE},"; then
    echo "Error: base image '$IMAGE' not found. Run 'ccbox init' first." >&2
    exit 1
  fi

  local p
  p=$(pool)
  echo "Launching temp container from $IMAGE..."
  local args=("launch" "$IMAGE" "$TEMP")
  [ -n "$p" ] && args+=("-s" "$p")
  $LXC "${args[@]}"

  $LXC config set "$TEMP" raw.idmap "both 1000 1000"
  $LXC restart "$TEMP"
  echo "Ready. Run commands via: lxc exec $TEMP -- ..."
}

cmd_publish() {
  echo "Stopping $TEMP..."
  $LXC stop "$TEMP" 2>/dev/null || true
  echo "Publishing as $IMAGE..."
  $LXC publish "$TEMP" --alias="$IMAGE" --reuse
  echo "Cleaning up..."
  $LXC delete "$TEMP"
  echo "Done. Base image '$IMAGE' updated."
}

case "${1:-}" in
  launch)  cmd_launch ;;
  publish) cmd_publish ;;
  *)
    echo "Usage: $0 {launch|publish}" >&2
    exit 1
    ;;
esac
