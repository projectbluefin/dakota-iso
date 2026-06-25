# Dakota ISO — Skill Router

Agent entry point for `projectbluefin/dakota-iso`. Load only the skill(s) that match your task.

> **Scope:** skills here cover work done *in this repo*. Org-level skills and factory operating model live in [`projectbluefin/common/docs/`](https://github.com/projectbluefin/common/tree/main/docs).

## Task → Skill

| I need to... | Load |
|---|---|
| **First time / onboarding** | |
| Set up a dev environment or clone this repo | [`docs/skills/onboarding.md`](skills/onboarding.md) |
| Understand issue labels and lifecycle | [`docs/skills/label-workflow.md`](skills/label-workflow.md) |
| Know when to stop and ask a human | [`docs/skills/human-gates.md`](skills/human-gates.md) |
| **QA and testing** | |
| Run any E2E test, verify a build, or make a verification claim | [`docs/skills/qa-policy.md`](skills/qa-policy.md) |
| **Building ISOs** | |
| Build ISOs locally, disk space, BTRFS/XFS quirks | [`docs/build.md`](build.md) |
| Unified nvidia ISO — size, compression, composefs vs non-composefs variants | [`docs/build.md`](build.md) |
| ISO is wrong size | [`docs/build.md`](build.md) → ISO size table at top |
| Installed system drops to emergency shell or won't boot | [`docs/skills/install-failures.md`](skills/install-failures.md) |
| UEFI falls to PXE after install (no bootloader found) | [`docs/skills/install-failures.md`](skills/install-failures.md) |
| Add or modify variants (`payload_ref` pattern) | [`docs/variants.md`](variants.md) |
| **Architecture** | |
| Two-container pipeline, boot flow, squashfs, VFS storage | [`docs/architecture.md`](architecture.md) |
| GPT layout, El Torito, systemd-boot, dmsquash-live | [`docs/architecture.md`](architecture.md) |
| **CI/CD** | |
| `build-iso.yml`, smoke test, R2 uploads, unified ISO pipeline | [`docs/ci.md`](ci.md) |
| LUKS E2E test (local QEMU, libvirt, CI-equivalent) + installed-disk boot | [`docs/luks-testing.md`](luks-testing.md) |
| **R2 / Release** | |
| Promoting ISOs to production, rclone, named releases | [`docs/r2-promotion.md`](r2-promotion.md) |
| **Factory / org** | |
| Cross-repo agent rules, branch targets, PR policy | [common: `docs/factory/agentic-model.md`](https://github.com/projectbluefin/common/blob/main/docs/factory/agentic-model.md) |
| Org structure, parity matrix, open gaps | [common: `docs/factory/README.md`](https://github.com/projectbluefin/common/blob/main/docs/factory/README.md) |
| **Skill improvement** | |
| Writing a skill update alongside a PR | [`docs/skills/skill-improvement.md`](skills/skill-improvement.md) |
| Skill-drift CI check failing on a PR | [`docs/skills/skill-drift.md`](skills/skill-drift.md) |

## Skill index

For the full list of skills in this repo, see [`docs/skills/INDEX.md`](skills/INDEX.md).

## How to add a skill

If a task surfaces a non-obvious pattern, workaround, or convention:

1. Create (or update) the relevant file in `docs/skills/`
2. Add a dated section: `### <what you learned> (YYYY-MM-DD)`
3. What failed → why → the fix → code example
4. Commit in the **same PR** as your change — never a follow-up

See [`docs/skills/skill-improvement.md`](skills/skill-improvement.md) for the full mandate.
