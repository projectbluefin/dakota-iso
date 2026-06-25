image-builder := "image-builder"
image-builder-dev := "image-builder-dev"

# Output directory for built ISOs and intermediate artifacts.
# Override with: just output_dir=/your/path iso-sd-boot dakota
output_dir := "output"

# Working directory for ISO builds where container storage staging
# and the squashfs-root are stored.
# override with: just workdir=/your/path iso-sd-boot dakota
workdir := output_dir

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

# Path to the projectbluefin/fisherman repo for building the patched fisherman binary
# used in bootcDirect mode (ostree variants: stable, lts).
# Override with: just fisher_repo=/path/to/fisherman/fisherman luks-test-qemu stable
fisher_repo := "/tmp/fisherman/fisherman"

# Squashfs compression preset:
#   fast    (default) — zstd level 3,  128K blocks — quick local builds/CI
#   release           — zstd level 15, 1M blocks   — ~20% smaller, ~5× slower
# Example: just compression=release iso-sd-boot dakota
compression := "fast"

# Map target to filesystem: btrfs for all targets to avoid boot timeout on LTS.
_filesystem_for target:
    @echo "btrfs"

# Create an XFS loopback mount at /mnt for faster VFS import.
#
# The chunkified Dakota images (~120 layers) cause VFS import under BTRFS
# to create ~450 GB of intermediate directories.  XFS handles this workload
# much faster.  This recipe creates a 45 GB XFS loopback at /mnt.
#
# Idempotent: skips if /mnt is already an XFS mount.
# Must be run as root: sudo just mount-xfs
mount-xfs:
    #!/usr/bin/bash
    set -euo pipefail
    # Already XFS? Nothing to do.
    if findmnt -n -o FSTYPE /mnt 2>/dev/null | grep -q '^xfs$'; then
        echo "/mnt is already XFS — skipping"
        exit 0
    fi
    echo "Creating 45G XFS loopback at /mnt..."
    IMG="/var/tmp/dakota-xfs-loopback.img"
    truncate -s 0 "${IMG}"
    # Disable copy-on-write on BTRFS hosts (harmless no-op on other fs)
    chattr +C "${IMG}" 2>/dev/null || true
    fallocate -l 45G "${IMG}"
    mkfs.xfs -f "${IMG}"
    mount -o loop "${IMG}" /mnt
    echo "XFS mounted at /mnt (45G)"
    echo ""
    echo "Now run your build with workdir on /mnt:"
    echo "  sudo just workdir=/mnt iso-sd-boot dakota"
    echo "To run rootless (replace \`user\` with your username):"
    echo "  sudo chown user:user /mnt && just workdir=/mnt iso-sd-boot dakota"
    df -h /mnt

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
    #!/usr/bin/bash
    test -f "{{target}}/payload_ref" || { echo "ERROR: {{target}}/payload_ref not found — create it with the base image reference, e.g.: echo 'ghcr.io/projectbluefin/dakota:latest' > {{target}}/payload_ref"; exit 1; }
    # live_target overrides the Containerfile TARGET build-arg when the live
    # environment image differs from the variant directory name.
    # e.g. the 'dakota' variant builds its live env from 'dakota-nvidia' so all
    # hardware can boot live, while payload_ref controls the offline store.
    LIVE_TARGET=$(cat "{{target}}/live_target" 2>/dev/null | tr -d '[:space:]' || echo "{{target}}")
    LIVE_TAG=$(cat "{{target}}/tag" 2>/dev/null | tr -d '[:space:]' || echo "stable")
    LIVE_REGISTRY=$(cat "{{target}}/registry" 2>/dev/null | tr -d '[:space:]' || echo "projectbluefin")
    podman build --cap-add sys_admin --security-opt label=disable \
        --layers \
        --build-arg DEBUG={{debug}} \
        --build-arg INSTALLER_CHANNEL={{installer_channel}} \
        --build-arg TARGET="${LIVE_TARGET}" \
        --build-arg TAG="${LIVE_TAG}" \
        --build-arg REGISTRY="${LIVE_REGISTRY}" \
        --build-arg CACHE_BUST="$(date +%Y%m%d)" \
        -t {{target}}-installer -f ./live/Containerfile ./live

# Build a systemd-boot UEFI live ISO for the given target.
#
# Builds the live environment container from live/Containerfile, then assembles
# the ISO on the host using build-iso.sh.  This produces a single-variant ISO
# for local testing.  CI builds a unified ISO with both NVIDIA (live) and
# non-NVIDIA (offline store) variants — see scripts/build-live-squashfs.sh and
# scripts/build-offline-store.sh.
#
# Output: output/<target>-live.iso
iso-sd-boot target:
    TARGET={{target}} \
    OUTPUT_DIR={{output_dir}} \
    WORKDIR={{workdir}} \
    DEBUG={{debug}} \
    INSTALLER_CHANNEL={{installer_channel}} \
    COMPRESSION={{compression}} \
    bash scripts/iso-sd-boot.sh
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
               /usr/bin/qemu-system-x86_64 \
               /home/linuxbrew/.linuxbrew/bin/qemu-system-x86_64 2>/dev/null | head -1)
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
        /usr/share/ovmf/OVMF.fd \
        /home/linuxbrew/.linuxbrew/share/qemu/edk2-x86_64-code.fd \
        /home/linuxbrew/.linuxbrew/Cellar/qemu/11.0.1/share/qemu/edk2-x86_64-code.fd; do
        [[ -f "$f" ]] && { OVMF_CODE="$f"; break; }
    done
    OVMF_VARS_SRC=""
    for f in \
        /usr/share/OVMF/OVMF_VARS.fd \
        /usr/share/edk2/ovmf/OVMF_VARS.fd \
        /usr/share/edk2-ovmf/x64/OVMF_VARS.fd \
              /var/home/jorge/VMs/bluefin-test/OVMF_VARS.fd \
              /var/home/james/dev/ostree-composefs-rebase/ovmf_vars.fd \
              /var/home/james/.local/share/Trash/files/e2e-logs-3/ovmf_vars.fd \
              /home/linuxbrew/.linuxbrew/share/qemu/edk2-i386-vars.fd; do
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
    "$QEMU" \
        -machine q35 \
        -m {{qemu-mem}} \
        -accel kvm \
        -cpu host \
        -smp {{qemu-smp}} \
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
        /usr/share/ovmf/OVMF.fd \
        /home/linuxbrew/.linuxbrew/share/qemu/edk2-x86_64-code.fd \
        /home/linuxbrew/.linuxbrew/Cellar/qemu/11.0.1/share/qemu/edk2-x86_64-code.fd; do
        [[ -f "$f" ]] && { OVMF_CODE="$f"; break; }
    done
    OVMF_VARS=""
    for f in \
        /usr/share/OVMF/OVMF_VARS.fd \
        /usr/share/edk2/ovmf/OVMF_VARS.fd \
        /usr/share/edk2-ovmf/x64/OVMF_VARS.fd \
              /var/home/jorge/VMs/bluefin-test/OVMF_VARS.fd \
              /var/home/james/dev/ostree-composefs-rebase/ovmf_vars.fd \
              /var/home/james/.local/share/Trash/files/e2e-logs-3/ovmf_vars.fd \
              /home/linuxbrew/.linuxbrew/share/qemu/edk2-i386-vars.fd; do
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
    PAYLOAD_IMAGE=$(cat "{{target}}/payload_ref" | tr -d '[:space:]')
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
    LIVE_TARGET=$(cat "{{target}}/live_target" 2>/dev/null | tr -d '[:space:]' || echo "{{target}}")
    BOOTLOADER_VARIANT=$(echo "$LIVE_TARGET" | sed 's/-nvidia-open$//;s/-nvidia$//')
    COMPOSEFS_BACKEND=$(cat "live/src/${BOOTLOADER_VARIANT}/composefs" 2>/dev/null | tr -d '[:space:]' || echo "true")
    BOOTLOADER=$(cat "live/src/${BOOTLOADER_VARIANT}/bootloader" 2>/dev/null | tr -d '[:space:]' || echo "systemd")
    if [[ "${BOOTLOADER}" == "grub" ]]; then BOOTLOADER="grub2"; fi
    printf '{\n  "disk": "%s",\n  "filesystem": "btrfs",\n  "image": "containers-storage:'"${PAYLOAD_IMAGE}"'",\n  "composeFsBackend": %s,\n  "bootloader": "%s",\n  "hostname": "dakota-luks-test",\n  "encryption": {"type": "luks-passphrase", "passphrase": "%s"},\n  "flatpaks": []\n}\n' \
        "${DISK}" "$([ "${COMPOSEFS_BACKEND}" == "true" ] && echo "true" || echo "false")" "${BOOTLOADER}" "${PASSPHRASE}" > "${RECIPE_TMP}"
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
    sudo python3 "dakota/src/luks-unlock.py" libvirt "$VM_NAME" "$PASSPHRASE" "$MAC"

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

# QEMU memory (MiB) for the live install phase.
# 4096 is intentionally tight: the overlay tmpfs is only ~2 GiB, which
# reliably triggers ENOSPC if fisherman writes scratch to /var/tmp instead
# of the target disk.  Override with qemu-mem=8192 for interactive debugging.
qemu-mem := "8192"
qemu-smp := "8"

# QEMU install disk path (override with: just luks-qemu-disk=/path/to/disk.qcow2 ...)
# Default includes the target variant so parallel CI jobs don't contend.
luks-qemu-disk := "/var/tmp/dakota-luks-install.qcow2"
# Scratch disk for /var/tmp inside the live LUKS VM — prevents ENOSPC during
# OCI blob extraction.  fisherman bind-mounts /var/tmp for plain targets but
# not LUKS; this 16G sparse file is mounted over /var/tmp before fisherman runs.
luks-scratch-disk := "/var/tmp/dakota-luks-scratch.img"

# QEMU monitor socket paths
luks-qemu-monitor-live := "/tmp/dakota-qemu-live.sock"
luks-qemu-monitor-installed := "/tmp/dakota-qemu-installed.sock"

# Serial log paths
luks-qemu-serial-live := "/tmp/dakota-qemu-live-serial.log"
luks-qemu-serial-installed := "/tmp/dakota-qemu-installed-serial.log"

# SSH port for QEMU SLIRP forwarding (LUKS test)
luks-qemu-ssh-port := "2222"

# ── Plain (no-encryption) install test paths ─────────────────────────────────
# Uses port 2223 and separate socket/disk paths so both tests can run concurrently.
plain-qemu-disk := "/var/tmp/dakota-plain-install.img"
# Scratch disk for /var/tmp inside the live plain-install VM — prevents ENOSPC
# during VFS→OCI blob export when the embedded OCI store is used.
plain-scratch-disk := "/var/tmp/dakota-plain-scratch.img"
plain-qemu-monitor-live := "/tmp/dakota-plain-qemu-live.sock"
plain-qemu-monitor-installed := "/tmp/dakota-plain-qemu-installed.sock"
plain-qemu-serial-live := "/tmp/dakota-plain-qemu-live-serial.log"
plain-qemu-serial-installed := "/tmp/dakota-plain-qemu-installed-serial.log"
plain-qemu-ssh-port := "2223"

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
    rm -f /var/tmp/dakota-luks-install-*.qcow2 /var/tmp/dakota-luks-scratch-*.img \
               "{{luks-qemu-monitor-live}}" "{{luks-qemu-monitor-installed}}" \
               "{{luks-qemu-serial-live}}" "{{luks-qemu-serial-installed}}"
    just luks-test-qemu {{target}}

# Run the full LUKS end-to-end test in QEMU (CI entry point).
# Builds nothing — expects the ISO to already exist in {{output_dir}}.
luks-test-qemu target installer_channel="dev":
    #!/usr/bin/bash
    set -euo pipefail
    DISK="/var/tmp/dakota-luks-install-{{target}}-{{installer_channel}}.qcow2"
    SCRATCH="/var/tmp/dakota-luks-scratch-{{target}}-{{installer_channel}}.img"
    just luks-qemu-disk="$DISK" luks-scratch-disk="$SCRATCH" luks-boot-qemu-live {{target}}
    just luks-qemu-ssh-port={{luks-qemu-ssh-port}} luks-install-qemu {{target}}
    just luks-qemu-disk="$DISK" luks-scratch-disk="$SCRATCH" luks-boot-qemu-installed {{target}}
    just luks-qemu-monitor-installed={{luks-qemu-monitor-installed}} \
         luks-qemu-serial-installed={{luks-qemu-serial-installed}} \
         luks-unlock-qemu {{target}}

# Boot the live ISO in QEMU (daemonized) with a blank install disk attached.
# Creates the install disk if it doesn't exist.
luks-boot-qemu-live target:
    #!/usr/bin/bash
    set -euo pipefail
    QEMU=$(command -v /usr/libexec/qemu-kvm /usr/bin/qemu-kvm \
               /usr/bin/qemu-system-x86_64 \
               /home/linuxbrew/.linuxbrew/bin/qemu-system-x86_64 2>/dev/null | head -1)
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
              /usr/share/edk2/ovmf/OVMF_CODE.fd /usr/share/ovmf/OVMF.fd \
              /home/linuxbrew/.linuxbrew/share/qemu/edk2-x86_64-code.fd \
              /home/linuxbrew/.linuxbrew/Cellar/qemu/11.0.1/share/qemu/edk2-x86_64-code.fd; do
        [[ -f "$f" ]] && { OVMF_CODE="$f"; break; }
    done
    for f in /usr/share/OVMF/OVMF_VARS_4M.fd /usr/share/OVMF/OVMF_VARS.fd \
              /usr/share/edk2/ovmf/OVMF_VARS.fd \
              /var/home/jorge/VMs/bluefin-test/OVMF_VARS.fd \
              /var/home/james/dev/ostree-composefs-rebase/ovmf_vars.fd \
              /var/home/james/.local/share/Trash/files/e2e-logs-3/ovmf_vars.fd \
              /home/linuxbrew/.linuxbrew/share/qemu/edk2-i386-vars.fd; do
        if [[ -f "$f" ]]; then cp "$f" /var/tmp/dakota-qemu-live-vars.fd; OVMF_VARS=/var/tmp/dakota-qemu-live-vars.fd; break; fi
    done
    [[ -z "$OVMF_CODE" ]] && { echo "OVMF firmware not found" >&2; exit 1; }

    [[ -f "{{luks-qemu-disk}}" ]] || qemu-img create -f qcow2 "{{luks-qemu-disk}}" 64G
    # Scratch disk: 16G sparse file mounted over /var/tmp in the live VM to
    # give skopeo disk-backed space for VFS blob extraction (~9 GB blob).
    [[ -f "{{luks-scratch-disk}}" ]] || truncate -s 16G "{{luks-scratch-disk}}"
    rm -f "{{luks-qemu-monitor-live}}" "{{luks-qemu-serial-live}}"

    echo "Booting live ISO: $ISO"
    # KVM access: try direct, then sudo, then fall back to TCG
    QEMU_ACCEL="-accel kvm"
    QEMU_PREFIX=""
    if ! test -r /dev/kvm 2>/dev/null; then
        if sudo test -r /dev/kvm 2>/dev/null; then
            echo "Using sudo for KVM access"
            QEMU_PREFIX="sudo"
        else
            echo "KVM not available, falling back to TCG emulation (slower)"
            QEMU_ACCEL="-accel tcg,thread=multi"
            QEMU_PREFIX=""
        fi
    fi
    CPU_FLAG="-cpu host"
    if [[ "$QEMU_ACCEL" =~ tcg ]]; then
        CPU_FLAG="-cpu qemu64"
    fi
    $QEMU_PREFIX "$QEMU" \
        -machine q35 $CPU_FLAG -m {{qemu-mem}} -smp {{qemu-smp}} $QEMU_ACCEL \
        -drive "if=pflash,format=raw,readonly=on,file=${OVMF_CODE}" \
        -drive "if=pflash,format=raw,file=${OVMF_VARS}" \
        -drive "if=none,id=iso,file=${ISO},media=cdrom,readonly=on,format=raw" \
        -device virtio-scsi-pci,id=scsi \
        -device scsi-cd,drive=iso \
        -drive "if=none,id=disk,file={{luks-qemu-disk}},format=qcow2" \
        -device virtio-blk-pci,drive=disk \
        -drive "if=none,id=scratch,file={{luks-scratch-disk}},format=raw,cache=unsafe" \
        -device virtio-blk-pci,drive=scratch \
        -netdev "user,id=net0,hostfwd=tcp::{{luks-qemu-ssh-port}}-:22" \
        -device virtio-net-pci,netdev=net0 \
        -monitor "unix:{{luks-qemu-monitor-live}},server,nowait" \
        -serial "file:{{luks-qemu-serial-live}}" \
        -display none \
        -daemonize
    echo "Live QEMU started (monitor: {{luks-qemu-monitor-live}})"

    SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -o PreferredAuthentications=password"
    echo "Waiting for live environment on port {{luks-qemu-ssh-port}}..."
    # Check for DAKOTA_LIVE_READY serial marker OR SSH connectivity.
    # The serial marker requires live-ready.service to print to journal+console.
    # On some installer channel builds (e.g. dev) the service starts but never
    # writes to the serial console; SSH still works because debug-ssh-banner
    # confirms sshd is up.  Either path means the live env is ready.
    for i in $(seq 1 60); do
        if grep -q "DAKOTA_LIVE_READY" "{{luks-qemu-serial-live}}" 2>/dev/null; then
            echo "Live environment ready (serial marker seen)"
            break
        fi
        if sshpass -p live ssh $SSH_OPTS liveuser@127.0.0.1 -p {{luks-qemu-ssh-port}} true 2>/dev/null; then
            echo "Live environment ready (SSH connected)"
            break
        fi
        [[ "$i" -eq 60 ]] && { echo "ERROR: live env not ready after 5m"; tail -30 "{{luks-qemu-serial-live}}" || true; exit 1; }
        sleep 5
    done

    # Wait for the live boot GUI to render and stabilize before taking screenshot
    sudo python3 "dakota/src/luks-unlock.py" wait-live \
        "{{luks-qemu-monitor-live}}" \
        "/tmp/luks-screenshot-live.ppm" || true

# Run fisherman LUKS install via SSH into the live QEMU VM.
# Reuses the same SSH logic as luks-install; install disk is /dev/vda in QEMU.
luks-install-qemu target:
    ./scripts/luks-install-qemu.sh "{{target}}" "{{luks-passphrase}}" "{{luks-qemu-ssh-port}}" "{{luks-qemu-monitor-live}}" "{{fisher_repo}}"

# Boot the installed disk in QEMU (no ISO). Called after luks-install-qemu.
luks-boot-qemu-installed target:
    #!/usr/bin/bash
    set -euo pipefail
    QEMU=$(command -v /usr/libexec/qemu-kvm /usr/bin/qemu-kvm \
               /usr/bin/qemu-system-x86_64 \
               /home/linuxbrew/.linuxbrew/bin/qemu-system-x86_64 2>/dev/null | head -1)
    [[ -z "$QEMU" ]] && { echo "qemu-kvm / qemu-system-x86_64 not found" >&2; exit 1; }
    OVMF_CODE=""; OVMF_VARS=""
    for f in /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_CODE.fd \
              /usr/share/edk2/ovmf/OVMF_CODE.fd /usr/share/ovmf/OVMF.fd \
              /home/linuxbrew/.linuxbrew/share/qemu/edk2-x86_64-code.fd \
              /home/linuxbrew/.linuxbrew/Cellar/qemu/11.0.1/share/qemu/edk2-x86_64-code.fd; do
        [[ -f "$f" ]] && { OVMF_CODE="$f"; break; }
    done
    for f in /usr/share/OVMF/OVMF_VARS_4M.fd /usr/share/OVMF/OVMF_VARS.fd \
              /usr/share/edk2/ovmf/OVMF_VARS.fd \
              /var/home/jorge/VMs/bluefin-test/OVMF_VARS.fd \
              /var/home/james/dev/ostree-composefs-rebase/ovmf_vars.fd \
              /var/home/james/.local/share/Trash/files/e2e-logs-3/ovmf_vars.fd \
              /home/linuxbrew/.linuxbrew/share/qemu/edk2-i386-vars.fd; do
        if [[ -f "$f" ]]; then cp "$f" /var/tmp/dakota-qemu-installed-vars.fd; OVMF_VARS=/var/tmp/dakota-qemu-installed-vars.fd; break; fi
    done
    [[ -z "$OVMF_CODE" ]] && { echo "OVMF firmware not found" >&2; exit 1; }

    rm -f "{{luks-qemu-monitor-installed}}" "{{luks-qemu-serial-installed}}"

    echo "Booting installed disk: {{luks-qemu-disk}}"
    # The install recipe sends system_powerdown + quit via QEMU monitor
    # but the daemonized QEMU may hold the qcow2 file lock briefly.
    # Wait for the QEMU process matching THIS variant's disk to exit.
    DISK_PATTERN="$(echo '{{luks-qemu-disk}}' | sed 's/\./\\./g')"
    for i in {1..15}; do
        if ! sudo pgrep -f "qemu-system.*${DISK_PATTERN}" >/dev/null 2>&1; then
            break
        fi
        echo "Waiting for live QEMU to exit (attempt $i)..."
        sleep 2
    done
    # KVM access: try direct, then sudo, then fall back to TCG
    QEMU_ACCEL="-accel kvm"
    QEMU_PREFIX=""
    if ! test -r /dev/kvm 2>/dev/null; then
        if sudo test -r /dev/kvm 2>/dev/null; then
            echo "Using sudo for KVM access"
            QEMU_PREFIX="sudo"
        else
            echo "KVM not available, falling back to TCG emulation (slower)"
            QEMU_ACCEL="-accel tcg,thread=multi"
            QEMU_PREFIX=""
        fi
    fi
    CPU_FLAG="-cpu host"
    if [[ "$QEMU_ACCEL" =~ tcg ]]; then
        CPU_FLAG="-cpu qemu64"
    fi
    $QEMU_PREFIX "$QEMU" \
        -machine q35 $CPU_FLAG -m {{qemu-mem}} -smp {{qemu-smp}} $QEMU_ACCEL \
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
    sudo python3 "dakota/src/luks-unlock.py" qemu \
        "{{luks-qemu-monitor-installed}}" \
        "$PASSPHRASE" \
        "{{luks-qemu-serial-installed}}"

    # Show key screenshots inline for terminals that support it (Kitty, iTerm2, etc.)
    for label in "Plymouth prompt" "Final boot"; do
        key=$(echo "$label" | tr ' ' '-' | tr '[:upper:]' '[:lower:]')
        bash "dakota/src/show-screenshot.sh" "/tmp/luks-screenshot-${key}.ppm" "$label" || true
    done

# Run Python unit tests.
# Note: pytest passing means source-file invariants and mocked logic are OK.
# It does NOT mean the ISO builds or installs correctly — see test-luks-install.yml / test-plain-install.yml.
test:
    pytest tests/ -v

# ────────────────────────────────────────────────────────────────────────────
# Plain (unencrypted) composefs install E2E test
# ────────────────────────────────────────────────────────────────────────────
#
# This is the primary regression guard for installer crashes.
# It runs a plain btrfs+composefs+systemd-boot install (no LUKS) using
# qemu-mem=4096 (tight overlay tmpfs) to reproduce ENOSPC-class bugs
# like https://github.com/ublue-os/bluefin/discussions/4754.
#
# CI entry point:  just plain-test-qemu dakota
# Local full test: just debug=1 installer_channel=dev plain-e2e dakota
#
# Step-by-step:
#   just debug=1 iso-sd-boot dakota
#   just plain-boot-qemu-live dakota
#   just plain-install-qemu dakota
#   just plain-boot-qemu-installed dakota
#   just plain-verify-qemu dakota

# Full plain E2E: build ISO then run the plain install test.
plain-e2e target:
    #!/usr/bin/bash
    set -euo pipefail
    echo "=== Step 1/2: Building ISO (debug={{debug}}, installer_channel={{installer_channel}}) ==="
    just debug={{debug}} installer_channel={{installer_channel}} output_dir={{output_dir}} iso-sd-boot {{target}}
    echo "=== Step 2/2: Plain composefs install test (qemu-mem={{qemu-mem}} MiB) ==="
    rm -f "{{plain-qemu-disk}}" "{{plain-scratch-disk}}" \
               "{{plain-qemu-monitor-live}}" "{{plain-qemu-monitor-installed}}" \
               "{{plain-qemu-serial-live}}" "{{plain-qemu-serial-installed}}"
    just output_dir={{output_dir}} qemu-mem={{qemu-mem}} plain-test-qemu {{target}}

# ENOSPC regression gate: boot live ISO + run fisherman only through the OCI
# export step, then exit.  Passes when skopeo copies the blob without hitting
# ENOSPC in /var/tmp.  Runs at 4 GiB to keep overlay tmpfs tight (~2 GiB).
# Deliberately does NOT wait for the full bootc install — that is tested
# separately at 8 GiB RAM by plain-install-qemu.
plain-enospc-gate target:
    #!/usr/bin/bash
    set -euo pipefail
    PAYLOAD_IMAGE=$(cat "{{target}}/payload_ref" | tr -d '[:space:]')
    SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -o PreferredAuthentications=password -o ServerAliveInterval=30 -o ServerAliveCountMax=20"
    SSH="sshpass -p live ssh $SSH_OPTS liveuser@127.0.0.1 -p {{plain-qemu-ssh-port}}"
    SCP="sshpass -p live scp $SSH_OPTS -P {{plain-qemu-ssh-port}}"
    if $SSH "sudo podman image exists '${PAYLOAD_IMAGE}' 2>/dev/null"; then
        INSTALL_IMAGE="containers-storage:${PAYLOAD_IMAGE}"
    else
        INSTALL_IMAGE="docker://${PAYLOAD_IMAGE}"
    fi
    RECIPE_TMP=$(mktemp /tmp/plain-enospc-recipe-XXXXXX.json)
    trap "rm -f '${RECIPE_TMP}'" EXIT
    LIVE_TARGET=$(cat "{{target}}/live_target" 2>/dev/null | tr -d '[:space:]' || echo "{{target}}")
    BOOTLOADER_VARIANT=$(echo "$LIVE_TARGET" | sed 's/-nvidia-open$//;s/-nvidia$//')
    COMPOSEFS_BACKEND=$(cat "live/src/${BOOTLOADER_VARIANT}/composefs" 2>/dev/null | tr -d '[:space:]' || echo "true")
    BOOTLOADER=$(cat "live/src/${BOOTLOADER_VARIANT}/bootloader" 2>/dev/null | tr -d '[:space:]' || echo "systemd")
    if [[ "${BOOTLOADER}" == "grub" ]]; then BOOTLOADER="grub2"; fi
    printf '{\n  "disk": "/dev/vda",\n  "filesystem": "btrfs",\n  "image": "%s",\n  "composeFsBackend": %s,\n  "bootloader": "%s",\n  "hostname": "dakota-enospc-test",\n  "encryption": {"type": "none"},\n  "flatpaks": []\n}\n' \
        "${INSTALL_IMAGE}" "$([ "${COMPOSEFS_BACKEND}" == "true" ] && echo "true" || echo "false")" "${BOOTLOADER}" > "${RECIPE_TMP}"
    $SCP "${RECIPE_TMP}" liveuser@127.0.0.1:/tmp/enospc-recipe.json
    echo "Running fisherman (watching for OCI export completion)..."
    # Run fisherman via process substitution (not a pipe) so that exit 0/1
    # inside the while loop exits the *outer* script rather than just the
    # pipe subshell.  With a pipe and set -o pipefail, sshpass returns 255
    # on the broken-pipe when the while subshell exits, causing a spurious
    # failure even though the ENOSPC gate passed.
    while IFS= read -r line; do
        echo "[fisherman] $line"
        if echo "$line" | grep -q 'OCI export complete'; then
            echo ">>> ENOSPC gate PASSED (OCI export complete without ENOSPC)"
            exit 0
        fi
        if echo "$line" | grep -qE '(ENOSPC|no space left|fatal:|error:)'; then
            echo ">>> ENOSPC gate FAILED: $line" >&2
            exit 1
        fi
    done < <($SSH 'sudo /usr/local/bin/fisherman /tmp/enospc-recipe.json' 2>&1)
    echo ">>> fisherman exited — OCI export completed successfully"


# Run the full plain install test (CI entry point).
# Expects ISO in {{output_dir}}; does not build.
plain-test-qemu target:
    #!/usr/bin/bash
    set -euo pipefail
    just output_dir={{output_dir}} qemu-mem={{qemu-mem}} plain-qemu-disk={{plain-qemu-disk}} \
         plain-qemu-monitor-live={{plain-qemu-monitor-live}} \
         plain-qemu-serial-live={{plain-qemu-serial-live}} \
         plain-qemu-ssh-port={{plain-qemu-ssh-port}} \
         plain-boot-qemu-live {{target}}
    just output_dir={{output_dir}} plain-qemu-ssh-port={{plain-qemu-ssh-port}} \
         plain-qemu-monitor-live={{plain-qemu-monitor-live}} \
         plain-qemu-disk={{plain-qemu-disk}} \
         plain-install-qemu {{target}}
    just output_dir={{output_dir}} qemu-mem={{qemu-mem}} plain-qemu-disk={{plain-qemu-disk}} \
         plain-qemu-monitor-installed={{plain-qemu-monitor-installed}} \
         plain-qemu-serial-installed={{plain-qemu-serial-installed}} \
         plain-boot-qemu-installed {{target}}
    just output_dir={{output_dir}} plain-qemu-monitor-installed={{plain-qemu-monitor-installed}} \
         plain-qemu-serial-installed={{plain-qemu-serial-installed}} \
         plain-verify-qemu {{target}}

# Boot the live ISO in QEMU for a plain install test.
plain-boot-qemu-live target:
    #!/usr/bin/bash
    set -euo pipefail
    QEMU=$(command -v /usr/libexec/qemu-kvm /usr/bin/qemu-kvm \
               /usr/bin/qemu-system-x86_64 \
               /home/linuxbrew/.linuxbrew/bin/qemu-system-x86_64 2>/dev/null | head -1)
    [[ -z "$QEMU" ]] && { echo "qemu-kvm / qemu-system-x86_64 not found" >&2; exit 1; }
    ISO=""
    for f in \
        "{{output_dir}}/{{target}}-debug-live.iso" \
        "{{output_dir}}/{{target}}-live.iso" \
        {{output_dir}}/{{target}}-live-*.iso; do
        [[ -f "$f" ]] && { ISO="$f"; break; }
    done
    [[ -z "$ISO" ]] && { echo "No ISO found — run: just debug=1 iso-sd-boot {{target}}" >&2; exit 1; }
    OVMF_CODE=""; OVMF_VARS=""
    for f in /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_CODE.fd \
              /usr/share/edk2/ovmf/OVMF_CODE.fd /usr/share/ovmf/OVMF.fd \
              /home/linuxbrew/.linuxbrew/share/qemu/edk2-x86_64-code.fd \
              /home/linuxbrew/.linuxbrew/Cellar/qemu/11.0.1/share/qemu/edk2-x86_64-code.fd; do
        [[ -f "$f" ]] && { OVMF_CODE="$f"; break; }
    done
    for f in /usr/share/OVMF/OVMF_VARS_4M.fd /usr/share/OVMF/OVMF_VARS.fd \
              /usr/share/edk2/ovmf/OVMF_VARS.fd \
              /var/home/jorge/VMs/bluefin-test/OVMF_VARS.fd \
              /var/home/james/dev/ostree-composefs-rebase/ovmf_vars.fd \
              /var/home/james/.local/share/Trash/files/e2e-logs-3/ovmf_vars.fd \
              /home/linuxbrew/.linuxbrew/share/qemu/edk2-i386-vars.fd; do
        if [[ -f "$f" ]]; then cp "$f" /var/tmp/dakota-plain-qemu-live-vars.fd; OVMF_VARS=/var/tmp/dakota-plain-qemu-live-vars.fd; break; fi
    done
    [[ -z "$OVMF_CODE" ]] && { echo "OVMF firmware not found" >&2; exit 1; }
    [[ -f "{{plain-qemu-disk}}" ]] || truncate -s 64G "{{plain-qemu-disk}}"
    # Scratch disk: 16G sparse file mounted over /var/tmp in the live VM to
    # give skopeo disk-backed space for VFS blob extraction (~9 GB blob).
    [[ -f "{{plain-scratch-disk}}" ]] || truncate -s 16G "{{plain-scratch-disk}}"
    rm -f "{{plain-qemu-monitor-live}}" "{{plain-qemu-serial-live}}"
    QEMU_ACCEL="-accel kvm"
    QEMU_PREFIX=""
    if ! test -r /dev/kvm 2>/dev/null; then
        if sudo test -r /dev/kvm 2>/dev/null; then
            QEMU_PREFIX="sudo"
        else
            QEMU_ACCEL="-accel tcg,thread=multi"
        fi
    fi
    CPU_FLAG="-cpu host"
    [[ "$QEMU_ACCEL" =~ tcg ]] && CPU_FLAG="-cpu qemu64"
    echo "Booting live ISO: $ISO (qemu-mem={{qemu-mem}} MiB)"
    $QEMU_PREFIX "$QEMU" \
        -machine q35 $CPU_FLAG -m {{qemu-mem}} -smp {{qemu-smp}} $QEMU_ACCEL \
        -drive "if=pflash,format=raw,readonly=on,file=${OVMF_CODE}" \
        -drive "if=pflash,format=raw,file=${OVMF_VARS}" \
        -drive "if=none,id=iso,file=${ISO},media=cdrom,readonly=on,format=raw" \
        -device virtio-scsi-pci,id=scsi \
        -device scsi-cd,drive=iso \
        -drive "if=none,id=disk,file={{plain-qemu-disk}},format=raw,cache=unsafe" \
        -device virtio-blk-pci,drive=disk \
        -drive "if=none,id=scratch,file={{plain-scratch-disk}},format=raw,cache=unsafe" \
        -device virtio-blk-pci,drive=scratch \
        -netdev "user,id=net0,hostfwd=tcp::{{plain-qemu-ssh-port}}-:22" \
        -device virtio-net-pci,netdev=net0 \
        -monitor "unix:{{plain-qemu-monitor-live}},server,nowait" \
        -serial "file:{{plain-qemu-serial-live}}" \
        -display none \
        -daemonize
    echo "Live QEMU started (monitor: {{plain-qemu-monitor-live}})"
    SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -o PreferredAuthentications=password"
    echo "Waiting for live environment on port {{plain-qemu-ssh-port}}..."
    for i in $(seq 1 60); do
        if grep -q "DAKOTA_LIVE_READY\|debug-ssh-banner" "{{plain-qemu-serial-live}}" 2>/dev/null; then
            echo "Serial marker seen — polling SSH (sshd lags systemd-ready by ~10-20 s on KVM)..."
            # The serial marker fires when systemd declares the target reached,
            # but sshd finishes host-key generation after that and temporarily
            # resets connections (kex_exchange_identification: read: Connection
            # reset by peer).  Poll until SSH accepts, then add a small settling
            # sleep before handing control to plain-install-qemu.
            for j in $(seq 1 30); do
                if sshpass -p live ssh $SSH_OPTS liveuser@127.0.0.1 -p {{plain-qemu-ssh-port}} true 2>/dev/null; then
                    echo "Live environment ready (serial marker + SSH confirmed)."
                    sleep 3
                    break 2
                fi
                sleep 3
            done
            echo "ERROR: serial marker seen but SSH not ready after 90 s" >&2
            cat "{{plain-qemu-serial-live}}" >&2
            exit 1
        fi
        if sshpass -p live ssh $SSH_OPTS liveuser@127.0.0.1 -p {{plain-qemu-ssh-port}} true 2>/dev/null; then
            # SSH up before marker — add settling sleep
            echo "SSH responded (pre-marker) — waiting 15 s for sshd to stabilise..."
            sleep 15
            break
        fi
        [[ $i -eq 60 ]] && { echo "Timeout waiting for live environment" >&2; cat "{{plain-qemu-serial-live}}" >&2; exit 1; }
        sleep 5
    done

# Run fisherman plain (no-encryption) composefs install via SSH.
plain-install-qemu target:
    ./scripts/plain-install-qemu.sh "{{target}}" "{{plain-qemu-ssh-port}}" "{{plain-qemu-monitor-live}}" "{{fisher_repo}}"

# Boot the installed disk (no ISO) after plain-install-qemu.
plain-boot-qemu-installed target:
    #!/usr/bin/bash
    set -euo pipefail
    QEMU=$(command -v /usr/libexec/qemu-kvm /usr/bin/qemu-kvm \
               /usr/bin/qemu-system-x86_64 \
               /home/linuxbrew/.linuxbrew/bin/qemu-system-x86_64 2>/dev/null | head -1)
    [[ -z "$QEMU" ]] && { echo "qemu-kvm / qemu-system-x86_64 not found" >&2; exit 1; }
    OVMF_CODE=""; OVMF_VARS=""
    for f in /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_CODE.fd \
              /usr/share/edk2/ovmf/OVMF_CODE.fd /usr/share/ovmf/OVMF.fd \
              /home/linuxbrew/.linuxbrew/share/qemu/edk2-x86_64-code.fd \
              /home/linuxbrew/.linuxbrew/Cellar/qemu/11.0.1/share/qemu/edk2-x86_64-code.fd; do
        [[ -f "$f" ]] && { OVMF_CODE="$f"; break; }
    done
    for f in /usr/share/OVMF/OVMF_VARS_4M.fd /usr/share/OVMF/OVMF_VARS.fd \
              /usr/share/edk2/ovmf/OVMF_VARS.fd \
              /var/home/jorge/VMs/bluefin-test/OVMF_VARS.fd \
              /var/home/james/dev/ostree-composefs-rebase/ovmf_vars.fd \
              /var/home/james/.local/share/Trash/files/e2e-logs-3/ovmf_vars.fd \
              /home/linuxbrew/.linuxbrew/share/qemu/edk2-i386-vars.fd; do
        if [[ -f "$f" ]]; then cp "$f" /var/tmp/dakota-plain-qemu-installed-vars.fd; OVMF_VARS=/var/tmp/dakota-plain-qemu-installed-vars.fd; break; fi
    done
    [[ -z "$OVMF_CODE" ]] && { echo "OVMF firmware not found" >&2; exit 1; }
    rm -f "{{plain-qemu-monitor-installed}}" "{{plain-qemu-serial-installed}}"
    # Wait for the live QEMU to release the disk (monitor socket disappears on exit)
    for i in $(seq 1 20); do
        [[ -S "{{plain-qemu-monitor-live}}" ]] || break
        sleep 2
    done
    QEMU_ACCEL="-accel kvm"
    QEMU_PREFIX=""
    if ! test -r /dev/kvm 2>/dev/null; then
        if sudo test -r /dev/kvm 2>/dev/null; then
            QEMU_PREFIX="sudo"
        else
            QEMU_ACCEL="-accel tcg,thread=multi"
        fi
    fi
    CPU_FLAG="-cpu host"
    [[ "$QEMU_ACCEL" =~ tcg ]] && CPU_FLAG="-cpu qemu64"
    echo "Booting installed disk: {{plain-qemu-disk}} (qemu-mem={{qemu-mem}} MiB)"
    $QEMU_PREFIX "$QEMU" \
        -machine q35 $CPU_FLAG -m {{qemu-mem}} -smp {{qemu-smp}} $QEMU_ACCEL \
        -drive "if=pflash,format=raw,readonly=on,file=${OVMF_CODE}" \
        -drive "if=pflash,format=raw,file=${OVMF_VARS}" \
        -drive "if=none,id=disk,file={{plain-qemu-disk}},format=raw,cache=unsafe" \
        -device virtio-blk-pci,drive=disk \
        -netdev user,id=net0 \
        -device virtio-net-pci,netdev=net0 \
        -monitor "unix:{{plain-qemu-monitor-installed}},server,nowait" \
        -serial "file:{{plain-qemu-serial-installed}}" \
        -display none \
        -daemonize
    echo "Installed QEMU started (monitor: {{plain-qemu-monitor-installed}})"
    for i in $(seq 1 15); do
        [[ -S "{{plain-qemu-monitor-installed}}" ]] && break
        sleep 2
    done

# Verify the plain-installed system reaches the graphical target.
# Polls the serial log (console=ttyS0 is patched in by plain-install-qemu)
# for systemd's "Reached target Graphical Interface" message.
# Also screenshots the framebuffer for CI artifact upload.
plain-verify-qemu target:
    #!/usr/bin/bash
    set -euo pipefail
    SERIAL="{{plain-qemu-serial-installed}}"
    MONITOR="{{plain-qemu-monitor-installed}}"
    SCREENSHOT="/tmp/plain-screenshot-final.ppm"
    echo "Waiting for installed system to reach Graphical Interface (up to 5 min)..."
    DEADLINE=$((SECONDS + 300))
    while [[ $SECONDS -lt $DEADLINE ]]; do
        LOG=$(cat "$SERIAL" 2>/dev/null || true)
        if echo "$LOG" | grep -q "Reached target.*Graphical\|Reached target.*Multi-User\|login:"; then
            echo "✅ Installed system boot verified — plain composefs install succeeded"
            SOCAT_PREFIX=""
            if ! test -w "$MONITOR" 2>/dev/null; then SOCAT_PREFIX="sudo"; fi
            echo "screendump $SCREENSHOT" | $SOCAT_PREFIX socat - "UNIX-CONNECT:$MONITOR" 2>/dev/null || true
            bash "dakota/src/show-screenshot.sh" "$SCREENSHOT" "Installed system" 2>/dev/null || true
            echo "quit" | $SOCAT_PREFIX socat - "UNIX-CONNECT:$MONITOR" 2>/dev/null || true
            exit 0
        fi
        # Detect emergency shell / kernel panic — fast-fail
        if echo "$LOG" | grep -q "Emergency mode\|You are in emergency mode\|Kernel panic"; then
            echo "❌ Emergency shell or kernel panic detected" >&2
            echo "--- last 30 lines of serial log ---" >&2
            echo "$LOG" | tail -30 >&2
            exit 1
        fi
        sleep 5
    done
    echo "❌ Timeout: installed system did not reach graphical target in 5 minutes" >&2
    echo "--- last 30 lines of serial log ---" >&2
    cat "$SERIAL" 2>/dev/null | tail -30 >&2
    SOCAT_PREFIX=""
    if ! test -w "$MONITOR" 2>/dev/null; then SOCAT_PREFIX="sudo"; fi
    echo "screendump $SCREENSHOT" | $SOCAT_PREFIX socat - "UNIX-CONNECT:$MONITOR" 2>/dev/null || true
    exit 1
