---
id: install-failures
name: Install Failures
one_line_purpose: Root cause analysis and remedies for live ISO install failures.
entry_point: docs/skills/install-failures.md
category: test-authoring
status: active
tags:
  - install
  - failures
  - troubleshooting
  - fisherman
description: Known failure modes for Dakota and Bluefin ISO installations, including ENOSPC, emergency shells, and bootloader missing.
version: "1.1"
last_updated: "2026-08-01"
metadata:
  type: reference
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

## Failure 5: install dies at 98% with "error writing hostname" (2026-08-01)

**Symptom:** GUI installer shows

> Installation failed
> Error: writing hostname: write /mnt/fisherman-target/state/deploy/&lt;hash&gt;/etc/hostname

**The hostname write is not the bug.** It is the first *fatal* write after the target
disk filled up. The real first symptom is a few lines earlier in the installer log:

```
tar: Exiting with failure status due to previous errors
{"message":"Warning: could not copy flatpaks: tar extract: exit status 2"}
```

**Root cause:** on live ISOs fisherman puts its scratch dir (extracted OCI blobs,
several GB — dakota:stable is 3.1 GB compressed / 120 layers) on the **target disk**,
because live `/var` is a space-constrained tmpfs/overlay. It was registered only as a
cleanup *post-removal*, so it survived on the target through every post-install step.
Steps 7–8 (Flatpak copy, hostname, fstab) then wrote into a nearly-full filesystem.
`tar` reports ENOSPC only on stderr, so the Flatpak failure arrived as a bare
`exit status 2`, got downgraded to a warning, and masked the real cause.

**Fix:** [projectbluefin/fisherman#15](https://github.com/projectbluefin/fisherman/pull/15) —
`Cleanup.ReleaseScratch()` unmounts and deletes the cache immediately after
`bootc install` returns; `post.IsNoSpace()` makes a full disk abort with
`target disk is full — /dev/… is too small for this image`.

**Rule of thumb:** any install failure whose message is a *write to the target* should
be triaged as ENOSPC first. Check the whole log for an earlier swallowed warning before
chasing the reported step.

**Minimum disk size:** budget ≥ 40 GB for a dakota install. 25 GB reproduces this
failure even with the fix in place, because the deployed image alone is ~9 GB and
Flatpaks add several more.

### How a fisherman fix reaches a dakota ISO

Fixing fisherman is not shipping it. The chain has three hops, and each one can be
stale independently:

```
projectbluefin/fisherman  main
        │  git submodule  (bootc-installer/fisherman, tracks branch `dev`)
        ▼
projectbluefin/bootc-installer  →  org.bootcinstaller.Installer flatpak
        │  configure-live.sh installs the flatpak into the live squashfs
        ▼
dakota-iso  →  dakota-live-latest.iso
```

Check where a given fix actually is before telling anyone it is fixed:

```bash
# What fisherman commit does the shipped installer build from?
gh api repos/projectbluefin/bootc-installer/contents/fisherman -q .sha

# How far behind fisherman main is that pin?
gh api repos/projectbluefin/fisherman/compare/<pin>...main -q '{ahead:.ahead_by,behind:.behind_by}'
```

As of 2026-08-01 that pin was a 2026-06-23 commit — 27 behind `main`, and diverged.
Note also that fisherman's *default* branch is `dev` while the active line is `main`
(`main` was 25 ahead of `dev`), so a PR opened with the default base can land on the
branch nobody ships. **Target `main`, then check the submodule pin.**

Bumping the submodule pulls in every unrelated installer change since the last bump,
so it is a maintainer decision — see [`human-gates.md`](human-gates.md), Design/Breakage.

---

## Reading an installer log out of a running VM without SSH (2026-08-01)

Production ISOs have SSH disabled (`debug=1` is required for E2E — see
[`qa-policy.md`](qa-policy.md)), so when a user reports a failure from an interactive
virt-manager session there is no shell. Drive the GUI through libvirt instead — no
root, no guest agent, no SSH needed:

```bash
VS='flatpak run --filesystem=/tmp --command=virsh org.virt_manager.virt-manager -c qemu:///session'

# 1. Screenshot the current screen
$VS screenshot <domain> /tmp/shot.png          # writes PNG despite the .ppm convention

# 2. Synthetic mouse — usb-tablet gives absolute coords in 0..32767
#    x = px * 32767 / width, y = px * 32767 / height
$VS qemu-monitor-command <domain> '{"execute":"input-send-event","arguments":{"events":[
  {"type":"abs","data":{"axis":"x","value":13058}},
  {"type":"abs","data":{"axis":"y","value":16097}},
  {"type":"btn","data":{"down":true,"button":"left"}},
  {"type":"btn","data":{"down":false,"button":"left"}}]}}'
```

Gotchas learned the hard way:
- `virsh send-key` did **not** reach the guest; QMP `input-send-event` did. Use QMP.
- SPICE clipboard sync does not work headlessly, so the log dialog's copy button is
  useless to an agent — read the text off screenshots.
- Wheel-scrolling a 10k-line log is hopeless. **Drag the scrollbar thumb** (press at
  the right edge, move to the bottom of the trough, release) to jump straight to the
  tail, which is where the fatal error is.
- The log dialog is a fixed-size `AdwDialog`: it cannot be maximised and long lines
  are clipped, not wrapped. Drag the *window* left to reveal more of the right-hand
  side of each line.

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
