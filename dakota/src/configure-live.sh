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

    # Enable root login with a known password so hotfixes can be applied
    # directly via `ssh root@<ip>` or `su -` without going through sudo.
    passwd --unlock root
    echo "root:root" | chpasswd

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
PermitRootLogin yes
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

    # Print SSH connection info to the serial console once the network is up.
    # This makes it trivial to find the guest IP from `virsh console` or raw
    # QEMU serial output without manual guesswork.
    cat > /usr/lib/systemd/system/debug-ssh-banner.service << 'BANNEREOF'
[Unit]
Description=Print SSH connection info to serial console
After=sshd.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/bin/bash -c '\
  IP=$(hostname -I | awk "{print \\$1}"); \
  echo ""; \
  echo "========================================"; \
  echo " DEBUG SSH READY"; \
  echo " ssh liveuser@${IP:-<no-ip>}  (password: live)"; \
  echo " ssh root@${IP:-<no-ip>}      (password: root)"; \
  echo "========================================"; \
  echo ""'
StandardOutput=journal+console

[Install]
WantedBy=multi-user.target
BANNEREOF
    systemctl enable debug-ssh-banner.service
fi

# Give liveuser passwordless sudo so the live session is fully manageable
# (polkit rules alone aren't enough — some tools like virsh and manual patches
# require a real sudo session).
echo 'liveuser ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/liveuser
chmod 0440 /etc/sudoers.d/liveuser

# Skip gnome-initial-setup in the live session so GNOME Shell starts directly
mkdir -p /home/liveuser/.config
touch /home/liveuser/.config/gnome-initial-setup-done
chown -R liveuser:liveuser /home/liveuser/.config

# Remove gnome-tour desktop file so GNOME Shell can never launch it on the
# live ISO regardless of dconf state.  This is belt-and-suspenders alongside
# the welcome-dialog-last-shown-version=999 key below.
rm -f /usr/share/applications/org.gnome.Tour.desktop

# Suppress the GNOME Tour / "Welcome to Bluefin" dialog on first login.
# GNOME Shell shows it whenever welcome-dialog-last-shown-version < current
# shell version.  Setting it to 999 via a system dconf policy ensures it is
# never shown in the live session for any user.
#
# The base image profile already references system-db:distro; write our
# overrides into distro.d/ so the existing profile picks them up without
# modification (replacing the profile would lose Bluefin's own settings).
mkdir -p /etc/dconf/db/distro.d /etc/dconf/db/distro.d/locks
cat > /etc/dconf/db/distro.d/50-live-iso << 'DCONFEOF'
[org/gnome/shell]
welcome-dialog-last-shown-version='999'
favorite-apps=['dakota-installer.desktop', 'org.mozilla.firefox.desktop', 'org.gnome.Nautilus.desktop', 'org.gnome.Console.desktop']

[org/gnome/desktop/screensaver]
lock-enabled=false
idle-activation-enabled=false

[org/gnome/desktop/session]
idle-delay=uint32 0

[org/gnome/settings-daemon/plugins/power]
sleep-inactive-ac-type='nothing'
sleep-inactive-battery-type='nothing'
sleep-inactive-ac-timeout=0
sleep-inactive-battery-timeout=0
power-button-action='nothing'
DCONFEOF

cat > /etc/dconf/db/distro.d/locks/50-live-iso << 'LOCKSEOF'
/org/gnome/desktop/screensaver/lock-enabled
/org/gnome/desktop/screensaver/idle-activation-enabled
/org/gnome/desktop/session/idle-delay
/org/gnome/shell/favorite-apps
/org/gnome/settings-daemon/plugins/power/sleep-inactive-ac-type
/org/gnome/settings-daemon/plugins/power/sleep-inactive-battery-type
/org/gnome/settings-daemon/plugins/power/sleep-inactive-ac-timeout
/org/gnome/settings-daemon/plugins/power/sleep-inactive-battery-timeout
LOCKSEOF

dconf update

# Mask systemd sleep/suspend targets so the kernel never suspends regardless
# of what any userspace tool requests — belt-and-suspenders for the install.
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target

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
# Using size=8G instead of size=50%: on VMs with less RAM the kernel silently
# caps to available memory, but this avoids the 2 GiB ceiling that a 4 GiB VM
# gets with 50%.  On real hardware with 16+ GiB RAM it's a non-issue.
cat > /usr/lib/systemd/system/var-tmp.mount << 'UNITEOF'
[Unit]
Description=Large tmpfs for /var/tmp in the live environment

[Mount]
What=tmpfs
Where=/var/tmp
Type=tmpfs
Options=size=8G,nr_inodes=1m

[Install]
WantedBy=local-fs.target
UNITEOF
systemctl enable var-tmp.mount

# fisherman (tuna-installer backend) creates /var/fisherman-tmp and bind-mounts
# it to /var/tmp.  Pre-create the directory so it exists at boot time.
mkdir -p /var/fisherman-tmp

# ── Dakota icon ───────────────────────────────────────────────────────────────
# Install icon in hicolor theme hierarchy for desktop integration
mkdir -p /usr/share/icons/hicolor/{16x16,24x24,32x32,48x48,64x64,128x128,256x256,512x512}/apps
for size in 16 24 32 48 64 128 256 512; do
  install -Dm644 "$SCRIPT_DIR/icons/hicolor/${size}x${size}/apps/dakota.png" \
    "/usr/share/icons/hicolor/${size}x${size}/apps/dakota.png"
done
# Symlink 512×512 to pixmaps for compatibility
install -Dm644 "$SCRIPT_DIR/icons/hicolor/512x512/apps/dakota.png" /usr/share/pixmaps/dakota.png
gtk-update-icon-cache /usr/share/icons/hicolor/

# ── Installer configuration ───────────────────────────────────────────────────
# The bootc-installer reads both overrides from /etc/bootc-installer/:
#   images.json — locks the catalog to Dakota only
#   recipe.json — sets distro branding, tour slides, and install steps
mkdir -p /etc/bootc-installer
cp "$SCRIPT_DIR/etc/bootc-installer/images.json" /etc/bootc-installer/images.json
cp "$SCRIPT_DIR/etc/bootc-installer/recipe.json" /etc/bootc-installer/recipe.json
# Flag file read by the installer (tuna-os/tuna-installer#26) to activate live
# ISO mode even when the installer is running inside a Flatpak sandbox.
touch /etc/bootc-installer/live-iso-mode

# ── Installer autostart ───────────────────────────────────────────────────────
# App ID differs between stable and dev channel builds.
INSTALLER_APP_ID="org.bootcinstaller.Installer"
[[ "${INSTALLER_CHANNEL:-stable}" == "dev" ]] && INSTALLER_APP_ID="org.bootcinstaller.Installer.Devel"

mkdir -p /etc/xdg/autostart
# VANILLA_CUSTOM_RECIPE workaround (tuna-os/tuna-installer#26): inside the
# Flatpak sandbox /etc is reserved; the host /etc is at /run/host/etc.  Pass
# the recipe via env var at the /run/host path so the installer finds it.
cat > /etc/xdg/autostart/tuna-installer.desktop << DTEOF
[Desktop Entry]
Name=Dakota Installer
Exec=flatpak run --env=VANILLA_CUSTOM_RECIPE=/run/host/etc/bootc-installer/recipe.json ${INSTALLER_APP_ID}
Icon=dakota
Type=Application
X-GNOME-Autostart-enabled=true
DTEOF

# A matching entry in /usr/share/applications/ lets GNOME Shell reference this
# app in the dock via favorite-apps. The autostart file auto-launches it; this
# entry makes it visible and pinnable as 'dakota-installer.desktop'.
mkdir -p /usr/share/applications
cat > /usr/share/applications/dakota-installer.desktop << DTEOF
[Desktop Entry]
Name=Dakota Installer
Comment=Install Dakota to your computer
Exec=flatpak run --env=VANILLA_CUSTOM_RECIPE=/run/host/etc/bootc-installer/recipe.json ${INSTALLER_APP_ID}
Icon=dakota
Type=Application
Categories=System;
NoDisplay=false
DTEOF

# ── Polkit rules for live installer (tuna-os/tuna-installer#25) ───────────────
# The installer's polkit action (org.tunaos.Installer.install) defaults to
# auth_admin.  On the live ISO we want liveuser to install without any password
# prompt.  Two complementary mechanisms are used for belt-and-suspenders:
#
#  1. Policy override: write the action definition directly with allow_active=yes
#     so polkit approves it at the policy level before rules even run.
#
#  2. JS rule: belt-and-suspenders fallback that grants YES for liveuser.
#     Omitting subject.active so GDM autologin sessions (which may not always
#     be marked active by logind) are also covered.
#
# The Flatpak does not export its policy file or fisherman to the host, so both
# are set up manually here.

# fisherman symlink — installer calls /usr/local/bin/fisherman via pkexec
INSTALLER_APP_DIR=$(find /var/lib/flatpak/app/${INSTALLER_APP_ID} -name fisherman -type f 2>/dev/null | head -1 | xargs dirname 2>/dev/null || true)
if [ -n "$INSTALLER_APP_DIR" ]; then
    mkdir -p /usr/local/bin
    ln -sf "${INSTALLER_APP_DIR}/fisherman" /usr/local/bin/fisherman
fi

# Policy file: write it directly so we're not dependent on Flatpak search.
# allow_active=yes means any active session can run the installer without auth.
mkdir -p /usr/share/polkit-1/actions
cat > /usr/share/polkit-1/actions/org.bootcinstaller.Installer.policy << 'POLICYEOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC
  "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
  "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<policyconfig>
  <action id="org.tunaos.Installer.install">
    <description>Install an operating system to disk</description>
    <message>Authentication is required to install an operating system</message>
    <icon_name>drive-harddisk</icon_name>
    <defaults>
      <allow_any>no</allow_any>
      <allow_inactive>no</allow_inactive>
      <allow_active>yes</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">/usr/local/bin/fisherman</annotate>
    <annotate key="org.freedesktop.policykit.exec.allow_gui">true</annotate>
  </action>
</policyconfig>
POLICYEOF

# JS rule: the installer copies fisherman to the user cache and calls
# `pkexec /path/to/cached/fisherman`, which polkit sees as the generic
# org.freedesktop.policykit.exec action — not our custom action above.
# Grant YES for liveuser on both actions to cover both paths.
mkdir -p /etc/polkit-1/rules.d
cat > /etc/polkit-1/rules.d/99-live-installer.rules << 'EOF'
polkit.addRule(function(action, subject) {
    if ((action.id === "org.freedesktop.policykit.exec" ||
         action.id === "org.tunaos.Installer.install") &&
            subject.user === "liveuser" && subject.local) {
        return polkit.Result.YES;
    }
});
EOF

# Flatpaks are pre-installed in a separate cached layer (install-flatpaks.sh).
# Nothing to do here.

# ── Live network defaults ─────────────────────────────────────────────────────
# /etc/hostname is bind-mounted by the container runtime during builds; writing
# to it in a RUN step doesn't persist into the image layer.  Use tmpfiles.d to
# create it at first boot instead.
mkdir -p /usr/lib/tmpfiles.d
echo 'f /etc/hostname 0644 - - - dakota-live' > /usr/lib/tmpfiles.d/live-hostname.conf

# ── VFS containers-storage ────────────────────────────────────────────────────
# The ISO embeds the Dakota OCI image as VFS containers-storage in the squashfs.
# Configure podman to use the VFS driver so the pre-embedded image is visible
# for offline installation.  Without this, podman initializes with the overlay
# driver at first boot, creating a db.sql that conflicts with VFS access.
cat > /etc/containers/storage.conf << 'STOREOF'
[storage]
driver = "vfs"
runroot = "/run/containers/storage"
graphroot = "/var/lib/containers/storage"
STOREOF

# ── Skopeo / Podman wrappers for offline install ─────────────────────────────
# fisherman (the tuna-installer backend) calls skopeo to export the embedded
# VFS image to OCI, then podman to run `bootc install to-filesystem`.
#
# Two problems on a live ISO:
#   1. fisherman passes `containers-storage:containers-storage:IMAGE` (doubled
#      prefix) — upstream bug in fisherman's bootc.go; the wrapper strips it.
#   2. The live rootfs overlay is tiny (~1.6 GiB).  Both skopeo temp files
#      (container_images_*, ~8 GiB) and bootc's VFS layer writes (~8 GiB)
#      overflow it.  The wrapper redirects writes to a btrfs @scratch
#      subvolume on the *target* disk (already partitioned and mounted by
#      fisherman at this point) via bind mounts and an overlayfs on
#      /var/lib/containers/storage.  The @scratch subvolume is mounted as a
#      mount point on the target, which bootc's "empty rootfs" check allows.

# Back up real binaries
cp /usr/bin/skopeo /usr/bin/skopeo.real
cp /usr/bin/podman /usr/bin/podman.real

cat > /usr/bin/skopeo << 'SKOPEOF'
#!/bin/bash
ARGS=()
for arg in "$@"; do
    ARGS+=("${arg/containers-storage:containers-storage:/containers-storage:}")
done

for target in /mnt/fisherman-target /var/mnt/fisherman-target; do
    if mountpoint -q "$target" 2>/dev/null; then
        SCRATCH="$target/@scratch"
        if [ ! -d "$SCRATCH" ]; then
            btrfs subvolume create "$SCRATCH"
        fi
        DEV=$(findmnt -n -o SOURCE "$target" | head -1)
        mount -o subvol=@scratch "$DEV" "$SCRATCH"

        mkdir -p "$SCRATCH/var-tmp"
        mount --bind "$SCRATCH/var-tmp" /var/tmp
        # Also redirect /var/fisherman-tmp so podman's -v bind sees the OCI cache
        if [ -d /var/fisherman-tmp ]; then
            mount --bind "$SCRATCH/var-tmp" /var/fisherman-tmp
        fi

        CS=/var/lib/containers/storage
        LOWER=/run/rootfsbase/var/lib/containers/storage
        if [ -d "$LOWER" ] && ! mountpoint -q "$CS" 2>/dev/null; then
            mkdir -p "$SCRATCH/cs-upper" "$SCRATCH/cs-work"
            mount -t overlay overlay \
                -o "lowerdir=$LOWER,upperdir=$SCRATCH/cs-upper,workdir=$SCRATCH/cs-work" \
                "$CS"
        fi
        break
    fi
done
exec /usr/bin/skopeo.real "${ARGS[@]}"
SKOPEOF
chmod +x /usr/bin/skopeo

cat > /usr/bin/podman << 'PODMEOF'
#!/bin/bash
exec /usr/bin/podman.real "$@"
PODMEOF
chmod +x /usr/bin/podman
