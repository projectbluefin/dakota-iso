#!/usr/bin/bash
# Enable SSH only in an E2E copy of the exact production squashfs and OCI bytes.
set -euo pipefail
SFS="${1:?Usage: build-debug-iso.sh <production-sfs> <boot-tar> <debug-iso> <title>}"
BOOT_TAR="${2:?}"
OUTPUT="${3:?}"
TITLE="${4:?}"
WORK=$(mktemp -d "$(dirname "${SFS}")/debug-iso.XXXXXX")
trap 'rm -rf "${WORK}"' EXIT
ROOT="${WORK}/root"
unsquashfs -d "${ROOT}" "${SFS}"
SSH_UNIT=sshd.service
[[ -f "${ROOT}/usr/lib/systemd/system/sshd.service" ]] || SSH_UNIT=ssh.service
mkdir -p "${ROOT}/etc/systemd/system/multi-user.target.wants" \
    "${ROOT}/etc/systemd/system-preset" "${ROOT}/etc/ssh/sshd_config.d"
ln -sf "/usr/lib/systemd/system/${SSH_UNIT}" \
    "${ROOT}/etc/systemd/system/multi-user.target.wants/${SSH_UNIT}"
printf 'PasswordAuthentication yes\n' > "${ROOT}/etc/ssh/sshd_config.d/00-e2e.conf"
# Fedora livesys clears live passwords at boot; reassert only in the debug copy.
cat > "${ROOT}/usr/lib/systemd/system/live-debug-passwords.service" <<'UNIT'
[Unit]
Description=E2E live password
After=livesys.service livesys-late.service
Before=sshd.service ssh.service
[Service]
Type=oneshot
ExecStart=/usr/bin/bash -c "echo 'liveuser:live' | chpasswd"
[Install]
WantedBy=multi-user.target
UNIT
ln -sf /usr/lib/systemd/system/live-debug-passwords.service \
    "${ROOT}/etc/systemd/system/multi-user.target.wants/live-debug-passwords.service"
printf 'enable %s\nenable live-debug-passwords.service\n' "${SSH_UNIT}" \
    > "${ROOT}/etc/systemd/system-preset/90-live-debug.preset"
mkdir -p "${ROOT}/proc" "${ROOT}/sys" "${ROOT}/dev" "${ROOT}/run" "${ROOT}/tmp"
mksquashfs "${ROOT}" "${WORK}/debug.sfs" -noappend -comp zstd \
    -Xcompression-level 1 -b 1M -processors 4 -wildcards \
    -e 'proc/*' -e 'sys/*' -e 'dev/*' -e 'run/*' -e 'tmp/*'
bash "$(dirname "${BASH_SOURCE[0]}")/../live/src/build-iso.sh" \
    --title "${TITLE}" "${BOOT_TAR}" "${WORK}/debug.sfs" "${OUTPUT}"
