#!/usr/bin/bash
# bump-installer-bundle.sh — refresh live/src/installer-bundle.lock.
#
# Resolves the release tag that tuna-os/bootc-installer's /releases/latest/
# redirect currently points at (or takes one as $1), downloads both installer
# bundles from that exact tag, and rewrites the lock with the tag and the
# SHA-256 of each bundle. install-flatpaks.sh refuses any bundle that does not
# match the lock, so a new upstream release reaches the ISO only through a
# reviewed change to this file.
#
# Usage: scripts/bump-installer-bundle.sh [<release-tag>]
#   INSTALLER_REPO  — override the upstream repository (tests)
#   INSTALLER_LOCK  — override the lock path (tests)

set -euo pipefail

INSTALLER_REPO="${INSTALLER_REPO:-tuna-os/bootc-installer}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER_LOCK="${INSTALLER_LOCK:-${SCRIPT_DIR}/../live/src/installer-bundle.lock}"
BUNDLES=(org.bootcinstaller.Installer.flatpak org.bootcinstaller.Installer.Devel.flatpak)

TAG="${1:-}"
if [[ -z "${TAG}" ]]; then
    final_url=$(curl --silent --show-error --fail --location --head \
        --output /dev/null --write-out '%{url_effective}' \
        "https://github.com/${INSTALLER_REPO}/releases/latest")
    TAG="${final_url##*/tag/}"
fi
case "${TAG}" in
    ""|*/*|latest|latest-dev|latest-stable|dev-rolling)
        echo "ERROR: refusing release tag '${TAG}' — need an exact versioned tag" >&2
        exit 1
        ;;
esac

workdir=$(mktemp -d)
trap 'rm -rf "${workdir}"' EXIT

{
    echo "# bootc-installer release baked into every live ISO (${INSTALLER_REPO})."
    echo "# install-flatpaks.sh downloads releases/download/<tag>/<file> and refuses a"
    echo "# bundle whose SHA-256 differs from the value listed here."
    echo "# Refresh with: scripts/bump-installer-bundle.sh"
    echo "tag=${TAG}"
    for bundle in "${BUNDLES[@]}"; do
        curl --silent --show-error --fail --location --retry 3 \
            "https://github.com/${INSTALLER_REPO}/releases/download/${TAG}/${bundle}" \
            -o "${workdir}/${bundle}"
        sum=$(sha256sum "${workdir}/${bundle}" | cut -d' ' -f1)
        echo "${bundle}=${sum}"
    done
} > "${workdir}/lock"

mv "${workdir}/lock" "${INSTALLER_LOCK}"
echo "updated ${INSTALLER_LOCK}:"
grep -v '^#' "${INSTALLER_LOCK}"
