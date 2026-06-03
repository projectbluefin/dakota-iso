# CI/CD

How the GitHub Actions workflows build, test, and publish Dakota ISOs.

## Workflows

| Workflow | File | Trigger |
|---|---|---|
| Build & Publish | `build-iso.yml` | push to main, daily 03:00 UTC, `workflow_dispatch` |
| LUKS E2E Test | `test-luks-install.yml` | PRs to main, weekly Mon 04:00 UTC, `workflow_dispatch` |

## build-iso.yml

**Job:** `build-and-publish` (single job, no matrix)
**Runner:** `ubuntu-24.04`  
**Runs as:** root via `sudo`
**Path triggers:** `live/**`, `scripts/**`, `.github/workflows/build-iso.yml`

### Pipeline steps

1. **Free disk space** — `jlumbroso/free-disk-space` reclaims ~119 GB at `/var/iso-build`
2. **Install deps** — `apt-get install podman buildah skopeo mtools xorriso squashfs-tools dosfstools isomd5sum`
3. **Log in to GHCR** — `sudo podman login ghcr.io`
4. **Pull payload images** — pulls `dakota-nvidia:latest` and `dakota:latest` into local podman store
5. **Build live container** — `podman build live/ --build-arg TARGET=dakota-nvidia` → `localhost/dakota-nvidia-live:latest`
6. **Build live squashfs** — `scripts/build-live-squashfs.sh` → `dakota-nvidia.rootfs.sfs` + `dakota-nvidia-boot.tar`
7. **Build offline store** — `scripts/build-offline-store.sh` → `store.squashfs.img` (both dakota + dakota-nvidia)
8. **Assemble ISO** — `live/src/build-iso.sh --store store.squashfs.img ...` → `dakota-live.iso` (~4.5 GB)
9. **Generate checksum** — dated + latest variants
10. **Upload to R2** — `dakota-live-YYYYMMDD-<sha>.iso` + `dakota-live-latest.iso` + checksums
11. **Boot verification** — QEMU UEFI boot, wait for `DAKOTA_LIVE_READY` serial marker
12. **Upload artifacts** — ISO + checksum + screenshot (7-day retention)

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
- `<target>-live-YYYYMMDD-<sha>.iso` — permanent dated record
- `<target>-live-latest.iso` — always points to the last successful build
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

---

## Lessons

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
