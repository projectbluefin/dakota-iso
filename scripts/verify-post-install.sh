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
#   1. efibootmgr -v shows BootCurrent pointing at a Boot#### entry that the
#      installer wrote to NVRAM (fisherman #2: /sys/firmware/efi/efivars
#      bind-mount) — i.e. an HD() device path with a vendor loader under
#      \EFI\<vendor>\, not an OVMF auto-enumerated device entry and not the
#      removable-media fallback \EFI\BOOT\BOOTX64.EFI.
#   2. flatpak list --system --app excludes the installer's own Flatpak
#      (fisherman #1: CopyFlatpaks must not carry over live-ISO-only apps).
#   3. (LUKS installs only) /proc/cmdline contains a well-formed LUKS UUID
#      (8-4-4-4-12 hex) via rd.luks.uuid=[luks-]<uuid> or
#      rd.luks.name=<uuid>=<name> (projectbluefin/common#385).
#
# Usage: verify-post-install.sh <ssh_port> <encryption_type: none|luks-passphrase>
#
# Exit code: 0 if all applicable assertions pass, 1 if any fails, 2 if the
# installed system was never reachable over SSH (setup failure, not an
# assertion result).
#
# Assertion failures are OBSERVABILITY-ONLY by default: they print a warning
# (plus a ::warning:: annotation under GitHub Actions) and exit 0 until the
# upstream dependencies (fisherman #1, #2, common#385) land. Set
# STRICT_POST_BOOT=1 to make them exit 1. CI does not set it, so a green run
# does NOT prove the invariants.
#
# SSH unreachable is likewise non-blocking by default (warning + exit 0)
# until the debug-SSH path has a first green E2E run on main; with
# STRICT_POST_BOOT=1 it exits 2.

set -uo pipefail

# check_uefi_boot_entry <efibootmgr -v output>
# Succeeds only if BootCurrent names an entry whose device path is an HD()
# partition with a vendor loader under \EFI\<vendor>\.  OVMF auto-creates
# Boot#### entries for every attached disk (PciRoot(...) with no file path),
# UiApp and the EFI shell, and a disk booted via the removable-media fallback
# loader \EFI\BOOT\BOOTX64.EFI has no installer-written entry at all — none of
# those may satisfy this check.
check_uefi_boot_entry() {
    local out="$1" current entry
    current=$(printf '%s\n' "$out" | sed -n 's/^BootCurrent:[[:space:]]*\([0-9A-Fa-f]\{4\}\).*/\1/p' | head -1)
    if [[ -z "$current" ]]; then
        echo "   BootCurrent not reported"
        return 1
    fi
    entry=$(printf '%s\n' "$out" | grep -iE "^Boot${current}[* ]" | head -1)
    if [[ -z "$entry" ]]; then
        echo "   BootCurrent=${current} has no matching Boot${current} entry"
        return 1
    fi
    if ! printf '%s\n' "$entry" | grep -qE 'HD\('; then
        echo "   Boot${current} is not an HD() partition entry (OVMF auto-enumerated?): ${entry}"
        return 1
    fi
    if ! printf '%s\n' "$entry" | grep -qiE '\\EFI\\[^\\]+\\[^\\]+\.efi'; then
        echo "   Boot${current} has no \\EFI\\<vendor>\\*.efi loader path: ${entry}"
        return 1
    fi
    if printf '%s\n' "$entry" | grep -qiE '\\EFI\\BOOT\\'; then
        echo "   Boot${current} is the removable-media fallback loader, not an installer-written entry: ${entry}"
        return 1
    fi
    echo "   BootCurrent=${current} -> ${entry}"
    return 0
}

# Allow tests to source this file for the helper functions only.
(return 0 2>/dev/null) && return 0

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
for _ in $(seq 1 60); do
    if $SSH true 2>/dev/null; then
        READY=1
        break
    fi
    sleep 5
done

if [[ "$READY" -ne 1 ]]; then
    echo "❌ Post-boot assertions SKIPPED: could not reach installed system over SSH after 5 minutes" >&2
    echo "   (debug SSH may not have been enabled, or sshd/network wasn't up in time)" >&2
    if [[ "${STRICT_POST_BOOT:-0}" != "1" ]]; then
        echo "⚠️ SSH unreachable is NON-BLOCKING until the debug-SSH path has a green E2E run on main. Set STRICT_POST_BOOT=1 to fail hard." >&2
        [[ -n "${GITHUB_ACTIONS:-}" ]] && echo "::warning title=Post-boot assertions skipped (SSH unreachable, non-blocking)::verify-post-install.sh could not reach the installed system over SSH; see log. Set STRICT_POST_BOOT=1 to gate."
        exit 0
    fi
    exit 2
fi

FAIL=0
TOTAL=2
[[ "$ENCRYPTION_TYPE" == "luks-passphrase" ]] && TOTAL=3

echo "=== Assertion 1/${TOTAL}: UEFI boot entry (fisherman #2) ==="
EFIBOOTMGR_OUT=$($SSH 'efibootmgr -v' 2>&1) || true
echo "$EFIBOOTMGR_OUT"
if check_uefi_boot_entry "$EFIBOOTMGR_OUT"; then
    echo "✅ Booted via installer-written UEFI NVRAM entry"
else
    echo "❌ BootCurrent does not reference an installer-written UEFI entry (HD() + \\EFI\\<vendor>\\*.efi)"
    FAIL=1
fi

echo "=== Assertion 2/${TOTAL}: installer Flatpak excluded (fisherman #1) ==="
FLATPAK_RC=0
FLATPAK_OUT=$($SSH 'flatpak list --system --app 2>&1') || FLATPAK_RC=$?
if [[ "$FLATPAK_RC" -ne 0 ]]; then
    echo "❌ flatpak list command failed with code ${FLATPAK_RC}: ${FLATPAK_OUT}"
    FAIL=1
elif echo "$FLATPAK_OUT" | grep -q 'org.bootcinstaller'; then
    echo "❌ Installer Flatpak leaked onto installed system: $(echo "$FLATPAK_OUT" | grep 'org.bootcinstaller')"
    FAIL=1
else
    echo "✅ Installer Flatpak (org.bootcinstaller) not present on installed system"
fi

if [[ "$ENCRYPTION_TYPE" == "luks-passphrase" ]]; then
    echo "=== Assertion 3/${TOTAL}: LUKS cmdline UUID parseable (common#385) ==="
    CMDLINE=$($SSH 'cat /proc/cmdline' 2>&1) || true
    echo "$CMDLINE"
    if echo "$CMDLINE" | grep -qE 'rd\.luks\.(uuid=(luks-)?[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}([[:space:]]|$)|name=[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}=)'; then
        echo "✅ /proc/cmdline contains a well-formed rd.luks.uuid=/rd.luks.name= UUID"
    else
        echo "❌ /proc/cmdline missing a well-formed (8-4-4-4-12) rd.luks.uuid=/rd.luks.name= UUID"
        FAIL=1
    fi
fi

if [[ "$FAIL" -eq 0 ]]; then
    echo "✅ All post-boot assertions passed"
    exit 0
fi

echo "❌ One or more post-boot assertions failed" >&2
if [[ "${STRICT_POST_BOOT:-0}" != "1" ]]; then
    echo "⚠️ Post-boot assertions FAILED but are NON-BLOCKING (observability-only until fisherman #1/#2 and common#385 land). Set STRICT_POST_BOOT=1 to fail hard." >&2
    [[ -n "${GITHUB_ACTIONS:-}" ]] && echo "::warning title=Post-boot assertions failed (non-blocking)::verify-post-install.sh assertions failed; see log. Set STRICT_POST_BOOT=1 to gate."
    exit 0
fi
exit 1
