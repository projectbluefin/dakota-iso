#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Configure podman storage driver intelligently based on available filesystem

set -eo pipefail

# Detect filesystem type at /var/lib/containers (where BTRFS loopback is mounted)
if [ -d /var/lib/containers ]; then
    FS_TYPE=$(stat --file-system --format=%T /var/lib/containers 2>/dev/null || echo "unknown")
else
    FS_TYPE=$(stat --file-system --format=%T /var/lib 2>/dev/null || echo "unknown")
fi

echo "Detected filesystem for /var/lib/containers: $FS_TYPE"

# Choose driver based on filesystem (native drivers preferred for COW efficiency)
case "$FS_TYPE" in
    btrfs)
        DRIVER="btrfs"
        echo "Using btrfs driver (native COW support on BTRFS filesystem)"
        ;;
    zfs)
        DRIVER="zfs"
        echo "Using zfs driver (native COW support on ZFS filesystem)"
        ;;
    ext4|xfs)
        DRIVER="overlay"
        echo "Using overlay driver (space-efficient on ext4/xfs)"
        ;;
    *)
        DRIVER="vfs"
        echo "Using vfs driver (fallback for $FS_TYPE)"
        ;;
esac

# Write storage.conf
echo "Configuring podman storage driver: $DRIVER"
GRAPHROOT="/var/lib/containers/storage"

sudo bash -c "cat > /etc/containers/storage.conf" << CONF
[storage]
driver = "$DRIVER"
graphroot = "$GRAPHROOT"
runroot = "/run/containers/storage"
CONF

# Verify configuration
echo ""
echo "=== Podman storage configuration ==="
sudo podman info | grep -A 5 "storage:" || sudo podman info | head -20
