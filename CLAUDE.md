# Dakota ISO – Build Notes for AI Assistants

## Local build setup

### Background builds — use `just build-bg`

Running the build as a plain background job (`&`) will get killed by SIGHUP
when the shell session ends. Use the dedicated recipe instead:

```bash
just installer_channel=dev build-bg dakota
# Ctrl-C stops the log tail — build keeps running in background
# Check progress any time: tail -f output/build.log
```

The recipe uses `setsid ... & disown` internally so the build survives
terminal closure.

### ⚠️ Never build from /tmp

`/tmp` is a tmpfs with only ~16 GB. The build needs ~30 GB of intermediate space
(OCI tar ~4 GB, VFS storage ~10 GB, squashfs tree ~10 GB, final ISO ~4 GB).

**Always work from `/var/home/james/dev/dakota-iso/`** (or any path on `/var`,
which has 800 GB+ and is where the repo lives).  The default `output_dir=output`
resolves to `./output/` relative to the justfile, so it inherits whatever
filesystem the repo is on.

### Build command (local, no sudo)

```bash
cd /var/home/james/dev/dakota-iso
just debug=1 installer_channel=dev iso-sd-boot dakota
```

- **No `sudo`** — `podman unshare` only works for rootless podman (non-root user).
  Prefixing with `sudo` will fail with `please use unshare with rootless`.
- `debug=1` enables SSH (`ssh liveuser@<IP>`, password `live`) and the debug banner.
- `installer_channel=dev` uses the `continuous-dev` Flatpak release of tuna-installer
  which includes fixes not yet in the stable channel.

### CI build (GitHub Actions)

CI uses `sudo just installer_channel=dev output_dir=output iso-sd-boot dakota`
(runs as root). The justfile detects root via `id -u` and skips `podman unshare`,
running commands directly instead — so the same justfile works for both cases.

### Boot the ISO locally

```bash
# Quick QEMU serial test (validates GDM starts):
just debug=1 boot-iso-serial dakota

# Full libvirt VM with SSH access:
just debug=1 boot-libvirt-debug dakota
```

## Key architecture notes

- **composefs / VFS**: The ISO embeds Dakota as VFS containers-storage (not overlay)
  because squashfs is read-only. `configure-live.sh` sets `driver = "vfs"` so podman
  uses the pre-embedded image at boot.
- **Scratch dir**: `fisherman` detects tmpfs `/var` on live ISOs and uses a
  self-bind-mounted scratch dir on the target disk to avoid ENOSPC during OCI export.
- **Installer channel**: `dev` uses `org.bootcinstaller.Installer.Devel` app ID;
  `stable` uses `org.bootcinstaller.Installer`. Both can coexist.
- **Download URL**: https://download.tunaos.org/dakota/dakota-live-latest.iso
