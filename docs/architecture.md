# Architecture

How the Dakota live ISO is assembled and how it boots.

## Two-container pipeline

```
just iso-sd-boot dakota
  └─ just container dakota          → builds localhost/dakota-installer
  └─ just iso-sd-boot (assembly)    → runs build-iso.sh on host
```

### Container 1: `dakota-installer` (Containerfile — 3 stages)

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
- Creates `liveuser` (uid 1000, passwordless)
- Configures GDM autologin for `liveuser`
- Installs and configures `org.bootcinstaller.Installer` Flatpak
- Sets up `live-ready.service` (writes `DAKOTA_LIVE_READY` to serial when GDM starts)
- Debug builds only: enables SSH, sets passwords, opens firewall port 22

### Container 2: ISO assembly (`build-iso.sh`)

Runs on the host (not inside a container). Assembles the final ISO from the exported rootfs.

**Why host-side?** Host tools (xorriso, mksquashfs, mtools) avoid the overhead
of a build container and allow the justfile to control output paths directly.
xorriso available via brew at `/home/linuxbrew/.linuxbrew/bin/xorriso`.

## ISO layout

```
EFI/efi.img              — FAT32 ESP: systemd-boot + kernel + initramfs
EFI/BOOT/BOOTX64.EFI    — EFI fallback (Proxmox OVMF / Ventoy)
LiveOS/squashfs.img      — squashfs of the full live rootfs (+ embedded OCI)
boot/grub/loopback.cfg   — Ventoy/GRUB loopback metadata
images/pxeboot/*         — kernel/initramfs copies for loopback ISO boot
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
- **Source:** `projectbluefin/bootc-installer` (primary), `tuna-os/tuna-installer` (fallback)
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

Pre-installed into the live squashfs at build time. List in `dakota/src/flatpaks`.
The `install-flatpaks.sh` script uses `--mount=type=cache` to avoid re-downloading
on rebuilds. The cache is keyed by debug/production mode — switching busts the cache.

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
