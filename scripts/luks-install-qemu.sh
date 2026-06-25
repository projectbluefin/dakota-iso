#!/usr/bin/bash
# scripts/luks-install-qemu.sh
# Run fisherman LUKS install via SSH into the live QEMU VM.
# Reuses the same SSH logic as luks-install; install disk is /dev/vda in QEMU.

set -euo pipefail

if [[ $# -lt 5 ]]; then
    echo "Usage: $0 <target> <luks_passphrase> <ssh_port> <monitor_live_socket> <fisher_repo>" >&2
    exit 1
fi

TARGET="$1"
PASSPHRASE="$2"
SSH_PORT="$3"
MONITOR_LIVE="$4"
FISHER_REPO="$5"

DISK="/dev/vda"
PAYLOAD_IMAGE=$(cat "${TARGET}/payload_ref" | tr -d '[:space:]')
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -o PreferredAuthentications=password -o ServerAliveInterval=30 -o ServerAliveCountMax=20"
SSH="sshpass -p live ssh $SSH_OPTS liveuser@127.0.0.1 -p ${SSH_PORT}"
SCP="sshpass -p live scp $SSH_OPTS -P ${SSH_PORT}"

# Use local containers-storage if the image is cached there (offline install);
# otherwise fall back to a network pull via docker://.
if $SSH "sudo podman image exists '${PAYLOAD_IMAGE}' 2>/dev/null"; then
    INSTALL_IMAGE="containers-storage:${PAYLOAD_IMAGE}"
    echo "Image found in local containers-storage — using offline install."
else
    INSTALL_IMAGE="docker://${PAYLOAD_IMAGE}"
    echo "Image not in local store — fisherman will pull from network."
fi

RECIPE_TMP=$(mktemp /tmp/luks-recipe-XXXXXX.json)
trap "rm -f '${RECIPE_TMP}'" EXIT
LIVE_TARGET=$(cat "${TARGET}/live_target" 2>/dev/null | tr -d '[:space:]' || echo "${TARGET}")
BOOTLOADER_VARIANT=$(echo "$LIVE_TARGET" | sed 's/-nvidia-open$//;s/-nvidia$//')
COMPOSEFS_BACKEND=$(cat "live/src/${BOOTLOADER_VARIANT}/composefs" 2>/dev/null | tr -d '[:space:]' || echo "true")
BOOTLOADER=$(cat "live/src/${BOOTLOADER_VARIANT}/bootloader" 2>/dev/null | tr -d '[:space:]' || echo "systemd")

# Normalise "grub" → "grub2" for fisherman's recipe validator.
if [[ "${BOOTLOADER}" == "grub" ]]; then BOOTLOADER="grub2"; fi

# Determine target filesystem
FILESYSTEM="btrfs"

echo "Mounting scratch disk (/dev/vdb) over /var/tmp..."
$SSH 'sudo bash -c "
    mkfs.ext4 -F /dev/vdb >/dev/null
    umount /var/tmp 2>/dev/null || true
    mount /dev/vdb /var/tmp
    echo \"/var/tmp is now disk-backed on /dev/vdb\"
"'

if [[ "${COMPOSEFS_BACKEND}" == "true" ]]; then
    # Composefs path (dakota): podman-based install with VFS containers-storage.
    printf '{\n  "disk": "%s",\n  "filesystem": "%s",\n  "image": "%s",\n  "composeFsBackend": true,\n  "bootloader": "%s",\n  "hostname": "dakota-luks-test",\n  "encryption": {"type": "luks-passphrase", "passphrase": "%s"},\n  "flatpaks": []\n}\n' \
        "${DISK}" "${FILESYSTEM}" "${INSTALL_IMAGE}" "${BOOTLOADER}" "${PASSPHRASE}" > "${RECIPE_TMP}"
    $SCP "${RECIPE_TMP}" liveuser@127.0.0.1:/tmp/luks-recipe.json
    echo "Uploaded recipe — running fisherman (takes several minutes)..."
    $SCP "scripts/fisherman-install.sh" liveuser@127.0.0.1:/tmp/fisherman-install.sh
    $SSH 'sudo bash /tmp/fisherman-install.sh /tmp/luks-recipe.json'
else
    # Ostree path (stable, lts): bootcDirect — fisherman runs bootc natively.
    # Empty image triggers bootcDirect; targetImgref sets the day-2 rebase ref.
    # Fisherman emits --source-imgref containers-storage:<targetImgref> when
    # targetImgref is present and image is empty, resolving the payload from
    # the overlay additionalimagestore embedded in the squashfs.
    printf '{\n  "disk": "%s",\n  "filesystem": "%s",\n  "image": "",\n  "targetImgref": "%s",\n  "composeFsBackend": false,\n  "bootloader": "%s",\n  "hostname": "dakota-luks-test",\n  "encryption": {"type": "luks-passphrase", "passphrase": "%s"},\n  "flatpaks": []\n}\n' \
        "${DISK}" "${FILESYSTEM}" "${PAYLOAD_IMAGE}" "${BOOTLOADER}" "${PASSPHRASE}" > "${RECIPE_TMP}"
    $SCP "${RECIPE_TMP}" liveuser@127.0.0.1:/tmp/luks-recipe.json
    echo "Uploaded recipe — building patched fisherman for bootcDirect..."
    FISHERMAN_BIN=$(mktemp /tmp/fisherman-XXXXXX)
    trap "rm -f '${RECIPE_TMP}' '${FISHERMAN_BIN}'" EXIT
    (cd "${FISHER_REPO}" && CGO_ENABLED=0 go build -o "${FISHERMAN_BIN}" ./cmd/fisherman/)
    $SCP "${FISHERMAN_BIN}" liveuser@127.0.0.1:/tmp/fisherman
    $SSH 'chmod +x /tmp/fisherman'
    echo "Running fisherman (bootcDirect, takes several minutes)..."
    $SCP "scripts/fisherman-install.sh" liveuser@127.0.0.1:/tmp/fisherman-install.sh
    if ! $SSH 'sudo FISHERMAN_BIN=/tmp/fisherman bash /tmp/fisherman-install.sh /tmp/luks-recipe.json'; then
        echo "=== INSTALL FAILURE DIAGNOSTICS ==="
        echo "--- dmesg ---"
        $SSH 'sudo dmesg | tail -n 100' || true
        echo "--- journalctl ---"
        $SSH 'sudo journalctl -n 100 --no-pager' || true
        echo "--- systemd-oomd status ---"
        $SSH 'sudo systemctl status systemd-oomd --no-pager' || true
        exit 1
    fi
fi

echo "Patching BLS entries to enable dual serial+VT console and LUKS unlock..."
$SSH 'sudo bash -c "
    set -euo pipefail
    BOOT_PART=\"/dev/vda1\"
    LUKS_PART=\"/dev/vda2\"
    if ls /dev/vda3 >/dev/null 2>&1; then
        echo \"Detected 3 partitions layout (separate boot partition for GRUB)\"
        BOOT_PART=\"/dev/vda2\"
        LUKS_PART=\"/dev/vda3\"
    fi
    LUKS_UUID=\$(cryptsetup luksUUID \"\$LUKS_PART\" 2>/dev/null || echo \"\")
    TMP=\$(mktemp -d)
    trap \"umount \$TMP 2>/dev/null || true; rmdir \$TMP\" EXIT
    mount \"\$BOOT_PART\" \$TMP
    COUNT=0
    for entry in \$TMP/loader/entries/*.conf \$TMP/EFI/loader/entries/*.conf; do
        [[ -f \"\$entry\" ]] || continue
        if grep -q \"^options \" \"\$entry\" && ! grep -q \"console=tty0\" \"\$entry\"; then
            if [[ -n \"\$LUKS_UUID\" ]]; then
                sed -i \"s|^options .*|& console=tty0 console=ttyS0 rd.luks.name=\${LUKS_UUID}=root|\" \"\$entry\"
            else
                sed -i \"s|^options .*|& console=tty0 console=ttyS0|\" \"\$entry\"
            fi
            COUNT=\$((COUNT+1))
            echo \"  patched: \$(basename \$entry)\"
        fi
    done
    echo \"BLS patch: \$COUNT entries updated\"
"'

echo "Install complete. Shutting down live QEMU..."
SOCAT_PREFIX=""
if ! test -w "${MONITOR_LIVE}" 2>/dev/null; then SOCAT_PREFIX="sudo"; fi
echo "system_powerdown" | $SOCAT_PREFIX socat - "UNIX-CONNECT:${MONITOR_LIVE}" 2>/dev/null || true
sleep 5
echo "quit" | $SOCAT_PREFIX socat - "UNIX-CONNECT:${MONITOR_LIVE}" 2>/dev/null || true
