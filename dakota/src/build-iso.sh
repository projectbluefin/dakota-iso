#!/usr/bin/bash
# build-iso.sh <boot-files-tar> <squashfs-img> <output-iso>
#
# Creates a UEFI-bootable systemd-boot live ISO from pre-built components:
#   <boot-files-tar>  — tar containing only kernel + EFI files from the rootfs
#   <squashfs-img>    — squashfs of the full live rootfs (built with correct UIDs
#                       via mksquashfs inside podman unshare)
#
# Boot architecture (no GRUB2, no shim):
#   El Torito EFI entry → EFI/efi.img (FAT ESP image containing):
#     EFI/BOOT/BOOTX64.EFI or BOOTAA64.EFI  systemd-boot EFI binary (arch-detected)
#     loader/loader.conf        systemd-boot configuration
#     loader/entries/dakota-live.conf   boot entry (kernel + initrd + cmdline)
#     images/pxeboot/vmlinuz    Dakota kernel
#     images/pxeboot/initrd.img dmsquash-live initramfs
#   ISO9660 root:
#     EFI/BOOT/BOOTX64.EFI      EFI fallback path (same binary) for Proxmox OVMF / Ventoy
#     EFI/efi.img               (also referenced by El Torito)
#     images/pxeboot/*          kernel/initramfs copies for loopback ISO boot tools
#     boot/grub/loopback.cfg    metadata for Ventoy/GRUB-style loopback boot
#     LiveOS/squashfs.img       squashfs of the full Dakota live rootfs
#
# Live boot flow:
#   UEFI firmware → El Torito → FAT ESP → systemd-boot → kernel+initramfs
#   dmsquash-live: scans for CDLABEL=DAKOTA_LIVE → mounts ISO → squashfs → overlayfs
#
# Validation: serial console output on ttyS0 should show gdm.service starting.

set -euo pipefail

BOOT_TAR="${1:?Usage: build-iso.sh <boot-files-tar> <squashfs-img> <output-iso>}"
SQUASHFS_SRC="${2:?Usage: build-iso.sh <boot-files-tar> <squashfs-img> <output-iso>}"
OUTPUT_ISO="${3:?Usage: build-iso.sh <boot-files-tar> <squashfs-img> <output-iso>}"
LABEL="DAKOTA_LIVE"

WORK=$(mktemp -d "${TMPDIR:-/tmp}/iso-build.XXXXXX")
# shellcheck disable=SC2064  # WORK is set above; expanding now is intentional
trap "chmod -R u+rwX '${WORK}' 2>/dev/null; rm -rf '${WORK}'" EXIT

BOOT_DIR="${WORK}/boot-files"
ISO_ROOT="${WORK}/iso-root"
ESP_STAGING="${WORK}/esp-staging"

mkdir -p "${BOOT_DIR}" "${ISO_ROOT}/EFI" "${ISO_ROOT}/LiveOS"

# ── Extract boot files (kernel, initramfs, systemd-boot EFI) ─────────────────
echo ">>> Extracting boot files..."
tar -xf "${BOOT_TAR}" -C "${BOOT_DIR}" --no-same-owner

# ── Locate kernel ────────────────────────────────────────────────────────────
kernel=$(ls "${BOOT_DIR}/usr/lib/modules" | sort -V | tail -1)
echo ">>> Kernel: ${kernel}"

VMLINUZ="${BOOT_DIR}/usr/lib/modules/${kernel}/vmlinuz"
INITRD="${BOOT_DIR}/usr/lib/modules/${kernel}/initramfs.img"

# Detect EFI binary: arm64 ships systemd-bootaa64.efi → BOOTAA64.EFI
#                   amd64 ships systemd-bootx64.efi  → BOOTX64.EFI
BOOT_EFI_SRC=""
BOOT_EFI_DEST=""
for _candidate in \
    "systemd-bootaa64.efi:EFI/BOOT/BOOTAA64.EFI" \
    "systemd-bootx64.efi:EFI/BOOT/BOOTX64.EFI"; do
    _src="${BOOT_DIR}/usr/lib/systemd/boot/efi/${_candidate%%:*}"
    _dest="${_candidate##*:}"
    if [[ -f "${_src}" ]]; then
        BOOT_EFI_SRC="${_src}"
        BOOT_EFI_DEST="${_dest}"
        break
    fi
done
[[ -n "${BOOT_EFI_SRC}" ]] || { echo "ERROR: no systemd-boot EFI binary found in boot-files tar"; exit 1; }

for f in "${VMLINUZ}" "${INITRD}" "${BOOT_EFI_SRC}"; do
    [[ -f "${f}" ]] || { echo "ERROR: missing ${f}"; exit 1; }
done
echo ">>> Kernel:   $(du -sh "${VMLINUZ}"  | cut -f1)"
echo ">>> Initramfs: $(du -sh "${INITRD}"   | cut -f1)"
echo ">>> EFI:      ${BOOT_EFI_SRC} → ${BOOT_EFI_DEST}"

# ── Assemble the ESP staging directory ──────────────────────────────────────
# systemd-boot reads loader entries and kernel/initramfs exclusively from the
# FAT volume it was loaded from.  Everything it needs must be in the ESP image.
mkdir -p \
    "${ESP_STAGING}/EFI/BOOT" \
    "${ESP_STAGING}/loader/entries" \
    "${ESP_STAGING}/images/pxeboot"

cp "${BOOT_EFI_SRC}" "${ESP_STAGING}/${BOOT_EFI_DEST}"
cp "${VMLINUZ}" "${ESP_STAGING}/images/pxeboot/vmlinuz"
cp "${INITRD}"  "${ESP_STAGING}/images/pxeboot/initrd.img"

cat > "${ESP_STAGING}/loader/loader.conf" << 'EOF'
timeout 5
default dakota-live.conf
EOF

# Kernel cmdline for dmsquash-live live boot:
#   root=live:CDLABEL=...       dmsquash-live: find the ISO by volume label
#   rd.live.image               enable dmsquash-live mode
#   rd.live.overlay.overlayfs=1 use overlayfs (not device mapper) for the rw layer
#   enforcing=0                 disable SELinux enforcement (GNOME OS ships it)
#   console=ttyS0,115200n8      serial output on amd64 (16550/QEMU q35) — validation target
#   console=ttyAMA0,115200n8    serial output on arm64 (PL011/QEMU virt) — validation target; listed
#                                last so it wins /dev/console on hardware where both UARTs exist
#   Both consoles listed: Linux silently ignores the one that doesn't exist on the running arch.
cat > "${ESP_STAGING}/loader/entries/dakota-live.conf" << EOF
title   Dakota Live
linux   /images/pxeboot/vmlinuz
initrd  /images/pxeboot/initrd.img
options root=live:CDLABEL=${LABEL} rd.live.image rd.live.overlay.overlayfs=1 enforcing=0 quiet console=ttyS0,115200n8 console=ttyAMA0,115200n8
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

mcopy -i "${ESP_IMG}" "${ESP_STAGING}/${BOOT_EFI_DEST}"            ::/"${BOOT_EFI_DEST}"
mcopy -i "${ESP_IMG}" "${ESP_STAGING}/loader/loader.conf"               ::/loader/loader.conf
mcopy -i "${ESP_IMG}" "${ESP_STAGING}/loader/entries/dakota-live.conf"  ::/loader/entries/dakota-live.conf
mcopy -i "${ESP_IMG}" "${ESP_STAGING}/images/pxeboot/vmlinuz"           ::/images/pxeboot/vmlinuz
mcopy -i "${ESP_IMG}" "${ESP_STAGING}/images/pxeboot/initrd.img"        ::/images/pxeboot/initrd.img

# ── EFI fallback path on the ISO9660 root ────────────────────────────────────
# UEFI firmware that does not use El Torito (e.g. Proxmox OVMF, some bare-metal
# boards, Ventoy UEFI chainloading) scans the ISO9660 root for the removable
# media fallback: EFI/BOOT/BOOTX64.EFI (amd64) or EFI/BOOT/BOOTAA64.EFI (arm64).
# Placing the systemd-boot binary here makes the ISO bootable on those platforms
# without touching the El Torito path used by libvirt/QEMU and standard OVMF.
mkdir -p "${ISO_ROOT}/EFI/BOOT"
cp "${BOOT_EFI_SRC}" "${ISO_ROOT}/${BOOT_EFI_DEST}"
echo ">>> EFI fallback: ${BOOT_EFI_DEST} added to ISO root"

# ── ISO-root kernel/initramfs and loopback metadata ──────────────────────────
# Ventoy and GRUB-style loopback boot tools expect kernel/initramfs paths in the
# ISO filesystem itself, not only inside the El Torito ESP image.
mkdir -p "${ISO_ROOT}/images/pxeboot" "${ISO_ROOT}/boot/grub"
cp "${VMLINUZ}" "${ISO_ROOT}/images/pxeboot/vmlinuz"
cp "${INITRD}"  "${ISO_ROOT}/images/pxeboot/initrd.img"
cat > "${ISO_ROOT}/boot/grub/loopback.cfg" << EOF
menuentry "Dakota Live" {
    linux /images/pxeboot/vmlinuz root=live:CDLABEL=${LABEL} rd.live.image rd.live.overlay.overlayfs=1 enforcing=0 quiet console=ttyS0,115200n8 console=ttyAMA0,115200n8 rd.dakota.isofile=\${iso_path}
    initrd /images/pxeboot/initrd.img
}
EOF
echo ">>> Loopback boot metadata added to ISO root"

# ── Place the pre-built squashfs ─────────────────────────────────────────────
echo ">>> Copying squashfs..."
cp "${SQUASHFS_SRC}" "${ISO_ROOT}/LiveOS/squashfs.img"
echo ">>> Squashfs: $(du -sh "${ISO_ROOT}/LiveOS/squashfs.img" | cut -f1)"

# ── Assemble the ISO with xorriso ────────────────────────────────────────────
echo ">>> Assembling ISO..."
# xorriso -as mkisofs mode:
#   -iso-level 3   required for files >2 GiB (squashfs is ~4.5 GiB)
#   -r             Rock Ridge extensions (Linux long filenames / permissions)
#   -J --joliet-long  Joliet extensions (Windows compatibility)
#   --efi-boot EFI/efi.img   El Torito EFI boot entry (platform 0xef)
#   -efi-boot-part           expose the EFI image as a GPT partition
#   --efi-boot-image         finalize the EFI boot partition record
#
# This is the approach used since the repo's first working ISO (commit 7ab0901).
# It produces:
#   - A protective MBR (type 0xEE) so UEFI firmware immediately switches to GPT
#   - A GPT entry covering the ESP image — old firmware (2022 Acer, Dell pre-2023)
#     scans for this and auto-discovers the USB as a bootable EFI device
#   - An El Torito EFI catalog entry for optical/VM/newer-firmware boot
#   - fdisk reports "Disklabel type: gpt" — confirming the protective MBR
#
# Why NOT native mode with part_like_isohybrid / partition_entry=gpt_basdat:
#   That approach creates a hybrid MBR (not protective), so fdisk reports "dos".
#   Old UEFI firmware sees a "dos" disk, skips GPT, finds no EFI entries in the
#   MBR partition table, and does not show the USB in the boot menu.
#   (See issues #15, https://github.com/projectbluefin/dakota-iso/issues/15)
xorriso -as mkisofs \
    -iso-level 3 \
    -r \
    -J --joliet-long \
    -V "${LABEL}" \
    --efi-boot EFI/efi.img \
    -efi-boot-part \
    --efi-boot-image \
    -o "${OUTPUT_ISO}" \
    "${ISO_ROOT}"

implantisomd5 "${OUTPUT_ISO}" 2>/dev/null || true

# ── Verify protective MBR + GPT layout ───────────────────────────────────────
# Expected: "System area summary: MBR protective-msdos-label cyl-align-off GPT"
# fdisk on the ISO should report "Disklabel type: gpt" (not "dos").
# "dos" means a hybrid MBR was created instead of a protective one — old
# firmware will not see the GPT and may not discover the USB as bootable.
echo ">>> Partition layout:"
xorriso -indev "${OUTPUT_ISO}" -report_system_area plain 2>/dev/null | \
    grep -E '^(System area|ISO image size|MBR|GPT|Partition)' || true
if xorriso -indev "${OUTPUT_ISO}" -report_system_area plain 2>/dev/null | \
    grep 'System area summary' | grep -q 'protective'; then
    echo ">>> Protective MBR + GPT: OK"
else
    echo "ERROR: protective MBR not found — ISO will not boot on older UEFI firmware (Acer, Dell pre-2023)." >&2
    echo "       This is a hard build failure. Check the xorriso flags and rebuild." >&2
    echo "       Ref: https://github.com/projectbluefin/dakota-iso/issues/15" >&2
    exit 1
fi

echo ">>> Done: ${OUTPUT_ISO} ($(du -sh "${OUTPUT_ISO}" | cut -f1))"
