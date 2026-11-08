#!/usr/bin/bash
# build-live-squashfs.sh  —  build a live squashfs and companion boot tar
#
# Two usage modes:
#
# 1. Target mode (used by test-plain-install.yml):
#      sudo -E bash scripts/build-live-squashfs.sh \
#          --target <name> \
#          [--installer-channel dev|stable] \
#          [--oci-image ghcr.io/projectbluefin/dakota-nvidia:stable] \
#          --output-dir <dir>
#    Builds the live container from live/Containerfile for the given target,
#    then exports it to squashfs. --oci-image supplies the required offline OCI
#    payload; live-iso-mode refuses installation without its media receipt.
#    Outputs: <dir>/<target>-live.squashfs  and  <dir>/<target>-boot-files.tar
#
# 2. Positional mode (used by build-iso.yml):
#      sudo -E bash scripts/build-live-squashfs.sh \
#          [--oci-image ghcr.io/projectbluefin/dakota-nvidia:stable] \
#          localhost/dakota-nvidia-live:latest \
#          /out/dakota-nvidia.rootfs.sfs \
#          /out/dakota-nvidia-boot.tar
#    Exports a pre-built container image as squashfs. --oci-image embeds the
#    prepared OCI layout once, bound by /etc/bootc-installer/media-input.json.
#    The payload must already be present in the local podman store.
#
# Must run as root (sudo).

set -euo pipefail
# Single host-side reader for per-variant config (see scripts/variant-config.sh).
# shellcheck source=scripts/variant-config.sh
source "$(dirname "${BASH_SOURCE[0]}")/variant-config.sh"
# shellcheck source=scripts/payload-oci.sh
source "$(dirname "${BASH_SOURCE[0]}")/payload-oci.sh"

OCI_IMAGE=""
TARGET=""
OUTPUT_DIR=""
DEBUG_ARG="0"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --oci-image)         OCI_IMAGE="${2:?--oci-image requires an image ref}"; shift 2 ;;
        --target)            TARGET="${2:?--target requires a target name}"; shift 2 ;;
        --installer-channel) INSTALLER_CHANNEL="${2:?--installer-channel requires a value}"; export INSTALLER_CHANNEL; shift 2 ;;
        --output-dir)        OUTPUT_DIR="${2:?--output-dir requires a path}"; shift 2 ;;
        --debug)             DEBUG_ARG="${2:?--debug requires 0 or 1}"; shift 2 ;;
        *) break ;;
    esac
done

if [[ -n "${TARGET}" ]]; then
    # ── Target mode: build live container then squashfs it ────────────────────
    [[ -z "${OUTPUT_DIR}" ]] && { echo "ERROR: --target requires --output-dir" >&2; exit 1; }

    LIVE_TARGET=$(variant_live_target "${TARGET}")
    LIVE_TAG=$(variant_tag "${TARGET}")
    LIVE_REGISTRY=$(variant_registry "${TARGET}")
    echo ">>> [live-squashfs] building live container: target=${TARGET} live_target=${LIVE_TARGET} channel=${INSTALLER_CHANNEL:-stable} debug=${DEBUG_ARG}"

    # Verify the cosign signature of the live base image and pin the build to
    # the verified digest — same gate as the justfile `container` recipe.
    BASE_PINNED=$("$(dirname "${BASH_SOURCE[0]}")/verify-image-signature.sh" \
        "ghcr.io/${LIVE_REGISTRY}/${LIVE_TARGET}:${LIVE_TAG}")

    podman build \
        --cap-add sys_admin \
        --security-opt label=disable \
        --layers \
        --build-arg INSTALLER_CHANNEL="${INSTALLER_CHANNEL:-stable}" \
        --build-arg TARGET="${LIVE_TARGET}" \
        --build-arg TAG="${LIVE_TAG}" \
        --build-arg REGISTRY="${LIVE_REGISTRY}" \
        --build-arg BASE_DIGEST="@${BASE_PINNED##*@}" \
        --build-arg DEBUG="${DEBUG_ARG}" \
        -t "${TARGET}-installer" \
        -f ./live/Containerfile ./live

    IMAGE="${TARGET}-installer"
    OUTPUT_SFS="${OUTPUT_DIR}/${TARGET}-live.squashfs"
    OUTPUT_BOOT_TAR="${OUTPUT_DIR}/${TARGET}-boot-files.tar"
else
    # ── Positional mode: use pre-built image ──────────────────────────────────
    IMAGE="${1:?Usage: build-live-squashfs.sh [--oci-image <ref>] <image> <output-squashfs> <output-boot-tar>}"
    OUTPUT_SFS="${2:?}"
    OUTPUT_BOOT_TAR="${3:?}"
fi

if [[ $(id -u) -ne 0 ]]; then
    echo "ERROR: must run as root (use sudo)" >&2
    exit 1
fi

# SUPERISO_TMPDIR lets CI redirect scratch space to a large disk-backed path.
WORK="$(mktemp -d "${SUPERISO_TMPDIR:-/var/tmp}/tbox-live-sfs.XXXXXX")"
trap 'podman image unmount "${IMAGE}" 2>/dev/null || true
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

# The prepared OCI layout is shared by composefs and ostree installs. Preserve
# all layers for ostree; only composefs payloads may be squashed.
COMPOSEFS_BACKEND=false
if [[ -n "${TARGET}" ]]; then
    COMPOSEFS_BACKEND=$(variant_composefs "${TARGET}")
else
    probe_rc=0
    podman run --rm --entrypoint="" "${IMAGE}" \
        grep -qi '"composeFsBackend": *true' /etc/bootc-installer/recipe.json \
        || probe_rc=$?
    case "${probe_rc}" in
        0) COMPOSEFS_BACKEND=true ;;
        1) COMPOSEFS_BACKEND=false ;;
        *)
            echo "ERROR: could not read composeFsBackend from /etc/bootc-installer/recipe.json in ${IMAGE} (exit ${probe_rc})" >&2
            exit 1
            ;;
    esac
fi
echo ">>> [live-squashfs] composeFsBackend=${COMPOSEFS_BACKEND}"
if [[ -n "${OCI_IMAGE}" ]]; then
    if [[ "${COMPOSEFS_BACKEND}" == "true" ]]; then
        # Single-copy media: unpacked store files share squashfs blocks with
        # the live root instead of duplicating it as an opaque OCI blob.
        echo "=== Preparing bound store media (single-copy) for ${OCI_IMAGE} ==="
        PAYLOAD_TAG=""; PAYLOAD_BASE=""
        payload_build_final "${OCI_IMAGE}" "true" PAYLOAD_TAG PAYLOAD_BASE
        PAYLOAD_INFO="$(payload_export_store "${PAYLOAD_TAG}" "${WORK}/payload-store" "${OCI_IMAGE}")" || {
            echo "ERROR: payload store export failed; refusing to build unverifiable media" >&2
            exit 1
        }
        read -r PAYLOAD_DIGEST PAYLOAD_CONFIGDIGEST PAYLOAD_PLATFORM <<< "${PAYLOAD_INFO}" || {
            echo "ERROR: payload store export produced no receipt values" >&2
            exit 1
        }
        [[ -n "${PAYLOAD_DIGEST}" && -n "${PAYLOAD_CONFIGDIGEST}" && -n "${PAYLOAD_PLATFORM}" ]] || {
            echo "ERROR: payload store export produced empty receipt values; refusing to build unverifiable media" >&2
            exit 1
        }
        payload_bind_store "${WORK}/payload-store" "${SFS_ROOT}" "${OCI_IMAGE}" "" \
            "${PAYLOAD_DIGEST}" "${PAYLOAD_CONFIGDIGEST}" "${PAYLOAD_PLATFORM}"
        podman rmi "${PAYLOAD_TAG}" >/dev/null 2>&1 || true
        if [[ -n "${PAYLOAD_BASE}" ]]; then
            podman rmi "${PAYLOAD_BASE}" >/dev/null 2>&1 || true
        fi
    else
        OCI_ARCHIVE="${WORK}/payload.oci.tar"
        payload_to_oci_archive "${OCI_IMAGE}" "${OCI_ARCHIVE}" "${OCI_IMAGE}" "${COMPOSEFS_BACKEND}"
        payload_embed_oci_archive "${OCI_ARCHIVE}" "${SFS_ROOT}"
        rm -f "${OCI_ARCHIVE}"
    fi
fi

SFS_LEVEL=3; SFS_BLOCK=131072
[[ "${SUPERISO_COMPRESSION:-}" == "release" ]] && { SFS_LEVEL=15; SFS_BLOCK=1048576; }

echo ">>> [live-squashfs] mksquashfs -> ${OUTPUT_SFS} (zstd-${SFS_LEVEL}) ..."
mkdir -p "$(dirname "${OUTPUT_SFS}")"

# dmsquash-live-root (Debian bookworm dracut) uses the squashfs directly as the
# live rootfs when it finds a /proc directory at the squashfs root.  Without it,
# it falls through to die "Failed to find a root filesystem".
# dracut's usable_root() requires ALL of proc/, sys/, and dev/ to exist at the
# squashfs root (the ld-*.so glob doesn't match modern glibc's ld-linux-x86-64.so.2).
# Ensure all three exist as empty directories and exclude only their CONTENTS.
# NOTE: do not use "-e proc" — newer mksquashfs (4.7+) removes the directory
# itself when given a bare "-e proc", while older versions kept an empty dir.
# Same applies to sys and dev.
# dracut only carries /run across switch-root when the new root has that directory.
mkdir -p "${SFS_ROOT}/proc" "${SFS_ROOT}/sys" "${SFS_ROOT}/dev" "${SFS_ROOT}/run" "${SFS_ROOT}/tmp"

mksquashfs "${SFS_ROOT}" "${OUTPUT_SFS}" \
    -noappend -comp zstd \
    -Xcompression-level "${SFS_LEVEL}" \
    -b "${SFS_BLOCK}" \
    -processors 4 \
    -wildcards \
    -e "proc/*" -e "sys/*" -e "dev/*" -e "run/*" -e "tmp/*"
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
