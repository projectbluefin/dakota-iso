#!/usr/bin/bash
# Pre-install flatpaks into the live squashfs.
#
# Runs with --mount=type=cache,target=/var/lib/flatpak so the flatpak ostree
# repo persists across builds.  Each run reconciles to match /tmp/flatpaks-list:
#   - installs missing apps
#   - updates outdated apps (ostree delta, fast)
#   - removes apps that were dropped from the list
#
# /tmp/flatpaks-list is COPYd by the Containerfile so it's always current.
# Requires network at build time; CAP_SYS_ADMIN for dbus.

set -exo pipefail

# overlayfs inside Podman builds doesn't support O_TMPFILE; /dev/shm does.
export TMPDIR=/dev/shm
mkdir -p /run/dbus
dbus-daemon --system --fork --nopidfile
sleep 1

flatpak remote-add --system --if-not-exists flathub \
    https://dl.flathub.org/repo/flathub.flatpakrepo

# bootc-installer bundle
# INSTALLER_CHANNEL controls which release tag to pull from:
#   stable (default) → continuous   (latest stable build from main/prod)
#   dev              → continuous-dev (latest dev build, tracks dev branch)
RELEASE_TAG="continuous"
FLATPAK_FILENAME="org.bootcinstaller.Installer.flatpak"
if [[ "${INSTALLER_CHANNEL:-stable}" == "dev" ]]; then
    RELEASE_TAG="continuous-dev"
    FLATPAK_FILENAME="org.bootcinstaller.Installer.Devel.flatpak"
fi
curl --retry 3 --location \
    "https://github.com/tuna-os/tuna-installer/releases/download/${RELEASE_TAG}/${FLATPAK_FILENAME}" \
    -o /tmp/tuna-installer.flatpak
INSTALLER_APP_ID="org.bootcinstaller.Installer"
[[ "${INSTALLER_CHANNEL:-stable}" == "dev" ]] && INSTALLER_APP_ID="org.bootcinstaller.Installer.Devel"

flatpak install --system --noninteractive --bundle /tmp/tuna-installer.flatpak || \
    flatpak update --system --noninteractive "${INSTALLER_APP_ID}"
rm /tmp/tuna-installer.flatpak

flatpak override --system --filesystem=/etc:ro "${INSTALLER_APP_ID}"

# ── Reconcile Flathub apps against the wanted list ───────────────────────────
# In debug mode, skip the full Flathub app list to keep builds fast.
if [[ "${DEBUG:-0}" == "1" ]]; then
    echo "DEBUG mode: skipping Flathub app list (installer-only ISO)"
    exit 0
fi

readarray -t WANTED < <(grep -v '^[[:space:]]*#' /tmp/flatpaks-list | grep -v '^[[:space:]]*$')

# Install or update everything in the list (--or-update = skip if current)
# --no-related skips locale packs and debug symbols (~3 GB uncompressed)
flatpak install --system --noninteractive --no-related --or-update flathub "${WANTED[@]}"

# Remove any system app that is no longer in the wanted list
readarray -t INSTALLED < <(flatpak list --app --system --columns=application 2>/dev/null || true)
for app in "${INSTALLED[@]}"; do
    # Keep the installer regardless (stable or devel app ID)
    [[ "$app" == "org.bootcinstaller.Installer" ]] && continue
    [[ "$app" == "org.bootcinstaller.Installer.Devel" ]] && continue
    if [[ ! " ${WANTED[*]} " =~ " ${app} " ]]; then
        echo "Removing dropped flatpak: $app"
        flatpak uninstall --system --noninteractive "$app" || true
    fi
done

# Prune unused runtimes left behind by removals
flatpak uninstall --system --noninteractive --unused || true
