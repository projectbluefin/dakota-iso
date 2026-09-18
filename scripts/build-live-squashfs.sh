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

if [[ $(id -u) -ne 0 ]]; then
    if command -v podman >/dev/null 2>&1; then
        echo ">>> [live-squashfs] Non-root user detected: re-executing inside user namespace via podman unshare ..."
        exec podman unshare bash "$0" "$@"
    else
        echo "ERROR: must run as root (use sudo) or have podman available for unshare" >&2
        exit 1
    fi
fi

OCI_IMAGE=""
TARGET=""
OUTPUT_DIR=""
DEBUG_ARG="0"
COMPRESSION="${SUPERISO_COMPRESSION:-fast}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --oci-image)         OCI_IMAGE="${2:?--oci-image requires an image ref}"; shift 2 ;;
        --target)            TARGET="${2:?--target requires a target name}"; shift 2 ;;
        --installer-channel) INSTALLER_CHANNEL="${2:?--installer-channel requires a value}"; export INSTALLER_CHANNEL; shift 2 ;;
        --output-dir)        OUTPUT_DIR="${2:?--output-dir requires a path}"; shift 2 ;;
        --debug)             DEBUG_ARG="${2:?--debug requires 0 or 1}"; shift 2 ;;
        --compression)       COMPRESSION="${2:?--compression requires fast or release}"; shift 2 ;;
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
        --build-arg CACHE_BUST="$(date +%Y%m%d%H%M%S)" \
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
#     Embed into VFS containers-storage at /usr/lib/containers/storage
#     (additionalimagestore).  VFS driver is required — the live ISO rootfs
#     is an overlayfs and el10 (LTS) lacks native overlay-on-overlay; an
#     overlay-format additional store silently fails, sending bootc to write
#     large blobs to /var/tmp (RAM tmpfs) → ENOSPC.  VFS is driver-agnostic.
#     bootcDirect resolves containers-storage:<ref> via the additional store.
#     Mirrors projectbluefin/iso commit 34fe6659.
#
# Detect composefs from the recipe.json baked into the live container.
# Run python3 directly (not via sh -c) to avoid nested double-quote parsing
# failures: sh -c 'python3 -c "...open("...")"' breaks because the inner
# double-quotes terminate the outer sh argument prematurely.
COMPOSEFS_BACKEND=false
if podman run --rm --entrypoint="" "${IMAGE}" \
       grep -qi '"composeFsBackend": *true' /etc/bootc-installer/recipe.json \
       2>/dev/null; then
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
        #
        # podman build, not buildah: buildah is unavailable on bootc image-mode
        # hosts (no rpm-ostree, no Homebrew formula), and podman is already a hard
        # dependency of this script.  The docs/build.md warning about Entrypoint
        # corruption applies to `podman create && podman commit`, NOT to
        # `podman build` — a FROM-only Containerfile inherits image config instead
        # of rewriting it.  The verification below enforces that.
        SQUASH_TAG="localhost/live-squash-$$:latest"
        ANNOT_TAG="localhost/live-annot-$$:latest"
        cleanup_squash_tags() {
            podman rmi -f "${SQUASH_TAG}" "${ANNOT_TAG}" >/dev/null 2>&1 || true
        }
        trap cleanup_squash_tags EXIT

        echo ">>> [live-squashfs] squashing ${OCI_IMAGE} to single layer ..."
        printf '[install]\nroot-mount-spec = "LABEL=root"\n' > "${WORK}/bootc-root-mount.toml"
        # COPY writes straight to the destination — no RUN, so no shell in the
        # payload and nothing left behind in /tmp to clean up.
        # The payload ships verbatim to installed systems — never bake a
        # storage.conf into it (installed podman would inherit VFS storage).
        # VFS config for reading the embedded store lives in the live env
        # (configure-live.sh) and the import step below (CONTAINERS_STORAGE_CONF).
        cat > "${WORK}/Containerfile.squash" <<SQUASHEOF
FROM ${OCI_IMAGE}
COPY bootc-root-mount.toml /usr/lib/bootc/install/00-defaults.toml
SQUASHEOF
        podman build \
            --pull=never \
            --squash-all \
            --network=none \
            -f "${WORK}/Containerfile.squash" \
            -t "${SQUASH_TAG}" \
            "${WORK}"

        # podman, not skopeo: this script re-execs inside `podman unshare`, and
        # `skopeo inspect containers-storage:<ref>` cannot resolve the rootless
        # store from in there. It fails, and because the old call swallowed
        # errors the diff_id silently came back empty — which would have shipped
        # a payload with no ostree.final-diffid at all.
        SOURCE_ENTRYPOINT="$(podman image inspect --format '{{json .Config.Entrypoint}}' "${OCI_IMAGE}")"
        SQUASHED_DIFFID="$(podman image inspect --format '{{index .RootFS.Layers 0}}' "${SQUASH_TAG}")"
        SQUASH_LAYERS="$(podman image inspect --format '{{len .RootFS.Layers}}' "${SQUASH_TAG}")"

        if [[ "${SQUASH_LAYERS}" != "1" ]]; then
            echo "ERROR: squash produced ${SQUASH_LAYERS} layers, expected 1" >&2
            exit 1
        fi
        if [[ -z "${SQUASHED_DIFFID}" ]]; then
            echo "ERROR: could not read squashed diff_id — ostree.final-diffid would be wrong" >&2
            exit 1
        fi

        echo ">>> [live-squashfs] updating ostree.final-diffid to ${SQUASHED_DIFFID}"
        # Metadata only: no filesystem instruction, so the squashed layer
        # content — and therefore its diff_id — is unchanged by this pass.
        printf 'FROM %s\n' "${SQUASH_TAG}" > "${WORK}/Containerfile.annot"
        podman build \
            --pull=never \
            --squash-all \
            --network=none \
            --label "ostree.final-diffid=${SQUASHED_DIFFID}" \
            --annotation "ostree.final-diffid=${SQUASHED_DIFFID}" \
            -f "${WORK}/Containerfile.annot" \
            -t "${ANNOT_TAG}" \
            "${WORK}"

        # Guard the three things podman build could plausibly get wrong versus
        # buildah commit --squash: extra layers, a rewritten Entrypoint, or a
        # diff_id that drifted when the metadata pass re-flattened the image.
        ANNOT_DIFFID="$(podman image inspect --format '{{index .RootFS.Layers 0}}' "${ANNOT_TAG}")"
        ANNOT_ENTRYPOINT="$(podman image inspect --format '{{json .Config.Entrypoint}}' "${ANNOT_TAG}")"
        if [[ "${ANNOT_DIFFID}" != "${SQUASHED_DIFFID}" ]]; then
            echo "ERROR: metadata pass changed diff_id ${SQUASHED_DIFFID} -> ${ANNOT_DIFFID}" >&2
            exit 1
        fi
        if [[ "${ANNOT_ENTRYPOINT}" != "${SOURCE_ENTRYPOINT}" ]]; then
            echo "ERROR: Entrypoint changed by squash: ${SOURCE_ENTRYPOINT} -> ${ANNOT_ENTRYPOINT}" >&2
            exit 1
        fi
        echo ">>> [live-squashfs] squash verified: 1 layer, diff_id stable, Entrypoint preserved (${ANNOT_ENTRYPOINT})"

        podman push "${ANNOT_TAG}" "oci-archive:${OCI_ARCHIVE}:${OCI_IMAGE}"

        cleanup_squash_tags
        trap - EXIT

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

        printf '[install]\nroot-mount-spec = "LABEL=root"\n' > "${WORK}/bootc-root-mount.toml"

        OCI_ARCHIVE="${WORK}/payload.oci.tar"
        CS_STAGING="${WORK}/overlay-storage"
        STORAGE_CONF="${WORK}/st.conf"
        mkdir -p "${CS_STAGING}"

        # No --squash-all here: the ostree commits in the original layers must
        # survive, so this adds one layer on top instead of flattening.
        # COPY creates /usr/lib/bootc/install itself, so no RUN is needed.
        echo ">>> [live-squashfs] injecting root-mount-spec without squash to preserve ostree commits ..."
        INJECT_TAG="localhost/live-inject-$$:latest"
        cleanup_inject_tag() { podman rmi -f "${INJECT_TAG}" >/dev/null 2>&1 || true; }
        trap cleanup_inject_tag EXIT
        cat > "${WORK}/Containerfile.inject" <<INJECTEOF
FROM ${OCI_IMAGE}
COPY bootc-root-mount.toml /usr/lib/bootc/install/00-defaults.toml
INJECTEOF
        podman build \
            --pull=never \
            --network=none \
            -f "${WORK}/Containerfile.inject" \
            -t "${INJECT_TAG}" \
            "${WORK}"
        podman push "${INJECT_TAG}" "oci-archive:${OCI_ARCHIVE}:${OCI_IMAGE}"
        cleanup_inject_tag
        trap - EXIT

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
[[ "${COMPRESSION}" == "release" || "${SUPERISO_COMPRESSION:-}" == "release" ]] && { SFS_LEVEL=15; SFS_BLOCK=1048576; }

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
