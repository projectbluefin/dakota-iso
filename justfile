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

# LUKS passphrase used by luks-install for reproducing issue #270.
# Example: just luks-passphrase=MySecret luks-install dakota
luks-passphrase := "testpassphrase"

# Squashfs compression preset:
#   fast    (default) — zstd level 3,  128K blocks — quick local builds/CI
#   release           — zstd level 15, 1M blocks   — ~20% smaller, ~5× slower
# Example: just compression=release iso-sd-boot dakota
compression := "fast"

# Build the ISO in the background, detached from the terminal session.
# Logs are written to {{output_dir}}/build.log and tailed live.
# Safe to close the terminal — the build will continue running.
# Usage: just build-bg dakota
#        just debug=1 installer_channel=dev build-bg dakota
build-bg target:
    #!/usr/bin/bash
    set -euo pipefail
    mkdir -p {{output_dir}}
    LOG=$(realpath {{output_dir}})/build.log
    echo "Starting background build → ${LOG}"
    setsid sudo just \
        debug={{debug}} \
        installer_channel={{installer_channel}} \
        output_dir={{output_dir}} \
        compression={{compression}} \
        iso-sd-boot {{target}} \
        > "${LOG}" 2>&1 &
    disown $!
    echo "Build PID $! — tailing log (Ctrl-C is safe, build continues)"
    tail -f "${LOG}"

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

    echo "=== Disk space before container build ==="
    df -h /
    podman images --format "table {{{{.Repository}}}}\t{{{{.Tag}}}}\t{{{{.Size}}}}" 2>/dev/null || true

    just debug={{debug}} installer_channel={{installer_channel}} container {{target}}

    echo "=== Disk space after container build ==="
    df -h /
    podman images --format "table {{{{.Repository}}}}\t{{{{.Tag}}}}\t{{{{.Size}}}}" 2>/dev/null || true

    # Aggressively free space: remove dangling images and known disposable images.
    # The only image we need is localhost/{{target}}-installer.
    podman rmi debian:sid 2>/dev/null || true
    podman image prune -f 2>/dev/null || true
    echo "=== Disk space after intermediate cleanup ==="
    df -h /

    mkdir -p {{output_dir}}
    OUTPUT_DIR=$(realpath "{{output_dir}}")

    # podman unshare enters the user namespace so rootless podman's sub-uid mapped
    # files are accessible/removable.  When running as root (e.g. CI with sudo),
    # there is no user namespace to enter — run commands directly instead.
    if [[ $(id -u) -eq 0 ]]; then
        _ns()    { bash -c "$1"; }
        _ns_rm() { rm -rf "$@"; }
    else
        _ns()    { podman unshare bash -c "$1"; }
        _ns_rm() { podman unshare rm -rf "$@"; }
    fi

    # The OCI payload (for offline install) is built into a staging directory on
    # /var (plenty of space) rather than inside the container's writable layer.
    # mksquashfs merges the image rootfs + staging dir, deduplicating blocks that
    # appear in both (most of the OCI layer data is identical to the live rootfs).
    # -processors 4 caps memory usage — default (all CPUs) OOMs on this machine.
    SQUASHFS="${OUTPUT_DIR}/{{target}}-rootfs.sfs"
    BOOT_TAR="${OUTPUT_DIR}/{{target}}-boot-files.tar"
    CS_STAGING="${CS_STAGING_OVERRIDE:-${OUTPUT_DIR}/{{target}}-cs-staging}"
    SQUASHFS_ROOT="${OUTPUT_DIR}/{{target}}-sfs-root"
    # CS_STAGING and SQUASHFS_ROOT contain sub-uid owned files; must be removed inside the namespace.
    trap "rm -f '${SQUASHFS}' '${BOOT_TAR}' '${OUTPUT_DIR}/{{target}}-payload.oci.tar'; _ns_rm '${CS_STAGING}' '${SQUASHFS_ROOT}' 2>/dev/null || true" EXIT
    echo "=== Disk space before squashfs assembly ==="
    df -h /
    echo "Building squashfs and boot tar from localhost/{{target}}-installer..."
    _ns "
        set -euo pipefail
        echo '=== Disk space inside _ns block ==='
        df -h /
        MOUNT=\$(podman image mount localhost/{{target}}-installer)
        PATH=/usr/sbin:/usr/bin:/home/linuxbrew/.linuxbrew/bin:\$PATH

        # Populate containers-storage in a staging dir on /var (197G free).
        # Two-step skopeo copy decouples source and destination storage configs.
        PAYLOAD_OCI='${OUTPUT_DIR}/{{target}}-payload.oci.tar'
        CS_STAGING='${CS_STAGING}'
        SQUASHFS_ROOT='${SQUASHFS_ROOT}'
        SQUASHFS_STORAGE=\"\${CS_STAGING}/var/lib/containers/storage\"
        # Storage conf for skopeo running inside the installer container.
        # Paths are container-relative: /vfs-storage is the bind-mounted SQUASHFS_STORAGE.
        STORAGE_CONF=\"\$(mktemp '${OUTPUT_DIR}'/live-storage-XXXXXX.conf)\"
        mkdir -p \"\${SQUASHFS_STORAGE}\"
        printf '[storage]\ndriver = \"vfs\"\nrunroot = \"/tmp/cs-runroot\"\ngraphroot = \"/vfs-storage\"\n' \
            > \"\${STORAGE_CONF}\"

        echo 'Exporting Dakota OCI image to archive...'
        skopeo copy \
            containers-storage:ghcr.io/projectbluefin/dakota:latest \
            oci-archive:\${PAYLOAD_OCI}:ghcr.io/projectbluefin/dakota:latest

        echo '=== Disk space after OCI export ==='
        df -h /
        ls -lh \${PAYLOAD_OCI} 2>/dev/null || true

        # Remove base image from podman storage — no longer needed after OCI export.
        # The OCI archive has all the data; the base image just takes up space.
        podman rmi ghcr.io/projectbluefin/dakota:latest || true
        podman image prune -f 2>/dev/null || true
        echo '=== Disk space after removing base image ==='
        df -h /

        echo 'Importing Dakota OCI image into squashfs containers-storage...'
        echo '=== Disk space before VFS import ==='
        df -h /
        # Run skopeo from inside the installer image so the VFS tar-split metadata is
        # written in a format the live ISO can read.  The build host links a newer
        # containers/storage that emits a binary tar-split format; the installer image
        # carries the same containers/storage version as the live ISO and writes the
        # JSON-based format it expects.
        podman run --rm \
            --privileged \
            -v \"\${PAYLOAD_OCI}:/payload.oci.tar:ro\" \
            -v \"\${SQUASHFS_STORAGE}:/vfs-storage\" \
            -v \"\${STORAGE_CONF}:/tmp/st.conf:ro\" \
            localhost/{{target}}-installer \
            sh -c 'mkdir -p /tmp/cs-runroot /var/tmp && CONTAINERS_STORAGE_CONF=/tmp/st.conf skopeo copy oci-archive:/payload.oci.tar:ghcr.io/projectbluefin/dakota:latest containers-storage:ghcr.io/projectbluefin/dakota:latest'


        rm -f \"\${PAYLOAD_OCI}\" \"\${STORAGE_CONF}\"

        echo '=== Disk space after VFS import ==='
        df -h /
        du -sh \${CS_STAGING} 2>/dev/null || true

        # mksquashfs adds each source directory as a named subdirectory — it does
        # NOT union-merge multiple sources into root. To get the VFS storage at
        # /var/lib/containers/storage/ in the squashfs (not at /dakota-cs-staging/...),
        # we build a single unified source tree using XFS reflinks (instant, ~zero space).
        echo 'Building unified squashfs source tree...'
        mkdir -p \"\${SQUASHFS_ROOT}\"
        cp -a --reflink=auto \"\${MOUNT}/.\" \"\${SQUASHFS_ROOT}/\" 2>/dev/null || \
            cp -a \"\${MOUNT}/.\" \"\${SQUASHFS_ROOT}/\"
        # Merge VFS storage into the correct path within the unified source tree.
        mkdir -p \"\${SQUASHFS_ROOT}/var/lib/containers/storage\"
        cp -a \"\${CS_STAGING}/var/lib/containers/storage/.\" \
            \"\${SQUASHFS_ROOT}/var/lib/containers/storage/\"
        rm -rf \"\${CS_STAGING}\"

        # Build squashfs from the unified source tree.
        # dedup removes blocks shared between live rootfs and OCI layers (same base image).
        # -processors 4: caps parallelism to avoid OOM (32 workers exhausts RAM).
        # Compression preset: fast=zstd/3/128K (quick), release=zstd/15/1M (~20% smaller)
        SFS_LEVEL=3; SFS_BLOCK=131072
        [[ '{{compression}}' == 'release' ]] && { SFS_LEVEL=15; SFS_BLOCK=1048576; }
        mksquashfs \"\${SQUASHFS_ROOT}\" '${SQUASHFS}' \
            -noappend -comp zstd -Xcompression-level \${SFS_LEVEL} -b \${SFS_BLOCK} \
            -processors 4 \
            -e proc -e sys -e dev -e run -e tmp

        # Clean up staging dirs inside unshare — vfs files are owned by sub-uids
        # and cannot be removed by the real user outside the user namespace.
        rm -rf \"\${SQUASHFS_ROOT}\"

        # Export only boot files needed for ESP assembly
        tar -C \"\$MOUNT\" \
            -cf '${BOOT_TAR}' \
            ./usr/lib/modules \
            ./usr/lib/systemd/boot/efi
        podman image umount localhost/{{target}}-installer
    "

    echo "=== Disk space after squashfs, before ISO assembly ==="
    df -h /
    du -sh "${SQUASHFS}" "${BOOT_TAR}" 2>/dev/null || true

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

# Run chunkah content-based layer splitting against a source image and push to a destination.
#
# Pulls the source image, runs chunkah to produce a zstd:chunked OCI archive,
# loads the result into podman, and pushes it to the destination ref.
#
# Usage:
#   just chunkify ghcr.io/projectbluefin/dakota:latest 192.168.122.1:5000/dakota:chunked
#   just chunkify ghcr.io/projectbluefin/dakota:latest ghcr.io/projectbluefin/dakota:chunked
chunkify src dst:
    #!/usr/bin/bash
    set -euo pipefail

    echo "==> Pulling source image: {{src}}"
    podman pull {{src}}

    echo "==> Running chunkah on {{src}}..."
    # Use /var (not /tmp) — the OCI archive can exceed the tmpfs size for large images
    CHUNK_OUT=$(mktemp -d --tmpdir=/var/tmp)
    trap 'rm -rf "${CHUNK_OUT}"' EXIT

    podman run --rm \
        --security-opt label=disable \
        --entrypoint="" \
        -v "${CHUNK_OUT}:/run/out:Z" \
        --mount "type=image,source={{src}},target=/chunkah" \
        ghcr.io/tuna-os/chunkah:latest \
        sh -c 'chunkah build > /run/out/out.ociarchive'

    echo "==> Loading rechunked archive..."
    LOADED_ID=$(podman load --input "${CHUNK_OUT}/out.ociarchive" | awk '/Loaded image/{print $NF}')
    if [[ -z "${LOADED_ID}" ]]; then
        echo "ERROR: podman load produced no image ID; the OCI archive may be corrupt or disk full" >&2
        exit 1
    fi

    echo "==> Tagging and pushing to {{dst}}..."
    podman tag "${LOADED_ID}" "{{dst}}"
    podman push --tls-verify=false "{{dst}}"

    echo "==> Done: {{dst}}"

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
    QEMU=$(command -v /usr/libexec/qemu-kvm /usr/bin/qemu-kvm \
               /usr/bin/qemu-system-x86_64 2>/dev/null | head -1)
    [[ -z "$QEMU" ]] && { echo "qemu-kvm / qemu-system-x86_64 not found" >&2; exit 1; }
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
    sudo "$QEMU" \
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

# Boot a built ISO in libvirt with UEFI, a target install disk, and SSH via
# the default libvirt network.  Prints the SSH command once the guest gets a
# DHCP lease.
#
# Requires: libvirt, virt-install, OVMF firmware
# Cleanup: sudo virsh destroy dakota-debug && sudo virsh undefine dakota-debug --nvram
boot-libvirt-debug target:
    #!/usr/bin/bash
    set -euo pipefail

    VM_NAME="dakota-debug"
    VM_RAM=8192
    VM_CPUS=4
    DISK_SIZE=64

    ISO=$(ls \
        {{output_dir}}/{{target}}-live.iso \
        output/bootiso/install.iso \
        output/bootc-{{target}}*.iso \
        2>/dev/null | head -1 || true)
    if [[ -z "$ISO" ]]; then
        echo "No ISO found for '{{target}}' — run: just debug=1 iso-sd-boot {{target}}" >&2
        exit 1
    fi

    # Locate OVMF firmware
    OVMF_CODE=""
    for f in \
        /usr/share/OVMF/OVMF_CODE.fd \
        /usr/share/edk2/ovmf/OVMF_CODE.fd \
        /usr/share/edk2-ovmf/x64/OVMF_CODE.fd \
        /usr/share/ovmf/OVMF.fd; do
        [[ -f "$f" ]] && { OVMF_CODE="$f"; break; }
    done
    OVMF_VARS=""
    for f in \
        /usr/share/OVMF/OVMF_VARS.fd \
        /usr/share/edk2/ovmf/OVMF_VARS.fd \
        /usr/share/edk2-ovmf/x64/OVMF_VARS.fd; do
        [[ -f "$f" ]] && { OVMF_VARS="$f"; break; }
    done
    if [[ -z "$OVMF_CODE" ]]; then
        echo "OVMF firmware not found — install edk2-ovmf or ovmf" >&2
        exit 1
    fi

    # Copy ISO to libvirt images pool
    sudo cp "$ISO" /var/lib/libvirt/images/${VM_NAME}.iso

    if sudo virsh dominfo "$VM_NAME" &>/dev/null; then
        echo "VM '${VM_NAME}' already exists — swapping ISO and rebooting..."
        sudo virsh destroy "$VM_NAME" 2>/dev/null || true
        CDROM_DEV=$(sudo virsh domblklist "$VM_NAME" \
            | awk 'NR>2 && $2 == "-" {print $1; exit}')
        if [[ -z "$CDROM_DEV" ]]; then
            CDROM_DEV=$(sudo virsh domblklist "$VM_NAME" \
                | awk 'NR>2 && ($2 ~ /\.iso$/) {print $1; exit}')
        fi
        sudo virsh change-media "$VM_NAME" "$CDROM_DEV" \
            /var/lib/libvirt/images/${VM_NAME}.iso --force
        sudo virsh start "$VM_NAME"
    else
        echo "Creating libvirt VM: ${VM_NAME} (${VM_RAM}M RAM, ${VM_CPUS} vCPUs, ${DISK_SIZE}G disk)"
        sudo virt-install \
            --name "$VM_NAME" \
            --memory "$VM_RAM" --vcpus "$VM_CPUS" \
            --boot loader="${OVMF_CODE}",loader.readonly=yes,loader.type=pflash,nvram.template="${OVMF_VARS}" \
            --cdrom /var/lib/libvirt/images/${VM_NAME}.iso \
            --disk size=${DISK_SIZE},format=qcow2 \
            --network network=default \
            --graphics vnc,listen=127.0.0.1 \
            --os-variant generic \
            --tpm none \
            --noautoconsole
    fi

    MAC=$(sudo virsh domiflist "$VM_NAME" | awk '/network/{print $5}')
    echo "VM started. MAC: ${MAC}"
    echo "Waiting for DHCP lease (this takes 30-90s while the ISO boots)..."

    GUEST_IP=""
    for i in $(seq 1 60); do
        GUEST_IP=$(sudo virsh net-dhcp-leases default 2>/dev/null \
            | awk -v mac="$MAC" '$3 == mac {split($5, a, "/"); print a[1]}' \
            | head -1)
        if [[ -n "$GUEST_IP" ]]; then
            break
        fi
        sleep 3
    done

    if [[ -z "$GUEST_IP" ]]; then
        echo "WARNING: No DHCP lease found after 3 minutes." >&2
        echo "Try: sudo virsh net-dhcp-leases default" >&2
        echo "Or:  sudo virsh console ${VM_NAME}" >&2
        exit 1
    fi

    echo ""
    echo "========================================"
    echo " SSH ready:"
    echo "   ssh liveuser@${GUEST_IP}"
    echo "   password: live"
    echo "========================================"
    echo ""
    echo "VNC: $(sudo virsh domdisplay ${VM_NAME} 2>/dev/null || echo 'unavailable')"
    echo "Serial: sudo virsh console ${VM_NAME}"
    echo "Cleanup: sudo virsh destroy ${VM_NAME} && sudo virsh undefine ${VM_NAME} --nvram"

# Reproduce issue #270: install Dakota with LUKS encryption via fisherman into
# the running dakota-debug libvirt VM, then eject the ISO and reboot.
#
# Prerequisites:
#   1. Build a debug ISO:  just debug=1 installer_channel=dev iso-sd-boot dakota
#   2. Boot the VM:        just debug=1 boot-libvirt-debug dakota
#      (wait for "SSH ready" output, then Ctrl-C or let it return)
#
# After install this recipe ejects the ISO and issues a reboot so the VM boots
# into the freshly installed system.  Observe the boot with: just luks-boot dakota
#
# The LUKS passphrase defaults to "testpassphrase"; override with:
#   just luks-passphrase=MySecret luks-install dakota
luks-install target:
    #!/usr/bin/bash
    set -euo pipefail

    VM_NAME="dakota-debug"
    PASSPHRASE="{{luks-passphrase}}"
    DISK="/dev/sda"
    SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -o IdentitiesOnly=yes -o PreferredAuthentications=password"
    SSH="sshpass -p live ssh $SSH_OPTS"
    SCP="sshpass -p live scp $SSH_OPTS"

    # ── Resolve guest IP from DHCP leases ────────────────────────────────────
    MAC=$(sudo virsh domiflist "$VM_NAME" 2>/dev/null | awk '/network/{print $5; exit}')
    if [[ -z "$MAC" ]]; then
        echo "ERROR: VM '${VM_NAME}' is not running."
        echo "Start it first: just debug=1 boot-libvirt-debug {{target}}"
        exit 1
    fi

    GUEST_IP=""
    echo "Looking up DHCP lease for ${VM_NAME} (${MAC})..."
    for i in $(seq 1 20); do
        GUEST_IP=$(sudo virsh net-dhcp-leases default 2>/dev/null \
            | awk -v mac="$MAC" '$3 == mac {split($5, a, "/"); print a[1]}' \
            | head -1)
        [[ -n "$GUEST_IP" ]] && break
        sleep 3
    done
    if [[ -z "$GUEST_IP" ]]; then
        echo "ERROR: no DHCP lease found — is the VM fully booted?"
        echo "Check: sudo virsh net-dhcp-leases default"
        exit 1
    fi
    echo "Guest IP: ${GUEST_IP}"

    # ── Wait for SSH ──────────────────────────────────────────────────────────
    echo "Waiting for SSH..."
    for i in $(seq 1 30); do
        $SSH liveuser@"$GUEST_IP" true 2>/dev/null && break
        sleep 3
    done
    $SSH liveuser@"$GUEST_IP" true || { echo "ERROR: SSH timed out"; exit 1; }

    # ── Upload fisherman recipe ───────────────────────────────────────────────
    # Use containers-storage so fisherman uses the OCI image already embedded in
    # the squashfs (no network pull needed; matches what the GUI installer does).
    # Write to a local temp file first to avoid $() heredoc syntax that confuses
    # just's parser (it sees the closing ) at column 0 as a delimiter).
    RECIPE_TMP=$(mktemp /tmp/luks-recipe-XXXXXX.json)
    trap "rm -f '${RECIPE_TMP}'" EXIT
    printf '{\n  "disk": "%s",\n  "filesystem": "btrfs",\n  "image": "containers-storage:ghcr.io/projectbluefin/dakota:latest",\n  "composeFsBackend": true,\n  "bootloader": "systemd",\n  "hostname": "dakota-luks-test",\n  "encryption": {"type": "luks-passphrase", "passphrase": "%s"},\n  "flatpaks": []\n}\n' \
        "${DISK}" "${PASSPHRASE}" > "${RECIPE_TMP}"
    $SCP "${RECIPE_TMP}" liveuser@"$GUEST_IP":/tmp/luks-recipe.json
    echo "Uploaded recipe to /tmp/luks-recipe.json"

    # ── Run fisherman ─────────────────────────────────────────────────────────
    # fisherman is symlinked at /usr/local/bin/fisherman by configure-live.sh.
    # Run as root (liveuser has NOPASSWD sudo) so fisherman can partition disks.
    echo "Running fisherman install (this takes several minutes)..."
    $SSH liveuser@"$GUEST_IP" 'sudo /usr/local/bin/fisherman /tmp/luks-recipe.json'
    echo "Install finished."

    # ── Eject ISO and reboot ──────────────────────────────────────────────────
    echo "Ejecting install ISO..."
    CDROM_DEV=$(sudo virsh domblklist "$VM_NAME" \
        | awk 'NR>2 && ($2 ~ /\.iso$/ || $2 == "-") {print $1; exit}')
    if [[ -n "$CDROM_DEV" ]]; then
        sudo virsh change-media "$VM_NAME" "$CDROM_DEV" --eject --force 2>/dev/null || true
        echo "ISO ejected from ${CDROM_DEV}."
    else
        echo "Warning: could not identify CD-ROM device; eject skipped."
    fi

    echo "Rebooting VM into installed system..."
    sudo virsh reboot "$VM_NAME" || $SSH liveuser@"$GUEST_IP" 'sudo reboot' || true

    echo ""
    echo "========================================"
    echo " VM is rebooting into the installed system."
    echo " Unlock LUKS: just luks-unlock {{target}}"
    echo " Watch boot:  just luks-boot {{target}}"
    echo " Reproduces:  projectbluefin/dakota#270"
    echo "========================================"

# Automate LUKS passphrase entry on the dakota-debug VM serial console.
#
# Uses a Python PTY to connect to the VM's serial console, waits for the
# cryptsetup passphrase prompt, sends the passphrase, then watches the boot
# for success or the #270 emergency shell.
#
# Run after: just luks-install dakota
# Passphrase defaults to {{luks-passphrase}}; override with luks-passphrase=X
luks-unlock target:
    #!/usr/bin/bash
    VM_NAME="dakota-debug"
    PASSPHRASE="{{luks-passphrase}}"
    if ! sudo virsh domstate "$VM_NAME" 2>/dev/null | grep -q running; then
        echo "ERROR: VM '${VM_NAME}' is not running."
        echo "Run: just luks-install {{target}}"
        exit 1
    fi
    MAC=$(sudo virsh domiflist "$VM_NAME" 2>/dev/null | awk '/network/{print $5; exit}')
    if [[ -z "$MAC" ]]; then
        echo "ERROR: VM '${VM_NAME}' is not running."
        exit 1
    fi
    echo "Waiting for Plymouth passphrase prompt (VM MAC: ${MAC})..."
    echo "Passphrase: ${PASSPHRASE}"
    sudo python3 "{{target}}/src/luks-unlock.py" libvirt "$VM_NAME" "$PASSPHRASE" "$MAC"

# Connect to the serial console of the dakota-debug VM to watch boot after
# luks-install.  At the LUKS passphrase prompt type the passphrase (default:
# "testpassphrase"), then watch for the systemd emergency shell (issue #270).
#
# Detach: Ctrl-]
# Cleanup after testing:
#   sudo virsh destroy dakota-debug && sudo virsh undefine dakota-debug --nvram
luks-boot target:
    #!/usr/bin/bash
    VM_NAME="dakota-debug"
    if ! sudo virsh domstate "$VM_NAME" 2>/dev/null | grep -q running; then
        echo "ERROR: VM '${VM_NAME}' is not running."
        echo "Run: just luks-install {{target}}"
        exit 1
    fi
    echo "Connecting to serial console (detach: Ctrl-])"
    echo "At the LUKS passphrase prompt type: {{luks-passphrase}}"
    echo "Reproducing: projectbluefin/dakota#270"
    echo "  Expected:  ~90s hang → systemd emergency shell"
    echo ""
    sudo virsh console "$VM_NAME"

# ── QEMU-native LUKS test (used by CI; mirrors the libvirt recipes) ───────────
#
# These recipes run the same end-to-end LUKS test as the libvirt workflow but
# use QEMU directly so they work in GitHub Actions (no libvirt available).
#
# Full CI test sequence:
#   just debug=1 installer_channel=dev iso-sd-boot dakota
#   just luks-test-qemu dakota
#
# Or step-by-step:
#   just luks-boot-qemu-live dakota   # boot live ISO in QEMU (daemonized)
#   just luks-install-qemu dakota     # SSH fisherman install (uses luks-install internals)
#   just luks-boot-qemu-installed dakota  # reboot QEMU into installed disk
#   just luks-unlock-qemu dakota      # send passphrase via QEMU monitor

# QEMU install disk path (override with: just luks-qemu-disk=/path/to/disk.qcow2 ...)
luks-qemu-disk := "/var/tmp/dakota-luks-install.qcow2"

# QEMU monitor socket paths
luks-qemu-monitor-live := "/tmp/dakota-qemu-live.sock"
luks-qemu-monitor-installed := "/tmp/dakota-qemu-installed.sock"

# Serial log paths
luks-qemu-serial-live := "/tmp/dakota-qemu-live-serial.log"
luks-qemu-serial-installed := "/tmp/dakota-qemu-installed-serial.log"

# SSH port for QEMU SLIRP forwarding
luks-qemu-ssh-port := "2222"

# Full end-to-end test: build the ISO then run the LUKS install + boot test.
# This is the primary integration test — mirrors .github/workflows/test-luks-install.yml.
# Usage: just debug=1 installer_channel=dev    e2e dakota
#        just debug=1 installer_channel=stable e2e dakota
e2e target:
    #!/usr/bin/bash
    set -euo pipefail
    echo "=== Step 1/2: Building ISO (debug={{debug}}, installer_channel={{installer_channel}}) ==="
    just debug={{debug}} installer_channel={{installer_channel}} output_dir={{output_dir}} iso-sd-boot {{target}}
    echo "=== Step 2/2: LUKS end-to-end test ==="
    sudo rm -f "{{luks-qemu-disk}}" "{{luks-qemu-monitor-live}}" "{{luks-qemu-monitor-installed}}" \
               "{{luks-qemu-serial-live}}" "{{luks-qemu-serial-installed}}"
    just luks-test-qemu {{target}}

# Run the full LUKS end-to-end test in QEMU (CI entry point).
# Builds nothing — expects the ISO to already exist in {{output_dir}}.
luks-test-qemu target:
    #!/usr/bin/bash
    set -euo pipefail
    just luks-qemu-disk={{luks-qemu-disk}} luks-boot-qemu-live {{target}}
    just luks-qemu-ssh-port={{luks-qemu-ssh-port}} luks-install-qemu {{target}}
    just luks-qemu-disk={{luks-qemu-disk}} luks-boot-qemu-installed {{target}}
    just luks-qemu-monitor-installed={{luks-qemu-monitor-installed}} \
         luks-qemu-serial-installed={{luks-qemu-serial-installed}} \
         luks-unlock-qemu {{target}}

# Boot the live ISO in QEMU (daemonized) with a blank install disk attached.
# Creates the install disk if it doesn't exist.
luks-boot-qemu-live target:
    #!/usr/bin/bash
    set -euo pipefail
    QEMU=$(command -v /usr/libexec/qemu-kvm /usr/bin/qemu-kvm \
               /usr/bin/qemu-system-x86_64 2>/dev/null | head -1)
    [[ -z "$QEMU" ]] && { echo "qemu-kvm / qemu-system-x86_64 not found" >&2; exit 1; }
    ISO=$(ls \
        {{output_dir}}/{{target}}-live.iso \
        output/bootiso/install.iso \
        output/bootc-{{target}}*.iso \
        2>/dev/null | head -1 || true)
    if [[ -z "$ISO" ]]; then
        echo "No ISO found — run: just debug=1 iso-sd-boot {{target}}" >&2
        exit 1
    fi

    OVMF_CODE=""; OVMF_VARS=""
    for f in /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_CODE.fd \
              /usr/share/edk2/ovmf/OVMF_CODE.fd /usr/share/ovmf/OVMF.fd; do
        [[ -f "$f" ]] && { OVMF_CODE="$f"; break; }
    done
    for f in /usr/share/OVMF/OVMF_VARS_4M.fd /usr/share/OVMF/OVMF_VARS.fd \
              /usr/share/edk2/ovmf/OVMF_VARS.fd; do
        if [[ -f "$f" ]]; then cp "$f" /var/tmp/dakota-qemu-live-vars.fd; OVMF_VARS=/var/tmp/dakota-qemu-live-vars.fd; break; fi
    done
    [[ -z "$OVMF_CODE" ]] && { echo "OVMF firmware not found" >&2; exit 1; }

    [[ -f "{{luks-qemu-disk}}" ]] || qemu-img create -f qcow2 "{{luks-qemu-disk}}" 64G
    sudo rm -f "{{luks-qemu-monitor-live}}" "{{luks-qemu-serial-live}}"

    echo "Booting live ISO: $ISO"
    sudo "$QEMU" \
        -machine q35 -cpu host -m 8192 -smp 4 -accel kvm \
        -drive "if=pflash,format=raw,readonly=on,file=${OVMF_CODE}" \
        -drive "if=pflash,format=raw,file=${OVMF_VARS}" \
        -drive "if=none,id=iso,file=${ISO},media=cdrom,readonly=on,format=raw" \
        -device virtio-scsi-pci,id=scsi \
        -device scsi-cd,drive=iso \
        -drive "if=none,id=disk,file={{luks-qemu-disk}},format=qcow2" \
        -device virtio-blk-pci,drive=disk \
        -netdev "user,id=net0,hostfwd=tcp::{{luks-qemu-ssh-port}}-:22" \
        -device virtio-net-pci,netdev=net0 \
        -monitor "unix:{{luks-qemu-monitor-live}},server,nowait" \
        -serial "file:{{luks-qemu-serial-live}}" \
        -display none \
        -daemonize
    echo "Live QEMU started (monitor: {{luks-qemu-monitor-live}})"

    echo "Waiting for live environment SSH on port {{luks-qemu-ssh-port}}..."
    for i in $(seq 1 60); do
        if sudo grep -q "DAKOTA_LIVE_READY" "{{luks-qemu-serial-live}}" 2>/dev/null; then
            echo "Live environment ready (serial marker seen)"
            break
        fi
        [[ "$i" -eq 60 ]] && { echo "ERROR: live env not ready after 5m"; sudo tail -30 "{{luks-qemu-serial-live}}" || true; exit 1; }
        sleep 5
    done
    SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -o PreferredAuthentications=password"
    for i in $(seq 1 30); do
        sshpass -p live ssh $SSH_OPTS liveuser@127.0.0.1 -p {{luks-qemu-ssh-port}} true 2>/dev/null && { echo "SSH ready"; break; }
        [[ "$i" -eq 30 ]] && { echo "ERROR: SSH timed out"; exit 1; }
        sleep 5
    done

    # Save a live boot screendump for CI diagnostics / PR comment
    sleep 2
    sudo socat - "UNIX-CONNECT:{{luks-qemu-monitor-live}}" \
        <<< "screendump /tmp/luks-screenshot-live.ppm" 2>/dev/null || true

# Run fisherman LUKS install via SSH into the live QEMU VM.
# Reuses the same SSH logic as luks-install; install disk is /dev/vda in QEMU.
luks-install-qemu target:
    #!/usr/bin/bash
    set -euo pipefail
    PASSPHRASE="{{luks-passphrase}}"
    DISK="/dev/vda"
    SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -o PreferredAuthentications=password -o ServerAliveInterval=30 -o ServerAliveCountMax=20"
    SSH="sshpass -p live ssh $SSH_OPTS liveuser@127.0.0.1 -p {{luks-qemu-ssh-port}}"
    SCP="sshpass -p live scp $SSH_OPTS -P {{luks-qemu-ssh-port}}"

    RECIPE_TMP=$(mktemp /tmp/luks-recipe-XXXXXX.json)
    trap "rm -f '${RECIPE_TMP}'" EXIT
    printf '{\n  "disk": "%s",\n  "filesystem": "btrfs",\n  "image": "containers-storage:ghcr.io/projectbluefin/dakota:latest",\n  "composeFsBackend": true,\n  "bootloader": "systemd",\n  "hostname": "dakota-luks-test",\n  "encryption": {"type": "luks-passphrase", "passphrase": "%s"},\n  "flatpaks": []\n}\n' \
        "${DISK}" "${PASSPHRASE}" > "${RECIPE_TMP}"
    $SCP "${RECIPE_TMP}" liveuser@127.0.0.1:/tmp/luks-recipe.json
    echo "Uploaded recipe — running fisherman (takes several minutes)..."
    $SSH 'sudo /usr/local/bin/fisherman /tmp/luks-recipe.json'
    echo "Patching BLS entries to enable dual serial+VT console..."
    $SSH 'sudo bash -c "
        set -euo pipefail
        TMP=$(mktemp -d)
        trap \"umount \$TMP 2>/dev/null || true; rmdir \$TMP\" EXIT
        mount /dev/vda1 \$TMP
        COUNT=0
        for entry in \$TMP/loader/entries/*.conf \$TMP/EFI/loader/entries/*.conf; do
            [[ -f \"\$entry\" ]] || continue
            if grep -q \"^options \" \"\$entry\" && ! grep -q \"console=tty0\" \"\$entry\"; then
                sed -i \"s|^options .*|& console=tty0 console=ttyS0|\" \"\$entry\"
                COUNT=\$((COUNT+1))
                echo \"  patched: \$(basename \$entry)\"
            fi
        done
        echo \"BLS patch: \$COUNT entries updated\"
    "'
    echo "Install complete. Shutting down live QEMU..."
    echo "system_powerdown" | sudo socat - "UNIX-CONNECT:{{luks-qemu-monitor-live}}" 2>/dev/null || true
    sleep 5
    echo "quit" | sudo socat - "UNIX-CONNECT:{{luks-qemu-monitor-live}}" 2>/dev/null || true

# Boot the installed disk in QEMU (no ISO). Called after luks-install-qemu.
luks-boot-qemu-installed target:
    #!/usr/bin/bash
    set -euo pipefail
    QEMU=$(command -v /usr/libexec/qemu-kvm /usr/bin/qemu-kvm \
               /usr/bin/qemu-system-x86_64 2>/dev/null | head -1)
    [[ -z "$QEMU" ]] && { echo "qemu-kvm / qemu-system-x86_64 not found" >&2; exit 1; }
    OVMF_CODE=""; OVMF_VARS=""
    for f in /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_CODE.fd \
              /usr/share/edk2/ovmf/OVMF_CODE.fd /usr/share/ovmf/OVMF.fd; do
        [[ -f "$f" ]] && { OVMF_CODE="$f"; break; }
    done
    for f in /usr/share/OVMF/OVMF_VARS_4M.fd /usr/share/OVMF/OVMF_VARS.fd \
              /usr/share/edk2/ovmf/OVMF_VARS.fd; do
        if [[ -f "$f" ]]; then cp "$f" /var/tmp/dakota-qemu-installed-vars.fd; OVMF_VARS=/var/tmp/dakota-qemu-installed-vars.fd; break; fi
    done
    [[ -z "$OVMF_CODE" ]] && { echo "OVMF firmware not found" >&2; exit 1; }

    sudo rm -f "{{luks-qemu-monitor-installed}}" "{{luks-qemu-serial-installed}}"

    echo "Booting installed disk: {{luks-qemu-disk}}"
    sudo "$QEMU" \
        -machine q35 -cpu host -m 8192 -smp 4 -accel kvm \
        -drive "if=pflash,format=raw,readonly=on,file=${OVMF_CODE}" \
        -drive "if=pflash,format=raw,file=${OVMF_VARS}" \
        -drive "if=none,id=disk,file={{luks-qemu-disk}},format=qcow2" \
        -device virtio-blk-pci,drive=disk \
        -netdev user,id=net0 \
        -device virtio-net-pci,netdev=net0 \
        -monitor "unix:{{luks-qemu-monitor-installed}},server,nowait" \
        -serial "file:{{luks-qemu-serial-installed}}" \
        -display none \
        -daemonize
    echo "Installed QEMU started (monitor: {{luks-qemu-monitor-installed}})"

    for i in $(seq 1 15); do
        [[ -S "{{luks-qemu-monitor-installed}}" ]] && break
        sleep 2
    done

# Send LUKS passphrase to installed QEMU VM via monitor screendump + sendkey.
# Polls screendump size to detect Plymouth takeover, then injects keystrokes.
luks-unlock-qemu target:
    #!/usr/bin/bash
    set -euo pipefail
    PASSPHRASE="{{luks-passphrase}}"
    echo "Unlocking LUKS on installed QEMU VM..."
    echo "Passphrase: ${PASSPHRASE}"
    sudo python3 "{{target}}/src/luks-unlock.py" qemu \
        "{{luks-qemu-monitor-installed}}" \
        "$PASSPHRASE" \
        "{{luks-qemu-serial-installed}}"

    # Show key screenshots inline for terminals that support it (Kitty, iTerm2, etc.)
    for label in "Plymouth prompt" "Final boot"; do
        key=$(echo "$label" | tr ' ' '-' | tr '[:upper:]' '[:lower:]')
        bash "{{target}}/src/show-screenshot.sh" "/tmp/luks-screenshot-${key}.ppm" "$label" || true
    done
