#!/usr/bin/bash
# Boot the unmodified prototype with no network and a read-only source disk.
set -euo pipefail

medium=${1:?raw disk image required}
firmware=${2:?OVMF code image required}
vars=${3:?OVMF variables template required}
serial=${4:?serial log path required}

scratch=$(mktemp -d)
qemu_pid=
cleanup() {
    if [[ -n $qemu_pid ]]; then
        kill "$qemu_pid" 2>/dev/null || true
        wait "$qemu_pid" 2>/dev/null || true
    fi
    rm -rf -- "$scratch"
}
trap cleanup EXIT
cp -- "$vars" "$scratch/vars.fd"
: >"$serial"

qemu-system-x86_64 -accel kvm -machine q35 -cpu host -smp 4 -m 8192 \
    -drive "if=pflash,unit=0,format=raw,readonly=on,file=$firmware" \
    -drive "if=pflash,unit=1,format=raw,file=$scratch/vars.fd" \
    -display none -vga virtio -monitor none -serial "file:$serial" -nic none -no-reboot \
    -drive "if=none,id=medium,format=raw,readonly=on,file=$medium" \
    -device virtio-blk-pci,drive=medium,bootindex=1 &
qemu_pid=$!

for ((i = 0; i < 180; ++i)); do
    if grep -aFq DAKOTA_LIVE_READY "$serial"; then
        echo 'DAKOTA_LIVE_READY: prototype booted from read-only media'
        exit 0
    fi
    kill -0 "$qemu_pid" 2>/dev/null || break
    sleep 1
done
tail -n 100 "$serial" >&2
echo 'Prototype did not reach live readiness' >&2
exit 1
