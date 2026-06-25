---
name: e2e-ci
description: "E2E CI for dakota-iso plain composefs install gate. Load when debugging a boot failure, install failure, dracut emergency shell, ENOSPC during install, or sshd connectivity issues during QEMU testing. Use when adding a new E2E step or interpreting a CI failure."
metadata:
  type: reference
  context7-sources: []
---

# E2E CI — Plain Install Test

Skill for the plain composefs install E2E gate in `build-iso.yml`.

---

## When to Use

- Debugging a live ISO boot failure (dracut emergency shell, no rootfs layout)
- Diagnosing ENOSPC during skopeo copy or fisherman install
- Investigating sshd connectivity issues in QEMU E2E
- Adding or modifying a CI E2E step
- Interpreting a CI failure in `build-iso.yml` or `test-plain-install.yml`

## When NOT to Use

- For R2 promotion — use `docs/r2-promotion.md`
- For LUKS-specific failures — use `docs/luks-testing.md`
- For build-time container failures — use `docs/build.md`

---

## Architecture

The E2E gate splits into four named CI steps, each with its own timeout:

| Step | Timeout | RAM | Purpose |
|---|---|---|---|
| `E2E 1/4 — Boot live ISO` | 10 min | 4 GiB | Live env boots, sshd responds |
| `E2E 2/4 — ENOSPC gate: OCI export only` | 10 min | 4 GiB | skopeo copies blob without ENOSPC |
| `E2E 3/4 — Full install composefs` | 60 min | 8 GiB | btrfs+composefs install completes |
| `E2E 4/4 — Boot installed + verify Graphical` | 15 min | 8 GiB | Installed system reaches Graphical target |

**Why split RAM between steps:**
The ENOSPC bug triggers when overlay tmpfs is ~2 GiB (4 GiB RAM).
The `bootc install to-filesystem --composefs-backend` step extracts 6 GB via
btrfs+overlay through QEMU — inherently slow at 4 GiB, ~3× faster at 8 GiB.

---

## The debug ISO

The production ISO (uploaded to R2) has sshd **disabled** — it is built
with `DEBUG=0`. The E2E test uses a debug ISO that is patched at CI time:

```bash
unsquashfs production.rootfs.sfs → debug-rootfs/
# add sshd.service symlink, password auth, liveuser:live password
mksquashfs debug-rootfs → debug.rootfs.sfs (zstd-1, fast)
build-iso.sh → output/dakota-debug-live.iso
```

`plain-boot-qemu-live` prefers `output/{{target}}-debug-live.iso` when
present, so CI uses the debug ISO and R2 gets the production ISO.

**Never enable sshd in the production squashfs.** The debug ISO is test-only.

---

## QEMU disk: raw sparse + cache=unsafe

Use raw sparse disk instead of qcow2 for sequential write workloads:

```bash
truncate -s 64G /var/tmp/dakota-plain-install.img   # not qemu-img create -f qcow2
```

QEMU drive args:
```
-drive if=none,id=disk,file={{plain-qemu-disk}},format=raw,cache=unsafe
```

`cache=unsafe` skips fsync entirely. Fine for tests — not for production.
Raw sparse + cache=unsafe delivers 200–500 MB/s vs ~10–50 MB/s for qcow2.

---

## ENOSPC root cause and fix

**Symptom:** `write /var/tmp/container_images_XXXXXXXX: no space left on device`

**Root cause chain:**
1. `containers/image` TypeBigFiles path calls `store.TmpDir()` first
2. `store.TmpDir()` in containers/storage returns `/var/tmp` (hardcoded default)
3. `/var/tmp` is on the dracut overlayfs (~1.4 GiB at 4 GiB RAM)
4. A single squashed 5–6 GiB OCI layer blob overflows it

**Why `TMPDIR` env var doesn't help:**
`TypeBigFiles` checks `store.TmpDir()` BEFORE `os.Getenv("TMPDIR")`. If the
store returns a non-empty string, `TMPDIR` is ignored entirely.

**Why `CONTAINERS_STORAGE_CONF` with `tmpdir =` doesn't help:**
Older containers/storage versions don't have `tmpdir` as a recognized TOML
field — they silently reject it with `Failed to decode the keys ["storage.tmpdir"]`.

**The fix that works (fisherman v2.7.3):**
Bind-mount a disk-backed scratch subdir over `/var/tmp` before the skopeo copy,
then umount it in a deferred call:
```go
varTmpOverride := filepath.Join(tmpdir, "var-tmp-override")
os.MkdirAll(varTmpOverride, 0o1777)
exec.Command("mount", "--bind", varTmpOverride, "/var/tmp").Run()
defer exec.Command("umount", "/var/tmp").Run()
// now skopeo copy runs — /var/tmp is disk-backed
```
This works across all containers/storage versions and is independent of env
vars, config keys, or source transport.

---

## flatpak-spawn does not forward sandbox env to host

**Symptom:** fisherman sets `TMPDIR=/scratch` and prints `# TMPDIR=...` before
running skopeo, but blobs still land in `/var/tmp`.

**Root cause:** When running inside a Flatpak, `runner.HostArgs` wraps the
command as `flatpak-spawn --host skopeo ...`. `flatpak-spawn --host` spawns the
command in the host mount namespace but does NOT forward the Flatpak sandbox's
environment. `cmd.Env` applies to `flatpak-spawn` itself, not to `skopeo`.

**Fix:** Use `runner.HostArgsWithEnv(name, args, envVars)` which injects
`--env=KEY=VALUE` flags into the `flatpak-spawn` args when inside a Flatpak.
For non-Flatpak invocations the result is identical to `HostArgs`.

**How to identify:** `# TMPDIR=<path>` in fisherman output followed by
`write /var/tmp/container_images_...: no space left on device`. The TMPDIR
debug print confirms fisherman set the var; skopeo ignoring it confirms the
flatpak-spawn env forwarding gap.

---

## sshd is only enabled in debug ISOs

**Symptom:** `kex_exchange_identification: read: Connection reset by peer` OR
`ERROR: serial marker seen but SSH not ready after 90 s`

**Root cause:** `configure-live.sh` only enables sshd when `DEBUG=1` is passed
as a build arg. Production ISOs have no sshd. QEMU user-mode networking accepts
the TCP connection on port 2223 and then resets it because port 22 is not open
inside the guest.

**Fix:** Build a debug ISO (see above) for E2E testing. Never change the
production ISO to enable sshd.

---

## Serial marker fires before sshd is stable

**Symptom:** `Serial marker seen — polling SSH...` then sshd resets
connections for 10–20 s before accepting.

**Cause:** `live-ready.service` fires `After=display-manager.service`,
which is before sshd finishes host-key generation. First connection after
serial marker gets `kex_exchange_identification: read: Connection reset by peer`.

**Fix in `plain-boot-qemu-live`:** After seeing the serial marker, poll SSH
in a 3-second retry loop (up to 90 s) before breaking out of the wait loop.
This ensures sshd is stable before `plain-install-qemu` SSHes in.

---

## SUPERISO_TMPDIR — squashfs build must use large disk

**Symptom:** `Build live squashfs + boot tar` fails with ENOSPC.

**Root cause:** `build-live-squashfs.sh` creates its WORK dir at `/var/tmp`
by default. The squash-to-1-layer + VFS embedding writes ~12 GB of
intermediates; `/var/tmp` on ubuntu-24.04 CI runners has ~14 GB total.
If the image grows, this overflows.

**Fix:** Set `SUPERISO_TMPDIR=/var/iso-build` (119 GB) in the workflow env:
```yaml
- name: Build live squashfs + boot tar
  env:
    SUPERISO_COMPRESSION: release
    SUPERISO_TMPDIR: /var/iso-build
```

---

## fisherman hostname failure — fixed in v2.7.4

**Symptom (pre-v2.7.4):**
```
fisherman: fatal: writing hostname: finding deployment dir: ostree admin --print-current-dir: exit status 1
```

**Root cause:** `ostree admin --print-current-dir` reads booted-deployment
state from the running kernel — it always exits 1 against a freshly-installed
target that has never been booted. This hit the ostree branch of `WriteHostname`
in step 7 of the fisherman pipeline, crashing every real install.

**Why CI didn't catch it for weeks:**
- Unit tests mocked `DeploymentDirFn` so `DefaultDeploymentDir` was never called
- The e2e wrapper `scripts/fisherman-install.sh` silently caught the crash,
  re-mounted the disk, and patched `/etc/hostname` directly
- CI appeared green; the real installer on the ISO was broken

**Fix (fisherman v0.2.1 / bootc-installer v2.7.4):** `DefaultDeploymentDir`
now falls back to `filepath.Glob(sysroot/ostree/deploy/*/deploy/*)` when
`--print-current-dir` fails. Three regression tests lock this down.

**If you see this error on v2.7.4+:** It is a different failure — investigate
whether the target disk structure is correct after `bootc install to-filesystem`.

**`scripts/fisherman-install.sh` status:** Still present as a safety net but
no longer load-bearing. Do not add new workaround logic to it — fix the root
cause in fisherman instead.

### XFS path not tested — broken ISO shipped to R2 (2026-06-16)

**What failed:** `mkfs.xfs` was copied into the live container without its shared library
dependencies (`libinih.so.1`, `liburcu.so.8`). The binary existed in the image but
failed at runtime on every XFS install with:
```
mkfs.xfs: error while loading shared libraries: libinih.so.1
```

Also: `dakota-nvidia:stable` initramfs has `btrfs.ko` but **not** `xfs.ko`. An XFS
install would complete but the installed system drops to emergency mode on first boot
(`sysroot.mount` fails — cannot load XFS driver).

**Current state:** All variants (`dakota`, `bluefin`, `bluefin-lts-hwe`) default to
`filesystem: btrfs` in `images.json` and all E2E recipes. XFS is available only as a
user-selectable option in the installer UI. `plain-install-qemu` uses `btrfs`.

**Why CI didn't catch it initially:**
1. `plain-install-qemu` hardcoded `"filesystem": "btrfs"` — `mkfs.xfs` was never called in CI
2. The unit test (`TestXfsprogs`) only checked that the string `"mkfs.xfs"` appeared in the
   Containerfile — not that its deps were also present
3. No smoke test verified the binary executed inside the container before squashfs assembly

**The fix (commit `3606cfc`):**
- Copy shared lib deps `libinih.so.1` and `liburcu.so.8` alongside the binary in the Containerfile
- Do **not** copy `libblkid.so.1` or `libuuid.so.1` from Debian — the base image ships newer
  versions; overwriting with Debian's breaks `sfdisk` (see `docs/build.md` — libblkid lesson)
- `TestXfsprogs` now asserts all required libs are present in the COPY block
- `build-iso.yml` runs `mkfs.xfs -V` inside the live container after build

**Rule:** For every binary copied into a container, assert its shared library deps are also
present. Use `ldd <binary>` on the source stage to enumerate them.

**Rule:** The E2E filesystem must match the installer default. `images.json` sets `btrfs`
as default — E2E uses `btrfs`. If this ever changes, update the E2E recipe too.

---

## QEMU resources — use 8 GiB RAM / 8 vCPUs for local testing

**Symptom:** Local E2E installs take 30–60 min or time out.

**Root cause:** The composefs install (`skopeo copy` + `bootc install to-filesystem --composefs-backend`) is CPU and I/O bound. At 4 GiB / 4 vCPUs the flatpak copy phase alone takes 20+ minutes.

**Fix:** The `justfile` defaults are now `qemu-mem=8192` and `qemu-smp=8`. All `just plain-test-qemu` / `just luks-test-qemu` / `just e2e` recipes pick these up automatically. Override as needed:
```bash
just qemu-mem=16384 qemu-smp=16 plain-test-qemu dakota
```

Do not hardcode `-m 4096 -smp 4` in any new QEMU command — use `{{qemu-mem}}` / `{{qemu-smp}}` justfile variables.

---

## Live boot drops to dracut emergency shell — "no proper rootfs layout"

**Symptom:** QEMU boots ISO, dracut mounts squashfs, then immediately drops to `dracut:/#` with:
```
dracut Warning: /sysroot has no proper rootfs layout, ignoring and removing offending mount hook
```

**Root cause:** `dracut-lib.sh:usable_root()` accepts a root if either:
1. `$1/lib*/ld-*.so` glob matches (finds glibc dynamic linker), OR
2. All three of `$1/proc`, `$1/sys`, `$1/dev` exist

GNOME OS (glibc ≥2.38) ships `ld-linux-x86-64.so.2` — this does NOT match the `ld-*.so` glob. So path 2 is the only working path. The squashfs must have empty `sys/` and `dev/` at the root (alongside `proc/`).

**mksquashfs 4.7+ removes bare-excluded directories.** Using `-e sys` removes the `sys/` dir entirely. The fix — applied in `justfile` and `scripts/build-live-squashfs.sh`:
```bash
mkdir -p "${SFS_ROOT}/proc" "${SFS_ROOT}/sys" "${SFS_ROOT}/dev"
mksquashfs ... -wildcards -e "proc/*" -e "sys/*" -e "dev/*" ...
```

**Never use bare `-e sys -e dev -e proc`** on mksquashfs 4.7+. Always exclude with `-wildcards -e "dir/*"` to keep the empty directory.

---

## systemd ordering cycle deadlock (rechunker-group-fix) (2026-06-24)

**Symptom:**
Installed system boots, loops on waiting for device units (`dev-ttyS0.device`, `dev-zram0.device`, and `dev-disk-by-uuid-...device`), and drops to emergency mode after 90 seconds.

**Root cause:**
A circular systemd dependency (deadlock) exists on Universal Blue (LTS/stable) base images due to `rechunker-group-fix.service` running `Before=systemd-sysusers.service` without specifying `DefaultDependencies=no`. The full loop is:
`rechunker-group-fix` -> `local-fs.target` -> `boot.mount` -> device unit -> `udevd` -> `sysusers` -> `rechunker-group-fix`.
Depending on systemd's cycle breaking heuristics (e.g., whether `sysusers` or `tmpfiles-setup-dev` is deleted), this will either boot successfully (by luck) or deadlock (dropping to emergency shell).

**The fix:**
Write a systemd drop-in override during post-install (`scripts/fisherman-install.sh`) to set `DefaultDependencies=no` on `rechunker-group-fix.service`. This removes the default `After=local-fs.target` dependency, breaking the circular dependency loop.

---

## Red Flags

- Claiming "tests pass" without specifying which gate (`plain-e2e`, `luks-test-qemu`, or unit tests)
- Testing with a QEMU instance that predates the current build
- Using `-smp 4 -m 4096` in a QEMU command (too slow; use `{{qemu-smp}}` / `{{qemu-mem}}` justfile vars)
- Using bare `-e sys` or `-e dev` in mksquashfs (4.7+ removes the dir; use `-wildcards -e "sys/*"`)
- Declaring an install verified without booting the installed disk
- Using `installer_channel=dev` in CI or production builds (active fisherman regression)
- SSH connecting to a production ISO that has sshd disabled (build with `debug=1` for testing)

## Verification

Before marking any E2E work complete:

- [ ] Live ISO boots to `DAKOTA_LIVE_READY` on serial + SSH responds
- [ ] fisherman install exits `EXIT:0` in install.log
- [ ] Installed system boots to Graphical target (not just QEMU started)
- [ ] `just plain-test-qemu <target>` completed with `✅ Installed system boot verified`
- [ ] ISO size ~5.3 GB (release) or ~6.5 GB (fast) — not 8+ GB (double-embedded store)
- [ ] squashfs root contains `proc/`, `sys/`, `dev/` as empty dirs
- [ ] GPT type GUID = `28732ac1...` (EFI System Partition, not Basic Data)
