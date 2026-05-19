#!/usr/bin/bash
# build-offline-store.sh <output-squashfs> <image> [<image> ...]
#
# Pulls each listed OCI image into an isolated containers-storage overlay
# graphroot, then packs the result into a read-only squashfs at <output-squashfs>.
#
# The squashfs is a valid containers-storage overlay graphroot.  When loop-mounted
# at /var/lib/superiso-store and registered as additionalimagestores, the
# bootc installer can install any listed image offline without a network pull.
#
# This script is the plain-bash equivalent of tacklebox's
# install.BuildOfflineStore().  Run it after pulling the images so they are
# present in the local podman store.
#
# Usage:
#   sudo bash scripts/build-offline-store.sh /out/store.squashfs.img \
#       ghcr.io/projectbluefin/dakota-nvidia:latest \
#       ghcr.io/projectbluefin/dakota:latest

set -euo pipefail

OUTPUT_SFS="${1:?Usage: build-offline-store.sh <output-squashfs> <image> [<image>...]}"
shift
IMAGES=("$@")

if [[ ${#IMAGES[@]} -eq 0 ]]; then
    echo "ERROR: at least one image ref required" >&2
    exit 1
fi

STORE_ROOT="$(mktemp -d /var/tmp/tbox-offline-store.XXXXXX)"
RUN_ROOT="$(mktemp -d /tmp/tbox-offrun.XXXXXX)"
trap 'umount -R "${STORE_ROOT}" 2>/dev/null || true; rm -rf "${STORE_ROOT}" "${RUN_ROOT}" 2>/dev/null || true' EXIT

chmod 0777 "${STORE_ROOT}" "${RUN_ROOT}"

echo ">>> [offline-store] copying ${#IMAGES[@]} image(s) into isolated overlay store..."
for IMG in "${IMAGES[@]}"; do
    # Verify image is present in the local podman store before we try to copy it.
    if ! podman image exists "${IMG}" 2>/dev/null; then
        echo "ERROR: ${IMG} is not in the local podman store. Pull it first." >&2
        exit 1
    fi

    DEST="containers-storage:[overlay@${STORE_ROOT}+${RUN_ROOT}]${IMG}"
    echo ">>> [offline-store] copying ${IMG} ..."
    skopeo copy --remove-signatures "containers-storage:${IMG}" "${DEST}"
done

echo ">>> [offline-store] raw store size: $(du -sh "${STORE_ROOT}" | cut -f1)"

SFS_LEVEL=3; SFS_BLOCK=131072
[[ "${SUPERISO_COMPRESSION:-}" == "release" ]] && { SFS_LEVEL=15; SFS_BLOCK=1048576; }

TMP_SFS="$(mktemp /tmp/tbox-store-XXXXXX.squashfs)"
trap 'rm -f "${TMP_SFS}"' RETURN
chmod 0666 "${TMP_SFS}"

echo ">>> [offline-store] mksquashfs -> ${OUTPUT_SFS} (zstd-${SFS_LEVEL}) ..."
mksquashfs "${STORE_ROOT}" "${TMP_SFS}" \
    -noappend -comp zstd \
    -Xcompression-level "${SFS_LEVEL}" \
    -b "${SFS_BLOCK}" \
    -processors 4

echo ">>> [offline-store] squashfs size: $(du -sh "${TMP_SFS}" | cut -f1)"

mkdir -p "$(dirname "${OUTPUT_SFS}")"
mv "${TMP_SFS}" "${OUTPUT_SFS}"
echo ">>> [offline-store] done: ${OUTPUT_SFS}"
