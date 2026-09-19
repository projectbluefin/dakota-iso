#!/usr/bin/bash
# Shared QEMU lifecycle operations for installer acceptance tests.

set -euo pipefail

qemu_binary() {
    command -v /usr/libexec/qemu-kvm /usr/bin/qemu-kvm \
        /usr/bin/qemu-system-x86_64 \
        /home/linuxbrew/.linuxbrew/bin/qemu-system-x86_64 2>/dev/null | head -1
}

ovmf_code() {
    local firmware
    for firmware in /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_CODE.fd \
        /usr/share/edk2/ovmf/OVMF_CODE.fd /usr/share/ovmf/OVMF.fd \
        /home/linuxbrew/.linuxbrew/share/qemu/edk2-x86_64-code.fd \
        /home/linuxbrew/.linuxbrew/Cellar/qemu/11.0.1/share/qemu/edk2-x86_64-code.fd; do
        [[ -f "$firmware" ]] && { printf '%s\n' "$firmware"; return; }
    done
    return 1
}

prepare_ovmf_vars() {
    local destination="$1"
    local template
    for template in /usr/share/OVMF/OVMF_VARS_4M.fd /usr/share/OVMF/OVMF_VARS.fd \
        /usr/share/edk2/ovmf/OVMF_VARS.fd \
        /var/home/jorge/VMs/bluefin-test/OVMF_VARS.fd \
        /var/home/james/dev/ostree-composefs-rebase/ovmf_vars.fd \
        /var/home/james/.local/share/Trash/files/e2e-logs-3/ovmf_vars.fd \
        /home/linuxbrew/.linuxbrew/share/qemu/edk2-i386-vars.fd; do
        [[ -f "$template" ]] && { cp "$template" "$destination"; printf '%s\n' "$destination"; return; }
    done
    return 1
}

qemu_acceleration() {
    if test -r /dev/kvm 2>/dev/null; then
        printf '%s\n' "-accel kvm|"
    elif sudo test -r /dev/kvm 2>/dev/null; then
        printf '%s\n' "-accel kvm|sudo"
    else
        printf '%s\n' "-accel tcg,thread=multi|"
    fi
}

boot_live() {
    local target="$1" output_dir="$2" disk="$3" disk_format="$4" scratch="$5"
    local monitor="$6" serial="$7" ssh_port="$8" vars_destination="$9"
    local qemu iso="" ovmf firmware_vars acceleration qemu_prefix cpu_flag disk_cache

    qemu=$(qemu_binary || true)
    [[ -n "$qemu" ]] || { echo "qemu-kvm / qemu-system-x86_64 not found" >&2; exit 1; }
    for candidate in \
        "${output_dir}/${target}-debug-live.iso" \
        "${output_dir}/${target}-live.iso" \
        "${output_dir}/${target}-live-"*.iso; do
        [[ -f "$candidate" ]] && { iso="$candidate"; break; }
    done
    [[ -n "$iso" ]] || { echo "No ISO found — run: just debug=1 iso-sd-boot ${target}" >&2; exit 1; }

    ovmf=$(ovmf_code || true)
    firmware_vars=$(prepare_ovmf_vars "$vars_destination" || true)
    [[ -n "$ovmf" && -n "$firmware_vars" ]] || { echo "OVMF firmware not found" >&2; exit 1; }

    case "$disk_format" in
        raw)
            [[ -f "$disk" ]] || truncate -s 64G "$disk"
            disk_cache=",cache=unsafe"
            ;;
        qcow2)
            [[ -f "$disk" ]] || qemu-img create -f qcow2 "$disk" 64G
            disk_cache=""
            ;;
        *) echo "Unsupported disk format: ${disk_format}" >&2; exit 1 ;;
    esac
    [[ -f "$scratch" ]] || truncate -s 16G "$scratch"
    rm -f "$monitor" "$serial"

    IFS='|' read -r acceleration qemu_prefix <<<"$(qemu_acceleration)"
    cpu_flag="-cpu host"
    [[ "$acceleration" =~ tcg ]] && cpu_flag="-cpu qemu64"

    echo "Booting live ISO: ${iso}"
    $qemu_prefix "$qemu" \
        -machine q35 $cpu_flag -m "${QEMU_MEM:-8192}" -smp "${QEMU_SMP:-8}" $acceleration \
        -drive "if=pflash,format=raw,readonly=on,file=${ovmf}" \
        -drive "if=pflash,format=raw,file=${firmware_vars}" \
        -drive "if=none,id=iso,file=${iso},media=cdrom,readonly=on,format=raw" \
        -device virtio-scsi-pci,id=scsi \
        -device scsi-cd,drive=iso \
        -drive "if=none,id=disk,file=${disk},format=${disk_format}${disk_cache}" \
        -device virtio-blk-pci,drive=disk \
        -drive "if=none,id=scratch,file=${scratch},format=raw,cache=unsafe" \
        -device virtio-blk-pci,drive=scratch \
        -netdev "user,id=net0,hostfwd=tcp::${ssh_port}-:22" \
        -device virtio-net-pci,netdev=net0 \
        -monitor "unix:${monitor},server,nowait" \
        -serial "file:${serial}" \
        -display none \
        -daemonize
    # A sudo-prefixed qemu (the /dev/kvm fallback in qemu_acceleration) creates a
    # root-owned serial file, which silently defeats every grep in wait_live:
    # `grep ... 2>/dev/null` on an unreadable file looks identical to "marker not
    # present yet", so readiness never becomes true no matter how long the guest runs.
    sudo chmod a+r "$serial" 2>/dev/null || chmod a+r "$serial" 2>/dev/null || true
    echo "Live QEMU started (monitor: ${monitor})"
}

wait_live() {
    local ssh_port="$1" serial="$2"
    local ssh_opts="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -o PreferredAuthentications=password"

    echo "Waiting for live environment on port ${ssh_port}..."
    # Loop bound: each non-ready iteration costs ~10s (SSH's own 5s ConnectTimeout
    # plus the 5s sleep below), so 150 iterations is a ~25min ceiling, not the
    # ~12.5min the iteration count alone suggests. The `bluefin` (full GNOME
    # desktop) variant has been observed taking ~8min to become reachable under
    # loaded CI runners — GDM plus its full unit set boots meaningfully slower
    # than the lighter dakota/bluefin-lts-hwe variants — while still making
    # steady progress (not hung), so this widens the ceiling rather than fixing
    # a stall.
    for i in $(seq 1 150); do
        if grep -q "DAKOTA_LIVE_READY\|debug-ssh-banner" "$serial" 2>/dev/null; then
            echo "Serial marker seen — polling SSH..."
            for _ in $(seq 1 30); do
                if sshpass -p live ssh $ssh_opts liveuser@127.0.0.1 -p "$ssh_port" true 2>/dev/null; then
                    echo "Live environment ready (serial marker + SSH confirmed)."
                    sleep 3
                    return
                fi
                sleep 3
            done
            echo "ERROR: serial marker seen but SSH not ready after 90 s" >&2
            cat "$serial" >&2
            exit 1
        fi
        if sshpass -p live ssh $ssh_opts liveuser@127.0.0.1 -p "$ssh_port" true 2>/dev/null; then
            echo "SSH responded (pre-marker) — waiting 15 s for sshd to stabilise..."
            sleep 15
            return
        fi
        [[ "$i" -eq 150 ]] && { echo "Timeout waiting for live environment after ~25m" >&2; cat "$serial" >&2; exit 1; }
        sleep 5
    done
}

wait_for_live_qemu_exit() {
    local disk="$1" live_monitor="$2" disk_pattern pids

    disk_pattern=$(printf '%s' "$disk" | sed 's/[][\\.^$*+?(){}|]/\\&/g')

    # A live GNOME session can take well over 30s to finish an ACPI powerdown,
    # and `shutdown` only gets one `quit` in before the monitor socket goes away.
    # Erroring out here used to abort the whole E2E run *after* a successful
    # install — the disk was fine, the previous VM just had not reaped yet.
    # Wait generously, then escalate, and only fail if the disk is still pinned.
    for attempt in $(seq 1 60); do
        if ! sudo pgrep -f "qemu.*${disk_pattern}" >/dev/null 2>&1; then
            rm -f "$live_monitor" 2>/dev/null || sudo rm -f "$live_monitor"
            return
        fi
        [[ $((attempt % 5)) -eq 0 ]] && \
            echo "Waiting for live QEMU to release ${disk} (attempt ${attempt}/60)..."
        sleep 2
    done

    echo "Live QEMU still holds ${disk} after 120s — escalating to SIGTERM" >&2
    pids=$(sudo pgrep -f "qemu.*${disk_pattern}" || true)
    [[ -n "$pids" ]] && sudo kill $pids 2>/dev/null
    for _ in $(seq 1 10); do
        sudo pgrep -f "qemu.*${disk_pattern}" >/dev/null 2>&1 || break
        sleep 1
    done

    if sudo pgrep -f "qemu.*${disk_pattern}" >/dev/null 2>&1; then
        echo "Live QEMU ignored SIGTERM — escalating to SIGKILL" >&2
        pids=$(sudo pgrep -f "qemu.*${disk_pattern}" || true)
        [[ -n "$pids" ]] && sudo kill -9 $pids 2>/dev/null
        sleep 2
    fi

    if sudo pgrep -f "qemu.*${disk_pattern}" >/dev/null 2>&1; then
        echo "ERROR: live QEMU still holds ${disk} after SIGKILL" >&2
        exit 1
    fi
    rm -f "$live_monitor" 2>/dev/null || sudo rm -f "$live_monitor"
}

boot_installed() {
    local disk="$1" disk_format="$2" monitor="$3" serial="$4" live_monitor="$5" vars_destination="$6"
    local qemu ovmf firmware_vars acceleration qemu_prefix cpu_flag disk_cache

    qemu=$(qemu_binary || true)
    [[ -n "$qemu" ]] || { echo "qemu-kvm / qemu-system-x86_64 not found" >&2; exit 1; }
    ovmf=$(ovmf_code || true)
    firmware_vars=$(prepare_ovmf_vars "$vars_destination" || true)
    [[ -n "$ovmf" && -n "$firmware_vars" ]] || { echo "OVMF firmware not found" >&2; exit 1; }
    case "$disk_format" in
        raw) disk_cache=",cache=unsafe" ;;
        qcow2) disk_cache="" ;;
        *) echo "Unsupported disk format: ${disk_format}" >&2; exit 1 ;;
    esac

    wait_for_live_qemu_exit "$disk" "$live_monitor"
    rm -f "$monitor" "$serial"
    for _ in $(seq 1 20); do
        if ! sudo fuser "$disk" >/dev/null 2>&1; then
            break
        fi
        sleep 2
    done
    if sudo fuser "$disk" >/dev/null 2>&1; then
        echo "Live QEMU did not release install disk: ${disk}" >&2
        sudo fuser -v "$disk" >&2 || true
        exit 1
    fi

    IFS='|' read -r acceleration qemu_prefix <<<"$(qemu_acceleration)"
    cpu_flag="-cpu host"
    [[ "$acceleration" =~ tcg ]] && cpu_flag="-cpu qemu64"

    echo "Booting installed disk: ${disk}"
    $qemu_prefix "$qemu" \
        -machine q35 $cpu_flag -m "${QEMU_MEM:-8192}" -smp "${QEMU_SMP:-8}" $acceleration \
        -drive "if=pflash,format=raw,readonly=on,file=${ovmf}" \
        -drive "if=pflash,format=raw,file=${firmware_vars}" \
        -drive "if=none,id=disk,file=${disk},format=${disk_format}${disk_cache}" \
        -device virtio-blk-pci,drive=disk \
        -netdev user,id=net0 \
        -device virtio-net-pci,netdev=net0 \
        -monitor "unix:${monitor},server,nowait" \
        -serial "file:${serial}" \
        -display none \
        -daemonize
    echo "Installed QEMU started (monitor: ${monitor})"
    for _ in $(seq 1 15); do
        [[ -S "$monitor" ]] && break
        sleep 2
    done
}

patch_bls_console() {
    local ssh_port="$1" encryption="$2"
    local ssh_opts="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -o PreferredAuthentications=password"

    sshpass -p live ssh $ssh_opts liveuser@127.0.0.1 -p "$ssh_port" "sudo bash -s -- '${encryption}'" <<'REMOTE'
set -euo pipefail
encryption="$1"
boot_part="/dev/vda1"
luks_part="/dev/vda2"
if ls /dev/vda3 >/dev/null 2>&1; then
    boot_part="/dev/vda2"
    luks_part="/dev/vda3"
fi
luks_uuid=""
if [[ "$encryption" == "luks" ]]; then
    luks_uuid=$(cryptsetup luksUUID "$luks_part" 2>/dev/null || true)
fi
mount_dir=$(mktemp -d)
trap 'umount "$mount_dir" 2>/dev/null || true; rmdir "$mount_dir"' EXIT
mount "$boot_part" "$mount_dir"
count=0
for entry in "$mount_dir"/loader/entries/*.conf "$mount_dir"/EFI/loader/entries/*.conf; do
    [[ -f "$entry" ]] || continue
    if grep -q '^options ' "$entry" && ! grep -q 'console=tty0' "$entry"; then
        if [[ -n "$luks_uuid" ]]; then
            sed -i "s|^options .*|& console=tty0 console=ttyS0 rd.luks.name=${luks_uuid}=root|" "$entry"
        elif [[ "$encryption" == "plain" ]]; then
            sed -i 's|^options .*|& console=tty0 console=ttyS0 rd.info systemd.journald.forward_to_console=yes|' "$entry"
        else
            sed -i 's|^options .*|& console=tty0 console=ttyS0|' "$entry"
        fi
        count=$((count + 1))
        echo "patched: $(basename "$entry")"
    fi
done
echo "BLS patch: ${count} entries updated"
REMOTE
}

verify_efi() {
    local ssh_port="$1"
    local ssh_opts="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5 -o PreferredAuthentications=password"

    sshpass -p live ssh $ssh_opts liveuser@127.0.0.1 -p "$ssh_port" 'sudo bash -s' <<'REMOTE'
set -euo pipefail
echo "=== Checking NVRAM UEFI boot entries ==="
efibootmgr -v || true
if efibootmgr -v | grep -qiE '(Linux|systemd|Fedora|BOOT|Dakota|Bluefin)'; then
    echo "✅ NVRAM UEFI boot entry verified."
else
    echo "⚠️ Warning: No explicit NVRAM UEFI boot entry found in efibootmgr."
fi
REMOTE
}

verify_installed() {
    local monitor="$1" serial="$2" screenshot="$3"
    local socat_prefix="" log

    echo "Waiting for installed system to reach Graphical Interface (up to 5 min)..."
    local deadline=$((SECONDS + 300))
    while [[ $SECONDS -lt $deadline ]]; do
        log=$(cat "$serial" 2>/dev/null || true)
        if echo "$log" | grep -q "Reached target.*Graphical\|Reached target.*Multi-User\|login:"; then
            echo "✅ Installed system boot verified — plain composefs install succeeded"
            test -w "$monitor" 2>/dev/null || socat_prefix="sudo"
            echo "screendump $screenshot" | $socat_prefix socat - "UNIX-CONNECT:${monitor}" 2>/dev/null || true
            bash "live/src/show-screenshot.sh" "$screenshot" "Installed system" 2>/dev/null || true
            echo "quit" | $socat_prefix socat - "UNIX-CONNECT:${monitor}" 2>/dev/null || true
            return
        fi
        if echo "$log" | grep -q "Emergency mode\|You are in emergency mode\|Kernel panic"; then
            echo "❌ Emergency shell or kernel panic detected" >&2
            echo "--- last 30 lines of serial log ---" >&2
            echo "$log" | tail -30 >&2
            exit 1
        fi
        sleep 5
    done
    echo "❌ Timeout: installed system did not reach graphical target in 5 minutes" >&2
    echo "--- last 30 lines of serial log ---" >&2
    cat "$serial" 2>/dev/null | tail -30 >&2
    test -w "$monitor" 2>/dev/null || socat_prefix="sudo"
    echo "screendump $screenshot" | $socat_prefix socat - "UNIX-CONNECT:${monitor}" 2>/dev/null || true
    exit 1
}

shutdown() {
    local monitor="$1"
    local socat_prefix=""
    test -w "$monitor" 2>/dev/null || socat_prefix="sudo"
    echo "system_powerdown" | $socat_prefix socat - "UNIX-CONNECT:${monitor}" 2>/dev/null || true
    # Give the guest a real chance to power down on its own: the monitor socket
    # disappearing is qemu exiting. Only force `quit` if it is still there — a
    # 5s fixed sleep used to fire mid-ACPI-shutdown, when the socket was already
    # gone, so the `quit` went nowhere and qemu kept running for another minute.
    for _ in $(seq 1 30); do
        [[ -S "$monitor" ]] || return
        sleep 2
    done
    echo "quit" | $socat_prefix socat - "UNIX-CONNECT:${monitor}" 2>/dev/null || true
}

case "${1:-}" in
    boot-live) shift; boot_live "$@" ;;
    wait-live) shift; wait_live "$@" ;;
    boot-installed) shift; boot_installed "$@" ;;
    patch-bls-console) shift; patch_bls_console "$@" ;;
    verify-efi) shift; verify_efi "$@" ;;
    verify-installed) shift; verify_installed "$@" ;;
    shutdown) shift; shutdown "$@" ;;
    *)
        echo "Usage: $0 {boot-live|wait-live|boot-installed|patch-bls-console|verify-efi|verify-installed|shutdown} ..." >&2
        exit 1
        ;;
esac
