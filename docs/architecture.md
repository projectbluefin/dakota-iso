# Architecture

How the Dakota live ISO is assembled and how it boots.

## Build pipeline

```
just iso-sd-boot dakota
  └─ just container dakota           → builds localhost/dakota-installer (live/Containerfile)
  └─ iso-sd-boot (assembly)          → embeds bound OCI layout, runs build-iso.sh on host

CI (build-iso.yml / build-iso-utah.yml) uses the same payload-oci.sh producer:
  └─ build-live-squashfs.sh → one OCI layout + media receipt + squashfs + ISO
```

### Container: `<target>-installer` (`live/Containerfile` — 4 stages)

| Stage | Base | Purpose |
|---|---|---|
| `ref` | Target image | Provides kernel modules |
| `initramfs-native` | `ref` | Builds the dmsquash-live + dakota-isofile (Ventoy/file-backed boot) initramfs with the target's own dracut when it has one (Fedora/RPM images); otherwise records a fallback to the Debian stage |
| `initramfs-builder` | Debian | Cross-builds the dmsquash-live + dakota-isofile initramfs against the target's kernel modules when the native stage fell back; otherwise passes the native initramfs through |
| final | Target image | Receives the selected initramfs + live-env setup + Flatpaks |

`tests/test_containerfile_stage_inventory.py` keeps this table in step with
the `FROM ... AS <name>` lines in `live/Containerfile`.

**Why Debian for initramfs?**
Dakota is GNOME OS / freedesktop-sdk based — no package manager, no dracut.
Building the initramfs in Debian's native environment avoids cross-distro binary grafting.
Only `/tmp/initramfs.img` crosses the stage boundary.
The Debian base is pinned by `@sha256` digest (Renovate bumps it), so a retagged
`debian:bookworm` cannot change what goes into the signed ISO unreviewed;
`tests/test_containerfile_pinned_bases.py` enforces this for every external base.

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

## ISO layout

```
EFI/efi.img                  — FAT32 ESP: systemd-boot + kernel + initramfs
EFI/BOOT/BOOTX64.EFI        — EFI fallback (Proxmox OVMF / Ventoy)
LiveOS/squashfs.img          — full live rootfs plus one embedded OCI layout
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

## Bound offline OCI media

Both build entry points source `scripts/payload-oci.sh`. They prepare the payload
with `payload_to_oci_archive`, then `payload_embed_oci_archive <archive> <root>`
extracts those exact bytes once under `/usr/share/bootc-installer/oci` and writes
`/etc/bootc-installer/media-input.json`. The temporary archive is deleted; there
is no VFS/overlay payload import, additional image store, or runtime OCI export.
Composefs payloads retain the existing single-layer squash and diff-id annotation;
Utah and retained non-composefs variants keep all ostree commit layers.

The schema-v1 receipt binds registry source identity (`image`) separately from
the installed update ref (`targetImgref`), plus the actual platform manifest and
config SHA-256 digests and config-derived `platform`. The producer verifies every
descriptor's size and hash. Fisherman v0.5.3 independently streams and validates
all blobs before disk mutation, including per-file read-only and no-symlink checks.

The layout path is **`/run/rootfsbase/usr/share/bootc-installer/oci`**, not the
writable live `/usr` overlay. dracut's `90dmsquash-live/dmsquash-live-root.sh`
(059, also the bookworm 057 path) selects `FSIMG=$SQUASHED` for a flat squashfs
containing `proc/`, and uses `mkdir /run/rootfsbase; mount -r $FSIMG /run/rootfsbase`
for the overlayfs lower root. This is a real read-only mount, not a path alias.

`live-media-setup.service` binds the receipt target before display-manager and
`live-ready.service`. `bind-media-target.py` follows the GUI's PCI display-class
NVIDIA detection and catalog mapping: NVIDIA hardware tracks the NVIDIA ref;
other hardware tracks the base ref. Recipe `imgref`/`targetImgref` agree with the
receipt; `local_imgref` stays the embedded registry source. CLI gates read the
same receipt, never guess from podman's cache, never fall back to a network pull,
and always use the shipped pinned fisherman binary.

The `live-iso-mode` marker remains mandatory. Missing, writable, corrupt, aliased,
wrong-platform or source/target-mismatched media must refuse installation.
The low-RAM gate invokes the real consumer with `/never-a-device` as the disk;
only the exact invalid-disk refusal after successful media validation passes.
Full plain/LUKS installation and installed-system boot remain separate proofs.

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
| `tests/test_luks_unlock.py` | 67 | `luks-unlock.py`: virsh/QEMU screenshot, serial parsing, passphrase injection, unlock/boot verdicts |
| `tests/test_build_iso_assembly.py` | 2 | `live/src/build-iso.sh`: real ISO assembly from a mock boot tar + squashfs; unknown flags rejected (skipped if tools absent) |
| `tests/test_iso_sd_boot.py` | 8 | `iso-sd-boot.sh`: arg/env option plumbing into `just` container invocation, composefs true/false modes, OCI payload embed path + storage driver, compression tiers |

Run locally:
```bash
pip install pytest
pytest tests/ -v
```

---

## Lessons

### Bound media producer/consumer cutover (2026-11-06)

The following VFS/tar-split lessons describe the superseded containers-storage
implementation. Do not restore its runtime export or double-embed a payload to
work around missing `media-input.json`; use the bound media contract above.


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
- `recipe.local_imgref` is the embedded registry identity bound to the OCI receipt.

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

**Dev channel:** no rolling tag, no fallback repo. Both channels download from the
`tuna-os/bootc-installer` release tag pinned in `live/src/installer-bundle.lock` and differ
only in filename (`org.bootcinstaller.Installer.flatpak` vs `...Installer.Devel.flatpak`);
the bundle's SHA-256 must match the lock — see `TestInstallerChannelURLs` in
`tests/test_live_build_invariants.py`. Bump with `scripts/bump-installer-bundle.sh`.
