#!/usr/bin/bash
# scripts/verify-post-install.sh
# Post-boot assertions against the *installed and booted* system for
# projectbluefin/dakota#651 ("[fisherman] e2e: add post-boot assertions for
# UEFI entries, Flatpak exclusion, and LUKS cmdline format").
#
# Connects as root over AF_VSOCK with the per-run key that the
# *-boot-qemu-installed recipe handed the guest as a systemd credential
# (scripts/guest-vsock.sh, #380). Nothing is patched onto the installed disk
# for this, so the system under test is the one users get. It checks the
# freshly-installed, rebooted system:
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
# Usage: verify-post-install.sh <vsock_state_dir> <encryption_type: none|luks-passphrase>
#   vsock_state_dir is the directory guest_vsock_prepare filled (key + CID).
#
# Exit code: 0 if every applicable soft assertion passes (or only non-strict
# ones fail), 1 if any STRICT_IDS assertion fails or a hard requirement is
# unmet, 2 if the installed system was never reachable over SSH (setup
# failure, not an assertion result).
#
# Three post-boot assertions are soft (observability-only): the UEFI
# BootCurrent entry, the installer Flatpak being absent, and the LUKS cmdline
# UUID. Each prints a TAP line (`ok - <id>` / `not ok - <id>`) and, on failure,
# blocks the lane only when its id is in the STRICT_IDS list. STRICT_IDS is
# empty by default, so a failing soft assertion is a ::warning:: and exits 0;
# a check graduates into STRICT_IDS only after the weekly graduation check
# (scripts/graduation-check.py) measures a green bar over the last 20 runs on
# main. SSH reachability and btrfs zstd compression/rootflags are hard
# requirements and always fail the lane regardless of STRICT_IDS.
#
# Per-assertion strictness (STRICT_IDS) is used rather than one global switch,
# so one never-greening check cannot hold back the others. Adapted from the
# tunaOS/tunaOS green-criteria design (see projectbluefin/dakota-iso#381).

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

# check_btrfs_compression <findmnt FSTYPE,OPTIONS for /var> <kernel cmdline>
check_btrfs_compression() {
    local mount="$1" cmdline="$2" token flags
    [[ "${mount}" == btrfs\ * ]] || return 1
    flags="${mount#* }"
    [[ ",${flags}," =~ ,compress=zstd(:1)?, ]] || return 1
    for token in ${cmdline}; do
        if [[ "${token}" == rootflags=* ]]; then
            flags="${token#rootflags=}"
            [[ ",${flags}," =~ ,compress=zstd(:1)?, ]] && return 0
        fi
    done
    return 1
}

# soft <id> <command...>: run a post-boot assertion that is observability-only
# until it graduates into the STRICT_IDS list. Prints a TAP line (`ok - <id>`
# / `not ok - <id>`) so the weekly graduation check can measure the check's
# pass rate across runs. A failure blocks the lane only when <id> is in
# STRICT_IDS; otherwise it emits a ::warning:: annotation and the script still
# exits 0. Hard requirements (SSH reachability, btrfs compression) are
# deliberately NOT soft: they always fail the lane. Per-assertion strictness is
# used rather than one global switch so one never-greening check cannot hold
# back the others.
soft() {
    local id="$1"; shift
    if "$@"; then
        echo "ok - ${id}"
    else
        echo "not ok - ${id}"
        if [[ " ${STRICT_IDS:-} " == *" ${id} "* ]]; then
            FAIL=1
        else
            echo "::warning title=Post-boot assertion '${id}' failed (non-blocking)::$id failed; add it to STRICT_IDS to block on it"
        fi
    fi
}

# check_no_installer_flatpak <flatpak_exit_code> <flatpak_output>
# Succeeds only if flatpak list ran (rc 0) and does not report the installer's
# own Flatpak (org.bootcinstaller). A flatpak list failure is a real problem,
# not a clean exclusion, so it fails too.
check_no_installer_flatpak() {
    local rc="$1" out="$2"
    if [[ "$rc" -ne 0 ]]; then
        echo "   flatpak list failed with code ${rc}: ${out}"
        return 1
    fi
    if printf '%s\n' "$out" | grep -q 'org.bootcinstaller'; then
        echo "   installer flatpak leaked: $(printf '%s\n' "$out" | grep 'org.bootcinstaller')"
        return 1
    fi
    return 0
}

# check_luks_uuid <kernel cmdline>
# Succeeds only if the cmdline carries a well-formed rd.luks.uuid= or
# rd.luks.name= UUID (8-4-4-4-12 hex).
check_luks_uuid() {
    local cmdline="$1"
    if printf '%s\n' "$cmdline" | grep -qE 'rd\.luks\.(uuid=(luks-)?[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}([[:space:]]|$)|name=[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}=)'; then
        return 0
    fi
    return 1
}

# Allow tests to source this file for the helper functions only.
(return 0 2>/dev/null) && return 0

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <vsock_state_dir> <encryption_type: none|luks-passphrase>" >&2
    exit 1
fi

VSOCK_DIR="$1"
ENCRYPTION_TYPE="$2"

# shellcheck source=scripts/guest-vsock.sh
source "$(dirname "${BASH_SOURCE[0]}")/guest-vsock.sh"
# A guest that was never given a key cannot be reached: setup failure.
guest_vsock_ssh "$VSOCK_DIR" || exit 2
SSH=("${GUEST_VSOCK_SSH[@]}")

echo "Waiting for installed system SSH over AF_VSOCK (CID $(cat "$VSOCK_DIR/cid"))..."
READY=0
if ! SSH_ERROR=$(mktemp "${TMPDIR:-/tmp}/installed-ssh.XXXXXX"); then
    echo "❌ Cannot create temporary file for SSH diagnostics in ${TMPDIR:-/tmp}" >&2
    exit 1
fi
trap 'rm -f "$SSH_ERROR"' EXIT
START_SECONDS=$SECONDS
SSH_RC=0
for ATTEMPT in $(seq 1 60); do
    # Keep only the last attempt's error in a private file.
    if "${SSH[@]}" true 2>"$SSH_ERROR"; then
        READY=1
        break
    else
        SSH_RC=$?
    fi
    sleep 5
done

if [[ "$READY" -ne 1 ]]; then
    echo "❌ Post-boot assertions SKIPPED: could not reach installed system over SSH after ${ATTEMPT} attempts ($((SECONDS - START_SECONDS)) seconds elapsed)" >&2
    echo "   Final SSH attempt exit status ${SSH_RC}; stderr:" >&2
    if [[ -s "$SSH_ERROR" ]]; then
        cat "$SSH_ERROR" >&2
    else
        echo "   (no SSH error output)" >&2
    fi
    # Compression is a required release property, not an observability check.
    exit 2
fi
rm -f "$SSH_ERROR"
trap - EXIT

MOUNT_OPTIONS=$("${SSH[@]}" 'findmnt -rn -o FSTYPE,OPTIONS -T /var') || exit 1
BOOT_CMDLINE=$("${SSH[@]}" 'cat /proc/cmdline') || exit 1
echo "Installed /var mount: ${MOUNT_OPTIONS}"
echo "Installed cmdline: ${BOOT_CMDLINE}"
if ! check_btrfs_compression "${MOUNT_OPTIONS}" "${BOOT_CMDLINE}"; then
    echo '❌ Installed btrfs compress=zstd[:1] mount option and persistent rootflags are required' >&2
    exit 1
fi
echo '✅ Installed btrfs zstd compression and persistent rootflags verified'

FAIL=0
TOTAL=2
[[ "$ENCRYPTION_TYPE" == "luks-passphrase" ]] && TOTAL=3

echo "=== Assertion 1/${TOTAL}: UEFI boot entry (fisherman #2) ==="
EFIBOOTMGR_OUT=$("${SSH[@]}" 'efibootmgr -v' 2>&1) || true
echo "$EFIBOOTMGR_OUT"
soft uefi-bootcurrent check_uefi_boot_entry "$EFIBOOTMGR_OUT"

echo "=== Assertion 2/${TOTAL}: installer Flatpak excluded (fisherman #1) ==="
FLATPAK_RC=0
FLATPAK_OUT=$("${SSH[@]}" 'flatpak list --system --app 2>&1') || FLATPAK_RC=$?
soft no-installer-flatpak check_no_installer_flatpak "$FLATPAK_RC" "$FLATPAK_OUT"

if [[ "$ENCRYPTION_TYPE" == "luks-passphrase" ]]; then
    echo "=== Assertion 3/${TOTAL}: LUKS cmdline UUID parseable (common#385) ==="
    CMDLINE=$("${SSH[@]}" 'cat /proc/cmdline' 2>&1) || true
    echo "$CMDLINE"
    soft luks-uuid check_luks_uuid "$CMDLINE"
fi

if [[ "$FAIL" -eq 0 ]]; then
    echo "✅ All post-boot assertions passed"
    exit 0
fi

echo "❌ One or more post-boot assertions failed" >&2
exit 1
