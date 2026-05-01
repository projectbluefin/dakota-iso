#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# mount_xfs.sh — Create an XFS loopback for Dakota ISO VFS staging.
#
# The chunkified Dakota images (~120 layers) cause VFS import to exhaust
# ext4 inodes (fixed at mkfs time). XFS has dynamic inode allocation and
# handles this workload without issue.
#
# Strategy:
#   1. If /mnt is a separate mount with ≥45GB, put XFS loopback there
#   2. Otherwise, create a sparse XFS loopback on root
#   3. Mount XFS at /var/dakota-staging
#   4. Set CS_STAGING_OVERRIDE for the just recipe
#
# This script does NOT reconfigure podman storage — podman stays on root
# with overlay driver. Only the VFS staging area moves to XFS.
#
# Environment variables (all optional):
#   XFS_SIZE_GB     — loopback size in GB (default: auto-calculated)

set -euo pipefail

XFS_MOUNTPOINT="/var/dakota-staging"
MIN_GB=45

echo "=== Setting up XFS staging for Dakota ISO build ==="

# ── Find the best location for the loopback image ─────────────────────────
pick_loopback_location() {
  # Prefer /mnt if it's a separate mount with enough space
  if mountpoint -q /mnt 2>/dev/null; then
    local avail_kb
    avail_kb=$(df --output=avail -B1024 /mnt | tail -1 | tr -d ' ')
    local avail_gb=$(( avail_kb / 1024 / 1024 ))
    if [[ "$avail_gb" -ge "$MIN_GB" ]]; then
      echo "/mnt has ${avail_gb}GB available — using it for XFS loopback"
      echo "/mnt/dakota-xfs.img"
      return
    fi
    echo "/mnt only has ${avail_gb}GB — not enough" >&2
  else
    echo "/mnt is not a separate mount" >&2
  fi

  # Fallback: use root filesystem (sparse file won't consume space immediately)
  local root_avail_kb
  root_avail_kb=$(df --output=avail -B1024 / | tail -1 | tr -d ' ')
  local root_avail_gb=$(( root_avail_kb / 1024 / 1024 ))
  if [[ "$root_avail_gb" -ge "$MIN_GB" ]]; then
    echo "Root has ${root_avail_gb}GB available — using sparse XFS loopback on root" >&2
    echo "/var/tmp/dakota-xfs.img"
    return
  fi

  echo "ERROR: Neither /mnt nor root has ${MIN_GB}GB available" >&2
  echo "  /mnt: $(df -h /mnt 2>/dev/null | tail -1 || echo 'not mounted')" >&2
  echo "  root: $(df -h / | tail -1)" >&2
  return 1
}

LOOPBACK_PATH=$(pick_loopback_location)

# ── Determine size ─────────────────────────────────────────────────────────
if [[ -n "${XFS_SIZE_GB:-}" ]]; then
  SIZE_GB="$XFS_SIZE_GB"
else
  # Use 90% of the filesystem holding the loopback image
  local_dir=$(dirname "$LOOPBACK_PATH")
  avail_kb=$(df --output=avail -B1024 "$local_dir" | tail -1 | tr -d ' ')
  # Reserve 50GB for runner overhead, podman overlay, and ISO output.
  # VFS import needs ~45GB per issue #19 testing.
  RESERVE_GB=50
  SIZE_GB=$(( avail_kb / 1024 / 1024 - RESERVE_GB ))
  [[ "$SIZE_GB" -lt 45 ]] && { echo "ERROR: not enough space for XFS staging (need 45GB + ${RESERVE_GB}GB reserve)"; exit 1; }
  [[ "$SIZE_GB" -gt 75 ]] && SIZE_GB=75  # 75GB ceiling is plenty
fi
echo "XFS loopback: ${LOOPBACK_PATH} (${SIZE_GB}GB sparse)"

# ── Create and format XFS loopback ────────────────────────────────────────
truncate -s 0 "$LOOPBACK_PATH"
chattr +C "$LOOPBACK_PATH" 2>/dev/null || true
truncate -s "${SIZE_GB}G" "$LOOPBACK_PATH"
mkfs.xfs -f "$LOOPBACK_PATH"

# ── Mount at the staging path ─────────────────────────────────────────────
mkdir -p "$XFS_MOUNTPOINT"
mount -o loop "$LOOPBACK_PATH" "$XFS_MOUNTPOINT"
echo "XFS mounted at ${XFS_MOUNTPOINT}"

# ── Set CS_STAGING_OVERRIDE ────────────────────────────────────────────────
CS_STAGING="${XFS_MOUNTPOINT}/cs-staging"
mkdir -p "$CS_STAGING"

if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "CS_STAGING_OVERRIDE=${CS_STAGING}" >> "$GITHUB_ENV"
  echo "CS_STAGING_OVERRIDE set via GITHUB_ENV"
else
  export CS_STAGING_OVERRIDE="$CS_STAGING"
  echo "export CS_STAGING_OVERRIDE=${CS_STAGING}"
fi

echo "=== XFS staging setup complete ==="
df -h "$XFS_MOUNTPOINT"
df -i "$XFS_MOUNTPOINT"
