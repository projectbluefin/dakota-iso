#!/usr/bin/bash
# build-live-squashfs.sh <image> <output-squashfs> <output-boot-tar>
#
# Exports a container image as a squashfs suitable for dmsquash-live boot,
# and a companion tar of the boot files (kernel, initramfs, EFI binary) needed
# to assemble the ISO ESP.
#
# The squashfs contains the full live rootfs.  OCI images for offline
# installation are NOT embedded here — they live in a separate store squashfs
# (see build-offline-store.sh) mounted at /var/lib/superiso-store.
#
# This is the plain-bash equivalent of tacklebox's runEnv() + squashfs stage.
#
# Usage (must run as root or with sudo):
#   sudo bash scripts/build-live-squashfs.sh \
#       localhost/dakota-nvidia-live:latest \
#       /out/dakota-nvidia.rootfs.sfs \
#       /out/dakota-nvidia-boot.tar

set -euo pipefail

IMAGE="${1:?Usage: build-live-squashfs.sh <image> <output-squashfs> <output-boot-tar>}"
OUTPUT_SFS="${2:?}"
OUTPUT_BOOT_TAR="${3:?}"

if [[ $(id -u) -ne 0 ]]; then
    echo "ERROR: must run as root (use sudo)" >&2
    exit 1
fi

WORK="$(mktemp -d /var/tmp/tbox-live-sfs.XXXXXX)"
trap 'podman image unmount "${IMAGE}" 2>/dev/null || true
      umount "${WORK}/squashfs-root/var/lib/containers/storage" 2>/dev/null || true
      umount "${WORK}/squashfs-root" 2>/dev/null || true
      chmod -R u+rwX "${WORK}" 2>/dev/null || true
      rm -rf "${WORK}"' EXIT

SFS_ROOT="${WORK}/squashfs-root"
UPPER="${WORK}/overlay-upper"
WDIR="${WORK}/overlay-work"
mkdir -p "${SFS_ROOT}" "${UPPER}" "${WDIR}"

echo ">>> [live-squashfs] mounting image ${IMAGE} ..."
MOUNT="$(podman image mount "${IMAGE}")"

echo ">>> [live-squashfs] building unified squashfs source tree ..."
FS_TYPE="$(findmnt -n -o FSTYPE -T "${SFS_ROOT}" 2>/dev/null || echo unknown)"
if [[ "${FS_TYPE}" == "xfs" || "${FS_TYPE}" == "ext4" ]]; then
    if ! mount -t overlay overlay \
        -o lowerdir="${MOUNT}",upperdir="${UPPER}",workdir="${WDIR}" \
        "${SFS_ROOT}" 2>/dev/null; then
        echo ">>> overlay mount failed on ${FS_TYPE}, falling back to cp"
        cp -a "${MOUNT}/." "${SFS_ROOT}/"
    fi
else
    cp -a "${MOUNT}/." "${SFS_ROOT}/"
fi

SFS_LEVEL=3; SFS_BLOCK=131072
[[ "${SUPERISO_COMPRESSION:-}" == "release" ]] && { SFS_LEVEL=15; SFS_BLOCK=1048576; }

echo ">>> [live-squashfs] mksquashfs -> ${OUTPUT_SFS} (zstd-${SFS_LEVEL}) ..."
mkdir -p "$(dirname "${OUTPUT_SFS}")"

mksquashfs "${SFS_ROOT}" "${OUTPUT_SFS}" \
    -noappend -comp zstd \
    -Xcompression-level "${SFS_LEVEL}" \
    -b "${SFS_BLOCK}" \
    -processors 4 \
    -e proc -e sys -e dev -e run -e tmp
echo ">>> [live-squashfs] squashfs: $(du -sh "${OUTPUT_SFS}" | cut -f1)"

echo ">>> [live-squashfs] exporting boot files tar ..."
mkdir -p "$(dirname "${OUTPUT_BOOT_TAR}")"
tar -C "${MOUNT}" \
    -cf "${OUTPUT_BOOT_TAR}" \
    ./usr/lib/modules \
    ./usr/lib/systemd/boot/efi
echo ">>> [live-squashfs] boot tar: $(du -sh "${OUTPUT_BOOT_TAR}" | cut -f1)"

podman image unmount "${IMAGE}" 2>/dev/null || true
echo ">>> [live-squashfs] done"
