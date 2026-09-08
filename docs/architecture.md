# Architecture

How the Dakota live ISO is assembled and how it boots.

## Build pipeline

```
just iso-sd-boot dakota
  └─ just container dakota           → builds localhost/dakota-installer (live/Containerfile)
  └─ iso-sd-boot (assembly)          → populates VFS store, runs build-iso.sh on host

CI (build-iso.yml) — same entry point:
  └─ sudo just ... iso-sd-boot dakota → container build + VFS store + squashfs + ISO
```

### Container: `<target>-installer` (`live/Containerfile` — 3 stages)

| Stage | Base | Purpose |
|---|---|---|
| `dakota-ref` | Dakota image | Provides kernel modules |
| `initramfs-builder` | Debian | Builds dmsquash-live initramfs against Dakota's kernel modules |
| final | Dakota | Receives rebuilt initramfs + live-env setup + Flatpaks |

**Why Debian for initramfs?**
Dakota is GNOME OS / freedesktop-sdk based — no package manager, no dracut.
Building the initramfs in Debian's native environment avoids cross-distro binary grafting.
Only `/tmp/initramfs.img` crosses the stage boundary.

**What `configure-live.sh` does in the final stage:**
- Sets `VERSION_ID=latest` in `os-release` (GNOME OS omits it)
- Temporarily bind-mounts `/var/home` onto `/home` when it is safe to do so (avoids binding or unmounting when `/home` already resolves to `/var/home` or is an existing mountpoint), then creates `liveuser`
  (uid 1000, passwordless) so the home directory lands in the runtime-visible tree
- Configures GDM autologin for `liveuser`
- Installs and configures `org.bootcinstaller.Installer` Flatpak
- Sets up `live-ready.service` (writes `DAKOTA_LIVE_READY` to serial when GDM starts)
- Debug builds only: enables SSH, sets passwords, opens firewall port 22

### ISO assembly (`build-iso.sh` / `live/src/build-iso.sh`)

Runs on the host (not inside a container). Assembles the final ISO from the exported rootfs.

**Why host-side?** Host tools (xorriso, mksquashfs, mtools) avoid the overhead
of a build container and allow the justfile to control output paths directly.
xorriso available via brew at `/home/linuxbrew/.linuxbrew/bin/xorriso`.

**Multi-arch support:** Pass `--arch <arch>:<boot-tar>:<squashfs>` (repeatable) to produce
a single fat-ESP ISO with per-arch kernels, initramfs images, squashfs rootfs images, and both
`BOOTX64.EFI` + `BOOTAA64.EFI`. Single-arch positional-arg invocation is unchanged.
See `docs/multi-arch.md` for design rationale and size estimates.

## ISO layout

```
EFI/efi.img                  — FAT32 ESP: systemd-boot + kernel + initramfs
EFI/BOOT/BOOTX64.EFI        — EFI fallback (Proxmox OVMF / Ventoy)
LiveOS/squashfs.img          — squashfs of the full live rootfs (NVIDIA variant, with embedded VFS store)
boot/grub/loopback.cfg       — Ventoy/GRUB loopback metadata
images/pxeboot/*             — kernel/initramfs copies for loopback ISO boot
```

**No GRUB2, no shim.** El Torito UEFI → FAT ESP → systemd-boot → kernel + initramfs.

## Boot flow

```
UEFI firmware
  → El Torito (no-emulation) → FAT32 ESP
  → systemd-boot
  → kernel + initramfs (dmsquash-live)
  → scans for CDLABEL=DAKOTA_LIVE
  → mounts ISO → mounts squashfs → overlayfs (writable live env)
  → systemd → GDM autologin → GNOME session
  → org.bootcinstaller.Installer (Flatpak, auto-launched)
```

## GPT partition layout

The ISO uses a hybrid MBR+GPT layout.

**Correct GPT type:** `28732ac11ff8d211ba4b00a0c93ec93b`
This is the little-endian encoding of `C12A7328-F81F-11D2-BA4B-00A0C93EC93B` — the
EFI System Partition GUID. UEFI firmware scanning a dd'd USB finds and boots from this.

**Wrong type (old code):** `a2a0d0eb...` — Basic Data GUID. Strict UEFI firmware won't
recognize it as bootable. If you see this, rebuild with current `build-iso.sh`.

Verify:
```bash
xorriso -indev output/dakota-live.iso -report_system_area plain 2>/dev/null | grep 'GPT type GUID'
# Must show: 28732ac1...  (EFI System Partition OK)
```

Note: `fdisk -l` shows `Disklabel type: dos` on hybrid layouts — this is expected and
does NOT mean GPT is missing. `gdisk`, `parted`, and UEFI firmware see GPT correctly.

## Offline image store (VFS embedded in main squashfs)

The OCI image is baked directly into the main `squashfs.img` as VFS containers-storage
at `/var/lib/containers/storage`. The installer finds it there for offline installation
without a network pull. No separate `store.squashfs.img` is needed.

`scripts/build-live-squashfs.sh --oci-image <ref>` populates the store (used by both
`just iso-sd-boot` and CI). It squashes the image to a single layer via `buildah commit
--squash`, then runs `skopeo copy` **inside the installer container** so tar-split
metadata is written in JSON format (the live ISO expects JSON; build-host
containers-storage emits binary tar-split). The VFS staging dir is then copied into the
squashfs root via `cp -a` (not bind-mount — see CI lessons).

**Why VFS not overlay?**
- Overlay driver creates a conflicting `db.sql` file at first live boot
- VFS layers are plain directories — readable without mount privileges
- `driver = "vfs"` in `/etc/containers/storage.conf` set by `configure-live.sh`

**skopeo /var/tmp sizing:**
The squashed dakota-nvidia image has a ~9GB uncompressed layer. skopeo writes temp
blobs to `/var/tmp` directly (ignores `TMPDIR` set by fisherman). Live ISO sets
`/var/tmp` tmpfs to `size=80%` so it scales with machine RAM (16GB → 13GB available).

## Embedded OCI image (VFS containers-storage)

The squashfs embeds the Dakota OCI image as VFS containers-storage so the installer
can install offline without a network pull.

Requirements:
- `driver = "vfs"` in `/etc/containers/storage.conf` (set by `configure-live.sh`)
- skopeo copy runs **inside the installer container** (not the build host) to ensure
  tar-split metadata is in JSON format the live ISO expects. Build-host containers/storage
  emits binary tar-split; the installer image expects JSON.
- `fisherman` scratch dir: on live ISOs `/var` is a small RAM overlay. fisherman detects
  tmpfs `/var` and uses a self-bind-mounted scratch dir on the target disk.

## Installer: tuna-installer / bootc-installer

- **Flatpak:** `org.bootcinstaller.Installer` (stable) / `org.bootcinstaller.Installer.Devel` (dev)
- **Source:** `tuna-os/bootc-installer` (sole upstream; `projectbluefin/bootc-installer` and
  `projectbluefin/fisherman` are retired — no fallback repo)
- **Backend binary:** `fisherman` → symlinked to `/usr/local/bin/fisherman` by `configure-live.sh`
- **Config:** `/etc/bootc-installer/images.json` (catalog) + `recipe.json` (branding)
- **Flatpak sandbox:** Inside the Flatpak, `/etc` is reserved. Host `/etc` is at `/run/host/etc`.
  Recipe passed via `BOOTC_CUSTOM_RECIPE=/run/host/etc/bootc-installer/recipe.json`.
- **live-iso-mode:** `touch /etc/bootc-installer/live-iso-mode` activates live ISO mode
  in the installer.

## live-ready.service

Writes `DAKOTA_LIVE_READY` to the serial console after display-manager starts.
CI boot verification greps for this token. Service must use:

```ini
StandardOutput=tty
TTYPath=/dev/ttyS0          # direct serial (NOT journal+console → /dev/console)
WantedBy=multi-user.target  # NOT display-manager.service (non-standard → silent failures)
After=display-manager.service   # ordering only
```

## Bundled Flatpaks

Pre-installed into the live squashfs at build time. List in `live/src/flatpaks`.
The `install-flatpaks.sh` script uses `--mount=type=cache` to avoid re-downloading
on rebuilds. The cache is keyed by debug/production mode — switching busts the cache.

## Tests

Unit tests live in `tests/` and run via `pytest tests/ -v` (gated on every PR by `test.yml`).

| File | Tests | Coverage |
|---|---|---|
| `tests/test_luks_unlock.py` | 52 | `luks-unlock.py`: virsh/QEMU screenshot, serial parsing, passphrase injection |
| `tests/test_multi_arch_iso.py` | 4 | `build-iso.sh`: `--arch` flag arg parsing; single-arch + multi-arch ISO integration (skipped if tools absent) |

Run locally:
```bash
pip install pytest
pytest tests/ -v
```

---

## Lessons

### xorriso `-append_partition` vs `-boot_image isolinux partition_entry=gpt_basdat` (2026-05)

The old `build-iso.sh` used `partition_entry=gpt_basdat` which produces GPT type
`a2a0d0eb` (Basic Data). Strict UEFI firmware (bare-metal USB boot) won't recognize
this as an EFI System Partition and reports "no bootable device".

Fix: use `-append_partition 2 C12A7328-F81F-11D2-BA4B-00A0C93EC93B`.
Always verify with xorriso `--report_system_area` before shipping an ISO.

### VFS vs overlay driver for containers-storage (2026-05)

If `driver = "overlay"` is active in `/etc/containers/storage.conf`, the first bootc
operation creates a `db.sql` that conflicts with VFS metadata. The installer then fails
to find the embedded OCI image. `configure-live.sh` must explicitly set `driver = "vfs"`.

### nvidia_imgref auto-detection via bootc-installer (2026-06)

bootc-installer v2.6.1 adds `nvidia_imgref` support in `processor.py`.
When an `images.json` catalog entry has `nvidia_imgref`, the installer auto-detects
the GPU at install time:

- **NVIDIA GPU present**: installs + tracks `nvidia_imgref`
- **No NVIDIA GPU**: installs from ISO offline store (nvidia image), but writes
  `targetImgref = base imgref` into the installed system — so the first `bootc upgrade`
  rebases to the lighter non-nvidia variant automatically.

**Critical: `recipe.imgref` must be the BASE image (not nvidia)** for
`_find_nvidia_imgref_for()` to match the `images.json` entry by `imgref`.
`configure-live.sh` uses `BASE_IMGREF` / `NVIDIA_IMGREF` separately:
- `recipe["imgref"] = BASE_IMGREF` (matched by processor.py)
- `recipe["local_imgref"] = "containers-storage:{NVIDIA_IMGREF}"` (offline install source)

**Flatpak path bug in `image.py`** (fixed in projectbluefin/bootc-installer#183):
`_load_manifest()` was hardcoded to `/etc/bootc-installer/images.json`. Inside a
Flatpak sandbox, host `/etc` is at `/run/host/etc` — so the live ISO's custom
`images.json` was never loaded, falling back to the bundled GResource which has no
`nvidia_imgref`. The fix applies the same `/.flatpak-info` detection already used by
`RecipeLoader` in `recipe.py`. This PR must land and ship in a new Flatpak release
for NVIDIA auto-detection to work end-to-end.

**Storage savings from single-image store**:
`dakota:stable` is NOT needed in the offline store — it's only a tracking ref fetched
from GHCR on the first `bootc upgrade`. Storing only `dakota-nvidia:stable` saves
~2.2 GB per ISO (~7.8 GB → ~5.6 GB).

### flatpak install --bundle missing deploy/ ref in container builds (2026-06)

`flatpak install --bundle` in a rootless podman container build creates the
`installer-origin:` remote ref but does NOT create the `deploy/` ostree ref or the
`active → <hash>` symlink inside the branch dir. `flatpak run` / `flatpak list` require
both. The system flatpak daemon would normally create these — but it doesn't run inside
container builds.

**Fix:** Use `ostree init` + `flatpak build-import-bundle` + local `file://` remote +
`flatpak install` from that remote. Then scan all branch dirs and create
`active → <hash>` symlinks if missing.

```bash
INSTALLER_LOCAL_REPO="/tmp/installer-local-repo"
ostree init --repo="${INSTALLER_LOCAL_REPO}" --mode=archive-z2
flatpak build-import-bundle "${INSTALLER_LOCAL_REPO}" /tmp/installer.flatpak
flatpak remote-add --system --no-gpg-verify installer-local "file://${INSTALLER_LOCAL_REPO}"
flatpak install --system --noninteractive installer-local "${INSTALLER_APP_ID}"
flatpak remote-delete --system --force installer-local

# Create missing active symlink
for _branch_dir in /var/lib/flatpak/app/${INSTALLER_APP_ID}/x86_64/*/; do
    if [[ ! -L "${_branch_dir%/}/active" ]]; then
        _hash=$(find "${_branch_dir%/}" -maxdepth 1 -mindepth 1 -type d -printf '%f\n' | head -1)
        [[ -n "${_hash}" ]] && ln -sfn "${_hash}" "${_branch_dir%/}/active"
    fi
done
```

### bluefin-remove-installer.service whiteout files on live ISO (2026-06)

The installed system ships `bluefin-remove-installer.service` which uninstalls the
installer Flatpak on first boot. On a live ISO, this service runs and tries to
`flatpak uninstall`, fails with "Invalid cross-device link" (squashfs-backed overlayfs),
but leaves overlayfs whiteout entries (`c--------- 0,0`) that hide the `active` and
`current` symlinks — making the installer invisible to `flatpak run`/`flatpak list`.

**Fix:** systemd drop-in `ConditionPathExists=!/etc/bootc-installer/live-iso-mode`.
`configure-live.sh` touches `/etc/bootc-installer/live-iso-mode` so the service skips
on live ISOs. On installed systems the file is absent — service runs normally.

Drop-in path: `/etc/systemd/system/bluefin-remove-installer.service.d/live-skip.conf`

```ini
[Unit]
ConditionPathExists=!/etc/bootc-installer/live-iso-mode
```

### QEMU installed-disk boot: use qcow2, no cdrom (2026-06)

When verifying an installed system in QEMU (not the live ISO), two things are
required for OVMF to auto-discover the virtio-blk disk without EFI NVRAM entries:

- Install disk must be **qcow2** format (convert raw → qcow2 with `qemu-img convert` if needed)
- **No cdrom device** attached — OVMF tries cdrom first and may fail to enumerate virtio-blk

The `luks-boot-qemu-installed` justfile recipe follows this pattern. Boot the serial log
to confirm — GDM appearing in the log (`Started gdm.service`) is sufficient evidence.
The GTK window will show GNOME once GDM starts (no additional NVRAM/EFI configuration needed).

### LUKS E2E CI build pipeline (2026-06)

`test-luks-install.yml` originally used a 4-step manual pipeline:
`podman build` + `build-live-squashfs.sh` + `build-offline-store.sh` + `build-iso.sh --store`.
This was tied to the old superiso/overlay store pattern. After switching to the VFS
embedded store, use `just iso-sd-boot` directly (same as `build-iso.yml`):

```yaml
sudo just debug=1 installer_channel=${{ matrix.installer_channel }} \
  output_dir=/var/iso-build iso-sd-boot dakota
```

**Dev channel:** no rolling tag, no fallback repo. Both channels take
`tuna-os/bootc-installer`'s `/releases/latest/download/` redirect and differ only in
filename (`org.bootcinstaller.Installer.flatpak` vs `...Installer.Devel.flatpak`) — see
`TestInstallerChannelURLs` in `tests/test_live_build_invariants.py`.
