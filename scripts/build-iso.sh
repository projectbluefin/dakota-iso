#!/usr/bin/bash
# scripts/build-iso.sh — named-arg interface wrapper for live/src/build-iso.sh
#
# Translates the named-flag interface used by test-plain-install.yml:
#   --squashfs <path>   path to the rootfs squashfs
#   --boot-tar  <path>  path to the boot files tar (kernel/initramfs/EFI)
#   --output    <path>  destination ISO path
#
# into the positional interface of live/src/build-iso.sh:
#   live/src/build-iso.sh <boot-tar> <squashfs> <output-iso>

set -euo pipefail

SQUASHFS=""
BOOT_TAR=""
OUTPUT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --squashfs) SQUASHFS="${2:?--squashfs requires a path}"; shift 2 ;;
        --boot-tar) BOOT_TAR="${2:?--boot-tar requires a path}"; shift 2 ;;
        --output)   OUTPUT="${2:?--output requires a path}"; shift 2 ;;
        *) echo "ERROR: unknown argument: $1" >&2; exit 1 ;;
    esac
done

[[ -z "$SQUASHFS" || -z "$BOOT_TAR" || -z "$OUTPUT" ]] && {
    echo "Usage: scripts/build-iso.sh --squashfs <sfs> --boot-tar <tar> --output <iso>" >&2
    exit 1
}

exec bash "$(dirname "$0")/../live/src/build-iso.sh" \
    "$BOOT_TAR" "$SQUASHFS" "$OUTPUT"
