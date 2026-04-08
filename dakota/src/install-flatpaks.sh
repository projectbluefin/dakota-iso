#!/usr/bin/bash
# Pre-install flatpaks into the live squashfs.
#
# This script runs in its own Containerfile layer so Podman's layer cache
# can skip it on rebuilds when the flatpaks list hasn't changed.
# The flatpaks list is COPYd to /tmp/flatpaks-list by the Containerfile,
# making it a proper cache anchor: the layer is only re-run when the
# list file content changes.
#
# Requires network at build time; CAP_SYS_ADMIN for dbus.

set -exo pipefail

# overlayfs (used inside Podman builds) does not support O_TMPFILE which
# flatpak uses for atomic downloads.  /dev/shm is always a real tmpfs.
export TMPDIR=/dev/shm
mkdir -p /run/dbus
dbus-daemon --system --fork --nopidfile
sleep 1

flatpak remote-add --system --if-not-exists flathub \
    https://dl.flathub.org/repo/flathub.flatpakrepo

# bootc-installer: bundle from GitHub Releases
curl --retry 3 --location \
    https://github.com/tuna-os/tuna-installer/releases/download/continuous/org.bootcinstaller.Installer.flatpak \
    -o /tmp/tuna-installer.flatpak
flatpak install --system --noninteractive --bundle /tmp/tuna-installer.flatpak
rm /tmp/tuna-installer.flatpak

# Grant the installer read access to /etc for branding overrides.
flatpak override --system --filesystem=/etc:ro org.bootcinstaller.Installer

# Bluefin system flatpaks — list is at /tmp/flatpaks-list (COPYd by Containerfile)
readarray -t FLATPAKS < <(grep -v '^[[:space:]]*#' /tmp/flatpaks-list | grep -v '^[[:space:]]*$')
flatpak install --system --noninteractive --or-update flathub "${FLATPAKS[@]}"
