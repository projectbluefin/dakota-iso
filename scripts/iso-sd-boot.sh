#!/usr/bin/bash
# scripts/iso-sd-boot.sh — Build a systemd-boot UEFI live ISO for a given target variant.
#
# Called by the iso-sd-boot justfile recipe. Receives configuration via environment:
#
#   TARGET            — variant directory name (e.g. dakota, bluefin, bluefin-lts-hwe)
#   OUTPUT_DIR        — directory for the final ISO and intermediate artifacts
#   WORKDIR           — working directory for CS staging and squashfs root
#   DEBUG             — 0 (default) or 1 to enable SSH in the live env
#   INSTALLER_CHANNEL — stable (default) or dev
#   COMPRESSION       — fast (default) or release
#
# All variables have defaults so the script can be run standalone for testing.
# Usage: TARGET=dakota OUTPUT_DIR=output bash scripts/iso-sd-boot.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

TARGET="${TARGET:?TARGET must be set}"
OUTPUT_DIR="${OUTPUT_DIR:-output}"
WORKDIR="${WORKDIR:-${OUTPUT_DIR}}"
DEBUG="${DEBUG:-0}"
INSTALLER_CHANNEL="${INSTALLER_CHANNEL:-stable}"
COMPRESSION="${COMPRESSION:-fast}"

PAYLOAD_IMAGE=$(cat "${TARGET}/payload_ref" | tr -d '[:space:]')

mkdir -p "${OUTPUT_DIR}"
OUTPUT_DIR=$(realpath "${OUTPUT_DIR}")
WORKDIR=$(realpath "${WORKDIR}")

echo "=== Disk space before build ==="
df -h "${OUTPUT_DIR}"

if ! findmnt -n -o FSTYPE -T "${WORKDIR}" 2>/dev/null | grep -qE '^(xfs|btrfs)$'; then
    echo "Hint: ${WORKDIR} is not an XFS/BTRFS mount.  For faster VFS import, run:" >&2
    echo "  sudo just mount-xfs" >&2
    echo "  sudo just workdir=/mnt iso-sd-boot ${TARGET}" >&2
fi

AVAILABLE_KB=$(df --output=avail -B1024 "${OUTPUT_DIR}" | tail -1 | tr -d ' ')
REQUIRED_KB=$((20 * 1024 * 1024))
if [ "${AVAILABLE_KB}" -lt "${REQUIRED_KB}" ]; then
    echo "WARNING: Only $(( AVAILABLE_KB / 1024 / 1024 ))GB free on $(df --output=target "${OUTPUT_DIR}" | tail -1) — ISO output needs ~5GB, full build needs more" >&2
fi

# Build squashfs and boot-files tar using build-live-squashfs.sh (handles container build and OCI store embedding)
bash scripts/build-live-squashfs.sh \
    --target "${TARGET}" \
    --installer-channel "${INSTALLER_CHANNEL}" \
    --oci-image "${PAYLOAD_IMAGE}" \
    --output-dir "${OUTPUT_DIR}" \
    --debug "${DEBUG}" \
    --compression "${COMPRESSION}"

SQUASHFS="${OUTPUT_DIR}/${TARGET}-live.squashfs"
BOOT_TAR="${OUTPUT_DIR}/${TARGET}-boot-files.tar"

echo "=== Disk space after squashfs, before ISO assembly ==="
df -h "${OUTPUT_DIR}"
du -sh "${SQUASHFS}" "${BOOT_TAR}" 2>/dev/null || true

LIVE_TITLE=$(cat "${TARGET}/live_title" 2>/dev/null || echo 'Dakota Live')
TMPDIR="${OUTPUT_DIR}" \
PATH="/usr/sbin:/usr/bin:/home/linuxbrew/.linuxbrew/bin:/home/linuxbrew/.linuxbrew/sbin:${PATH}" \
    bash "live/src/build-iso.sh" \
        --title "${LIVE_TITLE}" \
        "${BOOT_TAR}" "${SQUASHFS}" "${OUTPUT_DIR}/${TARGET}-live.iso"

echo "ISO ready: ${OUTPUT_DIR}/${TARGET}-live.iso"
