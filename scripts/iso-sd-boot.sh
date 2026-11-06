#!/usr/bin/bash
# scripts/iso-sd-boot.sh — Build a systemd-boot UEFI live ISO for a given target variant.
#
# Called by the iso-sd-boot justfile recipe. Receives configuration via environment:
#
#   TARGET            — variant directory name (e.g. dakota, stable, lts)
#   OUTPUT_DIR        — directory for the final ISO and intermediate artifacts
#   WORKDIR           — working directory for the squashfs root
#   DEBUG             — 0 (default) or 1 to enable SSH in the live env
#   INSTALLER_CHANNEL — stable (default) or dev
#   COMPRESSION       — fast (default) or release
#
# All variables have defaults so the script can be run standalone for testing.
# Usage: TARGET=dakota OUTPUT_DIR=output bash scripts/iso-sd-boot.sh
set -euo pipefail
# Single host-side reader for per-variant config (see scripts/variant-config.sh).
# shellcheck source=scripts/variant-config.sh
source "$(dirname "${BASH_SOURCE[0]}")/variant-config.sh"
# shellcheck source=scripts/payload-oci.sh
source "$(dirname "${BASH_SOURCE[0]}")/payload-oci.sh"

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
if [[ -z "${PAYLOAD_IMAGE}" ]]; then
    echo "ERROR: ${TARGET}/payload_ref is empty — expected an image reference" >&2
    exit 1
fi

# The payload ships verbatim to installed systems.  Like `just container` does
# for the live base and `_payload_ref_flag` for image-builder, verify the
# project's keyless cosign signature and pull exactly the verified digest, so
# the offline store below is built from an image project CI produced — not
# from whatever happens to sit under a mutable tag in the local podman store.
# Non-project refs (e.g. localhost/... for custom builds) are left to the
# caller: they must already be present locally (--pull-never).
PAYLOAD_SOURCE="${PAYLOAD_IMAGE}"
case "${PAYLOAD_IMAGE}" in
    ghcr.io/projectbluefin/*)
        PAYLOAD_SOURCE=$("${SCRIPT_DIR}/verify-image-signature.sh" "${PAYLOAD_IMAGE}")
        if [[ -z "${PAYLOAD_SOURCE}" ]]; then
            echo "ERROR: signature verification returned no pinned reference for ${PAYLOAD_IMAGE}" >&2
            exit 1
        fi
        ;;
esac

mkdir -p "${OUTPUT_DIR}"
OUTPUT_DIR=$(realpath "${OUTPUT_DIR}")
WORKDIR=$(realpath "${WORKDIR}")

echo "=== Disk space before container build ==="
df -h "${OUTPUT_DIR}"


AVAILABLE_KB=$(df --output=avail -B1024 "${OUTPUT_DIR}" | tail -1 | tr -d ' ')
REQUIRED_KB=$((20 * 1024 * 1024))
if [ "${AVAILABLE_KB}" -lt "${REQUIRED_KB}" ]; then
    echo "WARNING: Only $(( AVAILABLE_KB / 1024 / 1024 ))GB free on $(df --output=target "${OUTPUT_DIR}" | tail -1) — ISO output needs ~5GB, full build needs more" >&2
fi
podman images --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}" 2>/dev/null || true

just debug="${DEBUG}" installer_channel="${INSTALLER_CHANNEL}" container "${TARGET}"

echo "=== Disk space after container build ==="
df -h "${OUTPUT_DIR}"
podman images --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}" 2>/dev/null || true

podman rmi debian:sid 2>/dev/null || true
podman image prune -f 2>/dev/null || true
echo "=== Disk space after intermediate cleanup ==="
df -h "${OUTPUT_DIR}"

# podman unshare enters the user namespace for rootless builds.
# When running as root (CI with sudo) run directly.
if [[ $(id -u) -eq 0 ]]; then
    _ns() { bash -c "$1"; }
else
    _ns() { podman unshare bash -c "$1"; }
fi

SQUASHFS="${OUTPUT_DIR}/${TARGET}-rootfs.sfs"
BOOT_TAR="${OUTPUT_DIR}/${TARGET}-boot-files.tar"
SQUASHFS_ROOT="${WORKDIR}/${TARGET}-sfs-root"
PAYLOAD_OCI="${OUTPUT_DIR}/${TARGET}-payload.oci.tar"

trap 'rm -f "${SQUASHFS}" "${BOOT_TAR}" "${PAYLOAD_OCI}" 2>/dev/null || true' EXIT

echo "=== Disk space before squashfs assembly ==="
df -h "${OUTPUT_DIR}"
[[ "${WORKDIR}" != "${OUTPUT_DIR}" ]] && df -h "${WORKDIR}"
echo "Building squashfs and boot tar from localhost/${TARGET}-installer..."

COMPOSEFS_BACKEND=$(variant_composefs "${TARGET}")
echo "=== Preparing bound OCI media (composefs=${COMPOSEFS_BACKEND}) for ${PAYLOAD_IMAGE} ==="

# Pull after the prune above: a digest-only pull carries no tag, so
# `podman image prune` would discard it as dangling if it ran later.
if [[ "${PAYLOAD_SOURCE}" != "${PAYLOAD_IMAGE}" ]]; then
    echo "=== Pulling verified payload ${PAYLOAD_SOURCE} ==="
    podman pull "${PAYLOAD_SOURCE}"
fi

payload_to_oci_archive "${PAYLOAD_SOURCE}" "${PAYLOAD_OCI}" "${PAYLOAD_IMAGE}" "${COMPOSEFS_BACKEND}"

podman rmi "${PAYLOAD_SOURCE}" || true

# ── Squashfs assembly (runs in user namespace for rootless UID mapping) ───────
#
# All variables needed inside _ns are passed via the outer scope — no string
# interpolation of shell vars into the bash -c argument. Instead we export them
# and the inner bash inherits them directly.
export OUTPUT_DIR WORKDIR TARGET PAYLOAD_IMAGE COMPOSEFS_BACKEND COMPRESSION SCRIPT_DIR
export SQUASHFS_ROOT SQUASHFS BOOT_TAR PAYLOAD_OCI

_ns_build_squashfs() {
    set -euo pipefail

    echo "=== Disk space inside _ns block ==="
    df -h "${OUTPUT_DIR}"
    [[ "${WORKDIR}" != "${OUTPUT_DIR}" ]] && df -h "${WORKDIR}"

    OVERLAY_UPPER=$(mktemp -d "${SQUASHFS_ROOT}_upper_XXXXXX")
    OVERLAY_WORK=$(mktemp -d "${SQUASHFS_ROOT}_work_XXXXXX")

    ns_cleanup() {
        umount "${SQUASHFS_ROOT}" 2>/dev/null || true
        podman image unmount "localhost/${TARGET}-installer" 2>/dev/null || true
        rm -rf "${OVERLAY_UPPER}" "${OVERLAY_WORK}" 2>/dev/null || true
        rm -rf "${SQUASHFS_ROOT}" 2>/dev/null || true
    }
    trap ns_cleanup EXIT

    MOUNT=$(podman image mount "localhost/${TARGET}-installer")
    PATH=/usr/sbin:/usr/bin:/home/linuxbrew/.linuxbrew/bin:$PATH

    # Use the same producer for the rootless/local and CI entry points.
    # shellcheck source=scripts/payload-oci.sh
    source "${SCRIPT_DIR}/payload-oci.sh"
    echo 'Building unified squashfs source tree...'
    mkdir -p "${SQUASHFS_ROOT}"

    FS_TYPE=$(findmnt -n -o FSTYPE -T "${SQUASHFS_ROOT}" 2>/dev/null || echo "unknown")
    if [[ "${FS_TYPE}" == "xfs" || "${FS_TYPE}" == "ext4" ]]; then
        echo "Filesystem is ${FS_TYPE}, trying overlay"
        if ! mount -t overlay overlay \
                -o lowerdir="${MOUNT}",upperdir="${OVERLAY_UPPER}",workdir="${OVERLAY_WORK}" \
                "${SQUASHFS_ROOT}"; then
            echo "Overlay mount failed on ${FS_TYPE}; falling back to cp -a"
            cp -a "${MOUNT}/." "${SQUASHFS_ROOT}/"
        fi
    else
        echo "Filesystem is ${FS_TYPE}, doing it the boring way"
        cp -a "${MOUNT}/." "${SQUASHFS_ROOT}/"
    fi

    payload_embed_oci_archive "${PAYLOAD_OCI}" "${SQUASHFS_ROOT}"
    rm -f "${PAYLOAD_OCI}"

    echo "=== Disk space after creation of squashfs root ==="
    df -h "${OUTPUT_DIR}"
    [[ "${WORKDIR}" != "${OUTPUT_DIR}" ]] && df -h "${WORKDIR}"
    du -sh "${SQUASHFS_ROOT}" 2>/dev/null || true

    mkdir -p "${SQUASHFS_ROOT}/proc" "${SQUASHFS_ROOT}/sys" "${SQUASHFS_ROOT}/dev"
    SFS_LEVEL=3; SFS_BLOCK=131072
    [[ "${COMPRESSION}" == "release" ]] && { SFS_LEVEL=15; SFS_BLOCK=1048576; }
    mksquashfs "${SQUASHFS_ROOT}" "${SQUASHFS}" \
        -noappend -comp zstd -Xcompression-level "${SFS_LEVEL}" -b "${SFS_BLOCK}" \
        -processors 4 \
        -wildcards -e "proc/*" -e "sys/*" -e "dev/*" -e run -e tmp

    tar -C "${MOUNT}" \
        -cf "${BOOT_TAR}" \
        ./usr/lib/modules \
        ./usr/lib/systemd/boot/efi
}
export -f _ns_build_squashfs

if [[ $(id -u) -eq 0 ]]; then
    bash -c '_ns_build_squashfs'
else
    podman unshare bash -c '_ns_build_squashfs'
fi

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
