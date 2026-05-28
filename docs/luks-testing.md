# LUKS Testing

End-to-end LUKS encrypted install testing. Reproduces [projectbluefin/dakota#270](https://github.com/projectbluefin/dakota/issues/270).

## Quick start (all-in-one)

```bash
just debug=1 installer_channel=dev e2e dakota
# Builds ISO then runs the full LUKS E2E QEMU test
```

Or separately:

```bash
just debug=1 installer_channel=dev iso-sd-boot dakota
just luks-test-qemu dakota
```

## QEMU flow (automated, CI-equivalent)

```
luks-test-qemu dakota
  ├─ luks-boot-qemu-live       → daemonize QEMU with live ISO + blank install disk
  ├─ luks-install-qemu         → SSH fisherman LUKS install + BLS patch + shutdown
  ├─ luks-boot-qemu-installed  → daemonize QEMU booting installed disk (no ISO)
  └─ luks-unlock-qemu          → send passphrase via QEMU monitor, verify boot
```

### Key paths

| Variable | Default |
|---|---|
| `luks-qemu-disk` | `/var/tmp/dakota-luks-install.qcow2` |
| `luks-qemu-ssh-port` | `2222` |
| `luks-qemu-monitor-live` | `/tmp/dakota-qemu-live.sock` |
| `luks-qemu-monitor-installed` | `/tmp/dakota-qemu-installed.sock` |
| `luks-qemu-serial-live` | `/tmp/dakota-qemu-live-serial.log` |
| `luks-qemu-serial-installed` | `/tmp/dakota-qemu-installed-serial.log` |
| `luks-passphrase` | `testpassphrase` |

Clean up before a fresh run:
```bash
sudo rm -f /var/tmp/dakota-luks-install.qcow2 /tmp/dakota-qemu-*.sock /tmp/dakota-qemu-*.log
```

## Libvirt flow (interactive)

```bash
# 1. Build debug ISO
just debug=1 installer_channel=dev iso-sd-boot dakota

# 2. Boot in libvirt, wait for SSH ready
just debug=1 boot-libvirt-debug dakota

# 3. SSH fisherman LUKS install + reboot
just luks-install dakota

# 4. Automated passphrase unlock
just luks-unlock dakota
# or interactive serial console (type: testpassphrase)
just luks-boot dakota
```

Cleanup:
```bash
sudo virsh destroy dakota-debug && sudo virsh undefine dakota-debug --nvram
```

## Manual QEMU install (for debugging)

```bash
QEMU=/home/linuxbrew/.linuxbrew/bin/qemu-system-x86_64
OVMF=/home/linuxbrew/.linuxbrew/Cellar/qemu/11.0.0/share/qemu/edk2-x86_64-code.fd
VARS=/var/tmp/OVMF_VARS.fd; dd if=/dev/zero bs=1k count=256 of=$VARS 2>/dev/null
qemu-img create -f qcow2 /var/tmp/dakota-install-disk.qcow2 50G

$QEMU -machine q35 -m 4096 -accel kvm -cpu host -smp 4 \
    -drive if=pflash,format=raw,readonly=on,file=$OVMF \
    -drive if=pflash,format=raw,file=$VARS \
    -drive if=none,id=live,file=output/dakota-live.iso,media=cdrom,format=raw,readonly=on \
    -device virtio-scsi-pci,id=scsi -device scsi-cd,drive=live \
    -drive if=none,id=target,file=/var/tmp/dakota-install-disk.qcow2,format=qcow2 \
    -device virtio-blk-pci,drive=target \
    -net nic,model=virtio -net user,hostfwd=tcp::2222-:22 \
    -device usb-ehci -device usb-tablet \
    -display gtk,zoom-to-fit=on \
    -serial file:/var/tmp/dakota-serial.log &

# Wait for live env
until grep -q DAKOTA_LIVE_READY /var/tmp/dakota-serial.log 2>/dev/null; do sleep 5; done

# Write recipe and run fisherman install
sshpass -p live ssh -o StrictHostKeyChecking=no -o PubkeyAuthentication=no \
  -o PreferredAuthentications=password -p 2222 liveuser@localhost '
sudo /usr/local/bin/fisherman - << EOF
{"disk":"/dev/vda","filesystem":"btrfs","image":"containers-storage:ghcr.io/projectbluefin/dakota:latest","composeFsBackend":true,"bootloader":"systemd","hostname":"test","encryption":{"type":"luks-passphrase","passphrase":"testpassphrase"},"flatpaks":[]}
EOF
'
```

## fisherman recipe fields

```json
{
  "disk": "/dev/vda",
  "filesystem": "btrfs",
  "image": "containers-storage:ghcr.io/projectbluefin/dakota:latest",
  "composeFsBackend": true,
  "bootloader": "systemd",
  "hostname": "dakota-luks-test",
  "encryption": {
    "type": "luks-passphrase",
    "passphrase": "testpassphrase"
  },
  "flatpaks": []
}
```

`encryption: null` for unencrypted install.

## LUKS passphrase unlock (luks-unlock.py)

`dakota/src/luks-unlock.py` handles automated passphrase injection. It:
1. Polls QEMU monitor screendumps to detect Plymouth's passphrase prompt
2. Injects keystrokes via `sendkey` commands
3. Monitors serial log for successful boot

Accepts two modes: `qemu` (QEMU monitor socket) and `libvirt` (serial PTY).

## BLS console patch

After install, the justfile patches BLS boot entries to add both serial and VT consoles:
```
console=tty0 console=ttyS0
```
This ensures CI can read the installed system's serial log AND the user gets a
graphical console.

## Interactive QEMU rules

- **Always use `-display gtk,zoom-to-fit=on`** for interactive sessions
- **Always create disks in `/var/tmp/`** — never `/tmp` (16 GB tmpfs)
- **Install disk must be 50 GB+** — create before launching QEMU
- `usb-tablet` device is required for mouse tracking in GTK window

---

## Lessons

### /var/tmp for disk images, never /tmp (2026-05)

`/tmp` is a 16 GB tmpfs on this host. Two 50 GB sparse qcow2 images fill it
instantly, causing the QEMU VM to pause mid-install with an I/O error.
Always use `/var/tmp/` for OVMF VARS files and install disk images.

### setsid required for fisherman over SSH (2026-05)

Running fisherman directly via SSH (`ssh ... sudo fisherman recipe.json`) gets
killed when the SSH session terminates (SIGHUP). Wrap with `setsid`:
```bash
sshpass -p live ssh ... 'setsid bash -c "sudo fisherman recipe.json > /tmp/install.log 2>&1" </dev/null >/dev/null 2>&1 &'
```

### Poll interval: 15s not 120s (2026-05)

Polling fisherman completion at 120s intervals causes 2-minute detection gaps.
Poll at 15s — fisherman takes 5–15 min but the completion check is cheap.
