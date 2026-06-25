# Build System

How to build Dakota live ISOs locally and the key variables that control the build.

## Two variant types — understand this before changing anything

This repo produces ISOs for two different OCI store layouts:

| Variant | `composefs` | OCI store path | squash before embed? |
|---|---|---|---|
| `dakota` | `true` | `/var/lib/containers/storage` (VFS) | **yes** — squash to 1 layer |
| `bluefin`, `bluefin-lts-hwe` | `false` | `/var/lib/containers/oci-store` (OCI layout) | **yes** — squash to 1 layer |

**Both paths squash.** `buildah commit --squash --format oci` for non-composefs; `buildah commit --squash` (VFS) for composefs. Never use `--format oci` without `--squash`.

Any change to `justfile` or `scripts/build-live-squashfs.sh` must be tested on **both** a composefs variant (`dakota`) and a non-composefs variant (`bluefin`). The code paths diverge and bugs in one are invisible when testing only the other.

## Quick start

```bash
# Build and test dakota (composefs)
just debug=1 iso-sd-boot dakota
just plain-test-qemu dakota          # must exit: ✅ Installed system boot verified

# Build and test bluefin (non-composefs) — required when changing justfile/build-live-squashfs.sh
just debug=1 iso-sd-boot bluefin
just plain-test-qemu bluefin
```

Output: `output/<target>-live.iso`

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

**Expected ISO sizes:**

| Variant | fast compression | release compression |
|---|---|---|
| `dakota` (composefs) | ~5.5 GB | ~4.5 GB |
| `bluefin` / `bluefin-lts-hwe` (non-composefs) | ~7 GB | ~6 GB |

If a bluefin ISO is **~12 GB**: the non-composefs OCI embedding is not squashing.
See "Non-composefs OCI squash" lesson below.
If a dakota ISO is **~8 GB**: double-embedded store — see `ci.md` lessons.

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

### mksquashfs -e proc/sys/dev removes empty dirs in 4.7+ (2026-06)

`dmsquash-live-root` (Debian bookworm dracut) needs a `proc/` directory at the squashfs root to use the squashfs directly as the live rootfs. Without it: `FATAL: Failed to find a root filesystem in squashfs.img`.

dracut's `usable_root()` function also requires **all three** of `proc/`, `sys/`, and `dev/` at the squashfs root. On modern GNOME OS (glibc 2.38+), there is no `ld-2.XX.so` file — only `ld-linux-x86-64.so.2` (which does not match the `ld-*.so` glob). So the second fallback check (`proc sys dev` all exist) is the only path that works.

Old mksquashfs (≤4.6, Ubuntu 22.04/24.04 system package) with `-e proc` excluded proc's CONTENTS but kept the empty directory. mksquashfs ≥4.7 (homebrew, newer distros) removes the directory itself. Same applies to `sys` and `dev`.

Fix in `scripts/build-live-squashfs.sh` and `justfile`:
```bash
mkdir -p "${SFS_ROOT}/proc" "${SFS_ROOT}/sys" "${SFS_ROOT}/dev"  # ensure empty dirs exist
mksquashfs ... -wildcards -e "proc/*" -e "sys/*" -e "dev/*" ...  # exclude contents only
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
- `composefs=false` → **squash to 1 layer** → `buildah commit --squash --format oci` →
  `skopeo copy oci:... oci:<oci-store-dir>`

**Both paths squash.** Never use `--format oci` without `--squash` for embedded OCI stores.
bluefin-nvidia has ~120 OCI layers; without `--squash`, all layer blobs land in the squashfs
OCI layout → ~8 GB OCI store → 12 GB ISO. With `--squash`: ~4 GB OCI store → ~6 GB ISO.

**Critical: branch before squash.** The composefs flag must be read before any squash
operation, and both paths must squash.

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

### Non-composefs OCI must squash to 1 layer before embedding (2026-06-21)

**What failed:** bluefin and bluefin-lts-hwe ISOs built at 12 GB instead of ~6 GB.

**Why:** `buildah commit --format oci` without `--squash` in the non-composefs path embeds all ~120 OCI layers as individual blobs into `var/lib/containers/oci-store` inside the squashfs. Each layer is ~60–80 MB uncompressed; 120 layers → ~8 GB OCI store inside a ~6 GB rootfs → 12 GB squashfs → 12 GB ISO.

**Fix:** Both `justfile` and `scripts/build-live-squashfs.sh` non-composefs paths must use `--squash --format oci`:

```bash
buildah commit --squash --format oci "${INJECT_CTR}" "oci:${OCI_DIR}:${OCI_IMAGE}"
```

Squashing reduces the OCI store to a single ~4 GB layer → ~6 GB final ISO.

**NEVER remove `--squash` from this path.** The dakota (composefs) path squashes for VFS import; the bluefin (non-composefs) path squashes before OCI layout copy. Both paths squash.

### VFS additionalimagestore requires squashing to prevent storage explosion (2026-06-23)

**What failed:** Building `stable` and `lts` ISO targets failed in CI with `no space left on device` (ENOSPC) during the VFS import step of `Build debug ISO`.

**Why:** To support kernels without overlay-on-overlay, the non-composefs targets (`stable`, `lts`) use the `vfs` driver for their additional image store. If the payload image is NOT squashed before import, the VFS driver has to unpack and copy all ~120 layers sequentially. Because VFS lacks copy-on-write, this layer-on-layer unpacking causes an exponential disk space explosion (>100 GB), exhausting the runner's disk.

**Fix:** Ensure `buildah commit --squash --format oci` is used for the payload image in BOTH the `justfile` (inline `iso-sd-boot`) and `scripts/build-live-squashfs.sh` non-composefs paths before the `skopeo copy ... containers-storage:` import step. This squashes the payload to a single layer, preventing the VFS import explosion.

### Mksquashfs silently skips bind-mounted dirs on overlayfs host (2026-06-23)

**Symptom:** `just iso-sd-boot` ran successfully in CI, but the resulting live ISO was missing the entire embedded VFS store/OCI layout inside `/usr/lib/containers/storage` or `/var/lib/containers/storage`.

**Why:** In the `justfile`'s `iso-sd-boot` target, the intermediate storage staging directory was bind-mounted (`mount --bind`) into the squashfs root. When the runner's build directory is on `overlayfs` (as in CI), `mksquashfs` respects filesystem boundaries (stops when the device ID changes) and silently skips the bind-mounted directory.

**Fix:** Avoid bind mounts when structuring filesystems inside `SQUASHFS_ROOT`. Use `cp -a` to copy the staged container storage directory directly into the squashfs root instead of bind-mounting.

### Non-composefs bootcDirect QEMU E2E installs require the scratch disk (2026-06-23)

**Symptom:** E2E test runs for `stable` and `lts` failed with `no space left on device` (ENOSPC) during `bootc install to-filesystem` inside the live VM.

**Why:** The `justfile`'s `luks-install-qemu` and `plain-install-qemu` targets only mounted the `/dev/vdb` scratch disk over `/var/tmp` for `composefs=true` (dakota). However, `bootc install` on non-composefs targets also writes temporary layer/blob files to `/var/tmp/container_images_...` during its extraction/deployment phase. Without the scratch disk backing `/var/tmp`, `bootc` quickly exhausts the 4 GiB VM's RAM-backed overlay tmpfs.

**Fix:** Move the scratch disk mounting step outside the composefs checks in `justfile`'s `luks-install-qemu` and `plain-install-qemu` targets so it is formatted and mounted over `/var/tmp` for all variants.

### Architectural Decisions for OSTree (Stable/LTS) vs ComposeFS (Dakota) (2026-06-24)

Our consolidation of OSTree (GRUB) and ComposeFS (systemd-boot) installer flows into a unified repository highlighted two distinct execution requirements:

1. **Storage Drivers & Whiteouts**:
   * **ComposeFS (`dakota`)** relies on a squashed, single-layer `vfs` storage driver.
   * **OSTree (`stable`, `lts`)** requires `overlay` with `fuse-overlayfs` mapping because the host live ISO runs `dmsquash-live` overlayfs, and CentOS 10/el10 kernels lack native overlay-on-overlay support. Standard OSTree layers must **not** be squashed during image construction to preserve commits integrity. Whiteouts are stripped out during the rsync staging phase via `--no-specials --no-devices`.

2. **Filesystem Selection & Boot UUID Timeout**:
   * **`dakota` and `stable`** install successfully using raw `btrfs` partitions.
   * **`lts`** targets formatted as direct `xfs` fail boot verification due to a `/boot` partition ext4 UUID detection timeout in the CentOS 10 initramfs dracut loop.
   * **Fix**: LTS installations default to `xfs-in-lvm` which mounts through LVM volume activation hooks, bypassing the udev device-by-uuid wait step and ensuring reliable boot discovery.

### Debug ISO squashfs must mirror production mksquashfs flags (2026-06-21)

**What failed:** `build-iso.yml` E2E step 1/4 (boot live ISO) — `dracut Warning: /sysroot has no proper rootfs layout` → `Can't mount root filesystem`. Production ISO was correct; boot failed because E2E uses the debug ISO (preferred by `plain-boot-qemu-live`).

**Why:** The debug ISO rebuild step in CI used `-wildcards -e sys -e dev` (without glob patterns). With `-wildcards`, mksquashfs treats `-e sys` as "exclude any path matching 'sys'" which removes the `sys/` directory node entirely. dracut's `usable_root()` requires all three of `proc/`, `sys/`, `dev/` as empty dirs. The production squashfs was built correctly (using `-e "sys/*" -e "dev/*"` and `mkdir -p sys/ dev/`) but the debug rebuild step had stale flags.

**Fix in `.github/workflows/build-iso.yml`:**
```bash
sudo mkdir -p /var/iso-build/debug-rootfs/proc \
              /var/iso-build/debug-rootfs/sys \
              /var/iso-build/debug-rootfs/dev
sudo mksquashfs ... -wildcards -e "proc/*" -e "sys/*" -e "dev/*" -e run -e tmp
```

**Rule:** Any squashfs rebuild step (debug ISO, test fixtures, etc.) must use the exact same `mkdir -p proc/ sys/ dev/` + `-e "proc/*" -e "sys/*" -e "dev/*"` pattern as `scripts/build-live-squashfs.sh`. Using `-e sys` or `-e dev` (bare names, even with or without `-wildcards`) is dangerous — it may remove the directory node on newer mksquashfs.

### COMPOSEFS_BACKEND detection: never use `sh -c 'python3 -c "..."'` (2026-06-21)

**What failed silently:** `scripts/build-live-squashfs.sh` always selected the non-composefs OCI layout path for ALL variants, including dakota (which needs the VFS composefs path). Dakota installs would fail because recipe.json says `containers-storage:...` but the offline store was embedded as an OCI layout at `/var/lib/containers/oci-store`.

**Why:** Shell quoting bug. The detection ran:
```bash
sh -c 'python3 -c "import json; print(json.load(open("/etc/bootc-installer/recipe.json"))..."'
```
Inside the container, `sh` sees the inner `"` after `open(` as closing the outer double-quoted string. python3 gets a malformed `-c` argument, exits with an error (suppressed by `2>/dev/null`), output is empty, `grep -qi true` fails, `COMPOSEFS_BACKEND` stays `false` for ALL variants.

**Fix:** Run python3 directly as the container entrypoint; avoid `sh -c '...'` wrapping:
```bash
podman run --rm --entrypoint="" "${IMAGE}" \
    python3 -c 'import json; d=json.load(open("/etc/bootc-installer/recipe.json")); print(d.get("composeFsBackend", False))' \
    2>/dev/null | grep -qi true
```
Single-quoting the python3 `-c` argument prevents bash from expanding it, and avoids any sh-layer quoting entirely.
