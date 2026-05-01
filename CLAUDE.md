# Dakota ISO – Build Notes for AI Assistants

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

### Boot the ISO locally

```bash
# Quick QEMU serial test (validates GDM starts):
just debug=1 boot-iso-serial dakota

# Full libvirt VM with SSH access:
just debug=1 boot-libvirt-debug dakota
```

## Key architecture notes

- **composefs / VFS**: The ISO embeds Dakota as VFS containers-storage (not overlay)
  because squashfs is read-only. `configure-live.sh` sets `driver = "vfs"` so podman
  uses the pre-embedded image at boot.
- **tar-split format**: Build-host containers/storage writes binary tar-split;
  the installer container needs JSON format. This is why `skopeo copy` runs INSIDE
  the installer container (not on the host). This requirement survives the squash.
- **Scratch dir**: `fisherman` detects tmpfs `/var` on live ISOs and uses a
  self-bind-mounted scratch dir on the target disk to avoid ENOSPC during OCI export.
- **Installer channel**: `dev` uses `org.bootcinstaller.Installer.Devel` app ID;
  `stable` uses `org.bootcinstaller.Installer`. Both can coexist.
- **live-ready.service**: Writes `DAKOTA_LIVE_READY` to serial console after GDM
  starts. CI boot verification polls for this marker AND SSH connectivity
  (SSH fallback handles cases where serial output is missing). The service uses
  `WantedBy=multi-user.target` (standard) with `After=display-manager.service`
  (ordering only) — NOT `WantedBy=display-manager.service` which is non-standard
  and causes silent failures on some installer channels.

## Variants

The repo supports multiple ISO variants. Each variant is a directory with one file:
`<variant>/payload_ref` — the OCI image reference to embed.

| Variant | OCI image | ISO file |
|---|---|---|
| `dakota` | `ghcr.io/projectbluefin/dakota:latest` | `dakota-live.iso` |
| `dakota-nvidia` | `ghcr.io/projectbluefin/dakota-nvidia:latest` | `dakota-nvidia-live.iso` |

All variants share the same `dakota/Containerfile`, `dakota/src/`, and
`dakota/Containerfile.builder`. The `BASE_IMAGE` build-arg is set automatically
from `<variant>/payload_ref`.

To add a new variant, create `<variant>/payload_ref` with the OCI image reference
and add `<variant>` to the matrix in `.github/workflows/build-iso.yml`.
