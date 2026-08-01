---
id: ci-tooling
name: CI Tooling
one_line_purpose: Maintain GitHub Actions workflows, matrix builds, and R2 backup rotation.
entry_point: docs/skills/ci-tooling.md
category: ci-ops
status: active
tags:
  - ci
  - github-actions
  - r2
description: Workflow definitions, runner environment, caching, and release automation for dakota-iso.
version: "1.2"
last_updated: "2026-08-01"
metadata:
  type: reference
---

# CI Tooling — ISO Publish Workflows

## When to Use

Load this skill when:
- Editing `.github/workflows/build-iso.yml` or `build-iso-bluefin.yml`
- Adding or removing a variant from the build matrix
- Changing R2 upload logic, backup rotation, or publish policy
- Debugging missing backup slots or unexpected bucket clutter
- Updating the README download table

## When NOT to Use

- E2E test debugging → `docs/skills/e2e-ci.md`
- R2 credential rotation → `docs/r2-promotion.md`
- Named release promotion (alpha, stable) → `docs/r2-promotion.md`

## Core Process

### Publish policy

Every CI build publishes **latest-only** — no dated `YYYYMMDD-SHA` objects.
Each variant has exactly three backup slots that rotate on every build.

### Backup rotation order

Before uploading the new ISO, the workflow moves slots in this exact order:

```
backup-2 → backup-3      # free slot 2
backup-1 → backup-2      # free slot 1
latest   → backup-1      # preserve current latest
new ISO  → latest        # publish new build
```

**Order is critical.** Reversing any step overwrites a source before the copy completes.

Slots beyond 3 are pruned by the `Delete backup slots beyond 3` step.

### Current variants

| Workflow | Variants (matrix iso_name) |
|---|---|
| `build-iso.yml` | `dakota` (single job, no matrix) |
| `build-iso-bluefin.yml` | `bluefin-live`, `bluefin-lts-hwe-live` |

**`stable-live` and `lts-live` do not exist.** They were removed in June 2026.

### Adding a new Bluefin variant

1. Create `<variant>/payload_ref`, `<variant>/live_target`, `<variant>/live_title` files
2. Add matrix entry to `build-iso-bluefin.yml` with `iso_name: <variant>-live`
3. Add `live/src/<variant>/` config files (`images.json`, `recipe.json`)
4. Commit variant files and matrix update in the same PR
5. Build with `just debug=1 iso-sd-boot <variant>` locally before CI

### README auto-refresh (dakota only)

`build-iso.yml` includes a "Refresh README dakota table" step that rewrites the
`| \`dakota\` |` row with current ISO size, publish date, and CI run link. It then
git-commits and pushes to `main`. This step requires `contents: write` permission on the job.

**Branch Protection Note (July 2026):** The README refresh is advisory because
`main` may reject direct pushes from the workflow with `protected branch hook declined`.
The step emits a warning and remains non-blocking, so **the ISO stays successfully
built, tested, and published to R2**. A repository admin can manually update the
row in `README.md` when needed.

Bluefin variants do not auto-refresh the README — update their rows manually after a build.

### AHCI vs SCSI CD for smoke boot (bluefin CI)

GitHub Actions runners have no KVM. Without KVM, SCSI bus enumeration in OVMF is too
slow and the VM falls through to PXE. Always use AHCI for CI smoke boots:

```yaml
# ✅ Use this in CI:
-device ich9-ahci,id=ahci0
-device ide-cd,drive=iso,bus=ahci0.1

# ❌ Never use in CI (works locally with KVM, fails in CI):
-device virtio-scsi-pci,id=scsi0
-device scsi-cd,drive=iso,bus=scsi0.0
```

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "I'll add dated objects back for archival." | Policy is latest-only. Backup slots provide the 3-build history. Dated objects cause bucket clutter. |
| "stable and lts are useful aliases." | They duplicate bluefin/bluefin-lts-hwe and diverged silently. Removed in June 2026. |
| "I can reverse the backup rotation order." | No — reversing overwrites source before copy. The order is backup-2→3, backup-1→2, latest→1. |
| "SCSI CD works locally so it works in CI." | Local uses KVM (`-cpu host`). CI uses software emulation (`-cpu qemu64`). SCSI fails without KVM. |

## Red Flags

- `stable-live-*` or `lts-live-*` objects appear in the R2 bucket → old workflow was re-triggered; delete them
- `backup-4.iso` or higher appears → prune step is missing or broken
- Dated `YYYYMMDD-SHA` objects appear → an old workflow branch was re-run; delete them
- README dakota row has `—` for size/date after a build → README refresh step failed; check `contents: write` permission
- `build-iso-bluefin.yml` matrix lists a variant with no `<variant>/` directory → stale matrix entry

## Verification

Before submitting CI workflow changes:

- [ ] Backup rotation order: backup-2→3, backup-1→2, latest→backup-1, then upload
- [ ] `Delete backup slots beyond 3` step present for the affected workflow
- [ ] No `stable` or `lts` entries in `build-iso-bluefin.yml` matrix
- [ ] AHCI (`ich9-ahci`) used for smoke boot in bluefin CI (not SCSI)
- [ ] `contents: write` permission present in `build-iso.yml` job (required for README push)
- [ ] Tests pass: `python -m pytest tests/test_live_build_invariants.py -q`
- [ ] `rclone lsf R2:testing --files-only | sort` shows only `*-latest.iso`, `*-backup-{1,2,3}.iso`, and named alphas

---

## A wrong action SHA silently kills both E2E gates (2026-08-01)

`test-plain-install.yml` and `test-luks-install.yml` both pinned

```yaml
uses: actions/setup-go@f111f37a573bc6312437e3d1d36d22ef1492b453 # v5.3.0
```

That SHA does not exist. The real `actions/setup-go` v5.3.0 commit is
`f111f3307d8850f501ac008e886eec1fd1932a34` — same `f111f3` prefix, then divergent.
It is exactly the shape of a fabricated or mis-copied pin, and the trailing
`# v5.3.0` comment made it look reviewed.

**Why it is dangerous:** the job dies in *Set up job* with

```
##[error]Unable to resolve action `actions/setup-go@f111f37…`, unable to find version
```

Nothing in the workflow ever runs, so there is no install log, no QEMU output, and no
hint that a gate was skipped. The PR simply shows a red E2E check that looks like a
flake. Both mandatory functional gates in this repo were dead this way, which is how
an ENOSPC install regression reached a user's machine
([`install-failures.md`](install-failures.md) Failure 5).

**Rule:** a red check whose failure is in *Set up job* is never a flake and never a
product bug — it is a broken workflow definition. Read the first error before
re-running.

**Sweep for bad pins** (all 8 pins in this repo resolve as of 2026-08-01):

```bash
grep -rhoP 'uses:\s*\K[\w.-]+/[\w.-]+(?:/[\w.-]+)*@[0-9a-f]{40}' .github/workflows/ | sort -u |
while read -r pin; do
  repo="${pin%@*}"; sha="${pin#*@}"
  gh api "repos/$(echo "$repo" | cut -d/ -f1,2)/commits/$sha" -q .sha >/dev/null 2>&1 ||
    echo "UNRESOLVABLE: $pin"
done
```

Resolve the intended tag before pinning — never hand-write a SHA:

```bash
gh api repos/actions/setup-go/git/ref/tags/v5.3.0 -q .object.sha
```

**Automated since 2026-08-01:** `TestActionPinsResolve` in
`tests/test_live_build_invariants.py` runs that sweep on every PR — it resolves each
pinned SHA against the GitHub API, fails with the offending file name when one does
not exist, and skips cleanly when offline or unauthenticated. `test.yml` passes
`GITHUB_TOKEN` to pytest; without it the shared runner IP is rate limited and the
check would silently skip.
## The E2E gates were testing a six-week-old installer (2026-08-01)

`test-plain-install.yml` and `test-luks-install.yml` build the fisherman binary they
test with from a clone made in the *Clone patched fisherman* step. That step pinned

```yaml
git clone https://github.com/projectbluefin/fisherman.git \
  --branch fix/overlay-driver-for-ostree-bootc-install \
  --depth 1 /tmp/fisherman
```

whose last commit was **2026-06-17**. A feature branch is a fossil the moment it stops
moving, and nothing in CI notices — the gate stays green-looking while it validates an
installer nobody ships. Every fisherman fix merged after mid-June was invisible to E2E,
including the scratch-cache ENOSPC fix the gate should have caught
([`install-failures.md`](install-failures.md) Failure 5).

**Fixed:** both workflows now clone `--branch main` and log the resolved commit, so the
job output records exactly which installer was tested.

**Guarded:** `TestE2EFishermanRef` in `tests/test_live_build_invariants.py` fails the
build if either workflow clones anything other than a long-lived branch (`main`/`dev`).

**Branch note:** fisherman's *default* branch is `dev`, but `main` is the active line
(`main` was 25 commits ahead of `dev` on 2026-08-01). A PR opened with the default base
lands on the branch nobody ships. Target `main`.
