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
# An OCI flatpak remote (tuna-os, for Utah's Ghostty) makes flatpak spawn a
# session bus of its own, and a bus refuses to start without a machine id --
# which a container build does not have:
#   Cannot spawn a message bus without a machine-id
# Flathub's ostree remotes never ask for either. The id is build-time only;
# systemd regenerates a real one on first boot. Same fix as
# projectbluefin/utah iso/live/src/install-flatpaks.sh.
CREATED_MACHINE_ID=0
if [[ ! -s /etc/machine-id ]]; then
    CREATED_MACHINE_ID=1
    systemd-machine-id-setup >/dev/null 2>&1 || dbus-uuidgen > /etc/machine-id
fi
dbus-daemon --system --fork --nopidfile
if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]]; then
    DBUS_SESSION_BUS_ADDRESS="$(dbus-daemon --session --fork --print-address)"
    export DBUS_SESSION_BUS_ADDRESS
fi
sleep 1

# ── Seed flatpak repo from build cache (warm start) ──────────────────────────
if [ -d "${FLATPAK_CACHE}/repo/refs" ]; then
    echo "Seeding flatpak repo from build cache..."
    # cp, unlike rsync, will not create the destination directory, and
    # /var/lib/flatpak/repo does not exist until flatpak first runs.
    mkdir -p /var/lib/flatpak/repo
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --ignore-existing "${FLATPAK_CACHE}/repo/" /var/lib/flatpak/repo/ || true
    else
        cp -a -n "${FLATPAK_CACHE}/repo/." /var/lib/flatpak/repo/ || true
    fi
    echo "Cache seed complete"
fi

# ── Flatpak remotes come from checked-in descriptors, never the network ──────
# A .flatpakrepo file carries Url= and GPGKey=: it is the trust root every
# later download from that remote is verified against, and /var/lib/flatpak is
# copied onto installed systems (flatpak_var_path), so whatever lands here is
# the permanent trust root for every future `flatpak update` on every machine
# installed from the ISO. Fetching that file over the network at build time
# would let anyone who can alter one response during the build redefine the
# key. The descriptors are therefore reviewed files under live/src/remotes/
# (bind-mounted at /src by the Containerfile), and a key or URL rotation
# upstream shows up as a diff to this repository instead of flowing silently
# into the next nightly ISO.
REMOTES_DIR="/src/remotes"
add_remote() {
    local name="$1" file="${REMOTES_DIR}/$1.flatpakrepo"
    if [[ ! -s "${file}" ]]; then
        echo "Missing flatpak remote descriptor ${file} for remote '${name}'" >&2
        exit 1
    fi
    flatpak remote-add --system --if-not-exists "${name}" "${file}"
}

add_remote flathub

# bootc-installer bundle
# INSTALLER_CHANNEL controls which bundle to pull from the pinned release:
#   stable (default) → org.bootcinstaller.Installer.flatpak
#   dev              → Devel-branded bundle from the same versioned release
# Source: tuna-os/bootc-installer — the sole upstream for bootc-installer and
# its fisherman backend. projectbluefin/bootc-installer and
# projectbluefin/fisherman are retired; there is no fallback repo (the old
# fallback, tuna-os/tuna-installer, is itself archived and redirects here).
#
# Neither channel uses a rolling tag: a published release accepts no new
# assets under GitHub's immutable-release ruleset, and deleting one to start
# over permanently bans the tag name from ever being re-created.
# projectbluefin lost "latest-stable" and then "latest-dev" this way on
# 2026-08-01 (see TestInstallerChannelURLs in
# tests/test_live_build_invariants.py, which enforces this repo-wide).
# tuna-os/bootc-installer's auto-release job cuts a fresh date+sha tag on
# every push to `dev` and attaches both the stable and Devel bundles to it.
#
# The release tag and the SHA-256 of both bundles are pinned in
# installer-bundle.lock next to this script. The installer runs as root on
# every machine that boots the ISO, so the build refuses a bundle whose bytes
# differ from the reviewed lock instead of trusting whatever the moving
# /releases/latest/ redirect serves. scripts/bump-installer-bundle.sh
# rewrites the lock for a new upstream release.
INSTALLER_REPO="tuna-os/bootc-installer"
FLATPAK_FILENAME="org.bootcinstaller.Installer.flatpak"
if [[ "${INSTALLER_CHANNEL:-stable}" == "dev" ]]; then
    FLATPAK_FILENAME="org.bootcinstaller.Installer.Devel.flatpak"
fi
INSTALLER_LOCK_DIR="${BASH_SOURCE[0]%/*}"
[[ "${INSTALLER_LOCK_DIR}" == "${BASH_SOURCE[0]}" ]] && INSTALLER_LOCK_DIR="."
INSTALLER_LOCK="${INSTALLER_LOCK_DIR}/installer-bundle.lock"
INSTALLER_TAG=$(sed -n 's/^tag=//p' "${INSTALLER_LOCK}" | head -1)
INSTALLER_SHA256=$(sed -n "s/^${FLATPAK_FILENAME}=//p" "${INSTALLER_LOCK}" | head -1)
if [[ -z "${INSTALLER_TAG}" || ! "${INSTALLER_SHA256}" =~ ^[0-9a-f]{64}$ ]]; then
    echo "ERROR: ${INSTALLER_LOCK} must pin tag= and ${FLATPAK_FILENAME}=<sha256>" >&2
    exit 1
fi
PRIMARY_URL="https://github.com/${INSTALLER_REPO}/releases/download/${INSTALLER_TAG}/${FLATPAK_FILENAME}"
curl --retry 3 --fail --location \
    "${PRIMARY_URL}" \
    -o /tmp/tuna-installer.flatpak
if ! echo "${INSTALLER_SHA256}  /tmp/tuna-installer.flatpak" | sha256sum --check --quiet --strict -; then
    rm -f /tmp/tuna-installer.flatpak
    echo "ERROR: ${FLATPAK_FILENAME} from ${INSTALLER_TAG} does not match the SHA-256 in ${INSTALLER_LOCK}" >&2
    exit 1
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
readarray -t ENTRIES < <(grep -v '^[[:space:]]*#' "${FLATPAKS_LIST}" | grep -v '^[[:space:]]*$')

# An entry is an app ID from Flathub, or "remote:app-id" for an app a variant
# takes from another remote. Utah's terminal is Ghostty from the TunaOS OCI
# remote -- Hummingbird packages no ptyxis or vte -- and without this form the
# Utah ISO shipped no terminal at all. Every remote named here needs a
# checked-in descriptor at ${REMOTES_DIR}/<remote>.flatpakrepo (see add_remote).
declare -A KNOWN_REMOTES=(
    [tuna-os]=1
)
declare -A BY_REMOTE=()
WANTED=()
for entry in "${ENTRIES[@]}"; do
    remote=flathub app="${entry}"
    if [[ "${entry}" == *:* ]]; then
        remote="${entry%%:*}" app="${entry#*:}"
        [[ -n "${KNOWN_REMOTES[${remote}]:-}" ]] || { echo "Unknown flatpak remote '${remote}' in ${FLATPAKS_LIST}" >&2; exit 1; }
    fi
    BY_REMOTE[${remote}]+="${app} "
    WANTED+=("${app}")
done

# Install or update everything in the list (--or-update = skip if current)
# --no-related skips locale packs and debug symbols (~3 GB uncompressed)
for remote in "${!BY_REMOTE[@]}"; do
    [[ "${remote}" == flathub ]] || add_remote "${remote}"
    read -ra apps <<< "${BY_REMOTE[${remote}]}"
    flatpak install --system --noninteractive --no-related --or-update "${remote}" "${apps[@]}"
done

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
if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete /var/lib/flatpak/repo/ "${FLATPAK_CACHE}/repo/"
else
    # Stage into a sibling dir first so a failed copy cannot leave the cache
    # empty: the warm repo is only replaced once the copy fully succeeds.
    rm -rf "${FLATPAK_CACHE}/repo.new"
    if cp -a /var/lib/flatpak/repo "${FLATPAK_CACHE}/repo.new"; then
        rm -rf "${FLATPAK_CACHE}/repo.old"
        if [ -d "${FLATPAK_CACHE}/repo" ]; then
            mv "${FLATPAK_CACHE}/repo" "${FLATPAK_CACHE}/repo.old"
        fi
        mv "${FLATPAK_CACHE}/repo.new" "${FLATPAK_CACHE}/repo"
        rm -rf "${FLATPAK_CACHE}/repo.old"
    else
        echo "WARNING: cache save failed; keeping previous warm cache" >&2
        rm -rf "${FLATPAK_CACHE}/repo.new"
    fi
fi
echo "Cache updated"

# Do not ship the build-time machine id: every live boot would share it.
# An empty file lets systemd generate one per boot, as it did before.
if [[ "${CREATED_MACHINE_ID}" == 1 ]]; then
    : > /etc/machine-id
    rm -f /var/lib/dbus/machine-id
fi
