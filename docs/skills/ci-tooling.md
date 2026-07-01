---
name: ci-tooling
description: >
  ISO publish CI patterns for dakota-iso. Use when working on
  `.github/workflows/build-iso.yml` or `build-iso-bluefin.yml`,
  adding a variant to the matrix, changing R2 upload logic,
  or debugging backup rotation or README auto-refresh.
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

**Branch Protection Note (July 2026):** If the `main` branch is protected and direct pushes are disabled (even for bots), this push step will fail with `protected branch hook declined`. The workflow will show as failed on this step, but **the ISO has already been successfully built, tested, and published to R2**. In this event, a repository admin must manually update the row in `README.md` and push it.

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
