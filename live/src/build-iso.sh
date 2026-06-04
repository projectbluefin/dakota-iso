#!/usr/bin/bash
# build-iso.sh [--store <store-squashfs>] <boot-files-tar> <squashfs-img> <output-iso>
# build-iso.sh [--store <store-squashfs>] --arch <arch>:<boot-tar>:<squashfs> [...] <output-iso>
#
# Creates a UEFI-bootable systemd-boot live ISO from pre-built components.
#
# Single-arch mode (backwards compatible):
#   build-iso.sh <boot-files-tar> <squashfs-img> <output-iso>
#
# Multi-arch mode:
#   build-iso.sh --arch x86_64:<boot-tar>:<squashfs> \
#                --arch aarch64:<boot-tar>:<squashfs> \
#                <output-iso>
#
# When multiple --arch flags are provided, the ISO includes per-arch kernels,
# initramfs images, squashfs rootfs images, and both EFI binaries in a single
# "fat ESP" so UEFI firmware on either architecture finds its boot binary.
#
# Options:
#   --store <path>    — optional: squashfs of offline OCI image store; placed at
#                       LiveOS/store.squashfs.img so the live superiso-store.mount
#                       unit can loop-mount it for offline installation
#   --arch <spec>     — arch:boot-tar:squashfs triplet (repeatable)
#
# Boot architecture (no GRUB2, no shim):
#   El Torito EFI entry → EFI/efi.img (FAT ESP image containing):
#     EFI/BOOT/BOOTX64.EFI or BOOTAA64.EFI  systemd-boot EFI binary (arch-detected)
#     loader/loader.conf        systemd-boot configuration
#     loader/entries/*.conf     boot entries (one per arch in multi-arch mode)
#     images/pxeboot/           kernel/initramfs (per-arch subdirs in multi-arch mode)
#   ISO9660 root:
#     EFI/BOOT/BOOTX64.EFI      EFI fallback path (same binary) for Proxmox OVMF / Ventoy
#     EFI/efi.img               (also referenced by El Torito)
#     images/pxeboot/*          kernel/initramfs copies for loopback ISO boot tools
#     boot/grub/loopback.cfg    metadata for Ventoy/GRUB-style loopback boot
#     LiveOS/squashfs.img       squashfs of the full Dakota live rootfs (single-arch)
#     LiveOS/squashfs-<arch>.img  per-arch squashfs (multi-arch)
#     LiveOS/store.squashfs.img offline OCI image store (if --store was given)
#
# Live boot flow:
#   UEFI firmware → El Torito → FAT ESP → systemd-boot → kernel+initramfs
#   dmsquash-live: scans for CDLABEL=DAKOTA_LIVE → mounts ISO → squashfs → overlayfs
#
# Validation: serial console output on ttyS0 should show gdm.service starting.

set -euo pipefail

STORE_SFS=""
ARCH_SPECS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --store) STORE_SFS="${2:?--store requires a path}"; shift 2 ;;
        --arch)  ARCH_SPECS+=("${2:?--arch requires arch:boot-tar:squashfs}"); shift 2 ;;
        *)       break ;;
    esac
done

LABEL="DAKOTA_LIVE"
MULTI_ARCH=false

if [[ ${#ARCH_SPECS[@]} -gt 0 ]]; then
    # Multi-arch mode: remaining arg is output ISO
    MULTI_ARCH=true
    OUTPUT_ISO="${1:?Usage: build-iso.sh --arch <arch>:<boot-tar>:<squashfs> [...] <output-iso>}"
    echo ">>> Multi-arch mode: ${#ARCH_SPECS[@]} architecture(s)"
else
    # Single-arch mode (backwards compatible)
    BOOT_TAR="${1:?Usage: build-iso.sh [--store <store-squashfs>] <boot-files-tar> <squashfs-img> <output-iso>}"
    SQUASHFS_SRC="${2:?Usage: build-iso.sh [--store <store-squashfs>] <boot-files-tar> <squashfs-img> <output-iso>}"
    OUTPUT_ISO="${3:?Usage: build-iso.sh [--store <store-squashfs>] <boot-files-tar> <squashfs-img> <output-iso>}"
fi

WORK=$(mktemp -d "${TMPDIR:-/tmp}/iso-build.XXXXXX")
# shellcheck disable=SC2064  # WORK is set above; expanding now is intentional
trap "chmod -R u+rwX '${WORK}' 2>/dev/null; rm -rf '${WORK}'" EXIT

ISO_ROOT="${WORK}/iso-root"
ESP_STAGING="${WORK}/esp-staging"

mkdir -p "${ISO_ROOT}/EFI" "${ISO_ROOT}/LiveOS"
mkdir -p \
    "${ESP_STAGING}/EFI/BOOT" \
    "${ESP_STAGING}/loader/entries" \
    "${ESP_STAGING}/images/pxeboot"

# Map of arch → serial console device for kernel cmdline
declare -A SERIAL_CONSOLE
SERIAL_CONSOLE[x86_64]="ttyS0"
SERIAL_CONSOLE[aarch64]="ttyAMA0"

# EFI binary filename per arch (UEFI spec well-known paths)
declare -A EFI_BINARY_NAME
EFI_BINARY_NAME[x86_64]="BOOTX64.EFI"
EFI_BINARY_NAME[aarch64]="BOOTAA64.EFI"

# systemd-boot source binary name per arch
declare -A SYSTEMD_BOOT_SRC
SYSTEMD_BOOT_SRC[x86_64]="systemd-bootx64.efi"
SYSTEMD_BOOT_SRC[aarch64]="systemd-bootaa64.efi"

# ── Helper: process one architecture's boot files ───────────────────────────
# Sets: VMLINUZ, INITRD, BOOT_EFI_SRC, BOOT_EFI_DEST for the given arch
process_arch_boot_files() {
    local arch="$1"
    local boot_tar="$2"
    local boot_dir="${WORK}/boot-files-${arch}"

    mkdir -p "${boot_dir}"
    echo ">>> [${arch}] Extracting boot files..."
    tar -xf "${boot_tar}" -C "${boot_dir}" --no-same-owner

    local kernel
    kernel=$(ls "${boot_dir}/usr/lib/modules" | sort -V | tail -1)
    echo ">>> [${arch}] Kernel: ${kernel}"

    VMLINUZ="${boot_dir}/usr/lib/modules/${kernel}/vmlinuz"
    INITRD="${boot_dir}/usr/lib/modules/${kernel}/initramfs.img"

    # Detect EFI binary
    local efi_src_name="${SYSTEMD_BOOT_SRC[${arch}]}"
    local efi_dest_name="${EFI_BINARY_NAME[${arch}]}"
    BOOT_EFI_SRC="${boot_dir}/usr/lib/systemd/boot/efi/${efi_src_name}"
    BOOT_EFI_DEST="EFI/BOOT/${efi_dest_name}"

    if [[ ! -f "${BOOT_EFI_SRC}" ]]; then
        # Fallback: try all candidates (handles cross-arch boot tar naming)
        BOOT_EFI_SRC=""
        for _candidate in \
            "systemd-bootaa64.efi:EFI/BOOT/BOOTAA64.EFI" \
            "systemd-bootx64.efi:EFI/BOOT/BOOTX64.EFI"; do
            local _src="${boot_dir}/usr/lib/systemd/boot/efi/${_candidate%%:*}"
            local _dest="${_candidate##*:}"
            if [[ -f "${_src}" ]]; then
                BOOT_EFI_SRC="${_src}"
                BOOT_EFI_DEST="${_dest}"
                break
            fi
        done
    fi

    [[ -n "${BOOT_EFI_SRC}" ]] || { echo "ERROR: [${arch}] no systemd-boot EFI binary found"; exit 1; }
    for f in "${VMLINUZ}" "${INITRD}" "${BOOT_EFI_SRC}"; do
        [[ -f "${f}" ]] || { echo "ERROR: [${arch}] missing ${f}"; exit 1; }
    done

    echo ">>> [${arch}] Kernel:    $(du -sh "${VMLINUZ}" | cut -f1)"
    echo ">>> [${arch}] Initramfs: $(du -sh "${INITRD}" | cut -f1)"
    echo ">>> [${arch}] EFI:       ${BOOT_EFI_SRC} → ${BOOT_EFI_DEST}"
}

# Track total ESP size across architectures
ESP_TOTAL_MB=36  # base headroom for loader config and FAT metadata

if [[ "${MULTI_ARCH}" == "true" ]]; then
    # ── Multi-arch mode ─────────────────────────────────────────────────────
    FIRST_ARCH=""
    for spec in "${ARCH_SPECS[@]}"; do
        IFS=':' read -r arch boot_tar squashfs_src <<< "${spec}"
        [[ -n "${arch}" && -n "${boot_tar}" && -n "${squashfs_src}" ]] || \
            { echo "ERROR: --arch requires arch:boot-tar:squashfs (got: ${spec})"; exit 1; }

        [[ -z "${FIRST_ARCH}" ]] && FIRST_ARCH="${arch}"

        process_arch_boot_files "${arch}" "${boot_tar}"

        # Per-arch kernel/initrd in ESP
        mkdir -p "${ESP_STAGING}/images/pxeboot/${arch}"
        cp "${VMLINUZ}" "${ESP_STAGING}/images/pxeboot/${arch}/vmlinuz"
        cp "${INITRD}"  "${ESP_STAGING}/images/pxeboot/${arch}/initrd.img"

        # EFI binary — both can coexist in EFI/BOOT/
        cp "${BOOT_EFI_SRC}" "${ESP_STAGING}/${BOOT_EFI_DEST}"

        # Per-arch BLS entry with rd.live.squashimg pointing to the correct squashfs
        local_console="${SERIAL_CONSOLE[${arch}]:-ttyS0}"
        cat > "${ESP_STAGING}/loader/entries/dakota-live-${arch}.conf" << EOF
title   Dakota Live (${arch})
linux   /images/pxeboot/${arch}/vmlinuz
initrd  /images/pxeboot/${arch}/initrd.img
options root=live:CDLABEL=${LABEL} rd.live.image rd.live.overlay.overlayfs=1 rd.live.squashimg=squashfs-${arch}.img enforcing=0 quiet console=${local_console},115200n8
EOF

        # Per-arch squashfs
        echo ">>> [${arch}] Copying squashfs..."
        cp "${squashfs_src}" "${ISO_ROOT}/LiveOS/squashfs-${arch}.img"
        echo ">>> [${arch}] Squashfs: $(du -sh "${ISO_ROOT}/LiveOS/squashfs-${arch}.img" | cut -f1)"

        # ISO-root fallback boot files (per-arch subdirs)
        mkdir -p "${ISO_ROOT}/EFI/BOOT" "${ISO_ROOT}/images/pxeboot/${arch}"
        cp "${BOOT_EFI_SRC}" "${ISO_ROOT}/${BOOT_EFI_DEST}"
        cp "${VMLINUZ}" "${ISO_ROOT}/images/pxeboot/${arch}/vmlinuz"
        cp "${INITRD}"  "${ISO_ROOT}/images/pxeboot/${arch}/initrd.img"

        # Accumulate ESP size
        initrd_mb=$(du -m "${INITRD}" | cut -f1)
        vmlinuz_mb=$(du -m "${VMLINUZ}" | cut -f1)
        ESP_TOTAL_MB=$(( ESP_TOTAL_MB + initrd_mb + vmlinuz_mb + 1 ))
    done

    # loader.conf defaults to the first architecture listed
    cat > "${ESP_STAGING}/loader/loader.conf" << EOF
timeout 5
default dakota-live-${FIRST_ARCH}.conf
EOF

    # Loopback config for Ventoy — one entry per arch
    mkdir -p "${ISO_ROOT}/boot/grub"
    {
        for spec in "${ARCH_SPECS[@]}"; do
            IFS=':' read -r arch _bt _sf <<< "${spec}"
            local_console="${SERIAL_CONSOLE[${arch}]:-ttyS0}"
            cat << EOF
menuentry "Dakota Live (${arch})" {
    linux /images/pxeboot/${arch}/vmlinuz root=live:CDLABEL=${LABEL} rd.live.image rd.live.overlay.overlayfs=1 rd.live.squashimg=squashfs-${arch}.img enforcing=0 quiet console=${local_console},115200n8 rd.dakota.isofile=\${iso_path}
    initrd /images/pxeboot/${arch}/initrd.img
}
EOF
        done
    } > "${ISO_ROOT}/boot/grub/loopback.cfg"

else
    # ── Single-arch mode (backwards compatible) ─────────────────────────────
    BOOT_DIR="${WORK}/boot-files"
    mkdir -p "${BOOT_DIR}"
    echo ">>> Extracting boot files..."
    tar -xf "${BOOT_TAR}" -C "${BOOT_DIR}" --no-same-owner

    kernel=$(ls "${BOOT_DIR}/usr/lib/modules" | sort -V | tail -1)
    echo ">>> Kernel: ${kernel}"

    VMLINUZ="${BOOT_DIR}/usr/lib/modules/${kernel}/vmlinuz"
    INITRD="${BOOT_DIR}/usr/lib/modules/${kernel}/initramfs.img"

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

    # EFI fallback path on the ISO9660 root
    mkdir -p "${ISO_ROOT}/EFI/BOOT"
    cp "${BOOT_EFI_SRC}" "${ISO_ROOT}/${BOOT_EFI_DEST}"
    echo ">>> EFI fallback: ${BOOT_EFI_DEST} added to ISO root"

    # ISO-root kernel/initramfs and loopback metadata
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

    echo ">>> Copying squashfs..."
    cp "${SQUASHFS_SRC}" "${ISO_ROOT}/LiveOS/squashfs.img"
    echo ">>> Squashfs: $(du -sh "${ISO_ROOT}/LiveOS/squashfs.img" | cut -f1)"

    INITRD_MB=$(du -m "${INITRD}"  | cut -f1)
    VMLINUZ_MB=$(du -m "${VMLINUZ}" | cut -f1)
    ESP_TOTAL_MB=$(( INITRD_MB + VMLINUZ_MB + 4 + 32 ))
fi

# ── Optional offline image store ─────────────────────────────────────────────
if [[ -n "${STORE_SFS}" ]]; then
    cp "${STORE_SFS}" "${ISO_ROOT}/LiveOS/store.squashfs.img"
    echo ">>> Offline store: $(du -sh "${ISO_ROOT}/LiveOS/store.squashfs.img" | cut -f1)"
fi

# ── Create the FAT ESP image ────────────────────────────────────────────────
ESP_IMG="${ISO_ROOT}/EFI/efi.img"

echo ">>> Creating ${ESP_TOTAL_MB} MiB FAT ESP image..."
truncate -s "${ESP_TOTAL_MB}M" "${ESP_IMG}"
mkfs.fat -F 32 -n "ESP" "${ESP_IMG}"

# Populate the FAT image using mtools — no loop mount required, works
# in unprivileged/restricted containers.
# MTOOLS_SKIP_CHECK=1 suppresses geometry-mismatch warnings on raw images.
export MTOOLS_SKIP_CHECK=1

# Create directory structure in the FAT image.
# mmd fails silently if a directory already exists, so create the deepest
# paths first and let parent creation be implicit where mtools supports it.
mmd -i "${ESP_IMG}" ::/EFI ::/EFI/BOOT ::/loader ::/loader/entries ::/images ::/images/pxeboot

if [[ "${MULTI_ARCH}" == "true" ]]; then
    for spec in "${ARCH_SPECS[@]}"; do
        IFS=':' read -r arch _bt _sf <<< "${spec}"
        mmd -i "${ESP_IMG}" "::/images/pxeboot/${arch}"
        mcopy -i "${ESP_IMG}" "${ESP_STAGING}/images/pxeboot/${arch}/vmlinuz" "::/images/pxeboot/${arch}/vmlinuz"
        mcopy -i "${ESP_IMG}" "${ESP_STAGING}/images/pxeboot/${arch}/initrd.img" "::/images/pxeboot/${arch}/initrd.img"
        mcopy -i "${ESP_IMG}" "${ESP_STAGING}/loader/entries/dakota-live-${arch}.conf" "::/loader/entries/dakota-live-${arch}.conf"

        local_efi_dest="EFI/BOOT/${EFI_BINARY_NAME[${arch}]}"
        mcopy -i "${ESP_IMG}" "${ESP_STAGING}/${local_efi_dest}" "::/${local_efi_dest}"
    done
else
    mcopy -i "${ESP_IMG}" "${ESP_STAGING}/${BOOT_EFI_DEST}" "::/${BOOT_EFI_DEST}"
    mcopy -i "${ESP_IMG}" "${ESP_STAGING}/images/pxeboot/vmlinuz" "::/images/pxeboot/vmlinuz"
    mcopy -i "${ESP_IMG}" "${ESP_STAGING}/images/pxeboot/initrd.img" "::/images/pxeboot/initrd.img"
    mcopy -i "${ESP_IMG}" "${ESP_STAGING}/loader/entries/dakota-live.conf" "::/loader/entries/dakota-live.conf"
fi
mcopy -i "${ESP_IMG}" "${ESP_STAGING}/loader/loader.conf" "::/loader/loader.conf"

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
xorriso -indev "${OUTPUT_ISO}" -report_system_area plain 2>/dev/null | \
    grep 'System area summary' | grep -q 'protective' && \
    echo ">>> Protective MBR + GPT: OK" || \
    echo ">>> WARNING: protective MBR not found — USB may not boot on older firmware"

echo ">>> Done: ${OUTPUT_ISO} ($(du -sh "${OUTPUT_ISO}" | cut -f1))"
