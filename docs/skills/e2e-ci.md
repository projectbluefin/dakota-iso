# E2E CI — Plain Install Test

Skill for the plain composefs install E2E gate in `build-iso.yml`.

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
