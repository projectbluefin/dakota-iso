# Dakota ISO – Build Notes for AI Assistants

Builds bootable UEFI live ISOs from Dakota/GNOME OS bootc images.
Variants: `dakota`, `dakota-nvidia` (payload ref in `<variant>/payload_ref`).

> ⚠️ This repo's remote is `projectbluefin/dakota-iso` (upstream). Pushes go to upstream.
> If working from a castrojo fork, push to `castrojo/dakota-iso` only.

## Essential commands

```bash
just iso-sd-boot dakota                              # full build
just debug=1 installer_channel=dev iso-sd-boot dakota   # debug build (SSH enabled)
just build-bg dakota                                 # background build (survives terminal close)
just boot-iso-serial dakota                          # QEMU serial boot test (Ctrl-A X to quit)
just e2e dakota                                      # build ISO + LUKS end-to-end test
```

## Non-obvious constraints

### ⛔ Never build from /tmp
`/tmp` is 16 GB tmpfs. The build needs ~22 GB. Always use `/var` or a path with ≥25 GB free.

### ⛔ No `sudo` for local builds
`podman unshare` is rootless-only. Prefixing with `sudo` fails with `please use unshare with rootless`.
CI runs as root and the justfile adapts automatically.

### ⛔ Never use `installer_channel=dev` in production
Fisherman dev channel has a regression (`open /var/tmp/oci-cache/index.json: no such file or directory`).
Use `installer_channel=stable` (the default). Track tuna-os/fisherman#38 for the fix.

### Squash to 1 layer before VFS import
Dakota has ~120 OCI layers. Without squashing → ~720 GB disk. The justfile squashes to 1 layer
(peak ~22 GB). Uses `buildah commit --squash` — NOT `podman create --entrypoint /bin/sh && podman commit`
(that corrupts the Entrypoint and causes "cannot execute binary file").

### GPT partition type matters for bare-metal boot
The GPT EFI partition MUST be type `C12A7328-F81F-11D2-BA4B-00A0C93EC93B` (shown as `28732ac1...` in xorriso).
If `a2a0d0eb...` (Basic Data) appears, the old code is active — rebuild with current `build-iso.sh`.

### ⛔ Interactive QEMU testing rules
- Always use `-display gtk,zoom-to-fit=on` (never `-display none` unless headless CI)
- Always create a 50GB install disk before launching QEMU
- Always use `/var/tmp/` for disk images and OVMF VARS — never `/tmp`
- Pass the ISO path directly — never create symlinks in `output/`

### BTRFS host? Use XFS loopback
```bash
sudo just mount-xfs                               # 45GB XFS at /mnt (idempotent)
sudo chown $USER:$USER /mnt
just workdir=/mnt iso-sd-boot dakota
```

### Immutable OS workarounds
buildah is unavailable on the host. Use the containerized buildah wrapper at `~/.local/bin/buildah`.
QEMU: `/home/linuxbrew/.linuxbrew/bin/qemu-system-x86_64` (v11.0.0).
OVMF: `/home/linuxbrew/.linuxbrew/Cellar/qemu/11.0.0/share/qemu/edk2-x86_64-code.fd`.
No VARS file in brew — create with `dd if=/dev/zero bs=1k count=256 of=/var/tmp/ovmf-vars.fd`.

### VFS containers-storage for offline install
`/etc/containers/storage.conf` must set `driver = "vfs"`. Overlay driver creates conflicting `db.sql`.
skopeo copy must run inside the installer container (not build host) for correct tar-split JSON format.
Fisherman detects tmpfs `/var` and auto-bind-mounts a scratch dir on the target disk.

### Compression presets
- `just compression=fast iso-sd-boot dakota` — default: zstd lvl 3, 128K blocks
- `just compression=release iso-sd-boot dakota` — zstd lvl 15, 1M blocks (~20% smaller, ~5× slower)
Use `release` for production ISOs to R2. Use `fast` for local/CI.

## R2 bucket management

R2 bucket: `testing`. All operations use `rclone` (`~/.config/rclone/rclone.conf`).

⚠️ `no_check_bucket = true` is required in rclone config — without it, CopyObject hangs on large files.

⚠️ Direct uploads from this host hang/fail (routing issue). Always use R2→R2 server-side copies:
```bash
rclone copyto -v R2:testing/dakota-live-YYYYMMDD-<sha>.iso R2:testing/dakota-live-latest.iso
```

## Don'ts
- Don't use `installer_channel=dev` in CI/production (fisherman regression)
- Don't rely on host sudo in automated sessions (different TTY, `sudo -v` doesn't carry over)
- Don't create symlinks in `output/` to satisfy the Justfile — pass real paths
- Don't use `/tmp` for anything build-related

## Reference docs
- `just --list` — all recipes
- `dakota/src/` — build scripts and Containerfiles
- `docs/` — deeper troubleshooting and LUKS testing procedures
- CI: `.github/workflows/build-iso.yml`, `.github/workflows/test-luks-install.yml`
