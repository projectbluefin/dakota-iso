# Build System

How to build Dakota live ISOs locally and the key variables that control the build.

## Quick start

```bash
just iso-sd-boot dakota               # full build, stable installer
just iso-sd-boot dakota-nvidia        # NVIDIA variant
just debug=1 installer_channel=dev iso-sd-boot dakota  # debug + dev installer
just build-bg dakota                  # background build (survives terminal close)
```

Output: `output/<target>-live.iso` (~4.5 GB, ~20–40 min depending on network)

## Key variables

| Variable | Default | Override example |
|---|---|---|
| `debug` | `0` | `debug=1` → SSH enabled (`liveuser`/`live`, `root`/`root`) |
| `installer_channel` | `stable` | `installer_channel=dev` → continuous-dev Flatpak |
| `output_dir` | `output` | `output_dir=/var/data/iso` |
| `workdir` | `output_dir` | `workdir=/mnt` → use XFS loopback on BTRFS hosts |
| `compression` | `fast` | `compression=release` → ~20% smaller, ~5× slower |

Never use `debug=1` for production/release ISOs.
Never use `installer_channel=dev` in production builds — see known regression in `ci.md`.

## Disk space requirements

The build needs ~22 GB free in `output_dir`:
- Squashed OCI image: ~4 GB
- VFS import (1 layer): ~6 GB
- squashfs staging tree: ~6 GB
- Final ISO: ~4.5 GB

⚠️ Never build from `/tmp` — it is a 16 GB tmpfs. Always use a path on `/var` or another
large filesystem.

## Rootless builds (no sudo)

The justfile uses `podman unshare` which requires rootless podman (non-root user).
Never prefix `just` with `sudo` locally — this breaks rootless podman with
`please use unshare with rootless`.

CI runs as root (`sudo just ...`) — the justfile detects root via `id -u` and skips
`podman unshare` automatically.

## BTRFS hosts — use the XFS loopback

BTRFS handles chunkified layers slowly even after squashing. Use the XFS loopback:

```bash
sudo just mount-xfs                  # creates 45 GB XFS at /mnt (idempotent)
sudo chown jorge:jorge /mnt          # make accessible rootless
just workdir=/mnt iso-sd-boot dakota
```

## Background builds

```bash
just installer_channel=dev build-bg dakota
# Ctrl-C stops the log tail — build continues running
# Check progress: tail -f output/build.log
```

Uses `setsid ... & disown` internally so the build survives terminal closure.

## Compression presets

```bash
just compression=fast    iso-sd-boot dakota   # default — fast CI/local
just compression=release iso-sd-boot dakota   # production ISOs for R2
```

Use `fast` for CI and local testing. Use `release` for ISOs that go to R2.

## Why squashing matters for VFS import

Dakota images are chunkified with ~120 OCI layers. Without squashing, VFS import
creates ~6 GB × 120 layers = ~720 GB of intermediate directories, overflowing any
standard CI runner or local disk.

**CI** avoids this with `scripts/build-live-squashfs.sh`, which uses `podman image mount`
to get a single merged overlay view and runs `mksquashfs` directly on that mount.
No per-layer VFS expansion — peak disk usage stays ~6 GB for the live squashfs.

**Local builds** (via `just iso-sd-boot`) use `buildah commit --squash` to squash
the image to one layer before VFS import. The squash uses `buildah from --pull-never`
+ `buildah commit --squash` — NOT `podman create --entrypoint ... && podman commit`
(the latter corrupts the Entrypoint config, breaking `bootc install`).

## Source layout: `live/src/` vs `dakota/src/`

Two parallel source trees exist:

| Path | Used by | Notes |
|---|---|---|
| `live/src/` | CI (`build-iso.yml`), `live/Containerfile` | Canonical for CI; `build-iso.sh` here supports `--store` for offline OCI store |
| `dakota/src/` | Local justfile (`iso-sd-boot`, `luks-*` recipes) | `build-iso.sh` here is the simpler local variant without `--store` |

The live container (`live/Containerfile`) is used for **both** local and CI builds.
`live/src/flatpaks` is the definitive list of bundled Flatpaks.

`dakota/src/flatpaks` is a legacy copy — it may diverge. Use `live/src/flatpaks` as the
source of truth when adding or removing apps.



```bash
# Quick headless QEMU test — watch for DAKOTA_LIVE_READY on serial (Ctrl-A X to quit)
just boot-iso-serial dakota

# Full libvirt VM with SSH (requires debug=1 build)
just debug=1 iso-sd-boot dakota
just debug=1 boot-libvirt-debug dakota
# SSH: liveuser@<IP>  password: live
# Cleanup: sudo virsh destroy dakota-debug && sudo virsh undefine dakota-debug --nvram
```

## Justfile recipe reference

| Recipe | Description |
|---|---|
| `iso-sd-boot <target>` | **Full build** — container + ISO assembly |
| `container <target>` | Build the live-env container only |
| `build-bg <target>` | Background build with live log tail |
| `mount-xfs` | Create 45 GB XFS loopback at /mnt (sudo, idempotent) |
| `boot-iso-serial <target>` | Boot ISO in QEMU, serial output (Ctrl-A X) |
| `boot-libvirt-debug <target>` | Boot in libvirt, waits for DHCP + SSH |
| `e2e <target>` | Build ISO + full LUKS E2E test |

---

## Lessons

### `dakota/src/flatpaks` diverged from `live/src/flatpaks` (2026-06)

`dakota/src/flatpaks` contains `be.alexandervanhee.gradia` but `live/src/flatpaks` does not.
Since `live/Containerfile` uses `live/src/flatpaks`, CI builds omit Gradia. Keep `live/src/flatpaks`
as the source of truth and sync `dakota/src/flatpaks` to match it.


`/tmp` is a 16 GB tmpfs on this host. A Dakota build needs ~22 GB peak. The build
does not fail immediately — it runs out of space mid-squash and produces a truncated
or corrupt ISO that fails to boot. Always use `/var` or an explicit `output_dir`.

### buildah commit --squash vs podman create --entrypoint (2026-05)

`podman create --entrypoint /bin/sh && podman commit` modifies the recorded Entrypoint
in the image config. Dakota/bootc images have no Entrypoint by design; a fake one causes
`bootc install` to fail with "cannot execute binary file". Always use
`buildah commit --squash` to squash layers cleanly without touching config.
