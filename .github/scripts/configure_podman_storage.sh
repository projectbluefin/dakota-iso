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

# Choose driver based on filesystem
case "$FS_TYPE" in
    btrfs)
        DRIVER="btrfs"
        GRAPHROOT="/var/lib/containers/storage"
        echo "Using btrfs driver (native COW support on BTRFS filesystem)"
        ;;
    ext4|xfs)
        DRIVER="overlay"
        GRAPHROOT="/var/lib/containers/storage"
        echo "Using overlay driver (space-efficient on ext4/xfs)"
        ;;
    *)
        DRIVER="vfs"
        GRAPHROOT="/var/lib/containers/storage"
        echo "Using vfs driver (fallback for $FS_TYPE)"
        ;;
esac

# Write storage.conf
echo "Configuring podman storage driver: $DRIVER"
sudo bash -c "cat > /etc/containers/storage.conf" << CONF
[storage]
driver = "$DRIVER"
graphroot = "$GRAPHROOT"
runroot = "/run/containers/storage"
CONF

# Verify configuration
echo ""
echo "=== Podman storage configuration ==="
sudo podman info | grep -A 5 "storage:"
