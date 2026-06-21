# Build System

How to build Dakota live ISOs locally and the key variables that control the build.

## Quick start

```bash
just iso-sd-boot dakota               # full build, stable installer
just debug=1 installer_channel=dev iso-sd-boot dakota  # debug + dev installer
just build-bg dakota                  # background build (survives terminal close)
```

Output: `output/dakota-live.iso` (~4.3 GB, ~20–40 min depending on network)

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

Uses `setsid sudo just ... & disown` internally so the build survives terminal closure.

⚠️ **`build-bg` requires `sudo` and a TTY.** In agent/headless sessions (no TTY), `sudo`
fails silently with `a terminal is required to read the password`. Use direct backgrounding
instead:

```bash
LOG=output/build.log
mkdir -p output
setsid bash -c "just output_dir=output iso-sd-boot dakota > '${LOG}' 2>&1" &
disown $!
echo "Build started → ${LOG}"
# Monitor:
tail -f output/build.log
```

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

### Unified ISO: one nvidia image for all hardware (2026-06)

Dakota ships a single ISO built from `ghcr.io/projectbluefin/dakota-nvidia:stable`.
The live environment runs the nvidia image. At install time, `bootc-installer`'s
`nvidia_imgref` mechanism auto-detects the GPU:

- **NVIDIA GPU present:** installs `dakota-nvidia:stable`, `targetImgref=dakota-nvidia:stable`
- **No NVIDIA GPU:** installs offline from the nvidia VFS store, `targetImgref=dakota:stable` —
  first `bootc upgrade` rebases to the correct non-nvidia variant automatically

**Expected ISO size:** ~5.3 GB (with `compression=release`). If the ISO is ~8 GB, the
offline OCI store was double-embedded — see `ci.md` lessons.

### CI uses `SUPERISO_COMPRESSION=release` (2026-06)

The `build-iso.yml` CI workflow sets `SUPERISO_COMPRESSION=release` in the squashfs build
step, producing zstd-15 compression. Local `just iso-sd-boot` defaults to `compression=fast`
(zstd-3). For production ISOs destined for R2, always use release compression:
```bash
just compression=release iso-sd-boot dakota
```

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

### live/src/install-flatpaks.sh must mirror dakota/src/install-flatpaks.sh (2026-06)

`live/src/install-flatpaks.sh` is a parallel copy of `dakota/src/install-flatpaks.sh`
for the live-squashfs build path. When the installer source logic changes in one, it
must be replicated in the other. After PR fc0346d added primary/fallback logic to the
`dakota` copy, the `live` copy was left behind still pointing only at `tuna-os/tuna-installer`.
Both files now use `projectbluefin/bootc-installer` as primary (with `--fail` so curl
exits non-zero on HTTP errors) and fall back to `tuna-os/tuna-installer` automatically.

### filesystem choice: always btrfs for dakota (2026-06)

The `dakota-nvidia:stable` initramfs (built by freedesktop-sdk) includes `btrfs.ko`
but **not** `xfs.ko`. Verify with:
```bash
# Extract and inspect the installed initramfs
podman run --rm ghcr.io/projectbluefin/dakota-nvidia:stable bash -c '
  python3 -c "
data = open(\"/usr/lib/modules/7.0.7/initramfs.img\", \"rb\").read()
idx = data.find(b\"TRAILER!!!\")
after = (idx + 10 + 511) & ~511
import subprocess, sys
payload = data[after:]
open(\"/tmp/p.zst\", \"wb\").write(payload)
"
  zstdcat /tmp/p.zst | cpio -it 2>/dev/null | grep -iE "xfs|btrfs"
'
```

Always use `filesystem: btrfs` in fisherman recipes. Do NOT use `btrfsSubvolumes: true`
— the subvolume setup interacts poorly with bootc's `root-mount-spec` config injection
(see `docs/ci.md` for the full root cause chain).

### root-mount-spec injection: how and why (2026-06)

`bootc install to-filesystem` auto-detects the root filesystem UUID using `findmnt`
inside a nested `podman run`. Inside that container, the udev database is not mounted,
so `findmnt --output UUID` returns empty and the install fails.

The `iso-sd-boot` recipe in the justfile injects `root-mount-spec = 'LABEL=root'` into
`/usr/lib/bootc/install/00-defaults.toml` inside the squashed OCI payload before
building the squashfs. This is safe because fisherman always formats with `-L root`.

If you see `No filesystem uuid found in target root` — check that:
1. The `.bootc-root-mount.toml` was created in `output/`
2. The `buildah copy` + `buildah run` steps ran (look for `Squashing` line in build log)
3. The injected config has no duplicate `[install]` section headers

### ostree.final-diffid: composefs vs ostree-native images (2026-06)

`buildah commit --squash` produces a **regular filesystem tar** (the merged OS files), not
an ostree commit blob.

- **Composefs images** (Dakota): `ostree.final-diffid` must be updated to point to the
  new squashed layer's diff_id — bootc reads it to locate the composefs commit layer.
- **Ostree-native images** (bluefin/Silverblue): the annotation must be **removed**.
  If it stays, bootc uses the "ostree-encapsulation" install path and fails with
  `Expected commit object, not File` — it's looking for an ostree commit blob but the
  squashed layer is a filesystem tar.

The `iso-sd-boot` recipe detects composefs mode from `live/src/<target>/composefs` and
either updates or removes the annotation accordingly.

### /run overlay size: 28 GB RAM needed for bluefin offline install (2026-06)

The live environment's `/run` tmpfs defaults to 20% of VM RAM (e.g. 5.6 GB on a 28 GB VM).
When fisherman runs `podman run containers-storage:bluefin-nvidia`, the VFS storage driver
creates a full 8.8 GB copy of the image in `/var/lib/containers/storage/vfs/dir/` (the
overlay upper dir at `/run/overlayfs`). With a 5.6 GB /run, the copy immediately fails
with "no space left on device".

Fix: `sudo mount -o remount,size=24G /run` before running fisherman. This is automated
in `configure-live.sh` for bluefin (adds a systemd dropin that remounts /run at boot).
Alternatively, set `rd.live.overlay.size=24576` in boot entries.

For QEMU testing of bluefin installs: use at least 28 GB RAM (`-m 28672`).
With 16 GB, the VFS container creation runs out of memory on the RAM-backed overlay.

### Ghost lab: ISO testing via Argo Workflows (2026-06)

`ghost` is the k3s control-plane + KubeVirt compute node. All ISO builds and
install testing run as **local ad-hoc Argo Workflows** submitted via the argo-mcp
pi extension. Direct SSH builds are forbidden — they bypass cluster scheduling,
consume untracked resources, and have crashed the node.

⛔ **NEVER `ssh ghost` to run builds.**
⛔ **NEVER `rsync` code to ghost and run `just` directly.**

Every workload must be submitted as an Argo Workflow. Always inspect what
WorkflowTemplates exist in the cluster before submitting:

```
k8s_nodes_top                                   # check headroom first
argo_list_workflow_templates namespace=argo     # find the right template
argo_submit_workflow namespace=argo manifest=<yaml>
argo_logs_workflow name=<name> namespace=argo tailLines=100
```

**Do NOT manually push ISOs to R2.** Let CI handle all R2 uploads.
The `latest` pointer is the production artifact — only CI may write it after
passing the boot verification gate. See `docs/r2-promotion.md`.

### Variant image refs must be verified against the publishing workflow (2026-06)

Never guess image names for a variant. Always verify what `projectbluefin` actually
publishes by reading the `execute-release.yml` workflow in the source repo:

```bash
gh api repos/projectbluefin/bluefin-lts/contents/.github/workflows/execute-release.yml \
  --jq '.content' | base64 -d | grep 'image'
```

The pattern for confirming a tag exists:
```bash
skopeo list-tags docker://ghcr.io/projectbluefin/<image> | python3 -c \
  "import json,sys; print([t for t in json.load(sys.stdin)['Tags'] if t in ('stable','lts','latest')])"
```

`projectbluefin/bluefin-lts` publishes: `bluefin-lts:stable`, `bluefin-lts-hwe:stable`, `bluefin-lts-hwe-nvidia:stable`.
There is no `bluefin-lts-nvidia` without `-hwe`. There is no `bluefin-gdx` in projectbluefin.

### systemd-boot title comes from live_title file (2026-06)

Each variant directory has a `live_title` file. `build-iso.sh` reads it via `--title`.
To change what users see in the boot menu, edit that file — do not touch `build-iso.sh`.

### libblkid/libuuid must come from base image, not Debian stage (2026-06)

`live/Containerfile` stage 3 copies some libs from Debian bookworm for `mkfs.xfs`:
`libinih.so.1` and `liburcu.so.8` genuinely must come from Debian (wrong version in Fedora / absent in GNOME OS).

**Do NOT copy `libblkid.so.1` or `libuuid.so.1` from Debian.** Both the freedesktop-sdk
(dakota) and Fedora (bluefin) base images ship their own newer versions at
`/usr/lib/x86_64-linux-gnu/`. Overwriting them with Debian bookworm's older copy
(only BLKID_2_21) breaks `sfdisk` which requires `BLKID_2_40` via `libfdisk.so.1`:

```
sfdisk: /usr/lib/x86_64-linux-gnu/libblkid.so.1: version `BLKID_2_40' not found
```

`mkfs.xfs` (Debian-compiled) works fine with the newer system `libblkid` because it
is backward-compatible with all earlier symbol versions. Only copy what is genuinely
missing from the target base image.

### mksquashfs -e proc removes empty dir in 4.7+ (2026-06)

`dmsquash-live-root` (Debian bookworm dracut) needs a `proc/` directory at the squashfs root to use the squashfs directly as the live rootfs. Without it: `FATAL: Failed to find a root filesystem in squashfs.img`.

Old mksquashfs (≤4.6, Ubuntu 22.04/24.04 system package) with `-e proc` excluded proc's CONTENTS but kept the empty directory. mksquashfs ≥4.7 (homebrew, newer distros) removes the directory itself.

Fix in `scripts/build-live-squashfs.sh`:
```bash
mkdir -p "${SFS_ROOT}/proc"   # ensure empty proc/ exists
mksquashfs ... -wildcards -e "proc/*" ...  # exclude contents only
```

### Non-composefs variants (bluefin, bluefin-lts-hwe): recipe.json fields that must be set (2026-06)

Two fields in recipe.json were missing/wrong for Fedora/CentOS variants, causing every install to fail:

**1. `filesystem` was never set** — fisherman defaulted to xfs. All variants must install to btrfs.
The fix: `configure-live.sh` now sets `recipe["filesystem"] = "btrfs"` for all variants.
XFS is only available as a user option in the installer UI (`images.json` `"filesystems"` array).

**2. `local_imgref` pointed at the wrong store** — non-composefs variants embed the payload as
an OCI layout at `oci:/var/lib/containers/oci-store`, NOT as VFS containers-storage.
The old code set `local_imgref = "containers-storage:<NVIDIA_IMGREF>"`, which pointed at a
VFS store that doesn't exist for these variants. Fisherman could not find the offline image.

Fix:
```python
if composefs == "true":
    recipe["image"] = f"containers-storage:{nvidia_imgref}"
    recipe["local_imgref"] = f"containers-storage:{nvidia_imgref}"
else:
    recipe["image"] = "oci:/var/lib/containers/oci-store"
    recipe["local_imgref"] = "oci:/var/lib/containers/oci-store"
recipe["filesystem"] = "btrfs"
```

**Rule: never test recipe changes with CI only.** Build the container locally, run
`podman run --rm <image> cat /etc/bootc-installer/recipe.json` to verify the recipe,
then do a local QEMU install test before pushing to CI.

### justfile `iso-sd-boot` must branch on composefs for OCI store embed (2026-06)

`configure-live.sh` sets `local_imgref` differently per variant type:
- composefs (dakota): `containers-storage:<NVIDIA_IMGREF>` → VFS store at `/var/lib/containers/storage`
- non-composefs (bluefin, lts): `oci:/var/lib/containers/oci-store` → OCI layout

The justfile `iso-sd-boot` recipe previously always used the VFS containers-storage path
regardless of composefs setting. For bluefin/lts-hwe, the installer looked for
`oci:/var/lib/containers/oci-store` but found nothing there — causing:
```
fisherman: fatal: bootc install: pulling image: podman pull oci:/var/lib/containers/oci-store: exit status 125
```

**Fix:** `iso-sd-boot` now reads `live/src/<target>/composefs` and branches **before** squash:
- `composefs=true` → squash to 1 layer → VFS import via skopeo inside installer container
- `composefs=false` → `buildah commit --format oci` (NO squash, preserves original layers) →
  `skopeo copy oci:... oci:<oci-store-dir>`

**Critical: branch before squash.** Squashing a non-composefs image (bluefin) to one layer
then storing as OCI layout produces a single ~9 GB uncompressed blob → ~11 GB ISO.
Original layers are already gzip-compressed and copy cleanly to OCI layout at expected size.

This mirrors `scripts/build-live-squashfs.sh` exactly.
`live/src/<target>/composefs` is the single source of truth — both scripts read it.

### Installer layer cache busting (2026-06)

The `COPY src/flatpaks` + `RUN install-flatpaks.sh` stage in the Containerfile is cached
by podman's layer cache. If neither file changes between builds, the old installer Flatpak
stays in the image indefinitely — even though `install-flatpaks.sh` calls curl to download
`releases/latest/download/`.

**Fix:** `ARG CACHE_BUST=0` before the flatpak stage; justfile passes
`--build-arg CACHE_BUST=$(date +%Y%m%d)` to invalidate the layer once per calendar day.

To verify installer version: the metainfo XML in the flatpak may say an old version (not
updated by upstream). The real check is the fisherman binary at
`var/lib/flatpak/app/org.bootcinstaller.Installer/.../files/bin/fisherman` — v3.x is a
Go binary containing `[fisherman] version:` strings and LUKS/flatpak/composefs logic.

### CI bluefin OCI store verification (2026-06)

`build-iso-bluefin.yml` previously checked `var/lib/containers/storage/vfs-images` (VFS path).
Bluefin/lts-hwe use `composeFsBackend=false` and embed at `var/lib/containers/oci-store`.
Fixed to check `var/lib/containers/oci-store/index.json` instead.
