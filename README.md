# Dakota Live ISO

[![Build and Publish](https://github.com/projectbluefin/dakota-iso/actions/workflows/build-iso.yml/badge.svg)](https://github.com/projectbluefin/dakota-iso/actions/workflows/build-iso.yml)

| Download | Checksum |
|----------|----------|
| [⬇ dakota-live-latest.iso](https://projectbluefin.dev/dakota-live-latest.iso) | [checksum](https://projectbluefin.dev/dakota-live-latest.iso-CHECKSUM) |

Builds a bootable UEFI live ISO from [Dakota](https://github.com/projectbluefin/dakota) images — GNOME OS-based workstations using composefs and systemd-boot. The live environment boots straight to GDM with a full GNOME session and launches the Dakota installer automatically.

The ISO is unified: it boots the **NVIDIA** variant live (for systems with NVIDIA GPUs) and ships the standard Dakota image in an offline store so it can be installed on non-NVIDIA hardware without a network pull.

## How it works

The build uses three steps:

1. **`live/Containerfile`** — a 3-stage multi-arch build that pulls the Dakota NVIDIA image, creates a live user, configures GDM autologin, installs Flatpaks from Flathub, and drops in the installer config.
2. **`scripts/build-live-squashfs.sh`** — mounts the live container image via `podman image mount` and runs `mksquashfs` directly on the merged overlay view. No per-layer VFS expansion needed.
3. **`scripts/build-offline-store.sh`** — uses `skopeo copy` to import both `dakota` and `dakota-nvidia` images into an isolated VFS store, then creates a second squashfs that ships inside the ISO for offline installation.
4. **`live/src/build-iso.sh`** — assembles the final ISO with the live squashfs, boot files, and offline store embedded at `LiveOS/store.squashfs.img`.

The ISO layout:
- **EFI/efi.img** — FAT32 ESP with systemd-boot, kernel, and initramfs
- **LiveOS/squashfs.img** — squashfs of the full live rootfs (NVIDIA variant)
- **LiveOS/store.squashfs.img** — offline OCI image store (both dakota and dakota-nvidia)
- **El Torito** UEFI entry (no-emulation mode) pointing to the ESP image

At boot, `dmsquash-live` mounts the squashfs and creates an overlayfs so the live environment is fully writable. The offline store is mounted at `/var/lib/containers/storage` so the installer can install Dakota without a network pull.

## Requirements

| Tool | Notes |
|---|---|
| `podman` | Rootless works; needs `--cap-add sys_admin` for the live env build |
| `buildah` | Squash OCI layers before VFS import |
| `skopeo` | Copy images into the offline store |
| `just` | Task runner — `cargo install just` or distro package |
| KVM + `qemu-system-x86_64` | For local boot testing on amd64 only |
| OVMF firmware | `edk2-ovmf` (Fedora/RHEL) or `ovmf` (Debian/Ubuntu) — amd64 |

**Disk space:** The build needs ~22 GB free:
- ~6 GB for the live squashfs (NVIDIA variant)
- ~6 GB for the offline store squashfs (both dakota and dakota-nvidia)
- ~5 GB for the final ISO

By default, output goes to `./output/`. If `/tmp` is a small tmpfs on your machine, override with `just output_dir=/path/with/space iso-sd-boot dakota`.

## Building

```bash
# Clone the repo
git clone https://github.com/projectbluefin/dakota-iso
cd dakota-iso

# Full build — live env container + ISO assembly (single-variant, for local testing)
just iso-sd-boot dakota

# Build the NVIDIA variant (boots live with NVIDIA drivers)
just iso-sd-boot dakota-nvidia

# Override output directory (if ./output/ is on a small filesystem)
just output_dir=/var/data/iso-output iso-sd-boot dakota
```

The build takes **20–40 minutes** depending on your internet connection — the Flatpak install step downloads ~2 GB from Flathub.

Output: `output/<target>-live.iso` (~4.5 GB)

> **CI builds** a unified ISO containing both NVIDIA (live boot) and non-NVIDIA (offline
> install) variants. To replicate the full CI pipeline locally, run the three scripts
> directly:
> ```bash
> sudo bash scripts/build-live-squashfs.sh localhost/dakota-nvidia-live:latest \
>   output/dakota-nvidia.rootfs.sfs output/dakota-nvidia-boot.tar
> sudo bash scripts/build-offline-store.sh output/store.squashfs.img \
>   ghcr.io/projectbluefin/dakota-nvidia:latest ghcr.io/projectbluefin/dakota:latest
> sudo bash live/src/build-iso.sh --store output/store.squashfs.img \
>   output/dakota-nvidia-boot.tar output/dakota-nvidia.rootfs.sfs output/dakota-live.iso
> ```

### Build stages

```
just container dakota          # Build the live environment container
just iso-sd-boot dakota        # Full end-to-end build (runs container + assembles ISO)
```

## Adding a new variant

Each variant is a directory containing a single file — `payload_ref` — with the OCI image reference:

```bash
mkdir my-variant
echo 'ghcr.io/projectbluefin/my-variant:latest' > my-variant/payload_ref
just iso-sd-boot my-variant
```

The Containerfile (`live/Containerfile`) accepts a `TARGET` build-arg (defaulting to `dakota-nvidia`). The justfile reads `<target>/payload_ref` but passes the target name directly as `TARGET`. The installer configs inside the ISO are patched at build time to reference the correct image.

## Testing

### Serial console (headless, CI-friendly)

Boots the ISO in QEMU with serial console output. Watch for `Started gdm.service` to confirm the live environment reached GDM.

```bash
just boot-iso-serial dakota
# Exit: Ctrl-A then X
```

### With a graphical display (VNC)

```bash
qemu-system-x86_64 \
  -m 4096 -accel kvm -cpu host -smp 4 \
  -drive if=pflash,format=raw,readonly=on,file=/usr/share/edk2/ovmf/OVMF_CODE.fd \
  -drive if=pflash,format=raw,file=/tmp/ovmf-vars.fd \
  -cdrom output/dakota-live.iso \
  -vnc 127.0.0.1:0
# Connect your VNC client to localhost:5900
```

### In libvirt / virt-manager

The recommended way to test debug ISOs with SSH access:

```bash
# Build a debug ISO first (enables SSH: user=liveuser, pass=live)
just debug=1 output_dir=output iso-sd-boot dakota

# Launch in libvirt — waits for DHCP lease and prints the SSH command
just boot-libvirt-debug dakota
```

The recipe creates an 8 GiB RAM VM with a 64 GiB install disk on the default libvirt network. Once the guest boots, it prints:

```
========================================
 SSH ready:
   ssh liveuser@192.168.122.x
   password: live
========================================
```

**Cleanup:**
```bash
sudo virsh destroy dakota-debug && sudo virsh undefine dakota-debug --nvram
```

For production ISOs (without SSH), use the manual virt-install approach:

```bash
sudo cp output/dakota-live.iso /var/lib/libvirt/images/dakota-live.iso

sudo virt-install \
  --name dakota-live \
  --memory 4096 --vcpus 4 \
  --boot loader=/usr/share/edk2/ovmf/OVMF_CODE.fd,loader.readonly=yes,loader.type=pflash,nvram.template=/usr/share/edk2/ovmf/OVMF_VARS.fd \
  --cdrom /var/lib/libvirt/images/dakota-live.iso \
  --disk size=50,format=qcow2 \
  --graphics vnc,listen=127.0.0.1 \
  --os-variant generic \
  --tpm none \
  --noautoconsole

virsh domdisplay dakota-live
# Connect to vnc://127.0.0.1:0  (port 5900)
```

## Installer configuration

The installer is pre-configured to install Dakota. Configuration lives in `live/src/etc/bootc-installer/`:

| File | Purpose |
|---|---|
| `images.json` | Locks the image catalog to Dakota — the installer shows only one choice |
| `recipe.json` | Sets distro branding (`distro_name`, `distro_logo`), tour slides, and install steps |

Both files are read by `org.bootcinstaller.Installer` from `/etc/bootc-installer/` at runtime.

### `images.json` — catalog entry

```json
{
  "name": "Dakota",
  "imgref": "ghcr.io/projectbluefin/dakota:latest",
  "bootloader": "systemd",
  "filesystem": "btrfs",
  "composefs": true,
  "needs_user_creation": false,
  "flatpak_var_path": "state/os/default/var"
}
```

Key fields for Dakota:
- `bootloader: "systemd"` — installs systemd-boot, not GRUB
- `composefs: true` — enables composefs backend
- `flatpak_var_path` — where the installer places Flatpak data on the installed system
- `needs_user_creation: false` — GNOME Initial Setup handles user creation on first boot

## Troubleshooting

**ISO fails to boot (UEFI says "no bootable device" or CDROM code 0009)**
The El Torito entry must be in no-emulation mode. This is set by `-no-emul-boot` in the xorriso command in `build-iso.sh`. Do not remove it.

**Flatpak build fails with `O_TMPFILE` error**
This happens when building inside a container on an overlayfs mount. The fix (`export TMPDIR=/dev/shm`) is already in `build.sh` — `/dev/shm` is always a real tmpfs that supports `O_TMPFILE`.

**Build runs out of disk space**
The default `./output/` directory needs ~22 GB free. If `/tmp` or your home directory is on a small filesystem, use a larger path:
```bash
just output_dir=/var/data/iso-output iso-sd-boot dakota
```

**`openh264` warning during Flatpak install**
```
Warning: Failed to install org.freedesktop.Platform.openh264
```
This is harmless — `openh264` requires user namespaces which aren't available inside Podman builds. The ISO functions correctly without it.

