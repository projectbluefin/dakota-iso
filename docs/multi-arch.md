# Multi-Arch ISO (x86_64 + aarch64)

Design document for building a single ISO that boots on both x86_64 and aarch64
hardware. Tracks issue #36.

## Current state

- **x86_64**: production ISOs built by `build-iso.yml`, boots via OVMF/systemd-boot
- **aarch64**: separate repo ([tuna-os/dakota-x13s](https://github.com/tuna-os/dakota-x13s)),
  targets Lenovo ThinkPad X13s (Qualcomm SC8280XP)
- `build-iso.sh` already detects `BOOTAA64.EFI` vs `BOOTX64.EFI` (lines 57-72)
  and includes both serial consoles in the kernel cmdline (`ttyS0` + `ttyAMA0`)

## Why a single-arch ISO is the correct default

A multi-arch ISO doubles the size (~9 GB) because each architecture needs its own:

| Component | Per-arch? | Reason |
|---|---|---|
| systemd-boot EFI binary | Yes | `BOOTX64.EFI` vs `BOOTAA64.EFI` are different binaries |
| Kernel (`vmlinuz`) | Yes | Different instruction sets |
| Initramfs (`initramfs.img`) | Yes | Contains arch-specific kernel modules |
| squashfs rootfs | Yes | Entire userspace is arch-specific (ELF binaries, libs, modules) |
| Offline OCI store | Yes | OCI images are single-arch |

Only loader config, branding, and Flatpak metadata can be shared.

## Fat ESP approach

UEFI firmware selects the boot binary by its well-known path:
- x86_64: `EFI/BOOT/BOOTX64.EFI`
- aarch64: `EFI/BOOT/BOOTAA64.EFI`

Both can coexist in the same FAT ESP — firmware only loads the one matching its
architecture. This is the standard "fat EFI" pattern used by Fedora Everything,
Ubuntu multi-arch, and Windows ARM install media.

### ESP layout (multi-arch)

```
EFI/
  BOOT/
    BOOTX64.EFI              ← systemd-boot (x86_64)
    BOOTAA64.EFI             ← systemd-boot (aarch64)
  efi.img                    ← FAT image containing all of the above
loader/
  loader.conf
  entries/
    dakota-live-x86_64.conf  ← kernel + initrd + cmdline (x86_64)
    dakota-live-aarch64.conf ← kernel + initrd + cmdline (aarch64)
images/
  pxeboot/
    x86_64/
      vmlinuz
      initrd.img
    aarch64/
      vmlinuz
      initrd.img
LiveOS/
  squashfs-x86_64.img        ← full rootfs (x86_64)
  squashfs-aarch64.img        ← full rootfs (aarch64)
  store.squashfs.img          ← offline OCI store (both arches)
```

### BLS entries

systemd-boot on each arch loads only its own BLS entry because the
kernel/initrd paths are arch-specific. The `machine-id` filter or explicit
`architecture` field in the BLS entry ensures only the matching entry appears.

```ini
# loader/entries/dakota-live-x86_64.conf
title     Dakota Live (x86_64)
linux     /images/pxeboot/x86_64/vmlinuz
initrd    /images/pxeboot/x86_64/initrd.img
options   root=live:CDLABEL=DAKOTA_LIVE rd.live.image rd.live.overlay.overlayfs=1 enforcing=0 quiet console=ttyS0,115200n8
```

```ini
# loader/entries/dakota-live-aarch64.conf
title     Dakota Live (aarch64)
linux     /images/pxeboot/aarch64/vmlinuz
initrd    /images/pxeboot/aarch64/initrd.img
options   root=live:CDLABEL=DAKOTA_LIVE rd.live.image rd.live.overlay.overlayfs=1 enforcing=0 quiet console=ttyAMA0,115200n8
```

### dmsquash-live arch selection

The initramfs `dmsquash-live` module mounts `LiveOS/squashfs.img` by default.
For multi-arch, the initramfs needs to select the correct squashfs. Options:

1. **Kernel cmdline parameter**: `rd.live.squashimg=squashfs-x86_64.img` in each
   BLS entry. dmsquash-live already supports this parameter.
2. **Symlink at build time**: Not viable — ISO9660 doesn't support symlinks in
   the way dmsquash-live expects.
3. **Custom dracut module**: A small module that detects `uname -m` and sets the
   correct `rd.live.squashimg` value. This would allow a single BLS entry.

**Recommendation**: Option 1 — explicit `rd.live.squashimg` per BLS entry.
Simplest, no dracut changes, already supported upstream.

## Implementation plan

### Phase 1: `build-iso.sh` multi-arch support (this PR)

Extend `build-iso.sh` to accept multiple boot-files tars and squashfs images:

```bash
# Current (single-arch):
build-iso.sh <boot-tar> <squashfs> <output-iso>

# Multi-arch:
build-iso.sh --arch x86_64:<boot-tar>:<squashfs> \
             --arch aarch64:<boot-tar>:<squashfs> \
             <output-iso>

# Single-arch (backwards-compatible):
build-iso.sh <boot-tar> <squashfs> <output-iso>
```

When multiple `--arch` flags are provided:
- Both EFI binaries placed in the ESP
- Per-arch kernel/initrd paths under `images/pxeboot/<arch>/`
- Per-arch BLS entries with `rd.live.squashimg=squashfs-<arch>.img`
- Per-arch squashfs images under `LiveOS/`
- ESP image sized to fit all architectures

### Phase 2: Justfile recipes

```bash
# Build single-arch (existing, unchanged):
just iso-sd-boot dakota

# Build multi-arch:
just multi-arch-iso dakota
```

The `multi-arch-iso` recipe:
1. Builds `dakota-installer` container for x86_64 (existing Containerfile)
2. Builds `dakota-installer` container for aarch64 (pulls from `tuna-os/dakota-x13s`)
3. Exports boot-files tar and squashfs for each arch
4. Calls `build-iso.sh --arch x86_64:... --arch aarch64:...`

### Phase 3: CI verification

Add a `test-multi-arch.yml` workflow that:
1. Builds a multi-arch ISO
2. Boots x86_64 via QEMU `q35` + KVM, verifies `DAKOTA_LIVE_READY`
3. Boots aarch64 via `qemu-system-aarch64 -machine virt` (TCG, no KVM on x86 runners),
   verifies `DAKOTA_LIVE_READY`

aarch64 QEMU on x86_64 runners uses TCG emulation — slow (~10 min to GDM) but
sufficient for boot verification. The `virt` machine type is the standard for
aarch64 QEMU.

## Blockers

| Blocker | Status | Notes |
|---|---|---|
| `rd.live.squashimg` support in dmsquash-live | Supported upstream | Verified in dracut source |
| aarch64 Dakota images published to GHCR | Blocked | `tuna-os/dakota-x13s` builds exist but may not be on GHCR |
| Fat ESP with both EFI binaries | Ready | Standard UEFI pattern, no firmware changes needed |
| CI aarch64 QEMU boot | Ready | TCG emulation works on ubuntu-24.04 runners |

## Size estimates

| Component | x86_64 | aarch64 | Combined |
|---|---|---|---|
| squashfs rootfs | ~4.5 GB | ~4.0 GB | ~8.5 GB |
| Kernel + initramfs | ~120 MB | ~100 MB | ~220 MB |
| EFI binary | ~150 KB | ~150 KB | ~300 KB |
| Offline store | ~4.5 GB | ~4.0 GB | ~8.5 GB |
| **Total ISO** | ~4.6 GB | — | **~9.2 GB** |

A multi-arch ISO with both offline stores would be ~17 GB — too large for USB
sticks and downloads. Recommendation: multi-arch ISO includes only the live
rootfs for each arch; offline store remains single-arch or is omitted from the
multi-arch variant.

## Alternative: arch-selector ISO (smaller)

Instead of embedding both full rootfs images, build a minimal ISO that:
1. Boots on either arch (fat ESP)
2. Contains only a network installer (no squashfs)
3. Downloads the correct arch's image at install time

This reduces the ISO to ~500 MB but requires network connectivity. Not viable
for the offline-first use case that Dakota targets.

## Decision

**Recommended approach**: Per-arch ISOs remain the default for downloads.
Multi-arch ISO is a CI/testing artifact for verifying both architectures
from a single build pipeline. The `build-iso.sh` multi-arch support enables
both use cases.
