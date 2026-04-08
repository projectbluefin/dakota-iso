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
    usermod -aG wheel liveuser
    # systemctl enable doesn't work in a container build; create the symlink directly
    mkdir -p /etc/systemd/system/multi-user.target.wants
    ln -sf /usr/lib/systemd/system/sshd.service \
        /etc/systemd/system/multi-user.target.wants/sshd.service
    cat >> /etc/ssh/sshd_config << 'SSHEOF'
PermitEmptyPasswords no
PasswordAuthentication yes
SSHEOF
fi

# Skip gnome-initial-setup in the live session so GNOME Shell starts directly
mkdir -p /home/liveuser/.config
touch /home/liveuser/.config/gnome-initial-setup-done
chown -R liveuser:liveuser /home/liveuser/.config

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

# ── Flatpak pre-installation ──────────────────────────────────────────────────
# Install tuna-installer + Bluefin system flatpaks into the live squashfs.
# Requires network at build time; adds ~1-3 GB to the ISO.
#
# flatpak system installs talk to flatpak-system-helper via D-Bus, so we start
# the system bus first.  CAP_SYS_ADMIN (granted by `just container`) allows
# dbus-daemon to create its socket under /run/dbus.
#
# TMPDIR=/dev/shm: overlayfs (used inside Podman builds) does not support
# O_TMPFILE, which flatpak uses for atomic downloads from Flathub.
# /dev/shm is always a real tmpfs and supports O_TMPFILE.
export TMPDIR=/dev/shm
mkdir -p /run/dbus
dbus-daemon --system --fork --nopidfile
sleep 1

flatpak remote-add --system --if-not-exists flathub \
    https://dl.flathub.org/repo/flathub.flatpakrepo

# tuna-installer: bundle from GitHub Releases per README install instructions
# (not yet on Flathub — see https://github.com/tuna-os/tuna-installer#installing)
curl --retry 3 --location \
    https://github.com/tuna-os/tuna-installer/releases/download/continuous/org.bootcinstaller.Installer.flatpak \
    -o /tmp/tuna-installer.flatpak
flatpak install --system --noninteractive --bundle /tmp/tuna-installer.flatpak
rm /tmp/tuna-installer.flatpak

# Grant the installer access to /etc so it can read our branding overrides.
# The bundle may not ship with filesystem=host; override ensures it works.
flatpak override --system --filesystem=/etc:ro org.bootcinstaller.Installer

# Remaining Bluefin system flatpaks from Flathub
readarray -t FLATPAKS < <(grep -v '^[[:space:]]*#' "$SCRIPT_DIR/flatpaks" | grep -v '^[[:space:]]*$')
flatpak install --system --noninteractive --or-update flathub "${FLATPAKS[@]}"
