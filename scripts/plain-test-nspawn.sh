#!/usr/bin/bash
# scripts/plain-test-nspawn.sh
# End-to-end plain install testing using systemd-nspawn instead of QEMU.

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <target>" >&2
    exit 1
fi

TARGET="$1"
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
MOUNT_DIR="/var/tmp/dakota-nspawn-live-mount"
INSTALL_IMG="/var/tmp/dakota-plain-nspawn-install.img"
SCRATCH_IMG="/var/tmp/dakota-plain-nspawn-scratch.img"

echo "=== Cleaning up old mounts and loop devices ==="
sudo umount -R "$MOUNT_DIR" 2>/dev/null || true
sudo rm -rf "$MOUNT_DIR"
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

# Clean up trap to ensure loop devices and mounts are cleaned up on exit
cleanup() {
    echo "=== Cleaning up ==="
    if [[ -n "${NSPAWN_PID:-}" ]]; then
        echo "Stopping systemd-nspawn container (PID: $NSPAWN_PID)..."
        sudo kill "$NSPAWN_PID" 2>/dev/null || true
        wait "$NSPAWN_PID" 2>/dev/null || true
    fi
    sudo umount -R "$MOUNT_DIR" 2>/dev/null || true
    sudo rm -rf "$MOUNT_DIR" 2>/dev/null || true
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
# We use:
#   -x / --ephemeral: creates copy-on-write overlay over the SquashFS directory
#   --privileged: grants full access to host hardware/devices
#   --bind=/dev and --bind=/sys: shares host hardware devices (loop devices, etc.)
#   -n / --network-veth: private network namespace
#   -p tcp:2222:22: forwards host port 2222 to guest port 22
#   -b / --boot: boots the init system (systemd)
# We run systemd-nspawn in the background.
sudo systemd-nspawn -x -D "$MOUNT_DIR" --privileged --bind=/dev --bind=/sys -n -p "tcp:${SSH_PORT}:22" -b >/tmp/nspawn-live.log 2>&1 &
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
        cat /tmp/nspawn-live.log >&2
        exit 1
    fi
    sleep 3
    if [[ $i -eq 60 ]]; then
        echo "ERROR: Timeout waiting for SSH in nspawn container." >&2
        cat /tmp/nspawn-live.log >&2
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

# Prepare recipe JSON for fisherman
RECIPE_TMP=$(mktemp /tmp/nspawn-recipe-XXXXXX.json)
printf '{\n  "disk": "%s",\n  "filesystem": "btrfs",\n  "image": "%s",\n  "composeFsBackend": %s,\n  "bootloader": "%s",\n  "hostname": "dakota-nspawn-test",\n  "encryption": {"type": "none"},\n  "flatpaks": []\n}\n' \
    "$INSTALL_LOOP" "$INSTALL_IMAGE" "$COMPOSEFS_BACKEND" "$BOOTLOADER" > "$RECIPE_TMP"

$SCP "$RECIPE_TMP" liveuser@127.0.0.1:/tmp/plain-recipe.json
rm -f "$RECIPE_TMP"

# Upload and run fisherman wrapper
if [[ "${COMPOSEFS_BACKEND}" == "true" ]]; then
    echo "Running fisherman (composefs)..."
    $SCP "scripts/fisherman-install.sh" liveuser@127.0.0.1:/tmp/fisherman-install.sh
    $SSH "sudo /usr/local/bin/fisherman /tmp/plain-recipe.json"
else
    # Non-composefs path requires custom fisherman build like in plain-install-qemu.sh
    echo "Building patched fisherman for bootcDirect..."
    FISHERMAN_BIN=$(mktemp /tmp/nspawn-fisherman-XXXXXX)
    (cd "${FISHER_REPO}" && CGO_ENABLED=0 go build -o "${FISHERMAN_BIN}" ./cmd/fisherman/)
    $SCP "$FISHERMAN_BIN" liveuser@127.0.0.1:/tmp/fisherman
    rm -f "$FISHERMAN_BIN"
    $SSH "chmod +x /tmp/fisherman"
    echo "Running fisherman (bootcDirect)..."
    if ! $SSH "sudo /tmp/fisherman /tmp/plain-recipe.json"; then
        echo "=== INSTALL FAILURE DIAGNOSTICS ==="
        $SSH 'sudo journalctl -n 100 --no-pager' || true
        exit 1
    fi
fi

echo "=== Installation completed successfully inside nspawn container ==="

# Shut down live container
echo "Stopping live nspawn container..."
sudo kill "$NSPAWN_PID" 2>/dev/null || true
wait "$NSPAWN_PID" 2>/dev/null || true
NSPAWN_PID=""

# Disassociate scratch loop device
sudo losetup -d "$SCRATCH_LOOP" 2>/dev/null || true
SCRATCH_LOOP=""

# The disk image $INSTALL_LOOP now contains the installed OS!
# Now, let's boot the installed disk image using systemd-nspawn -i!
echo "=== Booting installed OS using systemd-nspawn --image ($INSTALL_LOOP) ==="

# First, since systemd-nspawn --image needs standard GPT and mounting of filesystems,
# we need to check if the dissected image is recognized.
# Let's boot it.
sudo systemd-nspawn -x -i "$INSTALL_LOOP" --privileged --bind=/dev --bind=/sys -n -p "tcp:${SSH_PORT}:22" -b >/tmp/nspawn-installed.log 2>&1 &
NSPAWN_PID=$!

echo "systemd-nspawn installed PID: $NSPAWN_PID"

echo "Waiting for installed OS to boot and sshd to respond on port ${SSH_PORT}..."
# The credentials of the installed OS depend on the base image, but since we installed via fisherman,
# we should verify that it reaches the multi-user.target / boots correctly.
# If we just want to verify it booted, we can query its status or wait for SSH.
# Wait, does the installed system have a default user/password?
# Usually, bluefin/dakota images don't have default user passwords unless configured,
# but we can verify systemd status or check if systemd reaches target.
# Let's wait up to 60 seconds and run systemctl is-system-running inside the container.
for i in $(seq 1 60); do
    if sudo systemd-run -M "dakota-plain-nspawn-install" --pipe systemctl is-system-running >/dev/null 2>&1; then
        echo "Installed system is running!"
        break
    fi
    # Alternatively we can inspect the machine via machinectl
    if sudo machinectl status "dakota-plain-nspawn-install" >/dev/null 2>&1 || sudo machinectl list | grep -q "dakota-plain-nspawn-install"; then
        # Let's run a test command using machinectl shell
        STATUS=$(sudo machinectl shell root@dakota-plain-nspawn-install /usr/bin/systemctl is-system-running 2>/dev/null || echo "starting")
        echo "System state: $STATUS"
        if [[ "$STATUS" == "running" || "$STATUS" == "degraded" ]]; then
            echo "Installed system boot verified!"
            break
        fi
    fi
    if ! kill -0 "$NSPAWN_PID" 2>/dev/null; then
        echo "ERROR: installed OS nspawn exited prematurely." >&2
        cat /tmp/nspawn-installed.log >&2
        exit 1
    fi
    sleep 3
    if [[ $i -eq 60 ]]; then
        echo "ERROR: Timeout waiting for installed OS boot." >&2
        cat /tmp/nspawn-installed.log >&2
        exit 1
    fi
done

echo "✅ Installed system boot verified via systemd-nspawn!"
