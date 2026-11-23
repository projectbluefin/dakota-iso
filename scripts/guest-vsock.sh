#!/usr/bin/bash
# scripts/guest-vsock.sh — reach an E2E guest over AF_VSOCK with a per-run key.
#
# Source this file; it only defines functions.
#
# The installed system is booted with a vhost-vsock-pci device and two
# systemd credentials passed as SMBIOS type 11 strings (#380):
#
#   ssh.ephemeral-authorized_keys-all
#       A fresh ed25519 public key generated for this boot. The sshd units
#       that systemd-ssh-generator creates for vsock::22 import it and accept
#       it for any user, with no home directory involved.
#   systemd.unit-dropin.sshd-vsock.socket
#       A drop-in that runs `ssh-keygen -A` before the vsock socket starts
#       listening. The generated per-connection unit runs `sshd -i` with no
#       host key generation, and the image creates host keys only from
#       sshd.service, which it does not enable; without this every connection
#       ends with "sshd: no hostkeys available -- exiting".
#
# Nothing is written to the installed disk: the system under test is the one
# users get. scripts/fisherman-install.sh --enable-debug-ssh (root password +
# sshd config on disk) remains for local debugging only (E2E_DEBUG_SSH=1).

# Host-side dialer for AF_VSOCK, as ssh options. Prefers systemd-ssh-proxy
# (systemd >= 256, passes the connected socket to ssh), else a socat built with
# VSOCK support.
# Usage: guest_vsock_proxy_opts <cid>  — sets GUEST_VSOCK_PROXY_OPTS.
guest_vsock_proxy_opts() {
    local cid="$1" proxy
    for proxy in /usr/lib/systemd/systemd-ssh-proxy /usr/libexec/systemd/systemd-ssh-proxy; do
        if [[ -x "$proxy" ]]; then
            GUEST_VSOCK_PROXY_OPTS=(-o "ProxyCommand=${proxy} vsock/${cid} 22" -o ProxyUseFdpass=yes)
            return 0
        fi
    done
    if command -v socat >/dev/null 2>&1 && socat -V 2>/dev/null | grep -q 'define WITH_VSOCK 1'; then
        GUEST_VSOCK_PROXY_OPTS=(-o "ProxyCommand=socat - VSOCK-CONNECT:${cid}:22")
        return 0
    fi
    echo "❌ No AF_VSOCK dialer on this host: need /usr/lib/systemd/systemd-ssh-proxy (systemd >= 256) or socat with VSOCK support" >&2
    return 1
}

# Prepare a per-run key and the QEMU arguments that hand it to the guest.
# Usage: guest_vsock_prepare <state_dir> <cid> [<qemu_prefix>]
#   state_dir    recreated; receives the private key and the CID for
#                guest_vsock_ssh. Must not be shared between concurrent VMs.
#   cid          guest CID, >= 3 and unique among running VMs on this host.
#   qemu_prefix  "sudo" when QEMU itself runs under sudo (device access is
#                then checked as root).
# Sets GUEST_VSOCK_QEMU_ARGS. Fails with the reason when the host cannot do it.
# GUEST_VSOCK_DEVICE overrides /dev/vhost-vsock (unit tests).
guest_vsock_prepare() {
    local dir="$1" cid="$2" prefix="${3:-}" pub_b64 dropin_b64
    local dev="${GUEST_VSOCK_DEVICE:-/dev/vhost-vsock}"
    if [[ ! "$cid" =~ ^[0-9]+$ ]] || (( cid < 3 )); then
        echo "❌ Invalid vsock guest CID '${cid}' (must be an integer >= 3)" >&2
        return 1
    fi
    if [[ ! -e "$dev" ]]; then
        modprobe vhost_vsock 2>/dev/null || sudo -n modprobe vhost_vsock 2>/dev/null || true
    fi
    if [[ "$prefix" == "sudo" ]]; then
        if ! sudo -n test -w "$dev" 2>/dev/null; then
            echo "❌ ${dev} missing — run: sudo modprobe vhost_vsock" >&2
            return 1
        fi
    elif [[ ! -w "$dev" ]]; then
        echo "❌ ${dev} missing or not writable by $(id -un) — run: sudo modprobe vhost_vsock (and join the kvm group or grant access)" >&2
        return 1
    fi
    guest_vsock_proxy_opts "$cid" || return 1
    command -v ssh-keygen >/dev/null 2>&1 || { echo "❌ ssh-keygen not found" >&2; return 1; }

    rm -rf "$dir"
    mkdir -m 700 "$dir" || return 1
    ssh-keygen -q -t ed25519 -N "" -C "dakota-iso-e2e" -f "$dir/key" || return 1
    echo "$cid" > "$dir/cid"
    # One SMBIOS string per credential: strip base64 line wrapping. The base64
    # alphabet has no commas, so no QEMU option escaping is needed.
    pub_b64=$(base64 < "$dir/key.pub" | tr -d '\n')
    dropin_b64=$(printf '[Socket]\nExecStartPre=/usr/bin/ssh-keygen -A\n' | base64 | tr -d '\n')
    # shellcheck disable=SC2034  # consumed by the sourcing recipe
    GUEST_VSOCK_QEMU_ARGS=(
        -device "vhost-vsock-pci,guest-cid=${cid}"
        -smbios "type=11,value=io.systemd.credential.binary:ssh.ephemeral-authorized_keys-all=${pub_b64}"
        -smbios "type=11,value=io.systemd.credential.binary:systemd.unit-dropin.sshd-vsock.socket=${dropin_b64}"
    )
    echo "vsock guest access armed (guest-cid=${cid}, key in ${dir})"
}

# Build the ssh command for a guest prepared by guest_vsock_prepare.
# Usage: guest_vsock_ssh <state_dir>  — sets GUEST_VSOCK_SSH (append the
# remote command when calling it).
guest_vsock_ssh() {
    local dir="$1" cid
    if [[ ! -r "$dir/key" || ! -r "$dir/cid" ]]; then
        echo "❌ No per-run vsock key in ${dir} — boot the installed system with the *-boot-qemu-installed recipe first" >&2
        return 1
    fi
    read -r cid < "$dir/cid"
    guest_vsock_proxy_opts "$cid" || return 1
    # The host name is never resolved: the ProxyCommand carries the connection.
    # shellcheck disable=SC2034  # consumed by the sourcing script
    GUEST_VSOCK_SSH=(
        ssh -i "$dir/key" -o IdentitiesOnly=yes -o BatchMode=yes
        -o PreferredAuthentications=publickey
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
        -o LogLevel=ERROR -o ConnectTimeout=5
        -o ServerAliveInterval=10 -o ServerAliveCountMax=6
        "${GUEST_VSOCK_PROXY_OPTS[@]}"
        "root@vsock-${cid}"
    )
}
