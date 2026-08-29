#!/usr/bin/bash
# Verify the Sigstore (cosign) keyless signature of a registry image before
# it is consumed by the ISO build, and print the digest-pinned reference so
# callers pull exactly what was verified (no tag-vs-verify TOCTOU window).
#
# The images we package into the ISO (live base image, offline payload) are
# built by projectbluefin/ublue-os GitHub Actions and signed keylessly.
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
IDENTITY_REGEXP='^https://github\.com/(projectbluefin|ublue-os)/'

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <image-ref>" >&2
    exit 2
fi

ref="$1"

# Only projectbluefin/ublue-os GHCR images are signed by our CI.
# Anything else (localhost refs, other registries) is out of scope here.
case "$ref" in
    ghcr.io/projectbluefin/*|ghcr.io/ublue-os/*) ;;
    *)
        echo "ERROR: refusing to verify non-project image '${ref}' — no trusted signer identity" >&2
        exit 1
        ;;
esac

for cmd in cosign jq; do
    command -v "$cmd" >/dev/null 2>&1 || {
        echo "ERROR: '$cmd' not found on PATH — install cosign and jq to verify image signatures" >&2
        exit 1
    }
done

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
