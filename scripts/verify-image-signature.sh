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
# Usage:
#   PINNED=$(scripts/verify-image-signature.sh ghcr.io/projectbluefin/dakota-nvidia:stable)
#   sudo podman pull "$PINNED"
#
# Requires: cosign, jq.  Registry must be anonymously readable (public)
# or already authenticated via `podman login` / $REGISTRY_AUTH_FILE.

set -euo pipefail

OIDC_ISSUER="https://token.actions.githubusercontent.com"
IDENTITY_REGEXP='^https://github\.com/projectbluefin/'

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

command -v jq >/dev/null 2>&1 || {
    echo "ERROR: 'jq' not found on PATH — install jq to verify image signatures" >&2
    exit 1
}

# Self-install fallback for hosts without cosign (e.g. CI runners that do
# not run sigstore/cosign-installer). Version matches the installer's
# default; the checksums are hardcoded so a tampered release asset is refused.
COSIGN_VERSION="v2.4.3"
if ! command -v cosign >/dev/null 2>&1; then
    case "$(uname -m)" in
        x86_64)  cosign_arch=amd64; cosign_sha=caaad125acef1cb81d58dcdc454a1e429d09a750d1e9e2b3ed1aed8964454708 ;;
        aarch64) cosign_arch=arm64; cosign_sha=bd0f9763bca54de88699c3656ade2f39c9a1c7a2916ff35601caf23a79be0629 ;;
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

echo "==> Verifying cosign signature: ${ref}" >&2
verified=$(cosign verify "${ref}" \
    --certificate-oidc-issuer="${OIDC_ISSUER}" \
    --certificate-identity-regexp="${IDENTITY_REGEXP}" \
    --output json)

digest=$(jq -r '.[0].critical.image["docker-manifest-digest"]' <<<"$verified")
if [[ -z "$digest" || "$digest" == "null" ]]; then
    echo "ERROR: cosign verify succeeded but returned no manifest digest for ${ref}" >&2
    exit 1
fi

repo="${ref%%[:@]*}"
echo "==> OK: ${repo}@${digest} signed by project CI" >&2
echo "${repo}@${digest}"
