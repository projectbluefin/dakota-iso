#!/usr/bin/bash
# Live-environment setup for the Dakota ISO installer image.
#
# Runs inside the final Dakota container stage with:
#   --cap-add sys_admin --security-opt label=disable
#
# At this point the initramfs has already been replaced (by the Debian
# initramfs-builder stage) with a dmsquash-live capable one.  This script
# handles the runtime live-environment: user, GDM autologin, installer
# configuration + autostart, and Flatpak pre-installation.
#
# TARGET (set via ARG/ENV in the Containerfile) selects which Dakota variant
# is being built: "dakota" (stock) or "dakota-nvidia".  It controls the
# imgref and local_imgref written into recipe.json so each squashfs image on
# the dual-env ISO knows which OCI image it carries for offline installation.

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

# App ID differs between stable and dev channel builds.  Define early so it
# can be used in the desktop override and autostart sections below.
INSTALLER_APP_ID="org.bootcinstaller.Installer"
[[ "${INSTALLER_CHANNEL:-stable}" == "dev" ]] && INSTALLER_APP_ID="org.bootcinstaller.Installer.Devel"

# Override the installer flatpak's desktop entry so it appears as
# "Dakota Installer" with the dakota icon instead of "bootc Installer".
# /usr/share/applications/ takes precedence over flatpak exports in XDG_DATA_DIRS.
# Use /usr/share instead of /usr/local to avoid issues with /usr/local being a
# dangling symlink on Fedora Silverblue-based images (e.g. bluefin) where
# /usr/local -> /var/usrlocal and /var/usrlocal doesn't exist at build time.
mkdir -p /usr/share/applications
INSTALLER_DESKTOP_ID="${INSTALLER_APP_ID}.desktop"
cat > "/usr/share/applications/${INSTALLER_DESKTOP_ID}" << DESKTOPEOF
[Desktop Entry]
Name=Dakota Installer
Exec=/usr/bin/flatpak run --branch=master --arch=x86_64 --command=bootc-installer ${INSTALLER_APP_ID}
Icon=dakota
Terminal=false
Type=Application
Categories=GTK;System;Settings;
StartupNotify=true
X-Flatpak=${INSTALLER_APP_ID}
DESKTOPEOF

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

dconf update || echo 'Warning: dconf update failed (will compile on first boot)'

# Mask systemd sleep/suspend targets so the kernel never suspends regardless
# of what any userspace tool requests — belt-and-suspenders for the install.
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target || true

# ── GDM autologin ─────────────────────────────────────────────────────────────
mkdir -p /etc/gdm
cat > /etc/gdm/custom.conf << 'GDMEOF'
[daemon]
AutomaticLoginEnable=True
AutomaticLogin=liveuser
GDMEOF

# ── /var/tmp tmpfs ────────────────────────────────────────────────────────────
# The live overlayfs puts /var on a small RAM overlay.  During install skopeo
# writes intermediate blob temp files to /var/tmp regardless of TMPDIR.  With
# the squashed 9 GB dakota-nvidia image the uncompressed blob exceeds 8 GB, so
# use 80% of total RAM so it scales with the machine (min system requirement
# for the nvidia image is 16 GB, giving ~13 GB here).
cat > /usr/lib/systemd/system/var-tmp.mount << 'UNITEOF'
[Unit]
Description=Large tmpfs for /var/tmp in the live environment

[Mount]
What=tmpfs
Where=/var/tmp
Type=tmpfs
Options=size=80%,nr_inodes=1m

[Install]
WantedBy=local-fs.target
UNITEOF
systemctl enable var-tmp.mount || true

# ── /run overlay expansion ────────────────────────────────────────────────────
# The live root overlay's upper dir lives under /run (as /run/overlayfs).  The
# VFS storage driver creates a full copy of the squashed image here when podman
# creates a container from it — this copy can be 8–9 GB for large images like
# bluefin-nvidia.  The default /run tmpfs is 20% of RAM (~5.6 GB on a 28 GB
# machine), which is too small.  Expand it to 70% so offline installs succeed
# on machines with 16 GB+ RAM (min recommended for bluefin).
cat > /usr/lib/systemd/system/live-run-expand.service << 'UNITEOF'
[Unit]
Description=Expand /run tmpfs for large VFS offline installs
DefaultDependencies=no
After=systemd-remount-fs.service
Before=basic.target

[Service]
Type=oneshot
ExecStart=/bin/mount -o remount,size=70% /run
RemainAfterExit=yes

[Install]
WantedBy=sysinit.target
UNITEOF
systemctl enable live-run-expand.service || true

# ── Live-ready marker service ─────────────────────────────────────────────────
# Prints DAKOTA_LIVE_READY to the serial console after display-manager.service
# starts.  CI boot verification greps for this token in the serial log.
#
# StandardOutput=tty + TTYPath=/dev/ttyS0 ensures the echo goes to the serial
# device directly.  StandardOutput=journal+console routes to /dev/console which
# is NOT the serial device in headless QEMU (-display none, -serial file:...).
#
# WantedBy=multi-user.target (not display-manager.service) ensures reliable
# enablement; After=display-manager.service provides ordering only.
cat > /usr/lib/systemd/system/live-ready.service << 'LREOF'
[Unit]
Description=Live environment ready marker
After=display-manager.service
Requires=display-manager.service

[Service]
Type=oneshot
ExecStart=/bin/echo DAKOTA_LIVE_READY
StandardOutput=tty
TTYPath=/dev/ttyS0

[Install]
WantedBy=multi-user.target
LREOF
systemctl enable live-ready.service || true

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

# ── Installer tour images ─────────────────────────────────────────────────────
# The tuna-installer Flatpak has --filesystem=host so absolute paths are visible.
mkdir -p /usr/share/bootc-installer/images
install -Dm644 "$SCRIPT_DIR/images/dakotaraptor.png" /usr/share/bootc-installer/images/dakotaraptor.png

# ── Installer configuration ───────────────────────────────────────────────────
# The bootc-installer reads both overrides from /etc/bootc-installer/:
#   images.json — the full catalog of installable images (both variants)
#   recipe.json — distro branding, tour slides, install steps, and the
#                 local_imgref for offline installation
#
# TARGET controls which OCI image is embedded in this squashfs's VFS
# containers-storage and therefore available for offline install.  The live
# environment is always the NVIDIA variant (safe for all hardware).
#
# Per-variant config is read from /tmp/src/<TARGET>/ (bind-mounted from the
# build context at container build time) when present, with sensible defaults
# for dakota.  Each variant directory may contain:
#   base_imgref      — base (non-nvidia) image ref written as imgref
#   nvidia_imgref    — nvidia image ref used as local_imgref offline store
#   bootloader       — "systemd" (default) or "grub"
#   composefs        — "true" (default) or "false"
#   flatpak_var_path — flatpak data path inside target (default: state/os/default/var/lib/flatpak)
#   images_json      — variant-specific images.json (optional, overrides live/src/etc/bootc-installer/images.json)
TARGET="${TARGET:-dakota-nvidia}"

# Derive variant name by stripping the -nvidia/-nvidia-open suffix
VARIANT=$(echo "$TARGET" | sed 's/-nvidia-open$//;s/-nvidia$//')
VARIANT_DIR="/tmp/src/${VARIANT}"

# Read per-variant config with defaults
if [[ -f "$VARIANT_DIR/base_imgref" ]]; then
    BASE_IMGREF=$(cat "$VARIANT_DIR/base_imgref")
else
    BASE_IMGREF="ghcr.io/projectbluefin/dakota:stable"
fi
if [[ -f "$VARIANT_DIR/nvidia_imgref" ]]; then
    NVIDIA_IMGREF=$(cat "$VARIANT_DIR/nvidia_imgref")
else
    NVIDIA_IMGREF="ghcr.io/projectbluefin/dakota-nvidia:stable"
fi
BOOTLOADER=$(cat "$VARIANT_DIR/bootloader" 2>/dev/null || echo "systemd")
COMPOSEFS=$(cat "$VARIANT_DIR/composefs" 2>/dev/null || echo "true")

mkdir -p /etc/bootc-installer
# Use variant-specific images.json if present, otherwise use the shared one.
if [[ -f "$VARIANT_DIR/images.json" ]]; then
    cp "$VARIANT_DIR/images.json" /etc/bootc-installer/images.json
else
    cp "$SCRIPT_DIR/etc/bootc-installer/images.json" /etc/bootc-installer/images.json
fi

# Generate recipe.json with the correct imgref/local_imgref for this variant.
# All other fields (branding, tour, steps) are identical across variants.
python3 - << PYEOF
import json, sys

with open("$SCRIPT_DIR/etc/bootc-installer/recipe.json") as f:
    recipe = json.load(f)

# image = source for fisherman/bootc install
# For composefs (dakota): containers-storage: ref in the VFS store, podman-based
#   install.
# For non-composefs (bluefin, lts): empty image triggers bootcDirect — fisherman
#   runs bootc install --source-imgref containers-storage:<ref> natively,
#   into the squashfs at /usr/lib/containers/storage.  Mirrors projectbluefin/iso.
if "$COMPOSEFS" == "true":
    recipe["image"] = "containers-storage:$NVIDIA_IMGREF"
    recipe["local_imgref"] = "containers-storage:$NVIDIA_IMGREF"
else:
    recipe["image"] = ""
    recipe["local_imgref"] = "containers-storage:$NVIDIA_IMGREF"
recipe["targetImgref"] = "$BASE_IMGREF"
recipe["imgref"] = "$BASE_IMGREF"
# All variants default to btrfs. XFS is available as a UI option only.
recipe["filesystem"] = "btrfs"
# Variant-specific bootloader and composefs backend
# Normalise "grub" → "grub2": the bootloader file uses the short form but
# fisherman's recipe validator requires "grub2" or "systemd".
bootloader = "$BOOTLOADER"
if bootloader == "grub":
    bootloader = "grub2"
recipe["bootloader"] = bootloader
recipe["composeFsBackend"] = $([ "$COMPOSEFS" = "true" ] && echo "True" || echo "False")

with open("/etc/bootc-installer/recipe.json", "w") as f:
    json.dump(recipe, f, indent=2)
    f.write("\n")
PYEOF

# Flag file read by the installer to activate live ISO mode even when running
# inside a Flatpak sandbox.
touch /etc/bootc-installer/live-iso-mode

# Prevent bluefin-remove-installer.service from running in the live env.
# The service is designed for installed systems to remove the installer on first
# boot.  In the live ISO the installer must remain available.  The condition
# ConditionPathExists=!/etc/bootc-installer/live-iso-mode ensures:
#   live ISO  → live-iso-mode EXISTS  → service is skipped ✓
#   installed → live-iso-mode ABSENT  → service runs normally ✓
mkdir -p /usr/lib/systemd/system/bluefin-remove-installer.service.d
cat > /usr/lib/systemd/system/bluefin-remove-installer.service.d/live-skip.conf << 'SKIPCEOF'
[Unit]
ConditionPathExists=!/etc/bootc-installer/live-iso-mode
SKIPCEOF

# ── Installer autostart ───────────────────────────────────────────────────────

mkdir -p /etc/xdg/autostart
# BOOTC_CUSTOM_RECIPE (bootc-installer): inside the Flatpak sandbox /etc is
# reserved; the host /etc is at /run/host/etc.  Pass the recipe via env var
# at the /run/host path so the installer finds it.
cat > /etc/xdg/autostart/tuna-installer.desktop << DTEOF
[Desktop Entry]
Name=Dakota Installer
Exec=flatpak run --env=BOOTC_CUSTOM_RECIPE=/run/host/etc/bootc-installer/recipe.json ${INSTALLER_APP_ID}
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
Exec=flatpak run --env=BOOTC_CUSTOM_RECIPE=/run/host/etc/bootc-installer/recipe.json ${INSTALLER_APP_ID}
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
    # Resolve /usr/local symlink if dangling (Fedora Silverblue: /usr/local -> /var/usrlocal)
    USR_LOCAL_BIN=/usr/local/bin
    if [[ -L /usr/local ]] && [[ ! -d /usr/local ]]; then
        USR_LOCAL_BIN="$(readlink /usr/local)/bin"
        mkdir -p "$USR_LOCAL_BIN"
    else
        mkdir -p /usr/local/bin
    fi
    ln -sf "${INSTALLER_APP_DIR}/fisherman" "${USR_LOCAL_BIN}/fisherman"
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

# ── containers-storage ──────────────────────────────────────────────────────
# The OCI payload image is baked into the squashfs.
# Strategy depends on composefs vs. bootcDirect (non-composefs):
#
#   composefs (dakota): VFS driver at /var/lib/containers/storage.
#     Fisherman exports VFS → OCI at install time, podman-based install.
#
#   non-composefs (stable, lts): VFS driver at /var/lib/containers/storage
#     with a VFS-format additionalimagestore at /usr/lib/containers/storage
#     (read-only squashfs).  VFS driver is required — the live ISO rootfs is
#     overlayfs (dmsquash-live) and el10 (LTS) lacks native overlay-on-overlay;
#     an overlay-format additional store silently fails, causing bootc to write
#     blobs to /var/tmp (RAM-backed tmpfs) → ENOSPC.
#     bootcDirect runs bootc natively with containers-storage:<ref> resolving
#     via the VFS additional store.  Mirrors projectbluefin/iso commit 34fe6659.

mkdir -p /var/lib/containers/storage
mkdir -p /etc/containers

if [ "$COMPOSEFS" = "true" ]; then
    # composefs: VFS driver for VFS-format layers baked into squashfs
    cat > /etc/containers/storage.conf << 'STOREOF'
[storage]
driver = "vfs"
runroot = "/run/containers/storage"
graphroot = "/var/lib/containers/storage"
STOREOF
else
    # non-composefs (bootcDirect): overlay driver with fuse-overlayfs, payload in overlay additional store.
    # We must use fuse-overlayfs because the live ISO rootfs is overlayfs (dmsquash-live)
    # and the el10 kernel (LTS) lacks native overlay-on-overlay support.
    # Using fuse-overlayfs bypasses kernel restrictions and allows the overlay driver
    # to be used, which avoids the VFS storage explosion of unsquashed images.
    mkdir -p /usr/lib/containers/storage
    cat > /etc/containers/storage.conf << 'STOREOF'
[storage]
driver = "overlay"
runroot = "/run/containers/storage"
graphroot = "/var/lib/containers/storage"

[storage.options]
additionalimagestores = [
  "/usr/lib/containers/storage"
]

[storage.options.overlay]
mount_program = "/usr/bin/fuse-overlayfs"
STOREOF

    # Bind-mount the read-only squashfs payload into container namespaces
    # so fisherman/podman can resolve containers-storage:<ref> offline.
    cat > /etc/containers/mounts.conf << 'MOUNTSEOF'
/usr/lib/containers/storage:/usr/lib/containers/storage
MOUNTSEOF
fi

# fisherman handles scratch space, transport-prefix stripping, OCI export, and
# GPT partition retagging natively — no host-side wrappers needed.
