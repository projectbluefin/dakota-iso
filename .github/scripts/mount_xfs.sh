#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# mount_xfs.sh — Create an XFS loopback on /mnt for Dakota ISO builds.
#
# The chunkified Dakota images (~120 layers) cause VFS import to create
# ~450 GB of intermediate directories under BTRFS.  XFS handles this
# workload much faster and avoids the VFS layer explosion.
#
# What this script does:
#   1. Creates an XFS loopback image at /mnt (using available space)
#   2. Configures podman storage (overlay) on the XFS mount
#   3. Sets CS_STAGING_OVERRIDE for the just recipe
#
# Environment variables (all optional):
#   SIZE            — loopback size in bytes (default: 95% of /mnt available space)
#   XFS_MOUNT       — mount point (default: /mnt)
#   XFS_LOOPBACK    — loopback image path (default: /mnt/xfs-loopback.img)

set -euo pipefail

XFS_MOUNT="${XFS_MOUNT:-/mnt}"

# ── Check if /mnt is a separate mount point ────────────────────────────────
if ! mountpoint -q "${XFS_MOUNT}"; then
  echo "${XFS_MOUNT} is not a separate mount point, skipping XFS setup"
  exit 0
fi

# ── Check available space ──────────────────────────────────────────────────
MIN_SPACE=$((45 * 1000 * 1000 * 1000))  # 45 GB minimum for VFS import
AVAILABLE=$(findmnt "${XFS_MOUNT}" --bytes --df --json | jq -r '.filesystems[0].avail')
AVAILABLE_HUMAN=$(findmnt "${XFS_MOUNT}" --df --json | jq -r '.filesystems[0].avail')

if [[ "$AVAILABLE" -lt "$MIN_SPACE" ]]; then
  echo "${XFS_MOUNT} only has ${AVAILABLE_HUMAN} — need at least 45G for VFS import"
  echo "Continuing without XFS mount..."
  exit 0
fi

echo "Available space on ${XFS_MOUNT}: ${AVAILABLE_HUMAN}"

# ── Determine loopback size ────────────────────────────────────────────────
if [[ -z "${SIZE:-}" ]]; then
  # Use 95% of available space
  SIZE=$(jq -n --arg avail "$AVAILABLE" '($avail | tonumber) * 0.95 | floor')
fi
echo "Loopback size: $(( SIZE / 1024 / 1024 / 1024 )) GB"

# ── Unmount the existing filesystem on /mnt ────────────────────────────────
# We need to replace the existing mount (often ext4 on GHA runners) with XFS.
# Save the loopback image on the underlying block device's filesystem first.
XFS_LOOPBACK="${XFS_LOOPBACK:-${XFS_MOUNT}/xfs-loopback.img}"

echo "Creating XFS loopback at ${XFS_LOOPBACK}..."
truncate -s 0 "${XFS_LOOPBACK}"
# chattr +C disables copy-on-write on BTRFS hosts (no-op on other filesystems)
chattr +C "${XFS_LOOPBACK}" 2>/dev/null || true
fallocate -l "${SIZE}" "${XFS_LOOPBACK}"

# Format as XFS
mkfs.xfs -f "${XFS_LOOPBACK}"

# Mount the XFS loopback — we mount it at a subdirectory since /mnt itself
# holds the loopback image file.
XFS_DIR="${XFS_MOUNT}/xfs"
mkdir -p "${XFS_DIR}"
mount -o loop "${XFS_LOOPBACK}" "${XFS_DIR}"
echo "XFS mounted at ${XFS_DIR}"

# ── Configure podman storage on the XFS mount ─────────────────────────────
STORAGE_DIR="${XFS_DIR}/containers/storage"
mkdir -p "${STORAGE_DIR}"

# Clear any existing podman storage on root fs
rm -rf /var/lib/containers/storage 2>/dev/null || true

# Write podman storage config pointing to XFS
printf '[storage]\ndriver = "overlay"\ngraphroot = "%s"\nrunroot = "/run/containers/storage"\n' \
  "${STORAGE_DIR}" > /etc/containers/storage.conf

echo "Podman storage configured → ${STORAGE_DIR}"

# ── Set CS_STAGING_OVERRIDE ────────────────────────────────────────────────
CS_STAGING="${XFS_DIR}/cs-staging"
mkdir -p "${CS_STAGING}"

if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "CS_STAGING_OVERRIDE=${CS_STAGING}" >> "$GITHUB_ENV"
  echo "CS_STAGING_OVERRIDE exported to GITHUB_ENV"
else
  echo "CS_STAGING_OVERRIDE=${CS_STAGING}"
  echo "Set this in your environment: export CS_STAGING_OVERRIDE=${CS_STAGING}"
fi

echo "=== XFS setup complete ==="
df -h "${XFS_DIR}"
