image-builder := "image-builder"
image-builder-dev := "image-builder-dev"

# Output directory for built ISOs and intermediate artifacts.
# Override with: just output_dir=/your/path iso-sd-boot dakota
output_dir := "output"

# Set to 1 to enable SSH in the live session for debugging.
# Example: just debug=1 output_dir=/tmp/out iso-sd-boot dakota
# Never use debug=1 for production/release ISOs.
debug := "0"

# Set to "dev" to pull the tuna-installer dev build (continuous-dev release).
# Useful for testing PRs on the dev branch before they land in a stable release.
# Example: just installer_channel=dev iso-sd-boot dakota
installer_channel := "stable"

# Squashfs compression preset:
#   fast    (default) — zstd level 3,  128K blocks — quick local builds/CI
#   release           — zstd level 15, 1M blocks   — ~20% smaller, ~5× slower
# Example: just compression=release iso-sd-boot dakota
compression := "fast"

# Helper: returns "--bootc-installer-payload-ref <ref>" or "" if no payload_ref file
_payload_ref_flag target:
    @if [ -f "{{target}}/payload_ref" ]; then echo "--bootc-installer-payload-ref $(cat '{{target}}/payload_ref' | tr -d '[:space:]')"; fi

container target:
    podman build --cap-add sys_admin --security-opt label=disable \
        --layers \
        --build-arg DEBUG={{debug}} \
        --build-arg INSTALLER_CHANNEL={{installer_channel}} \
        -t {{target}}-installer ./{{target}}

# Build the Debian-based ISO assembly container for the given target.
# This container has xorriso, mksquashfs, dosfstools, and mtools.
iso-builder target:
    podman build --security-opt label=disable -t {{target}}-iso-builder \
        -f ./{{target}}/Containerfile.builder ./{{target}}

# Build a systemd-boot UEFI live ISO for the given target.
#
# Uses a two-container approach:
#   1. localhost/<target>-installer — the live environment (3-stage Containerfile)
#   2. localhost/<target>-iso-builder — Debian ISO assembly tools (Containerfile.builder)
#
# The installer image is exported as a clean rootfs tarball via `podman export`,
# then the ISO builder creates:
#   - A FAT ESP image with systemd-boot + loader entries + kernel + initramfs
#   - A squashfs of the full live rootfs
#   - An ISO9660 image with El Torito EFI pointing to the FAT ESP
#
# Output: output/<target>-live.iso
iso-sd-boot target:
    #!/usr/bin/bash
    set -euo pipefail

    just debug={{debug}} installer_channel={{installer_channel}} container {{target}}
    mkdir -p {{output_dir}}
    OUTPUT_DIR=$(realpath "{{output_dir}}")

    # Build squashfs and export boot files using podman unshare (user namespace).
    #
    # The OCI payload (for offline install) is built into a staging directory on
    # /var (plenty of space) rather than inside the container's writable layer.
    # mksquashfs merges the image rootfs + staging dir, deduplicating blocks that
    # appear in both (most of the OCI layer data is identical to the live rootfs).
    # -processors 4 caps memory usage — default (all CPUs) OOMs on this machine.
    SQUASHFS="${OUTPUT_DIR}/{{target}}-rootfs.sfs"
    BOOT_TAR="${OUTPUT_DIR}/{{target}}-boot-files.tar"
    CS_STAGING="${OUTPUT_DIR}/{{target}}-cs-staging"
    trap "rm -rf '${SQUASHFS}' '${BOOT_TAR}' '${OUTPUT_DIR}/{{target}}-payload.oci.tar' '${CS_STAGING}'" EXIT
    echo "Building squashfs and boot tar from localhost/{{target}}-installer..."
    podman unshare bash -c "
        set -euo pipefail
        MOUNT=\$(podman image mount localhost/{{target}}-installer)
        PATH=/usr/sbin:/usr/bin:/home/linuxbrew/.linuxbrew/bin:\$PATH

        # Populate containers-storage in a staging dir on /var (197G free).
        # Two-step skopeo copy decouples source and destination storage configs.
        PAYLOAD_OCI='${OUTPUT_DIR}/{{target}}-payload.oci.tar'
        CS_STAGING='${CS_STAGING}'
        SQUASHFS_STORAGE=\"\${CS_STAGING}/var/lib/containers/storage\"
        LIVE_RUNROOT=\"\$(mktemp -d '${OUTPUT_DIR}'/live-runroot-XXXXXX)\"
        STORAGE_CONF=\"\$(mktemp '${OUTPUT_DIR}'/live-storage-XXXXXX.conf)\"
        mkdir -p \"\${SQUASHFS_STORAGE}\"
        printf '[storage]\ndriver = \"vfs\"\nrunroot = \"%s\"\ngraphroot = \"%s\"\n' \
            \"\${LIVE_RUNROOT}\" \"\${SQUASHFS_STORAGE}\" > \"\${STORAGE_CONF}\"

        echo 'Exporting Dakota OCI image to archive...'
        skopeo copy \
            containers-storage:ghcr.io/projectbluefin/dakota:latest \
            oci-archive:\${PAYLOAD_OCI}:ghcr.io/projectbluefin/dakota:latest

        echo 'Importing Dakota OCI image into squashfs containers-storage...'
        CONTAINERS_STORAGE_CONF=\"\${STORAGE_CONF}\" \
        skopeo copy \
            oci-archive:\${PAYLOAD_OCI}:ghcr.io/projectbluefin/dakota:latest \
            containers-storage:ghcr.io/projectbluefin/dakota:latest

        rm -f \"\${PAYLOAD_OCI}\" \"\${STORAGE_CONF}\"
        rm -rf \"\${LIVE_RUNROOT}\"

        # Build squashfs from the live rootfs + containers-storage staging dir.
        # mksquashfs merges multiple source dirs at root; dedup removes blocks
        # shared between the live env and the OCI layers (same base image).
        # -processors 4: caps parallelism to avoid OOM (32 workers exhausts RAM).
        # Compression preset: fast=zstd/3/128K (quick), release=zstd/15/1M (~20% smaller)
        SFS_LEVEL=3; SFS_BLOCK=131072
        [[ '{{compression}}' == 'release' ]] && { SFS_LEVEL=15; SFS_BLOCK=1048576; }
        mksquashfs \"\$MOUNT\" \"\${CS_STAGING}\" '${SQUASHFS}' \
            -noappend -comp zstd -Xcompression-level \${SFS_LEVEL} -b \${SFS_BLOCK} \
            -processors 4 \
            -e proc -e sys -e dev -e run -e tmp
        # Export only boot files needed for ESP assembly
        tar -C \"\$MOUNT\" \
            -cf '${BOOT_TAR}' \
            ./usr/lib/modules \
            ./usr/lib/systemd/boot/efi
        podman image umount localhost/{{target}}-installer
    "
    rm -rf "${CS_STAGING}"

    # Run build-iso.sh directly on the host — no container needed.
    # All required tools (xorriso, mkfs.fat, mtools) are present.
    # TMPDIR is redirected to OUTPUT_DIR so mktemp avoids the full /tmp tmpfs.
    # just always runs recipes from the justfile directory, so relative path works.
    TMPDIR="${OUTPUT_DIR}" \
    PATH="/usr/sbin:/usr/bin:/home/linuxbrew/.linuxbrew/bin:${PATH}" \
        bash "{{target}}/src/build-iso.sh" "${BOOT_TAR}" "${SQUASHFS}" "${OUTPUT_DIR}/{{target}}-live.iso"

    echo "ISO ready: ${OUTPUT_DIR}/{{target}}-live.iso"

iso target:
    {{image-builder}} build --bootc-ref localhost/{{target}}-installer --bootc-default-fs ext4 `just _payload_ref_flag {{target}}` bootc-generic-iso

# We need some patches that are not yet available upstream, so let's build a custom version.
build-image-builder:
    #!/bin/bash
    set -euo pipefail
    if [ -d image-builder-cli ]; then
        cd image-builder-cli
        git fetch origin
        git reset --hard cf20ed6a417c5e4dd195b34967cd2e4d5dc7272f
    else
        git clone https://github.com/osbuild/image-builder-cli.git
        cd image-builder-cli
        git reset --hard cf20ed6a417c5e4dd195b34967cd2e4d5dc7272f
    fi
    # Apply fix for /dev mount failure in privileged containers
    sed -i '/mount.*devtmpfs.*devtmpfs.*\/dev/,/return err/ s/return err/log.Printf("check: failed to mount \/dev: %v", err)/' pkg/setup/setup.go
    # if go is not in PATH, install via brew and use the full brew path
    if ! command -v go &> /dev/null; then
        if [ -d "/home/linuxbrew/.linuxbrew" ]; then
            GO_BIN="/home/linuxbrew/.linuxbrew/bin/go"
        else
            echo "go not found in PATH and /home/linuxbrew/.linuxbrew not found"
            exit 1
        fi
    else
        GO_BIN="go"
    fi
    $GO_BIN mod tidy
    $GO_BIN mod edit -replace github.com/osbuild/images=github.com/ondrejbudai/images@bootc-generic-iso-dev
    $GO_BIN get github.com/osbuild/blueprint@v1.22.0
    # GOPROXY=direct so we always fetch the latest bootc-generic-iso-dev branch
    GOPROXY=direct $GO_BIN mod tidy
    podman build --security-opt label=disable --security-opt seccomp=unconfined -t {{image-builder-dev}} .

iso-in-container target:
    #!/bin/bash
    set -euo pipefail
    just container {{target}}
    mkdir -p /var/home/james/dakota-iso-output

    PAYLOAD_FLAG="$(just _payload_ref_flag {{target}})"

    # Generate the osbuild manifest
    echo "Manifest generation step"
    podman run --rm --privileged \
        -v /var/lib/containers/storage:/var/lib/containers/storage \
        --entrypoint /usr/bin/image-builder \
        {{image-builder-dev}} \
        manifest --bootc-ref localhost/{{target}}-installer --bootc-default-fs ext4 $PAYLOAD_FLAG bootc-generic-iso \
        > output/manifest.json

    # Patch manifest to add remove-signatures to org.osbuild.skopeo stages
    echo "Patching manifest to remove signatures from skopeo stages"
    jq '(.pipelines[] | .stages[]? | select(.type == "org.osbuild.skopeo") | .options) += {"remove-signatures": true}' \
        output/manifest.json > output/manifest-patched.json

    echo "Image building step"
    podman run --rm --privileged \
        -v /var/lib/containers/storage:/var/lib/containers/storage \
        -v ./output:/output:Z \
        -i \
        --entrypoint /usr/bin/osbuild \
        {{image-builder-dev}} \
        --output-directory /output --export bootiso - < output/manifest-patched.json

run-iso target:
    #!/usr/bin/bash
    set -eoux pipefail
    image_name="bootiso/install.iso"
    if [ ! -f "output/${image_name}" ]; then
         image_name=$(ls output/bootc-{{target}}*.iso 2>/dev/null | head -n 1 | xargs basename)
    fi



    # Determine which port to use
    port=8006;
    while grep -q :${port} <<< $(ss -tunalp); do
        port=$(( port + 1 ))
    done
    echo "Using Port: ${port}"
    echo "Connect to http://localhost:${port}"
    run_args=()
    run_args+=(--rm --privileged)
    run_args+=(--pull=always)
    run_args+=(--publish "127.0.0.1:${port}:8006")
    run_args+=(--env "CPU_CORES=4")
    run_args+=(--env "RAM_SIZE=8G")
    run_args+=(--env "DISK_SIZE=64G")
    run_args+=(--env "BOOT_MODE=windows_secure")
    run_args+=(--env "TPM=Y")
    run_args+=(--env "GPU=Y")
    run_args+=(--device=/dev/kvm)
    run_args+=(--volume "${PWD}/output/${image_name}":"/boot.iso")
    run_args+=(ghcr.io/qemus/qemu)
    xdg-open http://localhost:${port} &
    podman run "${run_args[@]}"
    echo "Connect to http://localhost:${port}"

dev target:
    just build-image-builder
    just iso-in-container {{target}}
    just run-iso {{target}}

# Boot a built ISO in QEMU via UEFI (OVMF) with serial console output on stdout.
#
# Validation target: watch serial output for "Started GNOME Display Manager"
# or "gnome-shell" to confirm the live environment reached the desktop.
#
# Requires: qemu-system-x86_64, KVM, OVMF firmware (edk2-ovmf / ovmf package)
# Exit: Ctrl-A then X
boot-iso-serial target:
    #!/usr/bin/bash
    set -euo pipefail
    ISO=$(ls \
        {{output_dir}}/{{target}}-live.iso \
        output/bootiso/install.iso \
        output/bootc-{{target}}*.iso \
        2>/dev/null | head -1 || true)
    if [[ -z "$ISO" ]]; then
        echo "No ISO found for '{{target}}' — run: just iso-sd-boot {{target}}" >&2
        exit 1
    fi

    # Locate OVMF firmware (path varies by distro)
    OVMF_CODE=""
    for f in \
        /usr/share/OVMF/OVMF_CODE.fd \
        /usr/share/edk2/ovmf/OVMF_CODE.fd \
        /usr/share/edk2-ovmf/x64/OVMF_CODE.fd \
        /usr/share/ovmf/OVMF.fd; do
        [[ -f "$f" ]] && { OVMF_CODE="$f"; break; }
    done
    OVMF_VARS_SRC=""
    for f in \
        /usr/share/OVMF/OVMF_VARS.fd \
        /usr/share/edk2/ovmf/OVMF_VARS.fd \
        /usr/share/edk2-ovmf/x64/OVMF_VARS.fd; do
        [[ -f "$f" ]] && { OVMF_VARS_SRC="$f"; break; }
    done
    if [[ -z "$OVMF_CODE" ]]; then
        echo "OVMF firmware not found — install edk2-ovmf or ovmf" >&2
        exit 1
    fi

    # OVMF_VARS must be writable (UEFI saves boot state to it)
    OVMF_VARS=$(mktemp /tmp/OVMF_VARS.XXXXXX.fd)
    [[ -n "$OVMF_VARS_SRC" ]] && cp "${OVMF_VARS_SRC}" "${OVMF_VARS}"
    trap "rm -f ${OVMF_VARS}" EXIT

    echo "Booting ${ISO} via UEFI — serial console below (Ctrl-A X to quit)"
    echo "SSH available on localhost:2222 (user: liveuser, password: live) if built with debug=1"
    sudo /usr/libexec/qemu-kvm \
        -machine q35 \
        -m 4096 \
        -accel kvm \
        -cpu host \
        -smp 4 \
        -drive if=pflash,format=raw,readonly=on,file="${OVMF_CODE}" \
        -drive if=pflash,format=raw,file="${OVMF_VARS}" \
        -drive if=none,id=live-disk,file="${ISO}",media=cdrom,format=raw,readonly=on \
        -device virtio-scsi-pci,id=scsi \
        -device scsi-cd,drive=live-disk \
        -net nic,model=virtio -net user,hostfwd=tcp::2222-:22 \
        -serial mon:stdio \
        -display none \
        -no-reboot
