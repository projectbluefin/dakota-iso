---
name: onboarding
description: "Verified setup commands, build prerequisites, and PR workflow for projectbluefin/dakota-iso. Use when setting up a new development environment or writing contributor documentation."
metadata:
  type: procedure
---

# Onboarding — Dakota ISO

## Prerequisites

| Tool | Notes |
|---|---|
| `podman` | Rootless. `sudo`-prefixed builds will fail with "please use unshare with rootless" |
| `xorriso` | brew: `brew install xorriso` · distro package also works |
| `mtools` | brew: `brew install mtools` · needed for FAT ESP construction |
| `mksquashfs` | brew: `brew install squashfs` |
| Free disk (~25 GB) | Must be on `/var` — **never `/tmp`** (tmpfs ~16 GB, too small) |
| QEMU (optional) | brew: `brew install qemu` — needed for boot testing |

## Clone and first build

```bash
git clone https://github.com/projectbluefin/dakota-iso
cd dakota-iso
just iso-sd-boot dakota            # full ISO build
just boot-iso-serial dakota        # QEMU smoke test (Ctrl-A X to quit)
```

See [`docs/build.md`](../build.md) for full build options, compression presets, and BTRFS workarounds.

## BTRFS hosts

If you're on a BTRFS filesystem (common on Bluefin/Dakota):

```bash
sudo just mount-xfs                # create 45GB XFS loopback at /mnt (idempotent)
sudo chown $USER:$USER /mnt
just workdir=/mnt iso-sd-boot dakota
```

## Branch target

PRs for `dakota-iso` target **`main`**.

```bash
# Push directly upstream (no castrojo fork needed)
git checkout -b fix/my-fix
# ... make changes ...
gh pr create --repo projectbluefin/dakota-iso --base main
```

## Key build variables

| Variable | Default | Override example |
|---|---|---|
| `debug` | `0` | `just debug=1 iso-sd-boot dakota` — enables SSH (liveuser/live) |
| `installer_channel` | `stable` | `just installer_channel=dev iso-sd-boot dakota` |
| `output_dir` | `output` | `just output_dir=/var/data/iso iso-sd-boot dakota` |
| `compression` | `fast` | `just compression=release iso-sd-boot dakota` — for production ISOs |

## Related repos

| Repo | Role |
|---|---|
| [`projectbluefin/dakota`](https://github.com/projectbluefin/dakota) | Source images |
| [`projectbluefin/common`](https://github.com/projectbluefin/common) | Shared OCI layer + org factory docs |
| [`projectbluefin/bootc-installer`](https://github.com/projectbluefin/bootc-installer) | Flatpak installer bundled in the ISO |
| [`tuna-os/fisherman`](https://github.com/tuna-os/fisherman) | Backend install binary |
