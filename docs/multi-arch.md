# Multi-Arch ISO

This repo builds **x86_64 ISOs only**. There is no aarch64 build here and no
multi-arch (single ISO, both architectures) build.

- `live/Containerfile` copies x86_64-only library paths
  (`/usr/lib/x86_64-linux-gnu/`) and the installer Flatpak is pinned to
  `--arch=x86_64`, so the live environment cannot be built for another arch.
- The aarch64 image source this was designed around (`tuna-os/dakota-x13s`)
  no longer exists.
- `live/src/build-iso.sh` used to carry an unused `--arch` multi-arch mode.
  It was removed in #289 (decision in #200) because nothing produced its
  inputs; it is recoverable from git history if an aarch64 producer appears.

What remains arch-aware in `build-iso.sh`, and costs nothing to keep: it picks
`BOOTAA64.EFI` or `BOOTX64.EFI` from whatever boot tar it is given, and the
kernel cmdline lists both serial consoles (`ttyS0` and `ttyAMA0`).

Reviving aarch64 means first building an aarch64 live container (a
`live/Containerfile` that does not hardcode x86_64 paths) from a published
aarch64 Dakota image, then adding a per-arch ISO job. Per-arch ISOs are
preferred over a combined ISO, which would roughly double the download size.
