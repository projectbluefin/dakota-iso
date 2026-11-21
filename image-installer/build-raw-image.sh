#!/usr/bin/bash
# build-raw-image.sh — build a raw bootable medium from a bootc container image.
# Installs with bootc to-disk, packs the live initrd, and configures the ESP.
set -euo pipefail

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

[[ ${EUID} -eq 0 ]] || die "must run as root"
[[ $# -eq 3 ]] || die "usage: build-raw-image.sh <container-image> <output-raw> <official-update-ref>"
IMAGE="$1"
OUT="$2"
UPDATE_REF="$3"
SIZE="${DAKOTA_RAW_SIZE:-20G}"
[[ -n "${IMAGE}" && -n "${OUT}" && "${OUT}" != */ ]] || die "provide an image and an output file path"
[[ "${UPDATE_REF}" =~ ^ghcr\.io/projectbluefin/dakota(-nvidia)?:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]] ||
    die "update ref must be a tagged official Dakota image (ghcr.io/projectbluefin/dakota or dakota-nvidia)"

for tool in blkid cpio cp dirname find ln losetup lsblk mkdir mktemp mount mountpoint podman python3 rm rmdir sync truncate udevadm umount; do
    command -v "${tool}" >/dev/null || die "required host command not found: ${tool}"
done

HERE="$(CDPATH='' cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="$(dirname -- "${OUT}")"
mkdir -p -- "${OUT_DIR}"
OUT_DIR="$(CDPATH='' cd -P -- "${OUT_DIR}" && pwd)"
OUT="${OUT_DIR}/${OUT##*/}"
[[ ! -e "${OUT}" && ! -L "${OUT}" ]] || die "output already exists: ${OUT}"

close_image() {
    if [[ -n "${MNT_ESP:-}" && -d "${MNT_ESP}" ]]; then
        if mountpoint -q -- "${MNT_ESP}"; then
            umount -- "${MNT_ESP}" || return 1
        fi
        rmdir -- "${MNT_ESP}" || return 1
    fi
    if [[ -n "${LOOPDEV:-}" ]]; then
        losetup --detach "${LOOPDEV}" || return 1
        LOOPDEV=""
    fi
}

cleanup() {
    close_image && { [[ -z "${WORK:-}" ]] || rm -rf -- "${WORK}"; }
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

WORK="$(mktemp -d -- "${OUT_DIR}/.dakota-raw.XXXXXXXX")"
RAW="${WORK}/image.raw"
truncate -s "${SIZE}" -- "${RAW}"

IMAGE_ID="$(podman image inspect --format '{{.Id}}' -- "${IMAGE}")"
podman push --format oci -- "${IMAGE_ID}" "oci:${WORK}/source"

echo ">>> Installing container image to raw disk via bootc..."
podman run --rm --privileged --pull=never --pid=host --ipc=host \
    --security-opt label=type:unconfined_t --env BOOTC_DIRECT_IO \
    -v /dev:/dev -v /var/lib/containers:/var/lib/containers \
    -v "${WORK}:/out" \
    "${IMAGE_ID}" bootc install to-disk --generic-image \
    --composefs-backend --bootloader systemd --block-setup direct --filesystem ext4 \
    --source-imgref oci:/out/source --target-transport registry --target-imgref "${UPDATE_REF}" \
    --via-loopback /out/image.raw

echo ">>> Setting up live initrd and ESP configuration..."
LOOPDEV="$(losetup --show --find --partscan -- "${RAW}")"
udevadm settle
PARTITIONS="$(lsblk -rnpo NAME,TYPE,PARTTYPE,FSTYPE -- "${LOOPDEV}")"
ESP_PART="" ROOT_PART=""
ESP_TYPE=c12a7328-f81f-11d2-ba4b-00a0c93ec93b
while read -r part kind part_type fs; do
    [[ "${kind}" == part ]] || continue
    if [[ "${part_type,,}" == "${ESP_TYPE}" ]]; then
        [[ -z "${ESP_PART}" && "${fs}" == vfat ]] || die "expected exactly one vfat EFI System Partition"
        ESP_PART="${part}"
    elif [[ "${fs}" == ext4 ]]; then
        [[ -z "${ROOT_PART}" ]] || die "found more than one ext4 root candidate"
        ROOT_PART="${part}"
    fi
done <<< "${PARTITIONS}"
[[ -n "${ESP_PART}" && -n "${ROOT_PART}" ]] || die "disk image missing a vfat ESP or ext4 root partition"
ROOT_UUID="$(blkid -p -s UUID -o value -- "${ROOT_PART}")" && [[ -n "${ROOT_UUID}" ]] ||
    die "could not read root partition UUID"

# Build the live-only initrd containing setup-root-conf.toml
INITRD_TMP="${WORK}/initrd"
MNT_ESP="${WORK}/esp"
mkdir -p -- "${INITRD_TMP}/usr/lib/composefs" "${MNT_ESP}"
cp -- "${HERE}/live/setup-root-conf.toml" "${INITRD_TMP}/usr/lib/composefs/setup-root-conf.toml"
mount -t vfat -o nosuid,nodev,noexec -- "${ESP_PART}" "${MNT_ESP}"

LIVE_INITRD_NAME="dakota-live-initrd.img"
(cd "${INITRD_TMP}" && find . -mindepth 1 -print0 | cpio --null --create --format=newc --quiet) > "${MNT_ESP}/${LIVE_INITRD_NAME}"

python3 "${HERE}/prepare-medium.py" "${MNT_ESP}" "${ROOT_UUID}" "${LIVE_INITRD_NAME}"
close_image
sync -- "${RAW}"
# A concurrent writer may have created OUT since the initial check.
ln -T -- "${RAW}" "${OUT}"

echo "Created and configured experimental raw medium at ${OUT}"
