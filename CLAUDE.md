# Dakota ISO – Build Notes for AI Assistants

## Repository identity

- **Upstream repo:** `git@github.com:projectbluefin/dakota-iso.git`
- **Purpose:** Builds bootable UEFI live ISOs from Dakota/GNOME OS bootc images
- **Variants:** `dakota`, `dakota-nvidia`, `bluefin`, `bluefin-lts-hwe`, `stable`, `lts` (each has a `<variant>/payload_ref` file)

> ⚠️ This repo's remote is `projectbluefin/dakota-iso` (upstream). Pushes go to upstream.
> If working from a castrojo fork, push to `castrojo/dakota-iso` only.

---

## Quick reference

```bash
just iso-sd-boot dakota                              # full build (default)
just debug=1 installer_channel=dev iso-sd-boot dakota   # debug build with SSH
just build-bg dakota                                 # background build (survives terminal close)
just boot-iso-serial dakota                          # boot + validate via QEMU serial
just e2e dakota                                      # build ISO + LUKS end-to-end test
```

---

## Local build setup

### Background builds — use `just build-bg`

Running the build as a plain background job (`&`) will get killed by SIGHUP
when the shell session ends. Use the dedicated recipe instead:

```bash
just installer_channel=dev build-bg dakota
# Ctrl-C stops the log tail — build keeps running in background
# Check progress any time: tail -f output/build.log
```

The recipe uses `setsid ... & disown` internally so the build survives
terminal closure.

### ⚠️ Never build from /tmp

`/tmp` is a tmpfs with only ~16 GB. The build needs ~22 GB of intermediate space.

**Always work from a path on `/var`** (or another filesystem with at least 25 GB free).
The default `output_dir=output` resolves to `./output/` relative to the justfile,
so it inherits whatever filesystem the repo is on.

### Build command (local, no sudo)

```bash
cd ~/src/dakota-iso
just debug=1 installer_channel=dev iso-sd-boot dakota
```

- **No `sudo`** — `podman unshare` only works for rootless podman (non-root user).
  Prefixing with `sudo` will fail with `please use unshare with rootless`.
- `debug=1` enables SSH (`ssh liveuser@<IP>`, password `live`) and the debug banner.
- `installer_channel=dev` uses the `continuous-dev` Flatpak release of tuna-installer
  which includes fixes not yet in the stable channel.

### CI build (GitHub Actions)

CI uses `sudo just installer_channel=dev output_dir=/var/iso-build iso-sd-boot dakota`
(runs as root). The justfile detects root via `id -u` and skips `podman unshare`,
running commands directly instead — so the same justfile works for both cases.

### Disk space (CI and local)

Dakota images are chunkified with many OCI layers (~120). Without squashing, VFS
storage imports ALL layers as full directories — ~6 GB × 120 layers = ~720 GB,
which overflows any standard CI runner.

**The justfile squashes to 1 layer BEFORE the VFS import.** This reduces peak
disk usage to ~22 GB:
- Squashed OCI image: ~4 GB
- VFS import (1 layer): ~6 GB
- squashfs tree: ~6 GB
- Final ISO: ~4.5 GB

The squash uses `buildah from --pull-never` + `buildah commit --squash` (not
`podman create --entrypoint ... && podman commit`) because `podman create --entrypoint`
modifies the container's recorded Entrypoint, and `podman commit` captures that
modified config. Bootc images have no Entrypoint by design; a fake `/bin/sh`
entrypoint causes `bootc install` to fail with "cannot execute binary file".

The disk check at the start of `iso-sd-boot` targets `${OUTPUT_DIR}` (not `/`)
because composefs/ostree hosts report 0 bytes free on the read-only `/` mount.

### XFS loopback (BTRFS hosts only)

BTRFS handles chunkified layers poorly. Even with squashing, VFS import creates
many intermediate directories that BTRFS manages slowly. Use the XFS loopback:

```bash
sudo just mount-xfs                               # creates 45GB XFS at /mnt (idempotent)
sudo chown jorge:jorge /mnt                       # make it accessible rootless
just workdir=/mnt iso-sd-boot dakota              # build with workdir on XFS
```

CI does not need this (squash-to-1-layer reduces the problem enough on ext4).

### Compression presets

```bash
just compression=fast    iso-sd-boot dakota   # default: zstd level 3, 128K blocks (fast)
just compression=release iso-sd-boot dakota   # zstd level 15, 1M blocks (~20% smaller, ~5× slower)
```

Use `release` for production ISOs published to R2. Use `fast` for local testing and CI.

### Boot the ISO locally

```bash
# Quick QEMU serial test (validates GDM starts, Ctrl-A X to quit):
just debug=1 boot-iso-serial dakota

# Full libvirt VM with SSH access:
just debug=1 boot-libvirt-debug dakota
# SSH: liveuser@<IP>  password: live
# Cleanup: sudo virsh destroy dakota-debug && sudo virsh undefine dakota-debug --nvram
```

---

## Architecture

### Two-container build pipeline

1. **`<target>-installer`** — built by `Containerfile` (3 stages):
   - Stage 1 `dakota-ref`: original Dakota image (provides kernel modules)
   - Stage 2 `initramfs-builder`: Debian; builds dmsquash-live initramfs against
     Dakota's kernel modules (no cross-distro binary grafting — only the `.img` crosses stages)
   - Stage 3 (final): Dakota + rebuilt initramfs + Flatpaks + live-env config
2. **`<target>-iso-builder`** — built by `Containerfile.builder` (Debian with xorriso,
   mksquashfs, dosfstools, mtools). Now mostly unused — `build-iso.sh` runs on the host.

### ISO layout

```
EFI/efi.img              — FAT32 ESP: systemd-boot + kernel + initramfs
EFI/BOOT/BOOTX64.EFI    — EFI fallback path (Proxmox OVMF / Ventoy)
LiveOS/squashfs.img      — squashfs of the full live rootfs (+ embedded OCI)
boot/grub/loopback.cfg   — Ventoy/GRUB loopback metadata
images/pxeboot/*         — kernel/initramfs copies for loopback ISO boot
```

**No GRUB2, no shim.** El Torito UEFI → FAT ESP → systemd-boot → kernel+initramfs.

### Live boot flow

```
UEFI → El Torito → FAT ESP → systemd-boot → kernel (initramfs: dmsquash-live)
dmsquash-live: scans for CDLABEL=DAKOTA_LIVE → mounts ISO → squashfs → overlayfs
```

### VFS containers-storage (embedded OCI)

The squashfs embeds the Dakota OCI image as VFS containers-storage. The ISO can
install offline without a network pull. This requires:
- `driver = "vfs"` in `/etc/containers/storage.conf` (set by `configure-live.sh`)
- skopeo copy runs **inside the installer container** (not the build host) to ensure
  tar-split metadata is written in the JSON format the live ISO expects.
  (Build-host containers/storage emits binary tar-split; installer image uses JSON.)
- `fisherman` scratch dir: on live ISOs `/var` is a small RAM overlay. fisherman
  detects tmpfs `/var` and uses a self-bind-mounted scratch dir on the target disk.

### Installer

- **Flatpak:** tuna-installer (`org.bootcinstaller.Installer` stable,
  `org.bootcinstaller.Installer.Devel` dev channel)
- **Backend binary:** `fisherman` — symlinked to `/usr/local/bin/fisherman` by `configure-live.sh`
- **Config path:** `/etc/bootc-installer/images.json` (catalog lock) + `recipe.json` (branding)
- **Flatpak sandbox trick:** Inside the Flatpak, `/etc` is reserved. The host `/etc` is at
  `/run/host/etc`. Recipe is passed via `BOOTC_CUSTOM_RECIPE=/run/host/etc/bootc-installer/recipe.json`.
- **live-iso-mode:** `touch /etc/bootc-installer/live-iso-mode` activates live ISO mode
  in the installer (see tuna-os/tuna-installer#26).

### live-ready.service

Writes `DAKOTA_LIVE_READY` to the serial console after display-manager starts.
CI boot verification greps for this token.

```ini
StandardOutput=tty
TTYPath=/dev/ttyS0          # direct serial, NOT journal+console (which goes to /dev/console)
WantedBy=multi-user.target  # NOT display-manager.service (non-standard → silent failures)
After=display-manager.service   # ordering only
```

CI also accepts SSH connectivity as a fallback (some dev channel builds don't write
the serial marker but SSH still works).

---

## Justfile recipes

| Recipe | Description |
|---|---|
| `just mount-xfs` | Create 45GB XFS loopback at /mnt (sudo, idempotent) |
| `just build-bg <target>` | Background build; logs to `output/build.log` |
| `just container <target>` | Build the live-env container only |
| `just iso-builder <target>` | Build the Debian ISO toolchain container |
| `just iso-sd-boot <target>` | **Full ISO build** (container + ISO assembly) |
| `just boot-iso-serial <target>` | Boot ISO in QEMU via serial (Ctrl-A X to quit) |
| `just boot-libvirt-debug <target>` | Boot in libvirt, waits for DHCP + SSH |
| `just luks-install <target>` | SSH fisherman LUKS install into dakota-debug libvirt VM |
| `just luks-unlock <target>` | Send LUKS passphrase via serial PTY |
| `just luks-boot <target>` | Connect to serial console post-install |
| `just luks-test-qemu <target>` | Full QEMU LUKS E2E (CI entry point, no libvirt) |
| `just luks-boot-qemu-live <target>` | Boot live ISO in QEMU (daemonized) |
| `just luks-install-qemu <target>` | SSH fisherman install into QEMU live |
| `just luks-boot-qemu-installed <target>` | Boot installed disk in QEMU |
| `just luks-unlock-qemu <target>` | Send passphrase via QEMU monitor socket |
| `just e2e <target>` | Build ISO + full LUKS E2E test |
| `just chunkify <src> <dst>` | Rechunkify OCI image with chunkah, push to dst |

### Key variables (all override-able on CLI)

| Variable | Default | Description |
|---|---|---|
| `debug` | `0` | `1` = SSH enabled (liveuser/live, root/root) |
| `installer_channel` | `stable` | `dev` = continuous-dev Flatpak |
| `output_dir` | `output` | ISO + intermediate artifacts output dir |
| `workdir` | `output_dir` | squashfs staging dir (override to XFS on BTRFS hosts) |
| `compression` | `fast` | `fast` or `release` |
| `luks-passphrase` | `testpassphrase` | LUKS passphrase for luks-install recipes |

---

## Variants

Each variant is a directory with a `payload_ref` file and optional metadata:

| Variant | `payload_ref` | Live target | ISO output |
|---|---|---|---|
| `dakota` | `ghcr.io/projectbluefin/dakota-nvidia:stable` | `dakota-nvidia` | `dakota-live.iso` |
| `dakota-nvidia` | `ghcr.io/projectbluefin/dakota-nvidia:latest` | `dakota-nvidia` | `dakota-nvidia-live.iso` |
| `bluefin` | `ghcr.io/projectbluefin/bluefin-nvidia:stable` | `bluefin-nvidia` | `bluefin-live.iso` |
| `bluefin-lts-hwe` | `ghcr.io/projectbluefin/bluefin-lts-hwe-nvidia:stable` | `bluefin-lts-hwe-nvidia` | `bluefin-lts-hwe-live.iso` |
| `stable` | `ghcr.io/projectbluefin/bluefin-nvidia:stable` | `bluefin-nvidia` | `stable-live.iso` |
| `lts` | `ghcr.io/projectbluefin/bluefin-lts-hwe-nvidia:stable` | `bluefin-lts-hwe-nvidia` | `lts-live.iso` |

All variants share `live/Containerfile` and `live/src/`. The `TARGET` build-arg is set
from `<variant>/live_target`. Per-variant config in `live/src/<variant>/` controls
images.json, composefs, and bootloader settings.

To add a new variant:
```bash
mkdir my-variant
echo 'ghcr.io/projectbluefin/my-variant:latest' > my-variant/payload_ref
echo 'my-variant-nvidia' > my-variant/live_target
echo 'My Variant Live' > my-variant/live_title
echo 'projectbluefin' > my-variant/registry
echo 'stable' > my-variant/tag
just iso-sd-boot my-variant
```

---

## CI workflows

### `build-iso.yml`

- **Trigger:** push to main (paths: `dakota/**`, `dakota-nvidia/**`, `justfile`, workflow),
  daily schedule at 03:00 UTC, `workflow_dispatch`
- **Matrix:** `[dakota, dakota-nvidia]` (fail-fast: false)
- **Runner:** `ubuntu-24.04`
- **Runs as:** root via `sudo just`
- **Build path:** `/var/iso-build` (~119 GB free after free-disk-space action)
- **Uploads:** ISOs to Cloudflare R2 (`testing` bucket) as `<target>-live-latest.iso` + CHECKSUM;
  also as dated `<target>-live-YYYYMMDD-<sha>.iso` — **no expiry, full history back to April 10**.
  GitHub artifacts have 7-day retention but R2 dated ISOs are permanent.
- **Smoke test:** boots the built ISO in QEMU, waits for `DAKOTA_LIVE_READY` on serial

### `test-luks-install.yml`

- **Trigger:** PRs to main, weekly Monday 04:00 UTC, `workflow_dispatch`
- **Matrix:** `installer_channel: [dev, stable]` (fail-fast: false)
- **Timeout:** 90 minutes
- **Purpose:** Reproduces projectbluefin/dakota#270 — LUKS encrypted install
- **Flow:** build debug ISO → boot live in QEMU → SSH fisherman LUKS install →
  reboot into installed disk → unlock via QEMU monitor → verify boot
- **Screenshots:** saved to `ci-screenshots` branch, posted to PR comments

---

## LUKS testing

Reproduces [projectbluefin/dakota#270](https://github.com/projectbluefin/dakota/issues/270).

### Libvirt (interactive):

```bash
# 1. Build debug ISO
just debug=1 installer_channel=dev iso-sd-boot dakota

# 2. Boot in libvirt, wait for SSH ready output
just debug=1 boot-libvirt-debug dakota

# 3. SSH fisherman LUKS install + reboot
just luks-install dakota

# 4. Watch boot (send passphrase at prompt OR use luks-unlock)
just luks-unlock dakota     # automated via luks-unlock.py
# or interactively:
just luks-boot dakota       # serial console (Ctrl-])  type: testpassphrase
```

### QEMU (automated, CI-equivalent):

```bash
just debug=1 installer_channel=dev iso-sd-boot dakota
just luks-test-qemu dakota
# or all-in-one:
just debug=1 installer_channel=dev e2e dakota
```

---

## Bundled Flatpaks

Pre-installed into the live squashfs at build time (list in `dakota/src/flatpaks`).
The `install-flatpaks.sh` script uses a build cache (`--mount=type=cache`) to avoid
re-downloading on rebuilds. Apps include Firefox, Thunderbird, GNOME core apps
(Calculator, Calendar, Files, Maps, etc.), and developer utilities (Warehouse,
Flatseal, Extension Manager, Mission Center, etc.).

---

## Debug builds

`debug=1` activates additional live-env setup in `configure-live.sh`:

- `liveuser` password: `live` (also SSH-accessible)
- `root` password: `root`
- `sshd.service` force-enabled via systemd preset (overrides Dakota's disabled preset)
- Firewalld zone opens port 22
- `debug-ssh-banner.service`: prints SSH IP/password to serial console when network is up

Never use `debug=1` for production/release ISOs.

---

## Verifying GPT layout

To inspect the partition table of a built ISO without installing any tools on the host,
run xorriso inside the Debian container:

```bash
podman run --rm \
    -v ./output:/iso:ro \
    debian:sid \
    bash -c "
        apt-get update -qq >/dev/null
        apt-get install -y -qq xorriso >/dev/null 2>&1
        xorriso -indev /iso/dakota-live.iso -report_system_area plain 2>/dev/null
    "
```

**Expected output (correct):**
```
System area summary: MBR cyl-align-off GPT
GPT type GUID      :   1  28732ac11ff8d211ba4b00a0c93ec93b
>>> GPT EFI System Partition type: OK
```

**What `28732ac11ff8d211ba4b00a0c93ec93b` means:** This is the little-endian encoding of
`C12A7328-F81F-11D2-BA4B-00A0C93EC93B` — the EFI System Partition GUID. UEFI firmware
scanning GPT on a dd'd USB will find and boot from this partition.

**If GPT type shows `a2a0d0eb...`:** The old code (`partition_entry=gpt_basdat`) is still
active. That's the Basic Data GUID — not recognized by strict UEFI firmware as EFI System
Partition. Rebuild with the current `build-iso.sh`.

**Why `fdisk -l` shows `Disklabel type: dos`:** This is expected and does NOT mean GPT
is missing. `fdisk` defaults to the MBR view when a hybrid MBR+GPT layout is detected.
`gdisk`, `parted`, and UEFI firmware all correctly see the GPT EFI partition. This is
standard behavior for live ISO hybrid partition layouts.

**xorriso warning during build (harmless):**
```
libisofs: WARNING : Prevented partition type 0xEE in MBR without GPT
```
This is an internal libisofs state message during hybrid layout assembly. The final ISO
has a correct hybrid layout (MBR type 0xcd + GPT EFI System Partition). Ignore it.

**xorriso installed via brew** (as of 2026-05-12): `which xorriso` → `/home/linuxbrew/.linuxbrew/bin/xorriso` (v1.5.8.pl01). `mtools` also installed via brew.
**buildah NOT available on the host** (immutable OS, can't `sudo dnf install` non-interactively). Working solution: buildah wrapper at `~/.local/bin/buildah` using a containerized buildah:
```bash
# One-time setup:
cat > /tmp/Containerfile.buildah << 'EOF'
FROM registry.fedoraproject.org/fedora-toolbox:42
RUN dnf install -y buildah && dnf clean all
EOF
podman build -t localhost/buildah-tool:latest -f /tmp/Containerfile.buildah /tmp/
cat > ~/.local/bin/buildah << 'WRAPPER'
#!/bin/bash
exec podman run --rm --privileged --net=host --security-opt label=disable \
    -v "$HOME/.local/share/containers:/var/lib/containers:rw" \
    -v "/tmp:/tmp:rw" -v "/var/tmp:/var/tmp:rw" \
    ${PWD:+-v "$PWD:$PWD:rw"} \
    localhost/buildah-tool:latest buildah "$@"
WRAPPER
chmod +x ~/.local/bin/buildah
```
**mksquashfs installed via brew** (as of 2026-05-13): `brew install squashfs`. Binary at `/home/linuxbrew/.linuxbrew/bin/mksquashfs`.
**QEMU available via linuxbrew**: `/home/linuxbrew/.linuxbrew/bin/qemu-system-x86_64` (v11.0.0). OVMF code at `/home/linuxbrew/.linuxbrew/Cellar/qemu/11.0.0/share/qemu/edk2-x86_64-code.fd`. No `edk2-x86_64-vars.fd` in brew — create a zeroed 256KB VARS file: `dd if=/dev/zero bs=1k count=256 of=/var/tmp/ovmf-vars.fd`. QEMU paths are added to the justfile `boot-iso-serial` / `luks-*` recipes.

**⛔ Interactive QEMU testing rules (agent-enforced):**
- **Always use `-display gtk,zoom-to-fit=on`** for interactive sessions — never `-display none` unless running headless CI.
- **Always create a 50GB install disk** before launching QEMU — do this without being asked.
- **Pass the ISO path directly** — never create symlinks in `output/` to satisfy the Justfile. Just run QEMU.
- ISOs are typically in `~/Downloads/` (e.g. `dakota-live-latest.iso`), not `output/`.
- **Always use `/var/tmp/` for disk images and OVMF VARS** — never `/tmp`. `/tmp` is a 16GB tmpfs; two 50GB sparse images fill it instantly, pausing the VM.

**Interactive boot + install test (standard command):**
```bash
QEMU=/home/linuxbrew/.linuxbrew/bin/qemu-system-x86_64
OVMF=/home/linuxbrew/.linuxbrew/Cellar/qemu/11.0.0/share/qemu/edk2-x86_64-code.fd
VARS=/var/tmp/OVMF_VARS.fd; dd if=/dev/zero bs=1k count=256 of=$VARS 2>/dev/null
qemu-img create -f raw /var/tmp/dakota-install-disk.img 50G
VARS=/var/tmp/OVMF_VARS.fd; dd if=/dev/zero bs=1k count=256 of=$VARS
$QEMU -machine q35 -m 4096 -accel kvm -cpu host -smp 4 \
    -drive if=pflash,format=raw,readonly=on,file=$OVMF \
    -drive if=pflash,format=raw,file=$VARS \
    -drive if=none,id=live-disk,file=~/Downloads/dakota-live-latest.iso,media=cdrom,format=raw,readonly=on \
    -device virtio-scsi-pci,id=scsi -device scsi-cd,drive=live-disk \
    -drive if=none,id=target,file=/var/tmp/dakota-install-disk.img,format=raw \
    -device virtio-blk-pci,drive=target \
    -net nic,model=virtio -net user,hostfwd=tcp::2222-:22 \
    -device usb-ehci -device usb-tablet \
    -display gtk,zoom-to-fit=on \
    -serial file:/var/tmp/dakota-serial.log &
# Inside VM: ISO boots as cdrom, install target = /dev/vda
# Watch for ready: tail -f /tmp/dakota-serial.log | grep DAKOTA_LIVE_READY
# usb-tablet = absolute pointing device, required for mouse to work in GTK window
```

**buildah workaround for partial ISO build testing** (extracts real kernel + EFI from already-built container):
```bash
podman unshare bash -c "
    MOUNT=\$(podman image mount localhost/dakota-installer)
    tar -C \$MOUNT -cf output/real-boot-files.tar ./usr/lib/modules ./usr/lib/systemd/boot/efi
    podman image unmount localhost/dakota-installer
"
# Then test build-iso.sh with real boot files + placeholder squashfs:
dd if=/dev/zero bs=1M count=1 > output/test.sfs
bash dakota/src/build-iso.sh output/real-boot-files.tar output/test.sfs output/test.iso
```
This proves the GPT layout is correct end-to-end without a full build.

**Inspect remote ISO GPT without downloading (4KB range request):**
```bash
curl --range 0-2047 https://projectbluefin.dev/dakota-live-latest.iso -o /var/tmp/head.bin
fdisk -l /var/tmp/head.bin   # gpt=correct, dos=broken
# Check MBR type byte: 0xEE=protective(good), 0x00=hybrid(bad)
printf "MBR type: 0x%02x\n" "$(od -An -tx1 -j450 -N1 /var/tmp/head.bin | tr -d ' ')"
```

**Test `build-iso.sh` in the Debian container** (if brew tools aren't available):
```bash
podman run --rm -v /var/tmp/test:/work \
    -v ./dakota/src/build-iso.sh:/build-iso.sh:ro \
    debian:sid \
    bash -c "apt-get update -qq >/dev/null && \
        apt-get install -y -qq xorriso mtools dosfstools isomd5sum >/dev/null 2>&1 && \
        bash /build-iso.sh /work/boot-files.tar /work/squashfs.img /work/out.iso"
```

---

## R2 bucket management

**Bucket:** `testing` · **Account ID:** `2a4147f637f7d9e6a67ca185357d3b0a`
**Endpoint:** `https://2a4147f637f7d9e6a67ca185357d3b0a.r2.cloudflarestorage.com`
**Full ISO history on R2 back to April 10 — no expiry.**

### rclone config (`~/.config/rclone/rclone.conf`)

```ini
[R2]
type = s3
provider = Cloudflare
region = auto
access_key_id = abfd2b00ed95ee9b17b7c35a68b0f959
secret_access_key = 8ab5b927c2bd2508cf3518fafaa458ba3176754f317291087dc3ab920d86490a
endpoint = https://2a4147f637f7d9e6a67ca185357d3b0a.r2.cloudflarestorage.com
acl = private
no_check_bucket = true
```

⚠️ `no_check_bucket = true` is **required** — without it, CopyObject hangs on large files.
`acl = private` is required per Cloudflare docs for Object-level permission tokens.

```bash
# List bucket contents
rclone ls R2:testing | grep dakota | sort -k2

# Server-side copy — takes 2-5 min for 4-5GB files, that is normal, do not assume failure
rclone copyto -v R2:testing/dakota-live-20260508-3059a71.iso R2:testing/dakota-live-alpha2.iso

# Promote a dated ISO to latest
rclone copyto -v R2:testing/dakota-live-YYYYMMDD-<sha>.iso R2:testing/dakota-live-latest.iso
rclone copyto -v R2:testing/dakota-live-YYYYMMDD-<sha>.iso-CHECKSUM R2:testing/dakota-live-latest.iso-CHECKSUM
```

⚠️ **Direct uploads from this host hang/fail** (routing issue). Always use R2→R2 server-side copies.

### Cloudflare CLI (`cf`)

Installed via `npm install -g cf` (v0.0.5). R2 not yet supported in this version.
Use `rclone` for all R2 operations.

### Named ISOs on R2

| Name | Source ISO | Notes |
|---|---|---|
| `dakota-live-alpha2.iso` | `20260508-3059a71` | Last build with fisherman v0.1.0 before v0.2.0 regression |
| `dakota-nvidia-live-alpha2.iso` | `20260508-3059a71` | nvidia variant, same build |

---

## Troubleshooting

**ISO doesn't boot on bare-metal USB (boots fine in VM)**
The GPT partition type may be wrong. Verify:
```bash
xorriso -indev output/dakota-live.iso -report_system_area plain 2>/dev/null | grep 'GPT type GUID'
# MUST show: 28732ac1...  (EFI System Partition)
# BAD:        a2a0d0eb...  (Basic Data — won't boot on strict UEFI GPT-scanning firmware)
```
If wrong type: rebuild with current `build-iso.sh`. The old code used
`-boot_image isolinux partition_entry=gpt_basdat` which produces Basic Data type.
The fix uses `-append_partition 2 C12A7328-F81F-11D2-BA4B-00A0C93EC93B`.

**ISO fails to boot ("no bootable device" / CDROM code 0009)**
El Torito entry must be in no-emulation mode (`-no-emul-boot` in xorriso). Do not remove it.

**Flatpak build fails with `O_TMPFILE` error**
Happens when building inside a container on overlayfs. Fix (`TMPDIR=/dev/shm`) is already
in `install-flatpaks.sh`.

**Build runs out of disk space**
Default `./output/` needs ~22 GB. Override: `just output_dir=/var/data/iso-output iso-sd-boot dakota`

**BTRFS host + slow VFS import (even after squash)**
Use XFS loopback: `sudo just mount-xfs` then `just workdir=/mnt iso-sd-boot dakota`

**`openh264` warning during Flatpak install**
```
Warning: Failed to install org.freedesktop.Platform.openh264
```
Harmless — openh264 requires user namespaces not available inside Podman builds.

**`DAKOTA_LIVE_READY` not seen in serial log (CI or local)**
Some dev channel builds don't write the marker to ttyS0. CI falls back to SSH
connectivity check. If both fail after 5 minutes, check `tail -50 /var/tmp/serial.log`.

**VFS containers-storage not found at boot**
Ensure `driver = "vfs"` is set in `/etc/containers/storage.conf`. Overlay driver
creates a conflicting `db.sql` at first boot.

**fisherman fails with "cannot execute binary file"**
Caused by a corrupted Entrypoint in the squashed image. Use `buildah commit --squash`
not `podman create --entrypoint /bin/sh && podman commit`.

**Install fails: `open /var/tmp/oci-cache/index.json: no such file or directory`**
This is a fisherman dev channel regression in the overlay storage code path (introduced
after the continuous-dev release ~2026-05). When composefs+btrfs is used, fisherman exports
the OCI to scratch, but bootc inside the container cannot see it via the bind mount.
Fix: use `installer_channel=stable` (the default). Do NOT use `installer_channel=dev` in
CI or production builds until tuna-os/fisherman#38 is resolved.
The `build-iso.yml` CI workflow must stay on `installer_channel=stable`.

**Host sudo unavailable in automated/pi sessions**
`sudo -v` in the user's terminal does NOT carry over to pi bash sessions (different TTY).
Never rely on host sudo in automated workflows. The justfile's rootless path works without
sudo. The QEMU VM's `liveuser` has NOPASSWD sudo — use that for install tests inside the VM.

**Local QEMU install test (end-to-end)**
```bash
# 1. Build ISO (no sudo needed)
just iso-sd-boot dakota

# 2. Create target disk
qemu-img create -f raw /var/tmp/dakota-install-disk.img 50G

# 3. Boot with ISO + disk (use FULL paths, not ~/)
QEMU=/home/linuxbrew/.linuxbrew/bin/qemu-system-x86_64
OVMF=/home/linuxbrew/.linuxbrew/Cellar/qemu/11.0.0/share/qemu/edk2-x86_64-code.fd
VARS=/var/tmp/OVMF_VARS.fd; dd if=/dev/zero bs=1k count=256 of=$VARS
$QEMU -machine q35 -m 4096 -accel kvm -cpu host -smp 4 \
  -drive if=pflash,format=raw,readonly=on,file=$OVMF \
  -drive if=pflash,format=raw,file=$VARS \
  -drive if=none,id=live,file=/var/home/jorge/src/dakota-iso/output/dakota-live.iso,media=cdrom,format=raw,readonly=on \
  -device virtio-scsi-pci,id=scsi -device scsi-cd,drive=live \
  -drive if=none,id=target,file=/var/tmp/dakota-install-disk.img,format=raw \
  -device virtio-blk-pci,drive=target \
  -net nic,model=virtio -net user,hostfwd=tcp::2222-:22 \
  -serial file:/var/tmp/boot-serial.log \
  -device usb-ehci -device usb-tablet \
  -display gtk,zoom-to-fit=on &
# Note: pass the real ISO path directly — do NOT create a symlink in output/
# usb-tablet required for mouse to work in GTK window

# 4. Wait for live env
until grep -q DAKOTA_LIVE_READY /var/tmp/boot-serial.log 2>/dev/null; do sleep 5; done
ssh-keygen -R '[localhost]:2222' 2>/dev/null

# 5. Write recipe and run install (liveuser has NOPASSWD sudo)
sshpass -p 'live' ssh -o StrictHostKeyChecking=no -o PubkeyAuthentication=no \
  -o PreferredAuthentications=password -p 2222 liveuser@localhost \
  'cat > /home/liveuser/recipe.json << EOF
{"disk":"/dev/vda","filesystem":"btrfs","image":"containers-storage:ghcr.io/projectbluefin/dakota:latest","composeFsBackend":true,"bootloader":"systemd","hostname":"test","encryption":null,"flatpaks":[]}
EOF
setsid bash -c "sudo /usr/local/bin/fisherman /home/liveuser/recipe.json > /home/liveuser/install.log 2>&1; echo EXIT:\$? >> /home/liveuser/install.log" </dev/null >/dev/null 2>&1 &'

# 6. Poll for completion (every 15s, not 120s)
while ! sshpass -p live ssh -o StrictHostKeyChecking=no -o PubkeyAuthentication=no \
  -o PreferredAuthentications=password -p 2222 liveuser@localhost \
  'grep -q EXIT: /home/liveuser/install.log 2>/dev/null' 2>/dev/null; do
  sleep 15; echo -n "."
done
sshpass -p live ssh -o StrictHostKeyChecking=no -o PubkeyAuthentication=no \
  -o PreferredAuthentications=password -p 2222 liveuser@localhost \
  'tail -5 /home/liveuser/install.log'
```
