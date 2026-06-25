# CI/CD

How the GitHub Actions workflows build, test, and publish Dakota ISOs.

## ISOs produced

Three NVIDIA-unified ISOs are built and published to R2:

| ISO | Workflow | R2 latest name | Image embedded |
|---|---|---|---|
| Dakota | `build-iso.yml` | `dakota-live-latest.iso` | `projectbluefin/dakota-nvidia:stable` |
| Bluefin | `build-iso-bluefin.yml` | `bluefin-live-latest.iso` | `projectbluefin/bluefin-nvidia:stable` |
| Bluefin LTS HWE | `build-iso-bluefin.yml` | `bluefin-lts-hwe-live-latest.iso` | `projectbluefin/bluefin-lts-hwe-nvidia:stable` |

All three are **unified NVIDIA ISOs** — the live environment boots the NVIDIA variant; the offline OCI store lets the installer deploy to non-NVIDIA hardware without a network pull (bootc auto-rebases on first upgrade).

To trigger a fresh publish of all three:
```bash
gh workflow run build-iso.yml --ref main
gh workflow run build-iso-bluefin.yml --ref main
```

## Workflows

| Workflow | File | Trigger |
|---|---|---|
| Dakota Build & Publish | `build-iso.yml` | 1st of month 03:00 UTC, `workflow_dispatch` |
| Bluefin Build & Publish | `build-iso-bluefin.yml` | 1st of month 05:00 UTC, `workflow_dispatch` |
| LUKS E2E Test | `test-luks-install.yml` | PRs to main, weekly Mon 04:00 UTC, `workflow_dispatch` |
| Plain Install E2E | `test-plain-install.yml` | PRs to main, weekly Tue 04:00 UTC, `workflow_dispatch` |
| ShellCheck Lint | `lint.yml` | PRs to main, push to main |
| Python Unit Tests | `test.yml` | PRs to main, push to main |

## build-iso.yml

**Triggers:** 1st of each month 03:00 UTC, `workflow_dispatch`
**Job:** `build-and-publish` (single job, no matrix)
**Runner:** `ubuntu-24.04`
**Runs as:** root via `sudo`
~~**Path triggers:** `live/**`, `scripts/**`, `.github/workflows/build-iso.yml`~~ — removed; see lessons.

### Pipeline steps

1. **Free disk space** — `jlumbroso/free-disk-space` reclaims ~119 GB at `/var/iso-build`
2. **Install deps** — `apt-get install podman buildah skopeo mtools xorriso squashfs-tools dosfstools isomd5sum`
3. **Log in to GHCR** — `sudo podman login ghcr.io`
4. **Pull payload image** — pulls only `dakota-nvidia:stable` (the unified ISO base)
5. **Build live container** — `podman build live/ --build-arg TARGET=dakota-nvidia` → `localhost/dakota-nvidia-live:latest`
6. **Build live squashfs** — `scripts/build-live-squashfs.sh` with `SUPERISO_COMPRESSION=release` → `<target>.rootfs.sfs` + `<target>-boot.tar` (~4.5 GB dakota, ~6 GB bluefin/lts-hwe)
7. **Assemble ISO** — `live/src/build-iso.sh` → `dakota-live.iso` (no `--store` flag — OCI already embedded in squashfs as VFS)
8. **Generate checksum** — dated + latest variants
9. **Plain-install E2E gates** — live boot, ENOSPC export gate, full install, installed-boot verification
10. **Boot verification** — QEMU UEFI smoke boot on the production ISO
11. **Upload to R2 + artifacts** — only after ENOSPC, full install, installed-boot verification, and production boot smoke all succeed

> ⚠️ **Do not add `--store` back or re-add the offline store squashfs step.**
> The OCI image is already embedded in the live squashfs via VFS containers-storage.
> Building a separate `store.squashfs.img` doubles the OCI payload, producing an ~8 GB
> ISO instead of ~5.3 GB. See lessons below.

### ⚠️ installer_channel is locked to `stable` in CI

Do NOT change `installer_channel` to `dev` in the live container build. There is an active
regression in the dev channel (`tuna-os/fisherman#38`) where the overlay storage
code path fails with:
```
open /var/tmp/oci-cache/index.json: no such file or directory
```
Production CI must stay on `installer_channel=stable` until the regression is fixed.

The stable channel resolves to `releases/latest/download` (the most recent non-prerelease tag).
As of 2026-06-14 this is **v2.7.4**.

### Disk layout in CI

The build path is `/var/iso-build` (~119 GB free after disk-space action).
Peak usage ~22 GB (live squashfs ~6 GB + offline store ~6 GB + ISO ~5 GB + intermediate).
No XFS loopback needed in CI.

### Boot verification logic

CI accepts either:
1. `DAKOTA_LIVE_READY` written directly to `/dev/ttyS0` by `live-ready.service`
2. `Finished live-ready.service` in the serial log (systemd journal console fallback)

Some dev channel builds don't write the serial marker but still reach GDM.
If both checks fail after 5 minutes, the job fails with `tail -50 /tmp/serial.log`.

### R2 upload

ISOs are uploaded to the `testing` bucket as:
- `dakota-live-YYYYMMDD-<sha>.iso` — permanent dated record
- `dakota-live-latest.iso` — points only to the last build whose ENOSPC gate, full install, installed-boot verification, and production ISO smoke boot all passed
- Matching `-CHECKSUM` files for both

⚠️ Direct uploads from the local host hang (routing issue). Always use R2→R2
server-side copies via rclone for local promotion. See `docs/r2-promotion.md`.

## build-iso-bluefin.yml

**Triggers:** 1st of each month 05:00 UTC, `workflow_dispatch`
**Matrix:** `bluefin`, `bluefin-lts-hwe`
**Runner:** `ubuntu-24.04`

This workflow builds the Bluefin and Bluefin LTS live ISOs, runs a QEMU smoke boot,
and uploads to R2 only when that smoke boot succeeds. It does **not** run the full
Dakota install/verify E2E sequence because those workflows are Dakota-specific.

### Boot verification: use AHCI, not SCSI CD (2026-06)

The smoke boot uses OVMF + QEMU. GitHub Actions runners have no KVM — they run
`-cpu qemu64` (software emulation). Without KVM, the SCSI bus enumeration in OVMF
is too slow: OVMF never discovers the SCSI CD and falls straight through to PXE boot,
failing with `BdsDxe: No bootable option or device was found`.

**Do not use:**
```yaml
-device virtio-scsi-pci,id=scsi0
-device scsi-cd,drive=iso,bus=scsi0.0
```

**Use instead:**
```yaml
-device ich9-ahci,id=ahci0
-device ide-cd,drive=iso,bus=ahci0.1
```

OVMF reliably auto-discovers AHCI optical drives on q35 regardless of CPU speed.
The local `boot-iso-serial` justfile recipe uses SCSI + KVM (`-accel kvm -cpu host`)
which works, but CI cannot use KVM.

### Adding a new Bluefin variant to the matrix

When a new image is ready to publish:
1. Add the matrix entry to `build-iso-bluefin.yml`
2. Commit the variant files in `<variant>/` and `live/src/<variant>/` in the same PR

## test-luks-install.yml

**Matrix:** `installer_channel: [dev, stable]` (fail-fast: false)
**Timeout:** 90 minutes
**Triggers:** PRs to main, weekly schedule, `workflow_dispatch`

### Pipeline steps

1. Ensure `ci-screenshots` branch exists
2. Free disk space
3. Install deps (adds `qemu-system-x86 ovmf socat sshpass`)
4. Configure podman storage (`configure_podman_storage.sh`)
5. Build ISO with `debug=1` and the matrix `installer_channel`
6. Boot live ISO in QEMU (daemonized) + wait for ready
7. SSH into live env, write recipe, run `fisherman` LUKS install
8. Patch BLS entries for dual console (`console=tty0 console=ttyS0`)
9. Boot installed disk, send LUKS passphrase via QEMU monitor
10. Verify boot success via serial log
11. Save screenshots to `ci-screenshots` branch + post PR comment

### Configure podman storage script

`.github/scripts/configure_podman_storage.sh` — intelligently selects the storage
driver based on the host filesystem:
- Clears existing podman storage to avoid driver mismatch errors
- On BTRFS: uses the native `btrfs` driver
- On ext4/xfs: uses `overlay`
- Falls back to `vfs` for unknown filesystems

### Screenshots

LUKS test screenshots are saved to the `ci-screenshots` branch and linked in PR
comments. Key screenshots:
- Live boot (after `DAKOTA_LIVE_READY`)
- Plymouth LUKS passphrase prompt
- Final boot (after passphrase unlock)

## test-plain-install.yml

**Matrix:** `installer_channel: [dev, stable]` (fail-fast: false)
**Timeout:** 120 minutes
**Triggers:** PRs to main, weekly schedule, `workflow_dispatch`

This workflow builds a debug Dakota ISO and runs the full plain-install QEMU path
(`just ... plain-test-qemu dakota`) to catch unencrypted installer regressions,
including the tight-memory ENOSPC class.

## Adding a new workflow

All workflow files go in `.github/workflows/`. Before adding:
- Run `actionlint`
- Check matrix `fail-fast: false` for variant builds
- Do not use `installer_channel=dev` in scheduled/release builds

## lint.yml — ShellCheck

Runs ShellCheck on every `.sh` file in the repository. Severity threshold is `warning`
(style/info is ignored). Uses `ludeeus/action-shellcheck@2.0.0`.

Any new shell script must pass ShellCheck before merge. For intentional suppression,
add an inline `# shellcheck disable=SCxxxx` comment with a justification.

## test.yml — Python Unit Tests

Runs `pytest tests/ -v` against Python 3.11.

> ⚠️ **`pytest tests/` passing does NOT mean the application works.**
> These are static-analysis and unit-level checks only. The real functional gates
> are `test-luks-install.yml` (LUKS E2E) and `test-plain-install.yml` (plain install E2E).
> Never claim a change is verified based on `pytest` alone.

| File | Tests | What it checks |
|---|---|---|
| `tests/test_live_build_invariants.py` | 32 | Static assertions on `live/Containerfile`, `live/src/build-iso.sh`, `live/src/configure-live.sh`, publish workflows, E2E workflow wiring, and variant config files. Also pins the DEBUG-only SSH guard, publish gating/concurrency, and `live/src` vs `dakota/src` `luks-unlock.py` sync. |
| `tests/test_luks_unlock.py` | 52 | `dakota/src/luks-unlock.py` routing, passphrase injection key sequences, and screenshot parsing. `tests/test_live_build_invariants.py` separately asserts the `live/src` helper stays byte-for-byte identical so local helpers and CI exercise the same logic. |
| `tests/test_multi_arch_iso.py` | 2 | `live/src/build-iso.sh --arch` flag: single-arch backwards compat and two-arch assembly. **Skipped when `xorriso`/`mtools` are absent.** CI installs these tools so the tests run; they are skipped only in local environments lacking them — and the skip message names the exact apt packages to install. |

Run locally with:
```bash
sudo apt-get install -y xorriso dosfstools mtools squashfs-tools
pip install pytest
pytest tests/ -v
```

### Lessons

### Publish workflows must gate `latest` on the last real safety check (2026-06-16)

If a workflow updates `*-latest.iso`, the publish step must come **after** the last
release-significant gate for that artifact — not before it. In practice:

- `build-iso.yml` must wait for ENOSPC, full install, installed-boot verification,
  **and** the final production ISO smoke boot before updating `dakota-live-latest.iso`
- `build-iso-bluefin.yml` must wait for its QEMU smoke boot before updating
  `bluefin-live-latest.iso` / `bluefin-lts-hwe-live-latest.iso`
- Publish workflows define workflow-level `concurrency` so overlapping manual/scheduled
  runs cannot race each other on the `latest` pointers
- Expensive E2E jobs (`plain-e2e`, `luks-e2e`) depend on `unit-tests` so cheap failures
  stop before QEMU burns runner time

### Double-embedded OCI store inflates ISO to 8 GB (2026-06)

The live container (`live/Containerfile`) already bakes the OCI image into the squashfs
as VFS containers-storage via `configure-live.sh` and `install-flatpaks.sh`.
Building a separate `store.squashfs.img` with `scripts/build-offline-store.sh` and
passing `--store store.squashfs.img` to `build-iso.sh` embeds the same ~4 GB OCI
image **twice** in the final ISO — resulting in ~8 GB instead of ~5.3 GB.

**Fix:** Remove the "Build offline image store squashfs" CI step entirely.
Call `build-iso.sh` without `--store`. This is the correct architecture for the
unified VFS-embedded ISO.

### Release compression for production ISOs (2026-06)

`scripts/build-live-squashfs.sh` defaults to zstd level 3 (`SUPERISO_COMPRESSION=fast`).
CI sets `SUPERISO_COMPRESSION=release` (zstd-15, 1M blocks) in the squashfs build step —
this produces ~20% smaller ISOs at ~5× longer squashfs build time. Always use `release`
for ISOs published to R2. Use `fast` only for local testing.

### installer_channel=dev regression: oci-cache/index.json not found (2026-05)

After the continuous-dev release ~2026-05, fisherman's overlay storage path fails
with `open /var/tmp/oci-cache/index.json: no such file or directory` when composefs+btrfs
is the backend. Root cause: fisherman exports the OCI to scratch but bootc inside the
container cannot see it via the bind mount.

Fix: use `installer_channel=stable`. Keep `build-iso.yml` on stable until
`tuna-os/fisherman#38` is resolved.

### DAKOTA_LIVE_READY not seen when live-ready.service uses journal+console (2026-05)

When `StandardOutput=journal+console`, the output goes to `/dev/console` (not `/dev/ttyS0`).
QEMU serial (`-serial file:...`) captures ttyS0 output only.

Fix: `StandardOutput=tty` + `TTYPath=/dev/ttyS0` for direct serial writes.
CI falls back to SSH connectivity check if the marker is absent.

### Offline install failed: VFS containers-storage missing from CI ISOs (2026-06)

**Symptom:** `fisherman: fatal: ... reference "containers-storage:ghcr.io/projectbluefin/dakota-nvidia:stable" does not resolve to an image ID`

**Root cause:** The CI build called `scripts/build-live-squashfs.sh` without `--oci-image`, so
the live squashfs shipped with an empty `/var/lib/containers/storage`.  The live `recipe.json`
has `local_imgref=containers-storage:ghcr.io/projectbluefin/dakota-nvidia:stable`, which
fisherman treats as authoritative.  When the local store is empty, the install fails even if
the user has a working internet connection (fisherman does not fall back to `docker://`).

**Why local builds were unaffected:** `just iso-sd-boot` always did the squash+skopeo step and
baked the OCI into the squashfs.  CI diverged from this path and the gap was undetected
because CI only validated boot, not install.

**Fix:** `scripts/build-live-squashfs.sh` now accepts `--oci-image <ref>`.  When provided it:
1. Squashes the payload to a single layer via `buildah commit --squash`
2. Runs `skopeo copy` inside the live container (for JSON tar-split compatibility)
3. Copies the populated VFS staging dir into the squashfs root with `cp -a` before mksquashfs

`build-iso.yml` now passes `--oci-image ghcr.io/projectbluefin/dakota-nvidia:stable` and
asserts the embedded store is non-empty before uploading the ISO to R2.

**Invariant:** The CI-built squashfs **must** contain a populated VFS store at
`var/lib/containers/storage` with the `dakota-nvidia:stable` image.  The assertion step
catches any regression before upload.

See issue #78.

### VFS store not captured by mksquashfs when using bind-mount into overlayfs (2026-06)

**Symptom:** `build-live-squashfs.sh --oci-image` runs successfully, VFS store logs 9.1G, but
the squashfs is only ~4.2G (no VFS data) and the assertion fails.

**Root cause:** When `SFS_ROOT` is an overlayfs mount (the default on ext4/XFS CI runners),
the overlayfs filesystem has a different `st_dev` than a bind-mounted directory inside it.
`mksquashfs` respects filesystem boundaries (stops when `st_dev` changes) and silently skips
the bind-mounted VFS tree.

**Fix:** Copy the VFS staging dir into the squashfs root with `cp -a` instead of bind-mounting.
Writes to an overlayfs path go into the overlay upper layer; the resulting files inherit the
overlayfs `st_dev` and are included by mksquashfs.

**Rule:** Never use `mount --bind` to inject data into a directory that will be squash-packed with
mksquashfs when the mount point is overlayfs.  Always copy.

### VFS storage paths don't contain image names — assertion must check vfs-images/ (2026-06)

**Symptom:** Assertion `grep -c "ghcr.io"` on `unsquashfs -lc` output returns 0 even when the
OCI store is correctly embedded.

**Root cause:** VFS containers-storage uses content-addressed hashes for all paths.  Image
names like `ghcr.io/projectbluefin/...` are stored in JSON metadata inside the hash-named
directories, not in the directory paths themselves.  `unsquashfs -lc` shows file paths only,
so grepping for `ghcr.io` always returns 0.

**Correct assertion:** Check for `var/lib/containers/storage/vfs-images` — this directory is
created by containers/storage for every imported image.  If it has entries, the VFS store
was populated.

**Note on mksquashfs deduplication:** The VFS layer is a squashed copy of the same OS as the
live rootfs.  mksquashfs deduplicates identical content blocks, so the squashfs size barely
increases despite embedding 9G of VFS data.  Use inode/file counts to confirm inclusion,
not squashfs file size.

### ENOSPC in skopeo OCI export — containers/storage tmpdir not redirected (2026-06)

**Symptom:** Live ISO installs fail with:
```
reading blob sha256:...: write /var/tmp/container_images_XXXXXXXX: no space left on device
```
The installer correctly sets `TMPDIR=/mnt/fisherman-target/.fisherman-scratch` but the
blob staging file still lands at `/var/tmp`.

**Root cause (3 layers):**
1. `configure-live.sh` writes `/etc/containers/storage.conf` with `driver = "vfs"` but **no
   `tmpdir` line**.
2. `containers/storage` defaults `TMPDir` to `/var/tmp` (hardcoded) when the config has no
   `tmpdir` field.  Setting `$TMPDIR` in the subprocess env is not sufficient — containers/storage
   reads the store config first and uses `/var/tmp` as the unconditional fallback.
3. `/var/tmp` on the live ISO is on the dracut overlayfs (~1.4 GiB writable layer) — too small
   for multi-GiB OCI layer blobs.

**Fix:** `skopeoExportOCI` (fisherman) now reads the current effective `storage.conf`, injects
`tmpdir = "<scratchDir>"`, writes the result to a temp file in the disk-backed scratch dir,
and passes it to skopeo via `CONTAINERS_STORAGE_CONF`.  `$TMPDIR` is retained for belt-and-
suspenders coverage of containers/image's copy-side blob staging.

**Why CI didn't catch it:** The LUKS E2E test runs QEMU with 8 GiB RAM; the overlay tmpfs
is ~4 GiB — large enough for individual blobs in most runs.  On 8 GiB user laptops with the
live environment loaded, free tmpfs headroom is much lower and ENOSPC triggers reliably.

**Prevention:** `plain-test-qemu` (new) runs with `qemu-mem=4096` (4 GiB RAM), which gives
only ~2 GiB overlay tmpfs — reliably reproducing this class of bug.  The test is gated
before R2 upload in `build-iso.yml`.

### build-live-squashfs.sh WORK dir must be on large disk (2026-06)

**Symptom:** `Build live squashfs + boot tar` step fails with:
```
write /usr/lib/locale/.../LC_COLLATE: no space left on device
mkdir /vfs-storage/vfs-layers/tmp: no space left on device
```

**Root cause:** `build-live-squashfs.sh` creates `WORK` at `/var/tmp` by default.
The squash-to-1-layer + VFS embedding writes ~12 GB of intermediates (`payload.oci.tar`
~6 GB + VFS staging ~6 GB).  `/var/tmp` on GitHub ubuntu-24.04 runners sits on the
root filesystem which has ~14 GB free after `jlumbroso/free-disk-space` — not enough
if the image grows at all.

**Fix:** `WORK` now uses `${SUPERISO_TMPDIR:-/var/tmp}`.  In CI, `build-iso.yml`
sets `SUPERISO_TMPDIR: /var/iso-build` so all intermediates land on the 119 GB
disk-backed path.  Locally the default `/var/tmp` still applies.

**Prevention:** If squashfs build ENOSPC recurs in CI, verify `SUPERISO_TMPDIR`
is set in the `Build live squashfs + boot tar` step env.

### E2E plain install test requires sshd — production ISO has it disabled (2026-06)

**Symptom:** `Plain install E2E` step fails with either:
- `kex_exchange_identification: read: Connection reset by peer` (QEMU user-net accepts TCP, no listener inside guest)
- `ERROR: serial marker seen but SSH not ready after 90 s` (sshd never starts)

**Root cause:** sshd is only enabled in the live ISO when the container is built with
`--build-arg DEBUG=1`.  The production build uses `DEBUG=0`, so no sshd.  The E2E test
uses SSH to invoke fisherman; without sshd the test cannot proceed.

**Fix:** After building the production ISO, a CI step patches the production squashfs:
1. `unsquashfs` the production rootfs (includes the embedded VFS store)
2. Add sshd.service symlink to `multi-user.target.wants`
3. Append `PasswordAuthentication yes` / `PermitEmptyPasswords yes` to sshd_config
4. Set `liveuser` password to `live` via `/etc/shadow` patch
5. `mksquashfs` back with zstd-1 (fast, debug-only)
6. Assemble `output/dakota-debug-live.iso` (uses same boot tar as production)

`plain-boot-qemu-live` in the justfile prefers `output/{{target}}-debug-live.iso`
when present, so CI runs against the debug ISO while R2 gets the production ISO.

**Why the VFS store must stay:** `ghcr.io/projectbluefin/dakota-nvidia` is private;
the live env inside QEMU has no GHCR credentials, so fisherman cannot pull from
network.  The VFS store (embedded in the squashfs) is the only install source.

### flatpak-spawn --host does not forward sandbox env to host process (2026-06)

**Symptom:** fisherman sets `TMPDIR=/mnt/fisherman-target/.fisherman-scratch` and prints
`# TMPDIR=<scratch>` before running skopeo, but the blob staging file is still created
at `/var/tmp/container_images_XXXXXXXX` causing ENOSPC.

**Root cause:** The bootc-installer runs inside a Flatpak.  When runner.go calls
`flatpak-spawn --host skopeo copy ...`, `flatpak-spawn --host` spawns the command in
the HOST mount namespace but does **not** automatically forward the Flatpak sandbox
environment to the spawned host process.  skopeo inherits the host's default env
(no `TMPDIR` set) and uses `/var/tmp` for blob staging.

Setting `cmd.Env` for the `flatpak-spawn` subprocess propagates env vars to
`flatpak-spawn` itself, but not to the command it spawns on the host.

**Fix (fisherman):** `runner.HostArgsWithEnv` injects critical env vars via
`--env=KEY=VALUE` flags in the `flatpak-spawn` args:
```
flatpak-spawn --host --env=TMPDIR=/scratch --env=CONTAINERS_STORAGE_CONF=... skopeo copy ...
```
Released in bootc-installer v2.7.1.

**How to identify:** Look for `# TMPDIR=<path>` in fisherman output followed by
`write /var/tmp/container_images_...: no space left on device`.  The TMPDIR debug
print confirms fisherman set the var correctly, but skopeo ignoring it confirms the
flatpak-spawn env forwarding gap.

### E2E test split into 4 named steps with individual timeouts (2026-06)

**Why:** The original single `plain-test-qemu` step had one monolithic timeout (90 min).
When it expired you had no idea which of the four stages (boot-live, install,
boot-installed, verify) was the bottleneck.

**New structure:**

| Step | Timeout | RAM | Purpose |
|---|---|---|---|
| `E2E 1/4 — Boot live ISO (4 GiB)` | 10 min | 4 GiB | Live env boots, sshd responds |
| `E2E 2/4 — ENOSPC gate: OCI export only (4 GiB)` | 10 min | 4 GiB | skopeo copies blob without ENOSPC (tight ~2 GiB overlay tmpfs) |
| `E2E 3/4 — Full install composefs (8 GiB)` | 60 min | 8 GiB | btrfs+composefs install completes |
| `E2E 4/4 — Boot installed + verify Graphical target (8 GiB)` | 20 min | 8 GiB | Installed system reaches systemd Graphical target |

Total worst-case ceiling: **100 min** (vs. 90 min monolithic), with precise attribution.

Gates 1+2 use 4 GiB to keep the overlay tmpfs tight (~2 GiB) for ENOSPC testing.
Gate 3 switches to 8 GiB for realistic btrfs+composefs install performance.

### Build trigger reduced to monthly + on-demand to cap R2 bucket growth (2026-06)

The original `build-iso.yml` ran on every push to `live/**`/`scripts/**` and
on a daily cron. Each successful run deposits a permanent dated ISO (~4.3 GB)
into R2, so daily builds produce ~60 ISOs/month of unbounded storage growth.

**Fix:** push triggers and the daily cron were removed. The workflow now runs:
- `schedule: cron '0 3 1 * *'` — 1st of each month at 03:00 UTC
- `workflow_dispatch` — on demand for releases, hotfixes, or manual triggers

This limits automatic R2 growth to ~2 objects/month (dated + latest overwrite).
For mid-cycle releases (e.g. a new named alpha), trigger manually and then
promote the dated ISO to the named slot via `rclone copyto` as documented in
`docs/r2-promotion.md`.

### btrfs composefs install — root cause chain and fixes (2026-06)

**Context:** dakota-nvidia:stable uses GNOME OS / freedesktop-sdk. Its initramfs
(built by the freedesktop-sdk pipeline) includes `btrfs.ko`, `erofs.ko`, `exfat.ko`,
and `squashfs.ko` — but **not** `xfs.ko`. Any attempt to install with `filesystem: xfs`
succeeds but produces a system that drops to emergency mode on first boot because the
initramfs cannot load the XFS driver to mount the root partition.

**Fix:** Set `filesystem: btrfs` everywhere — `live/src/etc/bootc-installer/images.json`,
all justfile install recipes (luks, plain, enospc-gate). `btrfs.ko` IS in the initramfs.

---

**Bug 2 — `images.json` overrides the fisherman CLI recipe**

`fisherman <recipe.json>` reads the recipe for `disk` and `image`, but looks up
`filesystem` from `/etc/bootc-installer/images.json` on the host, not from the recipe
JSON. Changing `"filesystem"` in the CLI recipe has no effect. The canonical place to
set the default filesystem is `images.json` baked into the live container.

---

**Bug 3 — bootc UUID auto-detect fails inside nested containers**

`bootc install to-filesystem` uses `findmnt --mountpoint /target --output UUID` to
discover the root filesystem UUID. Inside fisherman's `podman run --privileged` inner
container, `findmnt` cannot read the udev database (`/run/udev` is not mounted), so
the UUID column is always empty and the install fails with:
```
error: Installing to filesystem: No filesystem uuid found in target root
```

**Fix:** Inject `root-mount-spec = 'LABEL=root'` into the bootc install config before
squashing the OCI payload. fisherman always formats root partitions with `-L root`, so
`LABEL=root` is a stable, reliable mount spec that bypasses UUID detection entirely.

**How injection works:**
```
buildah from --pull-never ${PAYLOAD_IMAGE}
echo "root-mount-spec = 'LABEL=root'" > .bootc-root-mount.toml
buildah copy ${CTR} .bootc-root-mount.toml /tmp/.bootc-root-mount.toml
buildah run  ${CTR} -- sh -c 'cat /tmp/.bootc-root-mount.toml >> /usr/lib/bootc/install/00-defaults.toml'
buildah commit --squash ...
```
This is done in the `iso-sd-boot` recipe in `justfile` before buildah commit.

---

**Bug 4 — `btrfsSubvolumes: true` + `root-mount-spec` config = missing `rootflags=subvol=@`**

When `btrfsSubvolumes: true` is set in the fisherman recipe, fisherman creates `@`,
`@home`, `@snapshots` subvolumes and remounts with `subvol=@,compress=zstd:1`. bootc
installs the OS into the `@` subvolume. When booting, the initramfs needs
`rootflags=subvol=@` in the kernel cmdline to mount the right subvolume.

bootc DOES add `rootflags=subvol=@` automatically — but ONLY when it discovers the UUID
by itself (the `else` branch). When `root-mount-spec` is provided via config file, the
config branch returns `kargs: Vec::new()` (no `rootflags`). The `kargs` config field
in `[install]` section was tested but is not applied in the `to-filesystem` code path.

**Fix:** Remove `btrfsSubvolumes` entirely. Plain btrfs (root at subvolid=5) works
correctly with composefs. The OS is installed at the btrfs root, `root=LABEL=root`
mounts it, no subvolume flags needed.

---

**Bug 5 — fisherman hostname-fix condition missed new error format**

`fisherman-install.sh` detects hostname-write failures and patches the deployed `/etc/hostname`
manually. The original condition required BOTH `"writing hostname"` AND
`"ostree admin --print-current-dir"` in the fisherman log. New fisherman emits:
```
fisherman: fatal: writing hostname: finding composefs deploy etc: reading composefs deploy base
/mnt/fisherman-target/state/deploy: open /mnt/fisherman-target/state/deploy: no such file or directory
```
No `ostree admin` string → condition failed → install errored despite the OS being
fully installed.

**Fix:** `fisherman-install.sh` now matches `"writing hostname"` AND any of:
- `"ostree admin --print-current-dir"` (old fisherman)
- `"composefs deploy"` or `"state/deploy"` or `"no such file or directory"` (new fisherman)

---

### Lesson: nvidia-drm.modeset=1 required in all boot entries (2026-06)

**Symptom:** Black screen on boot on all NVIDIA hardware. Live ISO appears to hang
after BIOS handoff with no display output.

**Root cause:** `nvidia-drm.modeset=1` was never set in any of the four boot entry
locations in `live/src/build-iso.sh` and `dakota/src/build-iso.sh`. Without KMS
enabled, the NVIDIA proprietary driver cannot take over the framebuffer from the
BIOS, leaving the screen dark even though the system is running fine.

**Fix:** Added `nvidia-drm.modeset=1` to all 4 boot cmdline entries in both files
(BLS entries, loopback GRUB entries, single-arch and multi-arch paths).

**Regression test:** `TestBootCmdline::test_live_build_iso_has_nvidia_drm_modeset`
and `test_dakota_build_iso_has_nvidia_drm_modeset` in `tests/test_live_build_invariants.py`
assert every boot entry line with `root=live:/dev/sr0` contains this parameter.

---

### Lesson: root=live:/dev/sr0 breaks USB flash drive boots (2026-06)

**Symptom:** Black screen on boot on all non-NVIDIA real hardware. ISO boots
fine in QEMU but silently hangs on physical machines.

**Root cause:** `ca7f1e4` switched from `root=live:CDLABEL=DAKOTA_LIVE` to
`root=live:/dev/sr0` to fix CDLABEL detection in the Debian-built initramfs
on GnomeOS kernels. `/dev/sr0` is an optical-drive device node — it only
exists when the ISO is presented via a SCSI CD emulation (QEMU virtio-scsi,
physical optical drive). USB flash drive boots present the ISO as `/dev/sdX`.
Dracut waits indefinitely for `/dev/sr0`, which never appears. With `quiet`
suppressing all output, the result is a permanent, silent black screen.

This affected *every* user booting from USB — the majority of real hardware
users — regardless of GPU.

**Fix:** Use `root=live:LABEL=DAKOTA_LIVE` (blkid-based label scan). `LABEL=`
works on any block device (USB `/dev/sdX`, optical `/dev/sr0`, QEMU
virtio-scsi) without requiring the `cdrom_id` udev helper that broke with
the Debian-built initramfs.

**Why not CDLABEL=:** `CDLABEL=` uses the `cdrom_id` udev helper which is
absent or broken in the Debian-built initramfs on GnomeOS native kernels.
`LABEL=` uses `blkid` which is always present.

**Regression test:** `TestBootCmdline::test_{live,dakota}_build_iso_uses_label_not_cdlabel_or_sr0`
asserts `/dev/sr0` and `CDLABEL=` are banned from all boot entries.

**E2E verified:** `just debug=1 plain-e2e dakota` — full install + installed
system boot passed with `LABEL=DAKOTA_LIVE`.

### Lesson: build-live-squashfs.sh missing root-mount-spec injection → installed system emergency mode (2026-06)

**Symptom:** Installed system boots into emergency mode in CI with
`initrd-parse-etc.service: Failed with result 'start-limit-hit'`.
Local builds (`just iso-sd-boot`) succeed; CI builds (`build-iso.yml`) fail.

**Root cause:** `justfile`'s `iso-sd-boot` recipe injects
`root-mount-spec = 'LABEL=root'` into the squashed payload OCI image via
`buildah copy/run` before embedding it into VFS containers-storage.
`scripts/build-live-squashfs.sh` (used by `build-iso.yml`) squashed the
OCI payload but **skipped this injection**. Without it, `bootc install`
falls back to UUID auto-detect, which calls `findmnt` inside a nested
container where the udev database is inaccessible — UUID is always empty.
The installed BLS entry gets `root=UUID=` with an empty UUID, so the
initrd can't find the root partition → `bootc-root-setup.service` fails
→ `initrd-parse-etc.service` crash-loops → emergency mode.

**Fix:** Added the same `buildah copy/run` injection to
`scripts/build-live-squashfs.sh` immediately after `buildah from` and
before `buildah commit --squash`.

**Rule:** Any time `justfile`'s `iso-sd-boot` injects config into the
squashed OCI payload, `scripts/build-live-squashfs.sh` must apply the
same injection. They must stay in sync.

### Non-composefs variants use OCI layout, not VFS squash (2026-06)

Dakota (composefs) embeds the payload as VFS containers-storage inside the squashfs.
This path does not work for Fedora/Silverblue images (bluefin, bluefin-lts-hwe)
because squashing destroys the ostree commit structure bootc requires, producing:
```
Expected commit object, not File
```

Fix: `build-live-squashfs.sh` detects `composeFsBackend` from `recipe.json` and:
- **composefs = true** (dakota): squash → VFS containers-storage (unchanged)
- **composefs = false** (bluefin/*): store OCI layout directly at `/var/lib/containers/oci-store`

`configure-live.sh` writes the recipe accordingly:
- composefs: `"image": "containers-storage:<ref>"`
- non-composefs: `"image": "oci:/var/lib/containers/oci-store"`

fisherman needs the `bootcDirectOCI` code path (projectbluefin/fisherman dev→prod merge,
bootc-installer PR #192) to handle `oci:` source refs.

### fisherman prod branch must be kept in sync with dev (2026-06)

`projectbluefin/fisherman` has two branches: `dev` (active development) and `prod`
(what bootc-installer submodule points at). They diverge and must be explicitly merged.

To land dev fixes in a released bootc-installer:
1. `git checkout origin/prod && git merge dev --no-ff` (resolve any conflicts)
2. `git push origin HEAD:prod`
3. Update fisherman submodule in `projectbluefin/bootc-installer` to the new prod SHA
4. Open a PR in bootc-installer → CI builds and publishes a new Flatpak release

The fisherman submodule in bootc-installer points to `prod`, not `dev`. Changes on `dev`
do **not** automatically land in released installer builds.

### Workflow matrix must be kept in sync with variant config files (2026-06)

`build-iso-bluefin.yml` has a `strategy.matrix` that hardcodes `payload_image`,
`live_target`, `registry`, and `tag` per variant. These same values live in the
variant config files (`bluefin-lts-hwe/payload_ref`, `bluefin-lts-hwe/registry`, etc.).

**They can and do drift independently.**

**Rule:** Every change to a variant config file (`payload_ref`, `registry`, `live_target`)
must be accompanied by the matching matrix update in `build-iso-bluefin.yml` in the same commit.

**Long-term fix (Design Gate — requires human approval):** Remove the duplication by having
the matrix read config values from the variant files at build time instead of hardcoding them.
Until that refactor lands, keep the matrix and config files in sync manually on every change.
