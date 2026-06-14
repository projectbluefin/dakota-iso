#!/usr/bin/bash
# build-live-squashfs.sh  —  build a live squashfs and companion boot tar
#
# Two usage modes:
#
# 1. Target mode (used by test-plain-install.yml):
#      sudo -E bash scripts/build-live-squashfs.sh \
#          --target <name> \
#          [--installer-channel dev|stable] \
#          --output-dir <dir>
#    Builds the live container from live/Containerfile for the given target,
#    then exports it to squashfs.  No offline OCI store is embedded;
#    fisherman pulls the payload from the network at install time.
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
# the local podman/buildah store.
# skopeo runs inside the live container so the VFS tar-split metadata is
# written in the JSON format that the live ISO expects.
#
# Must run as root (sudo).

set -euo pipefail

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

    LIVE_TARGET=$(cat "${TARGET}/live_target" 2>/dev/null | tr -d '[:space:]' || echo "${TARGET}")
    echo ">>> [live-squashfs] building live container: target=${TARGET} live_target=${LIVE_TARGET} channel=${INSTALLER_CHANNEL:-stable} debug=${DEBUG_ARG}"

    podman build \
        --cap-add sys_admin \
        --security-opt label=disable \
        --layers \
        --build-arg INSTALLER_CHANNEL="${INSTALLER_CHANNEL:-stable}" \
        --build-arg TARGET="${LIVE_TARGET}" \
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
# When --oci-image is given, squash the payload to a single layer and import it
# into VFS containers-storage at /var/lib/containers/storage inside the squashfs
# root.  At boot, fisherman reads local_imgref=containers-storage:<ref> from
# recipe.json and finds the image there for an offline install.
if [[ -n "${OCI_IMAGE}" ]]; then
    echo ">>> [live-squashfs] embedding OCI image ${OCI_IMAGE} into VFS store ..."

    OCI_ARCHIVE="${WORK}/payload.oci.tar"
    CS_STAGING="${WORK}/vfs-storage"
    STORAGE_CONF="${WORK}/st.conf"

    mkdir -p "${CS_STAGING}"

    # Chunkified Dakota images have ~120 layers; VFS copies the full filesystem
    # at each layer.  Squash to 1 layer to keep the store to ~6 GB.
    echo ">>> [live-squashfs] squashing ${OCI_IMAGE} to single layer ..."
    SQUASH_CTR="$(buildah from --pull-never "${OCI_IMAGE}")"
    buildah commit --squash "${SQUASH_CTR}" "oci-archive:${OCI_ARCHIVE}:${OCI_IMAGE}"
    buildah rm "${SQUASH_CTR}"

    # Write a storage conf for skopeo that uses the staging dir as graphroot.
    # Paths are container-relative: /vfs-storage is bind-mounted to CS_STAGING.
    printf '[storage]\ndriver = "vfs"\nrunroot = "/tmp/cs-runroot"\ngraphroot = "/vfs-storage"\n' \
        > "${STORAGE_CONF}"

    # Run skopeo inside the live container so the VFS tar-split metadata is
    # written in the JSON format the live ISO expects (build-host containers/
    # storage may emit a binary format that the live VFS driver cannot read).
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

    # Copy the VFS staging dir INTO the squashfs root so mksquashfs captures it
    # at /var/lib/containers/storage.
    #
    # bind-mount does NOT work here: when SFS_ROOT is an overlayfs mount, the
    # overlayfs and the bind-mount have different st_dev values.  mksquashfs
    # respects filesystem boundaries (st_dev changes) and silently skips the
    # bind-mounted tree.  Copying the data into the overlayfs upper layer makes
    # it part of the same st_dev, so mksquashfs includes it.
    mkdir -p "${SFS_ROOT}/var/lib/containers/storage"
    echo ">>> [live-squashfs] copying VFS store into squashfs root ($(du -sh "${CS_STAGING}" | cut -f1)) ..."
    cp -a "${CS_STAGING}/." "${SFS_ROOT}/var/lib/containers/storage/"
    rm -rf "${CS_STAGING}"
    echo ">>> [live-squashfs] VFS store embedded: $(du -sh "${SFS_ROOT}/var/lib/containers/storage" | cut -f1)"
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
