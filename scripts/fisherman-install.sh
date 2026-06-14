#!/usr/bin/bash
# fisherman-install.sh — run fisherman with composefs hostname-write workaround.
#
# fisherman exits non-zero on composefs sysroots when writing hostname because
# it calls `ostree admin --print-current-dir` (against the running system, not the
# target) AFTER unmounting the target disk.  On a composefs/bootc deployment that
# uses ostree/bootc/ instead of ostree/deploy/default/, the command returns exit 1.
# The OS install itself is complete; only this post-unmount hostname step fails.
#
# This wrapper detects that specific failure, re-mounts the installed root
# (unlocking LUKS first when needed), locates /etc in the deployment directory
# tree, and writes the hostname directly.
#
# Upstream bug: https://github.com/tuna-os/fisherman/issues
#
# Usage: fisherman-install.sh <recipe.json>
#   recipe.json must contain a "hostname" key (and "encryption.passphrase" for
#   LUKS installs) whose values are used when patching on failure.

set -euo pipefail

RECIPE="${1:-/tmp/plain-recipe.json}"

FISH_RC=0
/usr/local/bin/fisherman "$RECIPE" >/tmp/fish.log 2>&1 || FISH_RC=$?
cat /tmp/fish.log

[[ $FISH_RC -eq 0 ]] && exit 0

# Non-zero exit — check whether only the hostname write failed.
# fisherman logs "writing hostname" as an info message just before the ostree call.
if grep -q "writing hostname" /tmp/fish.log && \
   grep -q "ostree admin --print-current-dir" /tmp/fish.log; then

    echo "==> fisherman hostname write failed (composefs/ostree compat bug) — patching manually"

    # Extract hostname from the recipe JSON.
    HOSTNAME=$(grep -o '"hostname"[[:space:]]*:[[:space:]]*"[^"]*"' "$RECIPE" \
               | grep -o '"[^"]*"$' | tr -d '"' || echo "dakota")

    # Detect whether this is a LUKS install (crypto_LUKS partition on /dev/vda)
    # or a plain btrfs install.
    # Use -r (raw) to suppress lsblk tree characters (├─/└─) in the NAME field.
    LUKS_DEV=$(lsblk -nrpo NAME,FSTYPE /dev/vda \
               | awk '$2=="crypto_LUKS"{print $1;exit}')
    ROOT_DEV=$(lsblk -nrpo NAME,FSTYPE /dev/vda \
               | awk '$2=="btrfs"{print $1;exit}')

    MNT=$(mktemp -d /tmp/hostname-fix-XXXX)
    MAPPER="hostname-fix-$$"
    MOUNTED=0

    if [[ -n "$LUKS_DEV" ]]; then
        # LUKS install: extract passphrase from recipe JSON and unlock the container.
        PASSPHRASE=$(python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
print(d.get('encryption', {}).get('passphrase', ''))
" "$RECIPE" 2>/dev/null || echo "")
        if [[ -z "$PASSPHRASE" ]]; then
            echo "ERROR: could not extract LUKS passphrase from recipe — hostname not patched"
        elif printf '%s' "$PASSPHRASE" | cryptsetup luksOpen --key-file=- --batch-mode "$LUKS_DEV" "$MAPPER" 2>/tmp/cryptsetup-err.log; then
            if mount "/dev/mapper/$MAPPER" "$MNT"; then
                MOUNTED=1
            else
                echo "ERROR: mount /dev/mapper/$MAPPER failed — hostname not patched"
                cat /tmp/cryptsetup-err.log 2>/dev/null || true
                cryptsetup luksClose "$MAPPER" || true
            fi
        else
            echo "ERROR: cryptsetup luksOpen $LUKS_DEV failed — hostname not patched"
            cat /tmp/cryptsetup-err.log 2>/dev/null || true
        fi
    elif [[ -n "$ROOT_DEV" ]]; then
        # Plain install: mount the btrfs partition directly.
        if mount "$ROOT_DEV" "$MNT"; then
            MOUNTED=1
        else
            echo "ERROR: mount $ROOT_DEV failed — hostname not patched"
        fi
    else
        echo "ERROR: no btrfs or crypto_LUKS partition found on /dev/vda — hostname not patched"
    fi

    if [[ $MOUNTED -eq 1 ]]; then
        # Locate /etc in the deployment.
        #   composefs/bootc layout: ostree/bootc/deploy/<stateroot>/<checksum>/etc
        #   classic ostree layout:  ostree/deploy/<stateroot>/deploy/<checksum>/etc
        DEPLOY_ETC=""
        DEPLOY_ETC=$(find "$MNT/ostree/bootc/deploy" -maxdepth 3 -name etc -type d 2>/dev/null | head -1) \
            || true
        if [[ -z "$DEPLOY_ETC" ]]; then
            DEPLOY_ETC=$(find "$MNT/ostree/deploy" -maxdepth 4 -name etc -type d 2>/dev/null | head -1) \
                || true
        fi

        if [[ -n "$DEPLOY_ETC" ]]; then
            echo "$HOSTNAME" > "$DEPLOY_ETC/hostname"
            echo "==> hostname '$HOSTNAME' written to $DEPLOY_ETC/hostname"
        else
            echo "WARNING: deployment etc/ not found under $MNT/ostree — hostname not set"
        fi

        umount -R "$MNT" || true
        if [[ -n "$LUKS_DEV" ]]; then
            cryptsetup luksClose "$MAPPER" || true
        fi
    fi

    rmdir "$MNT" || true
    echo "==> hostname patch complete"

else
    echo "==> fisherman failed for a non-hostname reason (rc=$FISH_RC) — propagating"
    exit "$FISH_RC"
fi
