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
# When --oci-image is given, embed the payload image for offline installation.
# Strategy depends on composefs vs. standard-ostree:
#
#   composefs (composeFsBackend=true, e.g. dakota):
#     Squash all layers to one, import into VFS containers-storage.
#     Fisherman exports VFS → OCI at install time and passes
#     --source-imgref oci:... --composefs-backend to bootc.
#
#   standard-ostree / non-composefs (e.g. bluefin, bluefin-lts-hwe):
#     Store as OCI layout directly — NO squash.
#     Squashing flattens the ostree commit structure and breaks bootc's
#     ostree-ext unencapsulation ("Expected commit object, not File").
#     The OCI layout preserves chunkah/zstd:chunked layers so bootc
#     can use chunk-aware streaming install with zero intermediate staging.
#     Fisherman calls bootc install to-filesystem --source-imgref oci:...
#     directly (no podman run, no ENOSPC).
#
# Detect composefs from the recipe.json baked into the live container.
COMPOSEFS_BACKEND=false
if podman run --rm --entrypoint="" "${IMAGE}" \
       sh -c 'python3 -c "import json; print(json.load(open("/etc/bootc-installer/recipe.json")).get(\"composeFsBackend\", False))"' \
       2>/dev/null | grep -qi true; then
    COMPOSEFS_BACKEND=true
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
        echo ">>> [live-squashfs] squashing ${OCI_IMAGE} to single layer ..."
        SQUASH_CTR="$(buildah from --pull-never "${OCI_IMAGE}")"
        printf '[install]\nroot-mount-spec = "LABEL=root"\n' > "${WORK}/bootc-root-mount.toml"
        buildah copy "${SQUASH_CTR}" "${WORK}/bootc-root-mount.toml" /tmp/.bootc-root-mount.toml
        buildah run  "${SQUASH_CTR}" -- sh -c 'cp /tmp/.bootc-root-mount.toml /usr/lib/bootc/install/00-defaults.toml && rm /tmp/.bootc-root-mount.toml'
        printf '[storage]\ndriver = "vfs"\nrunroot = "/run/containers/storage"\ngraphroot = "/var/lib/containers/storage"\n' > "${WORK}/vfs-storage.conf"
        buildah run  "${SQUASH_CTR}" -- mkdir -p /etc/containers
        buildah copy "${SQUASH_CTR}" "${WORK}/vfs-storage.conf" /etc/containers/storage.conf
        buildah commit --squash "${SQUASH_CTR}" "oci-archive:${OCI_ARCHIVE}:${OCI_IMAGE}"
        buildah rm "${SQUASH_CTR}"

        SQUASHED_DIFFID="$(skopeo inspect --config "oci-archive:${OCI_ARCHIVE}:${OCI_IMAGE}" 2>/dev/null | \
            python3 -c 'import json,sys; c=json.load(sys.stdin); print(c["rootfs"]["diff_ids"][0])' 2>/dev/null || true)"
        if [[ -n "${SQUASHED_DIFFID}" ]]; then
            echo ">>> [live-squashfs] updating ostree.final-diffid to ${SQUASHED_DIFFID}"
            ANNOT_CTR="$(buildah from --pull-never "oci-archive:${OCI_ARCHIVE}:${OCI_IMAGE}")"
            buildah config --label "ostree.final-diffid=${SQUASHED_DIFFID}" "${ANNOT_CTR}"
            buildah config --annotation "ostree.final-diffid=${SQUASHED_DIFFID}" "${ANNOT_CTR}"
            buildah commit --squash "${ANNOT_CTR}" "oci-archive:${OCI_ARCHIVE}:${OCI_IMAGE}"
            buildah rm "${ANNOT_CTR}"
        fi

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
        # Non-composefs (standard-ostree): store as OCI layout.
        # Inject 00-defaults.toml (root-mount-spec) as a thin extra layer
        # without squashing, so the original ostree layer structure is preserved.
        echo ">>> [live-squashfs] embedding OCI image ${OCI_IMAGE} as OCI layout (non-composefs path) ..."

        OCI_DIR="${SFS_ROOT}/var/lib/containers/oci-store"
        mkdir -p "${OCI_DIR}"

        # Inject root-mount-spec config into the image (adds one tiny layer).
        printf '[install]\nroot-mount-spec = "LABEL=root"\n' > "${WORK}/bootc-root-mount.toml"
        INJECT_CTR="$(buildah from --pull-never "${OCI_IMAGE}")"
        buildah copy "${INJECT_CTR}" "${WORK}/bootc-root-mount.toml" /tmp/.bootc-root-mount.toml
        buildah run  "${INJECT_CTR}" -- sh -c 'mkdir -p /usr/lib/bootc/install && cp /tmp/.bootc-root-mount.toml /usr/lib/bootc/install/00-defaults.toml && rm /tmp/.bootc-root-mount.toml'
        OCI_INJECTED="${WORK}/payload-injected.oci"
        buildah commit --format oci "${INJECT_CTR}" "oci:${OCI_INJECTED}:${OCI_IMAGE}"
        buildah rm "${INJECT_CTR}"

        # Copy to the squashfs root, preserving the original layer blobs.
        echo ">>> [live-squashfs] copying OCI layout into squashfs root ..."
        skopeo copy "oci:${OCI_INJECTED}:${OCI_IMAGE}" "oci:${OCI_DIR}"
        rm -rf "${OCI_INJECTED}"
        echo ">>> [live-squashfs] OCI store embedded: $(du -sh "${OCI_DIR}" | cut -f1)"
    fi
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
