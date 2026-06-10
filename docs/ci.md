# CI/CD

How the GitHub Actions workflows build, test, and publish Dakota ISOs.

## Workflows

| Workflow | File | Trigger |
|---|---|---|
| Build & Publish | `build-iso.yml` | push to main, daily 03:00 UTC, `workflow_dispatch` |
| LUKS E2E Test | `test-luks-install.yml` | PRs to main, weekly Mon 04:00 UTC, `workflow_dispatch` |
| ShellCheck Lint | `lint.yml` | PRs to main, push to main |
| Python Unit Tests | `test.yml` | PRs to main, push to main |

## build-iso.yml

**Job:** `build-and-publish` (single job, no matrix)
**Runner:** `ubuntu-24.04`  
**Runs as:** root via `sudo`
**Path triggers:** `live/**`, `scripts/**`, `.github/workflows/build-iso.yml`

### Pipeline steps

1. **Free disk space** — `jlumbroso/free-disk-space` reclaims ~119 GB at `/var/iso-build`
2. **Install deps** — `apt-get install podman buildah skopeo mtools xorriso squashfs-tools dosfstools isomd5sum`
3. **Log in to GHCR** — `sudo podman login ghcr.io`
4. **Pull payload image** — pulls only `dakota-nvidia:stable` (the unified ISO base)
5. **Build live container** — `podman build live/ --build-arg TARGET=dakota-nvidia` → `localhost/dakota-nvidia-live:latest`
6. **Build live squashfs** — `scripts/build-live-squashfs.sh` with `SUPERISO_COMPRESSION=release` → `dakota-nvidia.rootfs.sfs` + `dakota-nvidia-boot.tar` (~5.3 GB)
7. **Assemble ISO** — `live/src/build-iso.sh` → `dakota-live.iso` (no `--store` flag — OCI already embedded in squashfs as VFS)
8. **Generate checksum** — dated + latest variants
9. **Upload to R2** — `dakota-live-YYYYMMDD-<sha>.iso` + `dakota-live-latest.iso` + checksums
10. **Boot verification** — QEMU UEFI boot, wait for `DAKOTA_LIVE_READY` serial marker
11. **Upload artifacts** — ISO + checksum + screenshot (7-day retention)

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
- `dakota-live-latest.iso` — always points to the last successful build
- Matching `-CHECKSUM` files for both

⚠️ Direct uploads from the local host hang (routing issue). Always use R2→R2
server-side copies via rclone for local promotion. See `docs/r2-promotion.md`.

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
- On BTRFS: uses VFS driver (overlayfs is unreliable on BTRFS in CI)
- On ext4/other: uses overlay driver

### Screenshots

LUKS test screenshots are saved to the `ci-screenshots` branch and linked in PR
comments. Key screenshots:
- Live boot (after `DAKOTA_LIVE_READY`)
- Plymouth LUKS passphrase prompt
- Final boot (after passphrase unlock)

## Adding a new workflow

All workflow files go in `.github/workflows/`. Before adding:
- Run `actionlint` (config in `.github/actionlint.yaml`)
- Check matrix `fail-fast: false` for variant builds
- Do not use `installer_channel=dev` in scheduled/release builds

## lint.yml — ShellCheck

Runs ShellCheck on every `.sh` file in the repository. Severity threshold is `warning`
(style/info is ignored). Uses `ludeeus/action-shellcheck@2.0.0`.

Any new shell script must pass ShellCheck before merge. For intentional suppression,
add an inline `# shellcheck disable=SCxxxx` comment with a justification.

## test.yml — Python Unit Tests

Runs `pytest tests/ -v` against Python 3.11.

| File | Tests | Coverage |
|---|---|---|
| `tests/test_luks_unlock.py` | 52 | `luks-unlock.py` virsh/QEMU interaction, screenshot parsing, passphrase injection |
| `tests/test_multi_arch_iso.py` | 4 | `build-iso.sh` `--arch` arg parsing; integration tests (skipped if xorriso/mtools absent) |

Run locally with:
```bash
pip install pytest
pytest tests/ -v
```

### Lessons

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
