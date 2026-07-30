---
id: qa-policy
name: QA Policy
one_line_purpose: Rules for test freshness, artifact isolation, and E2E verification evidence.
entry_point: docs/skills/qa-policy.md
category: test-authoring
status: active
tags:
  - qa
  - policy
  - testing
  - verification
description: Mandatory rules for artifact freshness, test isolation, E2E verification, and proof of working builds for dakota-iso.
version: "1.0"
last_updated: "2026-07-30"
metadata:
  type: policy
---

# QA Policy — Dakota ISO

## The cardinal rule

**Always test fresh artifacts.**

Never interact with a running QEMU instance, an existing install disk, or a previously-built ISO unless you just created it in this session and have the build log to prove it. Stale artifacts produce false signals — a 12-hour-old QEMU tells you nothing about whether the current code works.

---

## What "fresh" means

| Artifact | Stale if... | Correct action |
|---|---|---|
| ISO (`output/*.iso`) | Built in a prior session or by a different command | Rebuild with `just debug=1 iso-sd-boot <target>` |
| Install disk (`/var/tmp/dakota-plain-install.img`) | Older than the current test run | `rm -f /var/tmp/dakota-plain-*.img` before every test |
| Running QEMU process | PID predates the current test sequence | `pkill -f 'qemu.*dakota'` before starting |
| OVMF vars (`/var/tmp/*-vars.fd`) | From a prior boot | Deleted automatically by `plain-boot-qemu-live` via `rm -f` |

---

## Mandatory pre-test checklist

Run these before every E2E test, without exception:

```bash
# 1. Kill any stale QEMU processes
pkill -f 'qemu.*dakota' 2>/dev/null || true

# 2. Remove stale install disks
rm -f /var/tmp/dakota-plain-install.img \
      /var/tmp/dakota-plain-scratch.img \
      /var/tmp/dakota-plain-qemu-live-vars.fd \
      /var/tmp/dakota-plain-qemu-installed-vars.fd

# 3. Verify nothing is still bound to the SSH port
ss -tlnp | grep 222

# 4. Build a fresh ISO (debug=1 is required for E2E — SSH is disabled otherwise)
just debug=1 iso-sd-boot dakota

# 5. Run the full E2E (or test-only if the ISO was just built above)
just plain-test-qemu dakota
```

Or use the single all-in-one command that does steps 4+5:

```bash
just debug=1 plain-e2e dakota
```

---

## What counts as proof

Per the AGENTS.md verification requirements — **do not claim the build works unless all three pass:**

| Gate | What it proves | Command |
|---|---|---|
| Unit tests | Source-file invariants, Python logic | `just test` |
| Live environment boots | initramfs, dmsquash-live, SSH | `plain-boot-qemu-live` step |
| **Install completes and boots** | Partitioning, fisherman, composefs, ostree | `just debug=1 plain-e2e dakota` |

"ISO booted" alone is **not** proof. The live session boot proves the initramfs works. Only a completed install and a successful boot of the installed disk proves fisherman, partitioning, and post-install steps work.

---

## ISO size invariant

| Size | Meaning |
|---|---|
| ~5.3 GB | ✅ Release compression — correct |
| ~4.7 GB | ⚠️ Acceptable (dev build or smaller payload) — verify install works |
| ~6–7 GB | ❌ Fast compression (`zstd-3`) — use `release` for R2 |
| ~8 GB | ❌ Double-embedded OCI store — do not pass `--store` to `build-iso.sh` |
| ~4.4 GB + install fails | ❌ VFS store missing — rebuild with `--oci-image <ref>` |

The build log prints the size as `du -sh` (allocated blocks). The real size is `stat -c%s`. On XFS with sparse files these can diverge significantly — use `stat` for the definitive number.

---

## debug=1 is required for E2E

SSH into the live environment is only enabled when the ISO is built with `debug=1`. Without it, the live environment boots but `sshd` never starts on TCP. Every E2E test will time out waiting for SSH.

```bash
# ❌ Wrong — SSH disabled, E2E will always time out
just iso-sd-boot dakota
just plain-test-qemu dakota

# ✅ Correct — SSH enabled, E2E can proceed
just debug=1 plain-e2e dakota
```

This is not optional. `debug=1` is safe for local testing. Never use `debug=1` for R2-published ISOs.

---

## Test isolation rules

Adapted from [addyosmani/agent-skills — test-driven-development](https://github.com/addyosmani/agent-skills):

1. **Each test run starts from a known-clean state.** Previous run's artifacts are explicitly removed before the new run starts — not reused.
2. **Never reuse a running QEMU from a previous session.** Even if it looks healthy, it was not started from the artifact you are testing.
3. **Never SSH into a running VM you didn't start.** The VM's state reflects whatever ran before, not the current codebase.
4. **Shared mutable state (install disk, OVMF vars) is always reset.** `/var/tmp/dakota-plain-*.img` is deleted before every E2E run. The justfile's `plain-boot-qemu-live` recipe creates fresh disks automatically.
5. **Build logs are evidence.** If you cannot produce a build log from this session, the artifact is stale. Rebuild.

---

## Diagnosing E2E failures

### SSH timeout ("Timeout waiting for live environment")

Most likely causes, in order:
1. ISO was built without `debug=1` — rebuild
2. `sudo` unavailable and serial log read was using `sudo cat` — fixed in justfile, pull latest
3. The live environment took longer than the timeout — rare; retry with same ISO

### `sysroot.mount` fails → emergency mode

The installed system's initramfs is missing the kernel module for the root filesystem:

```bash
# Extract and inspect the installed initramfs
mcopy -i /tmp/efi-inspect.img "::/EFI/Linux/<hash>/initrd" /tmp/initrd.img
# Find the real cpio (after microcode section) and check for xfs.ko / btrfs.ko
```

See [#100](https://github.com/projectbluefin/dakota-iso/issues/100) for the XFS case.

### ISO size unexpectedly large

See the size invariant table above and [docs/ci.md](../ci.md) for the double-embedded store lesson.

---

## Lessons learned (2026-06-16)

### Always test fresh artifacts (2026-06-16)

**What happened:** Agent attempted to SSH into a QEMU that had been running for 12 hours with a 15-hour-old install disk, treating it as a valid test target.

**Why it's wrong:** The stale QEMU reflected the state of code from a previous session. Any result — pass or fail — would have been a false signal about the current codebase.

**The rule:** Before any E2E test, run the pre-test checklist above. No exceptions. The cost of a fresh build (~10 min) is always lower than the cost of shipping a broken ISO.

### debug=1 is mandatory for E2E SSH (2026-06-16)

**What happened:** ISO built without `debug=1`. SSH is gated behind the debug flag. The E2E timed out waiting for SSH every time.

**The rule:** Use `just debug=1 plain-e2e dakota`. Never `just plain-test-qemu` against a non-debug ISO.

### sudo cat on user-owned serial logs breaks non-root E2E (2026-06-16)

**What happened:** The justfile used `sudo cat` and `sudo socat` to read serial logs and send monitor commands. When QEMU runs without sudo (KVM accessible to user), these files are user-owned. `sudo cat` fails without a TTY, silently breaking the serial-marker detection logic and causing the E2E to always time out.

**The fix:** Removed all `sudo cat`, `sudo grep`, and `sudo socat` calls on serial logs and monitor sockets. The files are always owned by whoever ran QEMU.

### permission denied on root-owned monitor sockets (2026-06-23)

**What happened:** On systems where the runner user does not have direct read/write access to `/dev/kvm`, QEMU is run with `sudo`. This makes the generated QEMU monitor socket files (e.g. `/tmp/dakota-qemu-live.sock`) owned by root. When the unprivileged runner user ran raw `socat`, it silently failed to connect due to permission denied. The live QEMU instance was never powered down/quit, causing subsequent stages to fail with `Failed to get "write" lock` as the QEMU process still held the disk image lock.

**The fix:** Use a conditional `$SOCAT_PREFIX` that is set to `sudo` when the monitor socket file is not writable by the current user. For example:
```bash
SOCAT_PREFIX=""
if ! test -w "$MONITOR" 2>/dev/null; then SOCAT_PREFIX="sudo"; fi
echo "quit" | $SOCAT_PREFIX socat - "UNIX-CONNECT:$MONITOR" 2>/dev/null || true
```


### xfs.ko missing from installed initramfs (2026-06-16)

**What happened:** Plain install E2E formats the root partition as XFS, but `dakota-nvidia:stable` initramfs ships `btrfs.ko` and `erofs.ko` — not `xfs.ko`. Installed system always boots to emergency mode.

**See:** [#100](https://github.com/projectbluefin/dakota-iso/issues/100)

---

## Filesystem and install verification (2026-06)

### Always verify btrfs, never xfs for dakota

The `dakota-nvidia:stable` initramfs has `btrfs.ko` but not `xfs.ko`. An XFS install
**will appear to succeed** but the installed system drops to emergency mode on first boot
(`sysroot.mount` fails). The plain E2E gate (`just debug=1 plain-e2e dakota`) will
catch this — but only if you actually run it and check that the **installed system boots**,
not just that the install completes.

### The hostname-fix wrapper catches a common fisherman failure

`scripts/fisherman-install.sh` wraps fisherman and patches `/etc/hostname` manually
when fisherman's internal hostname write fails on composefs deployments. If you see
`fisherman: fatal: writing hostname:` in the install log, the wrapper should handle it.
If the wrapper exits non-zero anyway, check that the condition in `fisherman-install.sh`
matches the error message from the fisherman version in the live ISO.

---

## Red Flags

- Waiting for CI to verify a change instead of running `just debug=1 iso-sd-boot <target>` + `just plain-test-qemu <target>` locally first — CI takes 60-90 min; local tests take 20-30 min and give immediate feedback
- Triggering CI without first confirming the ISO boots locally
- Calling `task_complete` after CI is triggered but before a boot + install is verified
- "CI is running" as a substitute for "I tested this"
- Testing only `dakota` when a change touches both `justfile` and `scripts/build-live-squashfs.sh` — always test a non-composefs variant (`bluefin` or `bluefin-lts-hwe`) separately

## Verification

- [ ] `just debug=1 iso-sd-boot <target>` completes without error
- [ ] `just plain-test-qemu <target>` exits with `✅ Installed system boot verified`
- [ ] ISO size is in expected range (dakota ~5.5 GB fast / ~4.5 GB release; bluefin ~7 GB fast / ~6 GB release)
- [ ] GPT type GUID = `28732ac1` (EFI System Partition)
- [ ] If change touches non-composefs path: verified on `bluefin` or `bluefin-lts-hwe`, not just `dakota`

### Always test non-composefs variants separately (2026-06-21)

**What failed:** A fix was verified on `dakota` (composefs) but `bluefin` (non-composefs) had a separate code path in both `justfile` and `scripts/build-live-squashfs.sh` that was wrong. CI was triggered without local testing of the non-composefs path.

**Rule:** Any change to `justfile` or `scripts/build-live-squashfs.sh` must be tested on **both**:
- a composefs variant: `just debug=1 iso-sd-boot dakota && just plain-test-qemu dakota`
- a non-composefs variant: `just debug=1 iso-sd-boot bluefin && just plain-test-qemu bluefin`

Do not trigger CI as a substitute. Local tests take 20-30 min; CI takes 60-90 min and gives no faster signal.
