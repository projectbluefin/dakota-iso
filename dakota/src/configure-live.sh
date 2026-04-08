#!/usr/bin/bash
# Live-environment setup for the Dakota ISO installer image.
#
# Runs inside the final Dakota container stage with:
#   --cap-add sys_admin --security-opt label=disable
#
# At this point the initramfs has already been replaced (by the Debian
# initramfs-builder stage) with a dmsquash-live capable one.  This script
# handles the runtime live-environment: user, GDM autologin, tuna-installer
# configuration + autostart, and Flatpak pre-installation.

set -exo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── VERSION_ID ────────────────────────────────────────────────────────────────
# GNOME OS omits VERSION_ID from os-release; image-builder and bootc tooling
# require it.  Replace if present, append if missing.
if grep -q '^VERSION_ID=' /usr/lib/os-release 2>/dev/null; then
    sed -i 's/^VERSION_ID=.*/VERSION_ID=latest/' /usr/lib/os-release
else
    echo 'VERSION_ID=latest' >> /usr/lib/os-release
fi

# ── Live user ─────────────────────────────────────────────────────────────────
# GNOME OS has no livesys-scripts; create a passwordless live user manually.
useradd --create-home --uid 1000 --user-group \
    --comment "Live User" liveuser || true
passwd --delete liveuser

# Debug builds only: enable SSH so the live session is reachable for testing.
# Never enabled in production ISOs.
if [[ "${DEBUG:-0}" == "1" ]]; then
    echo "liveuser:live" | chpasswd

    # Grant passwordless sudo via sudoers drop-in (usermod -aG wheel doesn't
    # persist reliably through the squashfs overlay at runtime).
    echo "liveuser ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/liveuser
    chmod 440 /etc/sudoers.d/liveuser

    # Enable sshd: the Dakota/Bluefin preset marks sshd disabled, so a plain
    # wants symlink gets overridden at first boot.  A preset file in
    # /etc/systemd/system-preset/ takes priority over /usr/lib and forces it on.
    mkdir -p /etc/systemd/system-preset
    echo "enable sshd.service" > /etc/systemd/system-preset/90-live-debug.preset
    mkdir -p /etc/systemd/system/multi-user.target.wants
    ln -sf /usr/lib/systemd/system/sshd.service \
        /etc/systemd/system/multi-user.target.wants/sshd.service

    cat >> /etc/ssh/sshd_config << 'SSHEOF'
PermitEmptyPasswords no
PasswordAuthentication yes
SSHEOF

    # Open SSH through firewalld so port 22 is reachable from the host
    mkdir -p /etc/firewalld/zones
    cat > /etc/firewalld/zones/public.xml << 'FWEOF'
<?xml version="1.0" encoding="utf-8"?>
<zone>
  <short>Public</short>
  <service name="ssh"/>
  <service name="mdns"/>
  <service name="dhcpv6-client"/>
</zone>
FWEOF
fi

# Skip gnome-initial-setup in the live session so GNOME Shell starts directly
mkdir -p /home/liveuser/.config
touch /home/liveuser/.config/gnome-initial-setup-done
chown -R liveuser:liveuser /home/liveuser/.config

# Suppress the GNOME Tour / "Welcome to Bluefin" dialog on first login.
# GNOME Shell shows it whenever welcome-dialog-last-shown-version < current
# shell version.  Setting it to 999 via a system dconf policy ensures it is
# never shown in the live session for any user.
mkdir -p /etc/dconf/db/local.d
cat > /etc/dconf/db/local.d/00-live-iso << 'DCONFEOF'
[org/gnome/shell]
welcome-dialog-last-shown-version='999'
DCONFEOF
dconf update

# ── GDM autologin ─────────────────────────────────────────────────────────────
mkdir -p /etc/gdm
cat > /etc/gdm/custom.conf << 'GDMEOF'
[daemon]
AutomaticLoginEnable=True
AutomaticLogin=liveuser
GDMEOF

# ── /var/tmp tmpfs ────────────────────────────────────────────────────────────
# The live overlayfs puts /var on a small RAM overlay.  bootc needs substantial
# space in /var/tmp when staging an install; mount a dedicated tmpfs there.
cat > /usr/lib/systemd/system/var-tmp.mount << 'UNITEOF'
[Unit]
Description=Large tmpfs for /var/tmp in the live environment

[Mount]
What=tmpfs
Where=/var/tmp
Type=tmpfs
Options=size=50%,nr_inodes=1m

[Install]
WantedBy=local-fs.target
UNITEOF
systemctl enable var-tmp.mount

# ── Dakota icon ───────────────────────────────────────────────────────────────
install -Dm644 "$SCRIPT_DIR/dakota.png" /usr/share/pixmaps/dakota.png

# ── Installer configuration ───────────────────────────────────────────────────
# The bootc-installer reads both overrides from /etc/bootc-installer/:
#   images.json — locks the catalog to Dakota only
#   recipe.json — sets distro branding, tour slides, and install steps
mkdir -p /etc/bootc-installer
cp "$SCRIPT_DIR/etc/bootc-installer/images.json" /etc/bootc-installer/images.json
cp "$SCRIPT_DIR/etc/bootc-installer/recipe.json" /etc/bootc-installer/recipe.json

# Autostart tuna-installer when the live GNOME session begins
mkdir -p /etc/xdg/autostart
cat > /etc/xdg/autostart/tuna-installer.desktop << 'DTEOF'
[Desktop Entry]
Name=Dakota Installer
Exec=flatpak run org.bootcinstaller.Installer
Icon=/usr/share/pixmaps/dakota.png
Type=Application
X-GNOME-Autostart-enabled=true
DTEOF

# Flatpaks are pre-installed in a separate cached layer (install-flatpaks.sh).
# Nothing to do here.

# ── Live network defaults ─────────────────────────────────────────────────────
# /etc/hostname is bind-mounted by the container runtime during builds; writing
# to it in a RUN step doesn't persist into the image layer.  Use tmpfiles.d to
# create it at first boot instead.
mkdir -p /usr/lib/tmpfiles.d
echo 'f /etc/hostname 0644 - - - dakota-live' > /usr/lib/tmpfiles.d/live-hostname.conf
