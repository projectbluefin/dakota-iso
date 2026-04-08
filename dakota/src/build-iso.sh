#!/usr/bin/bash
# build-iso.sh <rootfs-tar> <output-iso>
#
# Creates a UEFI-bootable systemd-boot live ISO from a clean Dakota rootfs
# tarball (produced by `podman export`).
#
# Boot architecture (no GRUB2, no shim):
#   El Torito EFI entry → EFI/efi.img (FAT ESP image containing):
#     EFI/BOOT/BOOTX64.EFI     systemd-bootx64.efi from Dakota
#     loader/loader.conf        systemd-boot configuration
#     loader/entries/dakota-live.conf   boot entry (kernel + initrd + cmdline)
#     images/pxeboot/vmlinuz    Dakota kernel
#     images/pxeboot/initrd.img dmsquash-live initramfs
#   ISO9660 root:
#     EFI/efi.img               (also referenced by El Torito)
#     LiveOS/squashfs.img       squashfs of the full Dakota live rootfs
#
# Live boot flow:
#   UEFI firmware → El Torito → FAT ESP → systemd-boot → kernel+initramfs
#   dmsquash-live: scans for CDLABEL=DAKOTA_LIVE → mounts ISO → squashfs → overlayfs
#
# Validation: serial console output on ttyS0 should show gdm.service starting.

set -euo pipefail

ROOTFS_TAR="${1:?Usage: build-iso.sh <rootfs-tar> <output-iso>}"
OUTPUT_ISO="${2:?Usage: build-iso.sh <rootfs-tar> <output-iso>}"
LABEL="DAKOTA_LIVE"

WORK=$(mktemp -d /tmp/iso-build.XXXXXX)
trap "rm -rf ${WORK}" EXIT

ROOTFS="${WORK}/rootfs"
ISO_ROOT="${WORK}/iso-root"
ESP_STAGING="${WORK}/esp-staging"

mkdir -p "${ROOTFS}" "${ISO_ROOT}/EFI" "${ISO_ROOT}/LiveOS"

# ── Extract the clean Dakota rootfs ─────────────────────────────────────────
echo ">>> Extracting rootfs..."
tar -xf "${ROOTFS_TAR}" -C "${ROOTFS}" \
    --exclude ./proc \
    --exclude ./sys \
    --exclude ./dev \
    --exclude ./run \
    --exclude ./tmp \
    --exclude ./etc/hosts \
    --exclude ./etc/resolv.conf \
    --exclude ./etc/hostname

# Reset container-injected network files to live-appropriate defaults
printf '127.0.0.1\tlocalhost\n::1\t\tlocalhost\n' > "${ROOTFS}/etc/hosts"
echo ""          > "${ROOTFS}/etc/resolv.conf"
echo "dakota-live" > "${ROOTFS}/etc/hostname"

# ── Locate kernel ────────────────────────────────────────────────────────────
kernel=$(ls "${ROOTFS}/usr/lib/modules" | sort -V | tail -1)
echo ">>> Kernel: ${kernel}"

VMLINUZ="${ROOTFS}/usr/lib/modules/${kernel}/vmlinuz"
INITRD="${ROOTFS}/usr/lib/modules/${kernel}/initramfs.img"
BOOTX64="${ROOTFS}/usr/lib/systemd/boot/efi/systemd-bootx64.efi"

for f in "${VMLINUZ}" "${INITRD}" "${BOOTX64}"; do
    [[ -f "${f}" ]] || { echo "ERROR: missing ${f}"; exit 1; }
done
echo ">>> Kernel:   $(du -sh "${VMLINUZ}"  | cut -f1)"
echo ">>> Initramfs: $(du -sh "${INITRD}"   | cut -f1)"

# ── Assemble the ESP staging directory ──────────────────────────────────────
# systemd-boot reads loader entries and kernel/initramfs exclusively from the
# FAT volume it was loaded from.  Everything it needs must be in the ESP image.
mkdir -p \
    "${ESP_STAGING}/EFI/BOOT" \
    "${ESP_STAGING}/loader/entries" \
    "${ESP_STAGING}/images/pxeboot"

cp "${BOOTX64}" "${ESP_STAGING}/EFI/BOOT/BOOTX64.EFI"
cp "${VMLINUZ}" "${ESP_STAGING}/images/pxeboot/vmlinuz"
cp "${INITRD}"  "${ESP_STAGING}/images/pxeboot/initrd.img"

cat > "${ESP_STAGING}/loader/loader.conf" << 'EOF'
timeout 5
default @saved
EOF

# Kernel cmdline for dmsquash-live live boot:
#   root=live:CDLABEL=...       dmsquash-live: find the ISO by volume label
#   rd.live.image               enable dmsquash-live mode
#   rd.live.overlay.overlayfs=1 use overlayfs (not device mapper) for the rw layer
#   enforcing=0                 disable SELinux enforcement (GNOME OS ships it)
#   console=ttyS0,115200n8      serial output — validation target
cat > "${ESP_STAGING}/loader/entries/dakota-live.conf" << EOF
title   Dakota Live
linux   /images/pxeboot/vmlinuz
initrd  /images/pxeboot/initrd.img
options root=live:CDLABEL=${LABEL} rd.live.image rd.live.overlay.overlayfs=1 enforcing=0 quiet console=ttyS0,115200n8
EOF

# ── Create the FAT ESP image ──────────────────────────────────────────────────
# Size = kernel + initramfs + EFI binary + loader files + 32 MiB headroom
INITRD_MB=$(du -m "${INITRD}"  | cut -f1)
VMLINUZ_MB=$(du -m "${VMLINUZ}" | cut -f1)
ESP_MB=$(( INITRD_MB + VMLINUZ_MB + 4 + 32 ))
ESP_IMG="${ISO_ROOT}/EFI/efi.img"

echo ">>> Creating ${ESP_MB} MiB FAT ESP image..."
truncate -s "${ESP_MB}M" "${ESP_IMG}"
mkfs.fat -F 32 -n "ESP" "${ESP_IMG}"

# Populate the FAT image using mtools — no loop mount required, works
# in unprivileged/restricted containers.
# MTOOLS_SKIP_CHECK=1 suppresses geometry-mismatch warnings on raw images.
export MTOOLS_SKIP_CHECK=1

mmd -i "${ESP_IMG}" \
    ::/EFI \
    ::/EFI/BOOT \
    ::/loader \
    ::/loader/entries \
    ::/images \
    ::/images/pxeboot

mcopy -i "${ESP_IMG}" "${ESP_STAGING}/EFI/BOOT/BOOTX64.EFI"            ::/EFI/BOOT/BOOTX64.EFI
mcopy -i "${ESP_IMG}" "${ESP_STAGING}/loader/loader.conf"               ::/loader/loader.conf
mcopy -i "${ESP_IMG}" "${ESP_STAGING}/loader/entries/dakota-live.conf"  ::/loader/entries/dakota-live.conf
mcopy -i "${ESP_IMG}" "${ESP_STAGING}/images/pxeboot/vmlinuz"           ::/images/pxeboot/vmlinuz
mcopy -i "${ESP_IMG}" "${ESP_STAGING}/images/pxeboot/initrd.img"        ::/images/pxeboot/initrd.img

# ── Squashfs of the full live rootfs ─────────────────────────────────────────
echo ">>> Creating squashfs (this may take several minutes)..."
mksquashfs "${ROOTFS}" "${ISO_ROOT}/LiveOS/squashfs.img" \
    -noappend \
    -comp zstd \
    -Xcompression-level 3

echo ">>> Squashfs: $(du -sh "${ISO_ROOT}/LiveOS/squashfs.img" | cut -f1)"

# ── Assemble the ISO with xorriso ────────────────────────────────────────────
# El Torito EFI entry points to EFI/efi.img (a FAT filesystem image, required).
# -efi-boot-part --efi-boot-image also exposes the FAT image as a GPT ESP
# partition, making the ISO hybrid-bootable when written to USB with dd.
echo ">>> Assembling ISO..."
xorriso -as mkisofs \
    -iso-level 3 \
    -r \
    -V "${LABEL}" \
    -eltorito-alt-boot \
    -e EFI/efi.img \
    -no-emul-boot \
    -efi-boot-part \
    --efi-boot-image \
    -o "${OUTPUT_ISO}" \
    "${ISO_ROOT}"

implantisomd5 "${OUTPUT_ISO}" 2>/dev/null || true

echo ">>> Done: ${OUTPUT_ISO} ($(du -sh "${OUTPUT_ISO}" | cut -f1))"
