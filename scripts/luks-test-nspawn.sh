#!/usr/bin/bash
# scripts/luks-test-nspawn.sh
# End-to-end LUKS encrypted install testing using systemd-nspawn instead of QEMU.

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <target> [luks_passphrase]" >&2
    exit 1
fi

TARGET="$1"
PASSPHRASE="${2:-testpassphrase}"
SSH_PORT="2222"
FISHER_REPO="${FISHER_REPO:-./tmp_bootc_installer}"

# Check for systemd-nspawn
if ! command -v systemd-nspawn >/dev/null 2>&1; then
    echo "ERROR: systemd-nspawn is not installed." >&2
    echo "Please install the systemd-container package on the host:" >&2
    echo "  sudo apt-get update && sudo apt-get install -y systemd-container" >&2
    exit 1
fi

# Locate the squashfs rootfs
SQUASHFS="output/${TARGET}-rootfs.sfs"
if [[ ! -f "$SQUASHFS" ]]; then
    echo "ERROR: Squashfs image not found at $SQUASHFS." >&2
    echo "Please build it first: just debug=1 iso-sd-boot $TARGET" >&2
    exit 1
fi

# Host paths
MOUNT_DIR="/var/tmp/dakota-luks-nspawn-live-mount"
INSTALL_IMG="/var/tmp/dakota-luks-nspawn-install.img"
SCRATCH_IMG="/var/tmp/dakota-luks-nspawn-scratch.img"
INSTALLED_MOUNT="/var/tmp/dakota-luks-installed-mount"
MAPPER_NAME="dakota-luks-nspawn-decrypted"

echo "=== Cleaning up old mounts, loop devices, and crypt mappings ==="
sudo umount -R "$INSTALLED_MOUNT" 2>/dev/null || true
sudo rm -rf "$INSTALLED_MOUNT" 2>/dev/null || true
sudo umount -R "$MOUNT_DIR" 2>/dev/null || true
sudo rm -rf "$MOUNT_DIR" 2>/dev/null || true

if [[ -e "/dev/mapper/$MAPPER_NAME" ]]; then
    sudo cryptsetup luksClose "$MAPPER_NAME" 2>/dev/null || true
fi

for dev in $(losetup -j "$INSTALL_IMG" | cut -d: -f1); do
    sudo losetup -d "$dev" 2>/dev/null || true
done
for dev in $(losetup -j "$SCRATCH_IMG" | cut -d: -f1); do
    sudo losetup -d "$dev" 2>/dev/null || true
done

mkdir -p "$MOUNT_DIR"

echo "=== Mounting live Squashfs image ==="
sudo mount -o loop,ro "$SQUASHFS" "$MOUNT_DIR"

echo "=== Preparing target disk and scratch images ==="
truncate -s 64G "$INSTALL_IMG"
truncate -s 16G "$SCRATCH_IMG"

# Setup loop devices with partition scanning
INSTALL_LOOP=$(sudo losetup --find --show -P "$INSTALL_IMG")
SCRATCH_LOOP=$(sudo losetup --find --show -P "$SCRATCH_IMG")

echo "Target Disk Loopback: $INSTALL_LOOP"
echo "Scratch Disk Loopback: $SCRATCH_LOOP"

# Format scratch disk as ext4
sudo mkfs.ext4 -F "$SCRATCH_LOOP" >/dev/null

# Clean up trap to ensure loop devices, crypt mappings, and mounts are cleaned up on exit
cleanup() {
    echo "=== Cleaning up ==="
    if [[ -n "${NSPAWN_PID:-}" ]]; then
        echo "Stopping systemd-nspawn container (PID: $NSPAWN_PID)..."
        sudo kill "$NSPAWN_PID" 2>/dev/null || true
        wait "$NSPAWN_PID" 2>/dev/null || true
    fi
    sudo umount -R "$INSTALLED_MOUNT" 2>/dev/null || true
    sudo rm -rf "$INSTALLED_MOUNT" 2>/dev/null || true
    sudo umount -R "$MOUNT_DIR" 2>/dev/null || true
    sudo rm -rf "$MOUNT_DIR" 2>/dev/null || true

    if [[ -e "/dev/mapper/$MAPPER_NAME" ]]; then
        sudo cryptsetup luksClose "$MAPPER_NAME" 2>/dev/null || true
    fi

    if [[ -n "${INSTALL_LOOP:-}" ]]; then
        sudo losetup -d "$INSTALL_LOOP" 2>/dev/null || true
    fi
    if [[ -n "${SCRATCH_LOOP:-}" ]]; then
        sudo losetup -d "$SCRATCH_LOOP" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# Get payload ref and configuration details
PAYLOAD_IMAGE=$(cat "${TARGET}/payload_ref" | tr -d '[:space:]')
LIVE_TARGET=$(cat "${TARGET}/live_target" 2>/dev/null | tr -d '[:space:]' || echo "${TARGET}")
BOOTLOADER_VARIANT=$(echo "$LIVE_TARGET" | sed 's/-nvidia-open$//;s/-nvidia$//')
COMPOSEFS_BACKEND=$(cat "live/src/${BOOTLOADER_VARIANT}/composefs" 2>/dev/null | tr -d '[:space:]' || echo "true")
BOOTLOADER=$(cat "live/src/${BOOTLOADER_VARIANT}/bootloader" 2>/dev/null | tr -d '[:space:]' || echo "systemd")
if [[ "${BOOTLOADER}" == "grub" ]]; then BOOTLOADER="grub2"; fi

# 1. Boot the live environment under systemd-nspawn
echo "=== Booting live environment using systemd-nspawn ==="
sudo systemd-nspawn -x -D "$MOUNT_DIR" --privileged --bind=/dev --bind=/sys -n -p "tcp:${SSH_PORT}:22" -b >/tmp/nspawn-live-luks.log 2>&1 &
NSPAWN_PID=$!

echo "systemd-nspawn live PID: $NSPAWN_PID"

SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -o PreferredAuthentications=password -o ServerAliveInterval=30 -o ServerAliveCountMax=20"
SSH="sshpass -p live ssh $SSH_OPTS liveuser@127.0.0.1 -p ${SSH_PORT}"
SCP="sshpass -p live scp $SSH_OPTS -P ${SSH_PORT}"

echo "Waiting for live environment to boot and sshd to respond on port ${SSH_PORT}..."
for i in $(seq 1 60); do
    if sshpass -p live ssh $SSH_OPTS liveuser@127.0.0.1 -p ${SSH_PORT} true 2>/dev/null; then
        echo "Live environment SSH is ready!"
        break
    fi
    if ! kill -0 "$NSPAWN_PID" 2>/dev/null; then
        echo "ERROR: systemd-nspawn exited prematurely." >&2
        cat /tmp/nspawn-live-luks.log >&2
        exit 1
    fi
    sleep 3
    if [[ $i -eq 60 ]]; then
        echo "ERROR: Timeout waiting for SSH in nspawn container." >&2
        cat /tmp/nspawn-live-luks.log >&2
        exit 1
    fi
done

# Inside the container, mount the scratch loop device over /var/tmp
echo "Mounting scratch device ($SCRATCH_LOOP) over /var/tmp inside container..."
$SSH "sudo mount $SCRATCH_LOOP /var/tmp"

# Check if image exists in local storage of live container
if $SSH "sudo podman image exists '${PAYLOAD_IMAGE}' 2>/dev/null"; then
    INSTALL_IMAGE="containers-storage:${PAYLOAD_IMAGE}"
    echo "Image found in local containers-storage — using offline install."
else
    INSTALL_IMAGE="docker://${PAYLOAD_IMAGE}"
    echo "Image not in local store — fisherman will pull from network."
fi

# Prepare recipe JSON for LUKS fisherman install
RECIPE_TMP=$(mktemp /tmp/nspawn-luks-recipe-XXXXXX.json)
printf '{\n  "disk": "%s",\n  "filesystem": "btrfs",\n  "image": "%s",\n  "composeFsBackend": %s,\n  "bootloader": "%s",\n  "hostname": "dakota-luks-nspawn-test",\n  "encryption": {"type": "luks-passphrase", "passphrase": "%s"},\n  "flatpaks": []\n}\n' \
    "$INSTALL_LOOP" "$INSTALL_IMAGE" "$COMPOSEFS_BACKEND" "$BOOTLOADER" "$PASSPHRASE" > "$RECIPE_TMP"

$SCP "$RECIPE_TMP" liveuser@127.0.0.1:/tmp/luks-recipe.json
rm -f "$RECIPE_TMP"

# Upload and run fisherman wrapper
if [[ "${COMPOSEFS_BACKEND}" == "true" ]]; then
    echo "Running fisherman (composefs LUKS)..."
    $SCP "scripts/fisherman-install.sh" liveuser@127.0.0.1:/tmp/fisherman-install.sh
    $SSH "sudo /usr/local/bin/fisherman /tmp/luks-recipe.json"
else
    # Non-composefs path requires custom fisherman build like in luks-install-qemu.sh
    echo "Building patched fisherman for bootcDirect..."
    FISHERMAN_BIN=$(mktemp /tmp/nspawn-luks-fisherman-XXXXXX)
    (cd "${FISHER_REPO}" && CGO_ENABLED=0 go build -o "${FISHERMAN_BIN}" ./cmd/fisherman/)
    $SCP "$FISHERMAN_BIN" liveuser@127.0.0.1:/tmp/fisherman
    rm -f "$FISHERMAN_BIN"
    $SSH "chmod +x /tmp/fisherman"
    echo "Running fisherman (bootcDirect)..."
    if ! $SSH "sudo /tmp/fisherman /tmp/luks-recipe.json"; then
        echo "=== INSTALL FAILURE DIAGNOSTICS ==="
        $SSH 'sudo journalctl -n 100 --no-pager' || true
        exit 1
    fi
fi

echo "=== LUKS Installation completed successfully inside nspawn container ==="

# Shut down live container
echo "Stopping live nspawn container..."
sudo kill "$NSPAWN_PID" 2>/dev/null || true
wait "$NSPAWN_PID" 2>/dev/null || true
NSPAWN_PID=""

# Disassociate scratch loop device
sudo losetup -d "$SCRATCH_LOOP" 2>/dev/null || true
SCRATCH_LOOP=""

# Unmount live squashfs rootfs to prepare for boot stage
sudo umount -R "$MOUNT_DIR" 2>/dev/null || true

# Find LUKS encrypted partition on the loop device
echo "Locating LUKS encrypted partition..."
LUKS_PART=$(lsblk -nrpo NAME,FSTYPE "$INSTALL_LOOP" | awk '$2=="crypto_LUKS"{print $1;exit}')
if [[ -z "$LUKS_PART" ]]; then
    echo "ERROR: Could not find crypto_LUKS partition on $INSTALL_LOOP" >&2
    exit 1
fi
echo "LUKS Partition: $LUKS_PART"

# Unlock the LUKS partition on the host
echo "Unlocking LUKS partition $LUKS_PART on host..."
echo -n "$PASSPHRASE" | sudo cryptsetup luksOpen "$LUKS_PART" "$MAPPER_NAME"

# Mount the decrypted partition
mkdir -p "$INSTALLED_MOUNT"
echo "Mounting decrypted rootfs /dev/mapper/$MAPPER_NAME to $INSTALLED_MOUNT..."
sudo mount "/dev/mapper/$MAPPER_NAME" "$INSTALLED_MOUNT"

# Mount boot and EFI partitions if present
BOOT_PART=$(lsblk -nrpo NAME,PARTLABEL "$INSTALL_LOOP" | awk '$2=="boot"{print $1;exit}')
if [[ -n "$BOOT_PART" ]]; then
    echo "Mounting boot partition $BOOT_PART..."
    sudo mkdir -p "$INSTALLED_MOUNT/boot"
    sudo mount "$BOOT_PART" "$INSTALLED_MOUNT/boot"
fi

EFI_PART=$(lsblk -nrpo NAME,PARTLABEL "$INSTALL_LOOP" | awk '$2=="EFI-System"||$2=="EFI"||$2=="ESP"{print $1;exit}')
if [[ -n "$EFI_PART" ]]; then
    echo "Mounting EFI system partition $EFI_PART..."
    sudo mkdir -p "$INSTALLED_MOUNT/boot/efi"
    sudo mount "$EFI_PART" "$INSTALLED_MOUNT/boot/efi"
fi

# 2. Boot the installed system under systemd-nspawn
echo "=== Booting LUKS-installed OS using systemd-nspawn ==="
sudo systemd-nspawn -x -D "$INSTALLED_MOUNT" --privileged --bind=/dev --bind=/sys -n -p "tcp:${SSH_PORT}:22" -b >/tmp/nspawn-installed-luks.log 2>&1 &
NSPAWN_PID=$!

echo "systemd-nspawn installed PID: $NSPAWN_PID"

echo "Waiting for LUKS-installed OS to boot..."
for i in $(seq 1 60); do
    if sudo machinectl status "dakota-luks-installed-mount" >/dev/null 2>&1 || sudo machinectl list | grep -q "dakota-luks-installed-mount"; then
        STATUS=$(sudo machinectl shell root@dakota-luks-installed-mount /usr/bin/systemctl is-system-running 2>/dev/null || echo "starting")
        echo "System state: $STATUS"
        if [[ "$STATUS" == "running" || "$STATUS" == "degraded" ]]; then
            echo "Installed system boot verified!"
            break
        fi
    fi
    if ! kill -0 "$NSPAWN_PID" 2>/dev/null; then
        echo "ERROR: installed OS nspawn exited prematurely." >&2
        cat /tmp/nspawn-installed-luks.log >&2
        exit 1
    fi
    sleep 3
    if [[ $i -eq 60 ]]; then
        echo "ERROR: Timeout waiting for installed OS boot." >&2
        cat /tmp/nspawn-installed-luks.log >&2
        exit 1
    fi
done

echo "✅ LUKS Installed system boot verified via systemd-nspawn!"
