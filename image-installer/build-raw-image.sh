#!/usr/bin/bash
# build-raw-image.sh — build a raw bootable medium from a bootc container image.
# Installs with bootc to-disk, packs the live initrd, and configures the ESP.
set -euo pipefail

IMAGE="${1:?usage: build-raw-image.sh <container-image> <output-raw> [filesystem]}"
OUT="${2:?usage: build-raw-image.sh <container-image> <output-raw> [filesystem]}"
FILESYSTEM="${3:-ext4}"
SIZE="${DAKOTA_RAW_SIZE:-20G}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ ${EUID} -eq 0 ]] || { echo "must run as root" >&2; exit 1; }

mkdir -p "$(dirname "${OUT}")"
truncate -s "${SIZE}" "${OUT}"

echo ">>> Installing container image to raw disk via bootc..."
podman run --rm --privileged --pid=host --ipc=host \
    --security-opt label=type:unconfined_t \
    -v /dev:/dev -v /var/lib/containers:/var/lib/containers \
    -v "$(dirname "${OUT}")":/out \
    "${IMAGE}" bootc install to-disk --generic-image \
    --composefs-backend --bootloader systemd --filesystem "${FILESYSTEM}" \
    --via-loopback "/out/$(basename "${OUT}")"

echo ">>> Setting up live initrd and ESP configuration..."
# Connect loop device with partition scanning
LOOPDEV="$(losetup --show -fP "${OUT}")"
cleanup() {
    set +e
    if [[ -n "${MNT_ESP:-}" ]] && mountpoint -q "${MNT_ESP}"; then
        umount "${MNT_ESP}"
        rmdir "${MNT_ESP}"
    fi
    if [[ -n "${LOOPDEV:-}" ]]; then
        losetup -d "${LOOPDEV}"
    fi
}
trap cleanup EXIT

udevadm settle
ESP_PART="${LOOPDEV}p1"
ROOT_PART="${LOOPDEV}p2"

ROOT_UUID="$(blkid -s UUID -o value "${ROOT_PART}")"
[[ -n "${ROOT_UUID}" ]] || { echo "ERROR: could not read root partition UUID" >&2; exit 1; }

# Build the live-only initrd containing setup-root-conf.toml
INITRD_TMP="$(mktemp -d /tmp/dakota-initrd.XXXXXX)"
mkdir -p "${INITRD_TMP}/usr/lib/composefs"
cp "${HERE}/live/setup-root-conf.toml" "${INITRD_TMP}/usr/lib/composefs/setup-root-conf.toml"

MNT_ESP="$(mktemp -d /tmp/dakota-esp.XXXXXX)"
mount "${ESP_PART}" "${MNT_ESP}"

LIVE_INITRD_NAME="dakota-live-initrd.img"
(cd "${INITRD_TMP}" && find . -mindepth 1 -print0 | cpio --null --create --format=newc 2>/dev/null) > "${MNT_ESP}/${LIVE_INITRD_NAME}"
rm -rf "${INITRD_TMP}"

python3 "${HERE}/prepare-medium.py" "${MNT_ESP}" "${ROOT_UUID}" "${LIVE_INITRD_NAME}"
sync

echo "Created and configured experimental raw medium at ${OUT}"
