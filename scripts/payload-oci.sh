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

# payload_build_final <from-image> <squash:true|false> <final-var> <base-var>
#   Builds the payload image (injecting the bootc install config) and stores
#   the resulting tag in <final-var>. Squash mode squashes to one layer and
#   records the squashed layer's diff_id as the ostree.final-diffid label and
#   annotation. The pre-squash base tag (if any) goes in <base-var> for the
#   caller to remove after embedding/saving; intermediate images are never
#   left behind by this helper itself.
payload_build_final() {
    local from="$1" squash="$2" _finalvar="$3" _basevar="$4"
    local base_tag="localhost/iso-payload-work:base-$$"
    local final_tag="localhost/iso-payload-work:final-$$"
    local diffid=""

    if [[ "${squash}" != "true" ]]; then
        payload_build "${base_tag}" "${from}" true
        printf -v "${_finalvar}" '%s' "${base_tag}"
        printf -v "${_basevar}" '%s' ""
        return 0
    fi

    payload_build "${base_tag}" "${from}" true --squash-all

    # Read the squashed layer's diff_id from local storage.
    diffid="$(podman image inspect --format '{{index .RootFS.Layers 0}}' "${base_tag}" 2>/dev/null || true)"
    if [[ -n "${diffid}" ]]; then
        echo ">>> [payload] updating ostree.final-diffid to ${diffid}"
        payload_build "${final_tag}" "${base_tag}" false --squash-all \
            --label "ostree.final-diffid=${diffid}" \
            --annotation "ostree.final-diffid=${diffid}"
        printf -v "${_finalvar}" '%s' "${final_tag}"
        printf -v "${_basevar}" '%s' "${base_tag}"
        return 0
    fi
    printf -v "${_finalvar}" '%s' "${base_tag}"
    printf -v "${_basevar}" '%s' ""
}

# payload_to_oci_archive <from-image> <archive> <ref> <squash:true|false>
#   The whole payload preparation: inject the bootc install config, then
#   - squash=true:  squash to one layer and record the squashed layer's diff_id
#                   as the ostree.final-diffid label and annotation;
#   - squash=false: keep every layer, so ostree commit objects survive.
#   Leaves <archive> holding the result; removes its intermediate images.
payload_to_oci_archive() {
    local from="$1" archive="$2" ref="$3" squash="$4"
    local tag="" base=""
    payload_build_final "${from}" "${squash}" tag base
    payload_save "${tag}" "${archive}" "${ref}"
    podman rmi "${tag}" >/dev/null 2>&1 || true
    if [[ -n "${base}" ]]; then
        podman rmi "${base}" >/dev/null 2>&1 || true
    fi
}

# payload_export_store <src-tag> <staging-dir> <identity-ref>
#   Copies the (already built) payload image into a fresh containers-storage
#   graphroot at <staging-dir> under the registry identity, using the vfs
#   driver so the store is plain unpacked files. Prints
#   "<manifest-digest> <config-digest> <os/arch>" to stdout for the receipt;
#   all progress goes to stderr. Podman (not skopeo) drives the copy: skopeo
#   initializes a custom store through a mount namespace, which restricted
#   hosts deny, while podman brings its own working namespace setup. The
#   manifest digest is content-defined, so it matches what the installer's
#   `skopeo inspect` resolves from the same store on the live system.
#   Caller owns tag cleanup.
payload_export_store() {
    local tag="$1" staging="$2" identity="$3"
    # Podman caps runroot at 50 characters (unix socket paths); a staging dir
    # under a repo checkout exceeds that, and TMPDIR may be long too, so the
    # runroot lives in a fixed short scratch dir that is removed before
    # returning. It holds only transient per-command state (blobs go to the
    # graphroot staging dir), so /tmp size is not a concern. Every step fails
    # loudly: callers invoke this in an `||` guard where `set -e` is
    # suppressed, so a bare failure would print an empty receipt instead of
    # aborting.
    local runroot
    runroot="$(mktemp -d /tmp/pstore-run.XXXXXX)" || return 1
    rm -rf "${staging}"
    mkdir -p "${staging}"
    local -a store=(podman --root "${staging}" --runroot "${runroot}" --storage-driver vfs)
    local digest="" configdigest="" platform="" manifest_json=""
    # The tag lives in the default store; the custom store starts empty, so a
    # `pull containers-storage:<tag>` against the custom store cannot resolve
    # it. Stream it across instead: any `save` failure truncates the tar,
    # which fails the `load`, so the transfer is fail-closed either way.
    podman save "${tag}" | "${store[@]}" load -q >&2 \
        || { echo "ERROR: payload store transfer failed for ${tag}" >&2; rm -rf "${runroot}"; return 1; }
    "${store[@]}" tag "${tag}" "${identity}" >&2 \
        || { echo "ERROR: payload store tag failed for ${identity}" >&2; rm -rf "${runroot}"; return 1; }
    digest="$("${store[@]}" image inspect --format '{{.Digest}}' "${identity}")" \
        || { echo "ERROR: payload store digest inspect failed for ${identity}" >&2; rm -rf "${runroot}"; return 1; }
    platform="$("${store[@]}" image inspect --format '{{.Os}}/{{.Architecture}}' "${identity}")" \
        || { echo "ERROR: payload store platform inspect failed for ${identity}" >&2; rm -rf "${runroot}"; return 1; }
    manifest_json="$("${store[@]}" save "${identity}" | tar -xO -f - manifest.json)" \
        || { echo "ERROR: payload store save failed for ${identity}" >&2; rm -rf "${runroot}"; return 1; }
    configdigest="sha256:$(printf '%s' "${manifest_json}" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["Config"].removesuffix(".json"))')" \
        || { echo "ERROR: payload store config digest parse failed for ${identity}" >&2; rm -rf "${runroot}"; return 1; }
    [[ -n "${digest}" && -n "${configdigest#sha256:}" && -n "${platform}" ]] \
        || { echo "ERROR: payload store export produced empty receipt values for ${identity}" >&2; rm -rf "${runroot}"; return 1; }
    # Containers/storage opens lockfiles inside the graphroot even for reads,
    # so a read-only bind of the store (which the live session needs for the
    # offline preflight) would refuse every operation. Redirect every
    # lockfile the export left behind to a runtime-writable dir; image
    # content (what the receipt digest covers) is untouched. The live session
    # creates the target dir via tmpfiles before the store is bound.
    local lock rel target
    while IFS= read -r -d '' lock; do
        rel="${lock#"${staging}"/}"
        target="/run/bootc-installer-media-locks/${rel//\//_}"
        ln -sfn "${target}" "${lock}" || {
            echo "ERROR: payload store lock redirect failed for ${lock}" >&2
            rm -rf "${runroot}"
            return 1
        }
    done < <(find "${staging}" -name '*.lock' -type f -print0) || {
        echo "ERROR: payload store lock inventory failed" >&2
        rm -rf "${runroot}"
        return 1
    }
    rm -rf "${runroot}"
    printf '%s %s %s\n' "${digest}" "${configdigest}" "${platform}"
}

# payload_bind_store <staging-dir> <squashfs-root> <identity-ref> <target-ref> <manifest-digest> <config-digest> <platform>
#   Moves the exported store into the squashfs root at
#   var/lib/containers/storage and writes the schema-v1 media-input.json
#   binding it (containersStoragePath form). Unpacked store files share
#   squashfs blocks with the live root instead of duplicating it as an
#   opaque blob. Refuses a stale store.
payload_bind_store() {
    local staging="$1" root="$2" identity="$3" target="$4" digest="$5" configdigest="$6" platform="$7"
    python3 - "$staging" "$root" "$identity" "$target" "$digest" "$configdigest" "$platform" <<'PY'
import json
import shutil
import sys
from pathlib import Path

staging, root = Path(sys.argv[1]), Path(sys.argv[2])
identity, target, digest, configdigest, platform = sys.argv[3:8]
if not digest or not configdigest or not platform:
    raise SystemExit("refusing to bind store with empty receipt values")
relative = "var/lib/containers/storage"
store = root / relative
if store.is_symlink() or store.is_file():
    raise SystemExit("refusing stale embedded containers-storage")
if store.is_dir():
    if any(store.iterdir()):
        raise SystemExit("refusing stale embedded containers-storage")
    # The live root ships an empty /var/lib/containers/storage placeholder
    # (configure-live.sh mkdirs it alongside storage.conf); take it over.
    # Anything already populated is a genuine leftover and still refused.
    store.rmdir()
shutil.copytree(staging, store, symlinks=True)
recipe = json.loads((root / "etc/bootc-installer/recipe.json").read_text())
image = recipe["local_imgref"]
if not image or image.startswith(("containers-storage:", "oci:", "docker:")):
    raise SystemExit("recipe requires registry source identity")
if image != identity:
    raise SystemExit(f"recipe source {image} does not match embedded store identity {identity}")
receipt = dict(schemaVersion=1, kind="dakota-live", image=image,
               platformManifestDigest=digest, configDigest=configdigest,
               containersStoragePath="/var/lib/containers/storage",
               targetImgref=target if target else recipe["imgref"],
               platform=platform)
(root / "etc/bootc-installer/media-input.json").write_text(json.dumps(receipt, indent=2) + "\n")
PY
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
               ociLayoutPath="/run/bootcmedia/" + relative, targetImgref=target,
               platform=config["os"] + "/" + config["architecture"])
(root / "etc/bootc-installer/media-input.json").write_text(json.dumps(receipt, indent=2) + "\n")
PY
}
