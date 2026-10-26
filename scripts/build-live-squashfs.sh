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
#    then exports it to squashfs.  When --oci-image is given, the referenced
#    payload is embedded into VFS containers-storage for offline installs;
#    otherwise fisherman pulls the payload from the network at install time.
#    Outputs: <dir>/<target>-live.squashfs  and  <dir>/<target>-boot-files.tar
#
# 2. Positional mode (used by build-iso.yml):
#      sudo -E bash scripts/build-live-squashfs.sh \
#          [--oci-image ghcr.io/projectbluefin/dakota-nvidia:stable] \
#          localhost/dakota-nvidia-live:latest \
#          /out/dakota-nvidia.rootfs.sfs \
#          /out/dakota-nvidia-boot.tar
#    Exports a pre-built container image as squashfs.  When --oci-image is
#    given the referenced OCI image is squashed to a single layer and embedded
#    into a VFS containers-storage tree at /var/lib/containers/storage inside
#    the squashfs — the offline install store that fisherman reads at boot.
#
# The OCI image (positional mode + --oci-image) must already be present in
# the local podman store.
# skopeo runs inside the live container so the VFS tar-split metadata is
# written in the JSON format that the live ISO expects.
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

# SUPERISO_TMPDIR lets CI redirect scratch space to a large disk-backed path
# (e.g. /var/iso-build).  The squash+VFS embedding writes ~12 GB of
# intermediates; /var/tmp on GitHub runners only has ~14 GB total.
WORK="$(mktemp -d "${SUPERISO_TMPDIR:-/var/tmp}/tbox-live-sfs.XXXXXX")"
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

# ── Embed offline OCI store (VFS) ─────────────────────────────────────────────
# When --oci-image is given, embed the payload image for offline installation.
# Strategy depends on composefs vs. standard-ostree:
#
#   composefs (composeFsBackend=true, e.g. dakota):
#     Squash all layers to one, import into VFS containers-storage.
#     Fisherman exports VFS → OCI at install time and passes
#     --source-imgref oci:... --composefs-backend to bootc.
#
#   standard-ostree / non-composefs (e.g. stable, lts):
#     Embed into VFS containers-storage at /usr/lib/containers/storage
#     (additionalimagestore).  VFS driver is required — the live ISO rootfs
#     is an overlayfs and el10 (LTS) lacks native overlay-on-overlay; an
#     overlay-format additional store silently fails, sending bootc to write
#     large blobs to /var/tmp (RAM tmpfs) → ENOSPC.  VFS is driver-agnostic.
#     bootcDirect resolves containers-storage:<ref> via the additional store.
#     Mirrors projectbluefin/iso commit 34fe6659.
#
# In --target mode, variant-config.sh is the single authority for the variant.
# In positional mode, detect composefs from the recipe.json baked into the live
# container.  The probe fails closed: grep exit 1 (no match) means the recipe
# says false; any other non-zero status (recipe.json missing, podman unable to
# run the image) aborts, so an infrastructure error cannot silently produce a
# non-composefs ISO layout.
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
        echo ">>> [live-squashfs] embedding OCI image ${OCI_IMAGE} into VFS store (composefs path) ..."

        OCI_ARCHIVE="${WORK}/payload.oci.tar"
        CS_STAGING="${WORK}/vfs-storage"
        STORAGE_CONF="${WORK}/st.conf"

        mkdir -p "${CS_STAGING}"

        # Chunkified images have many layers; squash to 1 to keep VFS store compact.
        # The payload ships verbatim to installed systems — never bake a
        # storage.conf into it (installed podman would inherit VFS storage).
        # VFS config for reading the embedded store lives in the live env
        # (configure-live.sh) and the import step below (CONTAINERS_STORAGE_CONF).
        echo ">>> [live-squashfs] squashing ${OCI_IMAGE} to single layer ..."
        payload_to_oci_archive "${OCI_IMAGE}" "${OCI_ARCHIVE}" "${OCI_IMAGE}" true

        printf '[storage]\ndriver = "vfs"\nrunroot = "/tmp/cs-runroot"\ngraphroot = "/vfs-storage"\n' \
            > "${STORAGE_CONF}"

        echo ">>> [live-squashfs] importing squashed OCI into VFS staging dir ..."
        podman run --rm \
            --privileged \
            -v "${OCI_ARCHIVE}:/payload.oci.tar:ro" \
            -v "${CS_STAGING}:/vfs-storage" \
            -v "${STORAGE_CONF}:/tmp/st.conf:ro" \
            "${IMAGE}" \
            sh -c "mkdir -p /tmp/cs-runroot /var/tmp && \
                   CONTAINERS_STORAGE_CONF=/tmp/st.conf \
                   skopeo copy \
                   oci-archive:/payload.oci.tar:${OCI_IMAGE} \
                   containers-storage:${OCI_IMAGE}"

        rm -f "${OCI_ARCHIVE}" "${STORAGE_CONF}"

        mkdir -p "${SFS_ROOT}/var/lib/containers/storage"
        echo ">>> [live-squashfs] copying VFS store into squashfs root ($(du -sh "${CS_STAGING}" | cut -f1)) ..."
        cp -a "${CS_STAGING}/." "${SFS_ROOT}/var/lib/containers/storage/"
        rm -rf "${CS_STAGING}"
        echo ">>> [live-squashfs] VFS store embedded: $(du -sh "${SFS_ROOT}/var/lib/containers/storage" | cut -f1)"
    else
        # Non-composefs (standard-ostree / bootcDirect): embed image into overlay
        # containers-storage at /usr/lib/containers/storage (additionalimagestore).
        echo ">>> [live-squashfs] non-composefs (bootcDirect) — embedding OCI image ${OCI_IMAGE} into overlay store ..."

        OCI_ARCHIVE="${WORK}/payload.oci.tar"
        CS_STAGING="${WORK}/overlay-storage"
        STORAGE_CONF="${WORK}/st.conf"
        mkdir -p "${CS_STAGING}"

        echo ">>> [live-squashfs] committing payload without squash to preserve ostree commits ..."
        payload_to_oci_archive "${OCI_IMAGE}" "${OCI_ARCHIVE}" "${OCI_IMAGE}" false

        printf '[storage]\ndriver = "overlay"\nrunroot = "/tmp/cs-runroot"\ngraphroot = "/vfs-storage"\n' \
            > "${STORAGE_CONF}"

        echo ">>> [live-squashfs] importing OCI into overlay staging dir ..."
        podman run --rm \
            --privileged \
            -v "${OCI_ARCHIVE}:/payload.oci.tar:ro" \
            -v "${CS_STAGING}:/vfs-storage" \
            -v "${STORAGE_CONF}:/tmp/st.conf:ro" \
            "${IMAGE}" \
            sh -c "mkdir -p /tmp/cs-runroot /var/tmp && \
                   CONTAINERS_STORAGE_CONF=/tmp/st.conf \
                   skopeo copy \
                   oci-archive:/payload.oci.tar:${OCI_IMAGE} \
                   containers-storage:${OCI_IMAGE}"

        rm -f "${OCI_ARCHIVE}" "${STORAGE_CONF}"

        mkdir -p "${SFS_ROOT}/usr/lib/containers/storage"
        echo ">>> [live-squashfs] copying overlay store into squashfs root ($(du -sh "${CS_STAGING}" | cut -f1)) ..."
        # Overlay containers-storage contains character-device whiteout files that
        # cp -a cannot create without privileges.  Use rsync to skip them — they
        # are write-layer artifacts not needed in the read-only additional store.
        rsync -a --no-specials --no-devices "${CS_STAGING}/" "${SFS_ROOT}/usr/lib/containers/storage/"
        rm -rf "${CS_STAGING}"
        echo ">>> [live-squashfs] overlay store embedded: $(du -sh "${SFS_ROOT}/usr/lib/containers/storage" | cut -f1)"
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
mkdir -p "${SFS_ROOT}/proc" "${SFS_ROOT}/sys" "${SFS_ROOT}/dev"

mksquashfs "${SFS_ROOT}" "${OUTPUT_SFS}" \
    -noappend -comp zstd \
    -Xcompression-level "${SFS_LEVEL}" \
    -b "${SFS_BLOCK}" \
    -processors 4 \
    -wildcards \
    -e "proc/*" -e "sys/*" -e "dev/*" -e run -e tmp
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
