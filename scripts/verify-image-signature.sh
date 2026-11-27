#!/usr/bin/bash
# Verify the Sigstore (cosign) keyless signature of a registry image before
# it is consumed by the ISO build, and print the digest-pinned reference so
# callers pull exactly what was verified (no tag-vs-verify TOCTOU window).
#
# The images we package into the ISO (live base image, offline payload) are
# built by projectbluefin GitHub Actions and signed keylessly.
# TLS proves transport integrity only — it does not prove the image under a
# mutable tag (e.g. :stable) was produced by this project's CI.
#
# "This project's CI" means the workflow run in the repository that owns the
# image: ghcr.io/projectbluefin/utah is built by projectbluefin/utah, dakota
# by projectbluefin/dakota, and so on. The certificate's SAN alone cannot say
# that — Utah and Bluefin sign through the shared reusable-build.yml in
# projectbluefin/actions, so their SAN names that workflow, not the caller —
# but the certificate also carries the repository that ran the workflow, and
# cosign enforces it with --certificate-github-workflow-repository. Without
# it a signature from any workflow in any repository of the organisation
# would pass, which is far more than the set of workflows allowed to publish
# these images.
#
# Usage:
#   PINNED=$(scripts/verify-image-signature.sh ghcr.io/projectbluefin/dakota-nvidia:stable)
#   sudo podman pull "$PINNED"
#
# Requires: cosign, jq.  Registry must be anonymously readable (public)
# or already authenticated via `podman login` / $REGISTRY_AUTH_FILE.

set -euo pipefail

OIDC_ISSUER="https://token.actions.githubusercontent.com"
# The signing identity must be a GitHub Actions workflow file in the
# organisation (own repository or the shared projectbluefin/actions reusable
# workflow); which repository ran it is pinned separately below.
IDENTITY_REGEXP='^https://github\.com/projectbluefin/[^/]+/\.github/workflows/[^/@]+@'

# Map an image name to the repository whose workflows may sign it. Anything
# not listed is refused: a new image family must be added here deliberately
# rather than accepted because it happens to carry some organisation signature.
signing_repository() {
    case "$1" in
        utah|utah-*)               echo "projectbluefin/utah" ;;
        dakota|dakota-*)           echo "projectbluefin/dakota" ;;
        bluefin-lts|bluefin-lts-*) echo "projectbluefin/bluefin-lts" ;;
        bluefin|bluefin-*)         echo "projectbluefin/bluefin" ;;
        *)                         return 1 ;;
    esac
}

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <image-ref>" >&2
    exit 2
fi

ref="$1"

# Only projectbluefin GHCR images are signed by our CI.
# Anything else (localhost refs, other registries) is out of scope here.
case "$ref" in
    ghcr.io/projectbluefin/*) ;;
    *)
        echo "ERROR: refusing to verify non-project image '${ref}' — no trusted signer identity" >&2
        exit 1
        ;;
esac

repo="${ref%%[:@]*}"
image="${repo#ghcr.io/projectbluefin/}"
if ! signing_repo=$(signing_repository "${image}"); then
    echo "ERROR: no known signing repository for image '${image}' — add it to signing_repository() in $0 once its producer is known" >&2
    exit 1
fi

command -v jq >/dev/null 2>&1 || {
    echo "ERROR: 'jq' not found on PATH — install jq to verify image signatures" >&2
    exit 1
}

# Self-install fallback for hosts without cosign (e.g. local builds or runners
# that do not run sigstore/cosign-installer). Pinned independently on cosign v3
# (Renovate does not update this fallback alongside the workflow action); the
# SHA-256 checksums come from sigstore/cosign releases so a tampered asset is
# refused.
COSIGN_VERSION="v3.1.5"
if ! command -v cosign >/dev/null 2>&1; then
    case "$(uname -m)" in
        x86_64)  cosign_arch=amd64; cosign_sha=912502def4baf810eaf36b210a4f51e1da214f9f6d664e2a9a2154cfc67ee0d6 ;;
        aarch64) cosign_arch=arm64; cosign_sha=516893301e9ec0f2594edfcf8f943ec3b3ba96f3aad2b507eff2b71e644e5fca ;;
        *)
            echo "ERROR: cosign not found and no pinned fallback for arch '$(uname -m)' — install cosign" >&2
            exit 1
            ;;
    esac
    cosign_dir=$(mktemp -d)
    trap 'rm -rf "$cosign_dir"' EXIT
    echo "==> cosign not found; fetching pinned cosign ${COSIGN_VERSION} (${cosign_arch})" >&2
    curl -fsSL --retry 3 -o "${cosign_dir}/cosign" \
        "https://github.com/sigstore/cosign/releases/download/${COSIGN_VERSION}/cosign-linux-${cosign_arch}"
    echo "${cosign_sha}  ${cosign_dir}/cosign" | sha256sum -c --quiet - || {
        echo "ERROR: cosign ${COSIGN_VERSION} download failed checksum verification" >&2
        exit 1
    }
    chmod +x "${cosign_dir}/cosign"
    PATH="${cosign_dir}:${PATH}"
fi

echo "==> Verifying cosign signature: ${ref} (signed from ${signing_repo})" >&2
verified=$(cosign verify "${ref}" \
    --certificate-oidc-issuer="${OIDC_ISSUER}" \
    --certificate-identity-regexp="${IDENTITY_REGEXP}" \
    --certificate-github-workflow-repository="${signing_repo}" \
    --output json)

digest=$(jq -r '.[0].critical.image["docker-manifest-digest"]' <<<"$verified")
if [[ -z "$digest" || "$digest" == "null" ]]; then
    echo "ERROR: cosign verify succeeded but returned no manifest digest for ${ref}" >&2
    exit 1
fi

echo "==> OK: ${repo}@${digest} signed by ${signing_repo} CI" >&2
echo "${repo}@${digest}"
