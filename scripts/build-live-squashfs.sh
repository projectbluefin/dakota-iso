#!/usr/bin/bash
# build-live-squashfs.sh [--oci-image <ref>] <image> <output-squashfs> <output-boot-tar>
#
# Exports a container image as a squashfs suitable for dmsquash-live boot,
# and a companion tar of the boot files (kernel, initramfs, EFI binary) needed
# to assemble the ISO ESP.
#
# When --oci-image is given, the referenced OCI image is squashed to a single
# layer and imported into a VFS containers-storage tree at
# /var/lib/containers/storage inside the squashfs.  This is the offline install
# store: at boot, fisherman reads local_imgref=containers-storage:<ref> from
# recipe.json and finds the image there without a network pull.
#
# The OCI image must already be present in the local podman/buildah store.
# skopeo runs inside the live container so the tar-split metadata is written
# in the JSON format that the live VFS storage driver expects (not the binary
# format that the build-host containers/storage might emit).
#
# This is the plain-bash equivalent of tacklebox's runEnv() + squashfs stage.
#
# Usage (must run as root or with sudo):
#   sudo bash scripts/build-live-squashfs.sh \
#       [--oci-image ghcr.io/projectbluefin/dakota-nvidia:stable] \
#       localhost/dakota-nvidia-live:latest \
#       /out/dakota-nvidia.rootfs.sfs \
#       /out/dakota-nvidia-boot.tar

set -euo pipefail

OCI_IMAGE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --oci-image) OCI_IMAGE="${2:?--oci-image requires an image ref}"; shift 2 ;;
        *) break ;;
    esac
done

IMAGE="${1:?Usage: build-live-squashfs.sh [--oci-image <ref>] <image> <output-squashfs> <output-boot-tar>}"
OUTPUT_SFS="${2:?}"
OUTPUT_BOOT_TAR="${3:?}"

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
