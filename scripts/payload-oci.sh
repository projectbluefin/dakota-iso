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
#   existing archive. <ref> records the embedded payload's registry identity.
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

# payload_embed_oci_archive <archive> <squashfs-root>
# Keep the prepared bytes, not a containers-storage import that must be exported again.
payload_embed_oci_archive() {
    python3 - "$1" "$2" <<'PY'
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile

archive, root = sys.argv[1], Path(sys.argv[2])
relative = "usr/share/bootc-installer/oci"
layout = root / relative
if layout.exists() or layout.is_symlink():
    raise SystemExit("refusing stale embedded OCI layout")
layout.mkdir(parents=True)
with tarfile.open(archive, "r|*") as tar:
    for member in tar:
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isfile()):
            raise SystemExit(f"unsafe OCI archive member: {member.name}")
        dest = layout.joinpath(*path.parts)
        if member.isdir():
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("xb") as out, tar.extractfile(member) as src:
                shutil.copyfileobj(src, out)

def blob(descriptor):
    digest = descriptor["digest"]
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise SystemExit(f"unsupported OCI digest: {digest}")
    path = layout / "blobs/sha256" / digest.split(":")[1]
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    if path.stat().st_size != descriptor["size"] or h.hexdigest() != digest.split(":")[1]:
        raise SystemExit(f"OCI blob size/hash mismatch: {digest}")
    return path

if json.loads((layout / "oci-layout").read_text())["imageLayoutVersion"] != "1.0.0":
    raise SystemExit("unsupported OCI layout version")
index = json.loads((layout / "index.json").read_text())
if index["schemaVersion"] != 2 or len(index["manifests"]) != 1:
    raise SystemExit("prepared payload must contain exactly one platform manifest")
selected = index["manifests"][0]
manifest = json.loads(blob(selected).read_text())
if manifest["schemaVersion"] != 2 or not manifest["layers"]:
    raise SystemExit("invalid OCI platform manifest")
config = json.loads(blob(manifest["config"]).read_text())
for layer in manifest["layers"]:
    blob(layer)
recipe_path = root / "etc/bootc-installer/recipe.json"
recipe = json.loads(recipe_path.read_text())
image = recipe["local_imgref"]
target = recipe["imgref"]
if not image or not target or image.startswith(("containers-storage:", "oci:", "docker:")):
    raise SystemExit("recipe requires registry source and update target")
receipt = dict(schemaVersion=1, kind="dakota-live", image=image,
               platformManifestDigest=selected["digest"], configDigest=manifest["config"]["digest"],
               ociLayoutPath="/run/rootfsbase/" + relative, targetImgref=target,
               platform=config["os"] + "/" + config["architecture"])
(root / "etc/bootc-installer/media-input.json").write_text(json.dumps(receipt, indent=2) + "\n")
PY
}
