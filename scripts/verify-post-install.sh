#!/usr/bin/bash
# scripts/verify-post-install.sh
# Post-boot assertions against the *installed and booted* system for
# projectbluefin/dakota#651 ("[fisherman] e2e: add post-boot assertions for
# UEFI entries, Flatpak exclusion, and LUKS cmdline format").
#
# Connects over SSH (enabled by scripts/fisherman-install.sh's
# --enable-debug-ssh path — root:root, mirroring the live ISO's DEBUG=1
# convention) to the freshly-installed, rebooted system and checks:
#
#   1. efibootmgr -v shows a BootCurrent + Boot#### entry written by the
#      installer (fisherman #2: /sys/firmware/efi/efivars bind-mount).
#   2. flatpak list --system --app excludes the installer's own Flatpak
#      (fisherman #1: CopyFlatpaks must not carry over live-ISO-only apps).
#   3. (LUKS installs only) /proc/cmdline contains a parseable LUKS UUID via
#      rd.luks.uuid= or rd.luks.name= (projectbluefin/common#385).
#
# Usage: verify-post-install.sh <ssh_port> <encryption_type: none|luks-passphrase>
#
# Exit code: 0 if all applicable assertions pass, 1 if any fails.

set -uo pipefail

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <ssh_port> <encryption_type: none|luks-passphrase>" >&2
    exit 1
fi

SSH_PORT="$1"
ENCRYPTION_TYPE="$2"

SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -o PreferredAuthentications=password -o ServerAliveInterval=10 -o ServerAliveCountMax=6"
SSH="sshpass -p root ssh $SSH_OPTS root@127.0.0.1 -p ${SSH_PORT}"

echo "Waiting for installed system SSH on port ${SSH_PORT}..."
READY=0
for i in $(seq 1 60); do
    if $SSH true 2>/dev/null; then
        READY=1
        break
    fi
    sleep 5
done

if [[ "$READY" -ne 1 ]]; then
    echo "❌ Post-boot assertions SKIPPED: could not reach installed system over SSH after 5 minutes" >&2
    echo "   (debug SSH may not have been enabled, or sshd/network wasn't up in time)" >&2
    exit 1
fi

FAIL=0
TOTAL=2
[[ "$ENCRYPTION_TYPE" == "luks-passphrase" ]] && TOTAL=3

echo "=== Assertion 1/${TOTAL}: UEFI boot entry (fisherman #2) ==="
EFIBOOTMGR_OUT=$($SSH 'efibootmgr -v' 2>&1) || true
echo "$EFIBOOTMGR_OUT"
if echo "$EFIBOOTMGR_OUT" | grep -q "BootCurrent" && echo "$EFIBOOTMGR_OUT" | grep -qE "^Boot[0-9A-Fa-f]{4}"; then
    echo "✅ UEFI boot entry present (BootCurrent + Boot#### entries found)"
else
    echo "❌ No UEFI boot entry found — expected 'BootCurrent' and 'Boot####' lines from efibootmgr -v"
    FAIL=1
fi

echo "=== Assertion 2/${TOTAL}: installer Flatpak excluded (fisherman #1) ==="
FLATPAK_OUT=$($SSH 'flatpak list --system --app 2>/dev/null | grep org.bootcinstaller' 2>&1) || true
if [[ -z "$FLATPAK_OUT" ]]; then
    echo "✅ Installer Flatpak (org.bootcinstaller) not present on installed system"
else
    echo "❌ Installer Flatpak leaked onto installed system: $FLATPAK_OUT"
    FAIL=1
fi

if [[ "$ENCRYPTION_TYPE" == "luks-passphrase" ]]; then
    echo "=== Assertion 3/${TOTAL}: LUKS cmdline UUID parseable (common#385) ==="
    CMDLINE=$($SSH 'cat /proc/cmdline' 2>&1) || true
    echo "$CMDLINE"
    if echo "$CMDLINE" | grep -qE 'rd\.luks\.(uuid|name)=[A-Za-z0-9-]+'; then
        echo "✅ /proc/cmdline contains a parseable rd.luks.uuid=/rd.luks.name= UUID"
    else
        echo "❌ /proc/cmdline missing a parseable rd.luks.uuid=/rd.luks.name= entry"
        FAIL=1
    fi
fi

if [[ "$FAIL" -eq 0 ]]; then
    echo "✅ All post-boot assertions passed"
    exit 0
else
    echo "❌ One or more post-boot assertions failed" >&2
    exit 1
fi
