#!/usr/bin/bash
# scripts/luks-install-qemu.sh
# Run fisherman LUKS install via SSH into the live QEMU VM.
# Reuses the same SSH logic as luks-install; install disk is /dev/vda in QEMU.

set -euo pipefail
# Single host-side reader for per-variant config (see scripts/variant-config.sh).
# shellcheck source=scripts/variant-config.sh
source "$(dirname "${BASH_SOURCE[0]}")/variant-config.sh"

if [[ $# -ne 4 ]]; then
    echo "Usage: $0 <target> <luks_passphrase> <ssh_port> <monitor_live_socket>" >&2
    exit 1
fi
TARGET="$1"
PASSPHRASE="$2"
SSH_PORT="$3"
MONITOR_LIVE="$4"
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -o PreferredAuthentications=password -o ServerAliveInterval=30 -o ServerAliveCountMax=20"
SSH="sshpass -p live ssh $SSH_OPTS liveuser@127.0.0.1 -p ${SSH_PORT}"
SCP="sshpass -p live scp $SSH_OPTS -P ${SSH_PORT}"


RECIPE_TMP=$(mktemp /tmp/luks-recipe-XXXXXX.json)
trap 'rm -f "${RECIPE_TMP}"' EXIT
COMPOSEFS_BACKEND=$(variant_composefs "${TARGET}")
BOOTLOADER=$(variant_bootloader_recipe "${TARGET}")
$SSH 'sudo cat /etc/bootc-installer/media-input.json' > "${RECIPE_TMP}"
python3 - "${RECIPE_TMP}" "${COMPOSEFS_BACKEND}" "${BOOTLOADER}" "${PASSPHRASE}" <<'PY'
import json
from pathlib import Path
import sys
path = Path(sys.argv[1])
media = json.loads(path.read_text())
recipe = dict(disk="/dev/vda", filesystem="btrfs", image=media["image"],
              targetImgref=media["targetImgref"], composeFsBackend=sys.argv[2] == "true",
              bootloader=sys.argv[3], hostname="dakota-luks-test",
              encryption={"type": "luks-passphrase", "passphrase": sys.argv[4]}, flatpaks=[])
path.write_text(json.dumps(recipe) + "\n")
PY


echo "Mounting scratch disk (/dev/vdb) over /var/tmp..."
printf 'live\n' | $SSH 'sudo -S -p "" bash -c "
    mkfs.ext4 -F /dev/vdb >/dev/null
    umount /var/tmp 2>/dev/null || true
    mount /dev/vdb /var/tmp
    echo \"/var/tmp is now disk-backed on /dev/vdb\"
"'

$SCP "${RECIPE_TMP}" liveuser@127.0.0.1:/tmp/luks-recipe.json
$SCP "scripts/fisherman-install.sh" liveuser@127.0.0.1:/tmp/fisherman-install.sh
if ! $SSH 'sudo bash /tmp/fisherman-install.sh /tmp/luks-recipe.json --enable-debug-ssh'; then
    echo "=== INSTALL FAILURE DIAGNOSTICS ==="
    $SSH 'sudo dmesg | tail -n 100' || true
    $SSH 'sudo journalctl -n 100 --no-pager' || true
    $SSH 'sudo systemctl status systemd-oomd --no-pager' || true
    exit 1
fi

echo "Patching BLS entries to enable dual serial+VT console and LUKS unlock..."
printf 'live\n' | $SSH 'sudo -S -p "" bash -c "
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
for _ in {1..30}; do
    [[ ! -S "${MONITOR_LIVE}" ]] && exit 0
    sleep 2
done
echo "quit" | $SOCAT_PREFIX socat - "UNIX-CONNECT:${MONITOR_LIVE}" 2>/dev/null || true
