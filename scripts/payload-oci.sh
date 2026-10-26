# shellcheck shell=bash
# payload-oci.sh — prepare the offline-install payload image with podman only.
#
# Sourced by scripts/iso-sd-boot.sh and scripts/build-live-squashfs.sh. Both
# turn the payload image into an oci-archive with the bootc install config
# written into it, squashed to one layer for composefs variants and left
# layered for the others.
#
# The payload is built with `podman build` from a FROM-only Containerfile. A
# build inherits the base image config (Entrypoint, Cmd, Env, Labels) instead
# of rewriting it, which `podman create --entrypoint ... && podman commit` does
# and which breaks `bootc install` (see docs/build.md). Callers must not
# replace this with create/commit.

# The only file the ISO build may inject into the payload: the installed system
# inherits payload content verbatim.
PAYLOAD_ROOT_MOUNT_DEST="/usr/lib/bootc/install/00-defaults.toml"

# payload_build <tag> <from-image> <inject:true|false> [podman build flags...]
#   Builds <tag> as `FROM <from-image>`, with the bootc root-mount-spec copied
#   to PAYLOAD_ROOT_MOUNT_DEST when <inject> is true. Extra flags go straight to
#   `podman build` (--squash-all, --label, --annotation). Never pulls. Always
#   writes OCI format: the Docker format (e.g. BUILDAH_FORMAT=docker) drops
#   --annotation, and ostree.final-diffid must survive as an annotation.
payload_build() {
    local tag="$1" from="$2" inject="$3"
    shift 3
    local ctx rc=0
    ctx="$(mktemp -d "${TMPDIR:-/tmp}/payload-build.XXXXXX")"
    printf 'FROM %s\n' "${from}" > "${ctx}/Containerfile"
    if [[ "${inject}" == "true" ]]; then
        printf '[install]\nroot-mount-spec = "LABEL=root"\n' > "${ctx}/bootc-root-mount.toml"
        printf 'COPY bootc-root-mount.toml %s\n' "${PAYLOAD_ROOT_MOUNT_DEST}" >> "${ctx}/Containerfile"
    fi
    podman build --pull=never --format oci "$@" -f "${ctx}/Containerfile" -t "${tag}" "${ctx}" || rc=$?
    rm -rf "${ctx}"
    return "${rc}"
}

# payload_save <tag> <archive> <ref>
#   Writes local image <tag> to oci-archive:<archive>:<ref>, replacing any
#   existing archive. <ref> is the name the embedded store and the installer
#   recipe look the payload up by.
payload_save() {
    local tag="$1" archive="$2" ref="$3"
    rm -f "${archive}"
    podman push "${tag}" "oci-archive:${archive}:${ref}"
}

# payload_to_oci_archive <from-image> <archive> <ref> <squash:true|false>
#   The whole payload preparation: inject the bootc install config, then
#   - squash=true:  squash to one layer and record the squashed layer's diff_id
#                   as the ostree.final-diffid label and annotation;
#   - squash=false: keep every layer, so ostree commit objects survive.
#   Leaves <archive> holding the result; removes its intermediate images.
payload_to_oci_archive() {
    local from="$1" archive="$2" ref="$3" squash="$4"
    local base_tag="localhost/iso-payload-work:base-$$"
    local final_tag="localhost/iso-payload-work:final-$$"
    local diffid=""

    if [[ "${squash}" != "true" ]]; then
        payload_build "${base_tag}" "${from}" true
        payload_save "${base_tag}" "${archive}" "${ref}"
        podman rmi "${base_tag}" >/dev/null 2>&1 || true
        return 0
    fi

    payload_build "${base_tag}" "${from}" true --squash-all

    # Read the squashed layer's diff_id from local storage: pushing to the
    # archive gzips the multi-GB layer, so push only the final image once.
    diffid="$(podman image inspect --format '{{index .RootFS.Layers 0}}' "${base_tag}" 2>/dev/null || true)"
    if [[ -n "${diffid}" ]]; then
        echo ">>> [payload] updating ostree.final-diffid to ${diffid}"
        payload_build "${final_tag}" "${base_tag}" false --squash-all \
            --label "ostree.final-diffid=${diffid}" \
            --annotation "ostree.final-diffid=${diffid}"
        payload_save "${final_tag}" "${archive}" "${ref}"
        podman rmi "${final_tag}" >/dev/null 2>&1 || true
    else
        payload_save "${base_tag}" "${archive}" "${ref}"
    fi
    podman rmi "${base_tag}" >/dev/null 2>&1 || true
}
