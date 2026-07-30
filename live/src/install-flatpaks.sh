#!/usr/bin/bash
# Pre-install flatpaks into the live squashfs.
#
# Uses --mount=type=cache,target=/var/cache/flatpak-dl to persist the flatpak
# ostree repo across builds.  On each run the script:
#   1. Seeds /var/lib/flatpak/repo from the build cache (warm start)
#   2. Reconciles to match /tmp/flatpaks-list (only deltas downloaded)
#   3. Saves the repo back to the cache for next build
#
# /tmp/flatpaks-list is COPYd by the Containerfile so it's always current.
# Requires network at build time; CAP_SYS_ADMIN for dbus.

set -exo pipefail

FLATPAK_CACHE="/var/cache/flatpak-dl"

# overlayfs inside Podman builds doesn't support O_TMPFILE.  /dev/shm would
# work but is only ~3.5 GB on GHA 7-GB runners — too small for GNOME Platform.
# The --mount=type=cache volume is a bind-mount from btrfs (supports O_TMPFILE)
# and has ~60 GB free, so use a subdirectory of it as TMPDIR instead.
mkdir -p "${FLATPAK_CACHE}/tmp"
export TMPDIR="${FLATPAK_CACHE}/tmp"
mkdir -p /run/dbus
dbus-daemon --system --fork --nopidfile
sleep 1

# ── Seed flatpak repo from build cache (warm start) ──────────────────────────
if [ -d "${FLATPAK_CACHE}/repo/refs" ]; then
    echo "Seeding flatpak repo from build cache..."
    rsync -a --ignore-existing "${FLATPAK_CACHE}/repo/" /var/lib/flatpak/repo/ || true
    echo "Cache seed complete"
fi

flatpak remote-add --system --if-not-exists flathub \
    https://dl.flathub.org/repo/flathub.flatpakrepo

# bootc-installer bundle
# INSTALLER_CHANNEL controls which release to pull from:
#   stable (default) → GitHub "latest" release (non-pre-release)
#   dev              → latest-dev rolling pre-release (tracks dev branch)
# Primary source: projectbluefin/bootc-installer (Project Bluefin's fork).
# Fallback: tuna-os/tuna-installer (upstream) if projectbluefin assets are unavailable.
# v2.6.1 adds nvidia_imgref GPU auto-detection support.
INSTALLER_REPO="projectbluefin/bootc-installer"
FALLBACK_REPO="tuna-os/tuna-installer"
FLATPAK_FILENAME="org.bootcinstaller.Installer.flatpak"
if [[ "${INSTALLER_CHANNEL:-stable}" == "dev" ]]; then
    FLATPAK_FILENAME="org.bootcinstaller.Installer.Devel.flatpak"
    # projectbluefin/bootc-installer uses tag "latest-dev" for dev builds.
    # tuna-os/tuna-installer uses tag "continuous-dev" — different naming convention.
    PRIMARY_URL="https://github.com/${INSTALLER_REPO}/releases/download/latest-dev/${FLATPAK_FILENAME}"
    FALLBACK_URL="https://github.com/${FALLBACK_REPO}/releases/download/continuous-dev/${FLATPAK_FILENAME}"
else
    # Use GitHub's /releases/latest/download/ redirect — always resolves to the
    # current latest stable release without needing to know the version tag.
    PRIMARY_URL="https://github.com/${INSTALLER_REPO}/releases/latest/download/${FLATPAK_FILENAME}"
    FALLBACK_URL="https://github.com/${FALLBACK_REPO}/releases/latest/download/${FLATPAK_FILENAME}"
fi
if ! curl --retry 3 --fail --location \
    "${PRIMARY_URL}" \
    -o /tmp/tuna-installer.flatpak 2>/dev/null; then
    echo "Primary source unavailable, falling back to ${FALLBACK_REPO}..."
    curl --retry 3 --fail --location \
        "${FALLBACK_URL}" \
        -o /tmp/tuna-installer.flatpak
fi
INSTALLER_APP_ID="org.bootcinstaller.Installer"
[[ "${INSTALLER_CHANNEL:-stable}" == "dev" ]] && INSTALLER_APP_ID="org.bootcinstaller.Installer.Devel"

# Import the bundle into a temporary local repo and install from there.
# flatpak install --bundle in a container build (no running flatpak system
# daemon) only creates the installer-origin: remote ref — it does NOT create
# the deploy/ ref that flatpak run/list require.  Installing from a local
# file:// remote goes through the full deploy pipeline and correctly creates
# the deploy/ ref so the app is visible and runnable.
INSTALLER_LOCAL_REPO="/tmp/installer-local-repo"
ostree init --repo="${INSTALLER_LOCAL_REPO}" --mode=archive-z2
flatpak build-import-bundle "${INSTALLER_LOCAL_REPO}" /tmp/tuna-installer.flatpak
rm -f /tmp/tuna-installer.flatpak
flatpak remote-add --system --no-gpg-verify installer-local "file://${INSTALLER_LOCAL_REPO}"
flatpak install --system --noninteractive installer-local "${INSTALLER_APP_ID}" || \
    flatpak update --system --noninteractive "${INSTALLER_APP_ID}"
flatpak remote-delete --system --force installer-local || true
rm -rf "${INSTALLER_LOCAL_REPO}"

# flatpak install inside a container build (no flatpak-system-helper daemon)
# creates the deployment directory but omits the 'active' symlink inside the
# branch directory, leaving the app unreachable to 'flatpak run'/'flatpak list'.
# Reproduce the symlink that a normal installation would create.
_app_arch_dir="/var/lib/flatpak/app/${INSTALLER_APP_ID}/x86_64"
for _branch_dir in "${_app_arch_dir}"/*/; do
    _branch_dir="${_branch_dir%/}"
    [[ -d "${_branch_dir}" ]] || continue
    if [[ ! -L "${_branch_dir}/active" ]]; then
        # Find the single deployment hash directory
        _hash=$(find "${_branch_dir}" -maxdepth 1 -mindepth 1 -type d -printf '%f\n' | head -1)
        if [[ -n "${_hash}" ]]; then
            ln -sfn "${_hash}" "${_branch_dir}/active"
            echo "Created active symlink: ${_branch_dir}/active → ${_hash}"
        fi
    fi
done

flatpak override --system --filesystem=/etc:ro "${INSTALLER_APP_ID}"

# ── Reconcile Flathub apps against the wanted list ───────────────────────────
# In debug mode, skip the full Flathub app list to keep builds fast.
# NOTE: Disabled to allow debug ISOs with full flatpak suite + SSH access
# if [[ "${DEBUG:-0}" == "1" ]]; then
#     echo "DEBUG mode: skipping Flathub app list (installer-only ISO)"
#     # Still save cache for the installer runtime
#     echo "Saving flatpak repo to build cache..."
#     mkdir -p "${FLATPAK_CACHE}"
#     rsync -a --delete /var/lib/flatpak/repo/ "${FLATPAK_CACHE}/repo/"
#     exit 0
# fi

# A variant may override the shared flatpak list with src/<variant>/flatpaks
# (e.g. installer-only ISOs).  VARIANT strips the -nvidia suffix from TARGET,
# mirroring configure-live.sh.
FLATPAKS_LIST="/tmp/flatpaks-list"
VARIANT=$(echo "${TARGET:-dakota-nvidia}" | sed 's/-nvidia-open$//;s/-nvidia$//')
if [ -f "/src/${VARIANT}/flatpaks" ]; then
    FLATPAKS_LIST="/src/${VARIANT}/flatpaks"
fi
readarray -t WANTED < <(grep -v '^[[:space:]]*#' "${FLATPAKS_LIST}" | grep -v '^[[:space:]]*$')

# Install or update everything in the list (--or-update = skip if current)
# --no-related skips locale packs and debug symbols (~3 GB uncompressed)
if [ "${#WANTED[@]}" -gt 0 ]; then
    flatpak install --system --noninteractive --no-related --or-update flathub "${WANTED[@]}"
fi

# Remove any system app that is no longer in the wanted list
readarray -t INSTALLED < <(flatpak list --app --system --columns=application 2>/dev/null || true)
for app in "${INSTALLED[@]}"; do
    # Keep the installer regardless (stable or devel app ID)
    [[ "$app" == "org.bootcinstaller.Installer" ]] && continue
    [[ "$app" == "org.bootcinstaller.Installer.Devel" ]] && continue
    if [[ ! " ${WANTED[*]} " == *" ${app} "* ]]; then
        echo "Removing dropped flatpak: $app"
        flatpak uninstall --system --noninteractive "$app" || true
    fi
done

# Prune unused runtimes left behind by removals
flatpak uninstall --system --noninteractive --unused || true

# ── Save flatpak repo to build cache for next build ──────────────────────────
echo "Saving flatpak repo to build cache..."
mkdir -p "${FLATPAK_CACHE}"
rsync -a --delete /var/lib/flatpak/repo/ "${FLATPAK_CACHE}/repo/"
echo "Cache updated"
