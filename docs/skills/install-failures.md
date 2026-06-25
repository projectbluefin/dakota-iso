---
name: install-failures
description: >
  Root causes for bluefin/dakota ISO install failures.
  Use when: ISO boots but installer fails, installed system does not boot,
  ENOSPC during install, bootloader missing after install, emergency shell on boot.
---

# Install Failures

## When to Use

Load this skill when:
- ISO boots live but installed system drops to emergency shell
- fisherman returns ENOSPC error during install
- Installed system shows no bootloader / UEFI PXE timeout
- Live ISO itself drops to emergency shell before the installer loads
- Diagnosing why a variant's install does not produce a bootable system

## When NOT to Use

- ISO build failures (wrong size, missing files) — see `docs/build.md`
- CI workflow issues — see `docs/ci.md`
- Dakota — it works. Do not touch it.

---

## STATUS (2026-06-21)

| Variant | Status |
|---|---|
| dakota | ✅ WORKS — verified plain-e2e-test3.log |
| bluefin | ⚠️ Fix applied (6f9ec1b), **not yet E2E verified** — run `just plain-test-qemu bluefin` |
| bluefin-lts-hwe | ❌ NOT TESTED — same fix applies, same command |

---

## Core Process

When an install fails:

1. Read the serial log (`plain-qemu-serial-installed`) for the first error line
2. Match the symptom below to the root cause
3. Apply the fix — do not invent new approaches

---

## Failure 1: ENOSPC during install (bluefin/lts-hwe)

**Symptom:**
```
no space left on device: /var/lib/containers/storage/vfs/dir/<id>/sysroot/...
```

**Root cause:**
fisherman non-composefs path runs `podman pull oci:/var/lib/containers/oci-store`
which imports the full ~9 GB image into VFS containers-storage on the live tmpfs → ENOSPC.

**Fix (applied in 6f9ec1b):**
`configure-live.sh` now sets `additionalImageStores: ["/var/lib/containers/oci-store"]`
in recipe.json for non-composefs variants. fisherman v0.2.0+ reads this field and
calls `appendImageStoreArgs()`, which writes a containers/storage config with
`driver = "overlay"` + `additionalimagestores = [...]` and passes it as
`CONTAINERS_STORAGE_CONF` into the bootc container. bootc finds the image via
additionalimagestores (read-only, no copy) — no ENOSPC.

**Requires rebuild** — configure-live.sh runs at container build time, not squashfs time.

**What NOT to do:**
- Do NOT change OCI layer squashing strategy — layers are irrelevant to this bug
- Do NOT add extra QEMU disks or scratch volumes to the test harness
- Do NOT file issues against fisherman — it already supports this field

---

## Failure 2: emergency shell on installed system boot (dakota, FIXED d974a1e)

**Symptom:** `dracut Warning: Refusing to install` or `Cannot mount root` in serial log.

**Root cause:** `scripts/build-live-squashfs.sh` COMPOSEFS_BACKEND detection used
`sh -c 'python3 -c "..."'` — nested double-quotes broke the python3 invocation →
always returned non-zero → dakota embedded as OCI layout instead of VFS containers-storage →
fisherman couldn't find image in containers-storage → pulled uninjected image from network →
missing `root-mount-spec = "LABEL=root"` → wrong `root=` in BLS entry → initramfs panic.

**Fix:** `python3 -c '...'` directly (no `sh -c` wrapper). Committed d974a1e. Verified.

---

## Failure 3: live ISO drops to emergency shell (FIXED d974a1e)

**Symptom:** dracut error before installer ever appears.

**Root cause:** CI debug ISO rebuild ran `mksquashfs ... -e sys -e dev` with `-wildcards`
active. This removes the `sys/` and `dev/` directory nodes entirely. dmsquash-live-root.sh
requires these directories to exist in the squashfs root.

**Fix:** `mkdir -p sys/ dev/` before mksquashfs; use `-e "sys/*" -e "dev/*"`. Committed d974a1e.

---

## Failure 4: no bootloader after install / UEFI PXE timeout

**Symptom:** installed QEMU shows UEFI PXE timeout; `systemd-bootx64.efi not found` in log.

**Root cause:** `installer_channel=dev` fisherman ignores `bootloader: grub2` in recipe.json
and auto-detects systemd-boot. bluefin uses grub2; `systemd-bootx64.efi` is absent.

**Fix:** Use `installer_channel=stable` for bluefin/lts-hwe. Never use dev channel for
grub2 variants.

---

## Variant configuration reference

| Variant | bootloader | composeFsBackend | image in recipe.json | additionalImageStores |
|---|---|---|---|---|
| dakota | systemd | true | `containers-storage:ghcr.io/projectbluefin/dakota-nvidia:stable` | (none) |
| bluefin | grub2 | false | `oci:/var/lib/containers/oci-store` | `["/var/lib/containers/oci-store"]` |
| bluefin-lts-hwe | grub2 | false | `oci:/var/lib/containers/oci-store` | `["/var/lib/containers/oci-store"]` |

Config files (read by `configure-live.sh` at container build time):
- `live/src/<variant>/composefs` — "true" or "false"
- `live/src/<variant>/bootloader` — "grub" (normalized to "grub2") or "systemd"

All variants: filesystem=btrfs. XFS is a UI option only, never the default.

---

## How fisherman uses additionalImageStores

Source: `tuna-os/fisherman` v0.2.0, `fisherman/internal/install/bootc.go`

`appendImageStoreArgs()` is called when `NeedsContainerStorageMount(opts)` is true
(i.e., `!ComposeFsBackend`). If `opts.AdditionalImageStores` is non-empty:
1. Writes `scratchDir/fisherman-conf/storage-*.conf`:
   ```toml
   [storage]
   driver = "overlay"
   [storage.options]
   additionalimagestores = ["<path>"]
   ```
2. Bind-mounts each store path read-only into the container at the same host path
3. Sets `CONTAINERS_STORAGE_CONF` env var in the container

fisherman reads `additionalImageStores` from recipe.json into `opts.AdditionalImageStores`.
No code changes to fisherman needed — this is pure configuration.

---

## Red Flags

- Any agent spending time on "layer count" or "squash strategy" for the ENOSPC bug — the problem is tmpfs, not layers
- Adding scratch disks or workarounds to the QEMU test harness — the fix is in recipe.json
- Testing bluefin with `installer_channel=dev` — always use stable for grub2 variants
- Assuming dakota is broken when it isn't — only bluefin and lts-hwe have open issues

---

## Verification

- [ ] `just plain-test-qemu bluefin` exits with `✅ Installed system boot verified`
- [ ] `just plain-test-qemu bluefin-lts-hwe` exits with `✅ Installed system boot verified`
- [ ] dakota still passes (do not touch it)
- [ ] ISO built after 6f9ec1b (configure-live.sh change requires rebuild)
