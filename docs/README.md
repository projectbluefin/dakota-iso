# docs/ — In-Repo Knowledge Base

Accumulated lessons from real work on this repo.
Every agent working here should read the relevant file before starting.

**→ Start with [`docs/SKILL.md`](SKILL.md) to find the right file for your task.**

When you fix a bug or discover a pattern, add a lesson here in the same PR as your change.
This is the feedback loop: lessons help every future agent and contributor.

## Technical docs

| File | Load when... |
|---|---|
| [`build.md`](build.md) | Building ISOs locally, disk space, BTRFS/XFS quirks, `just` variables |
| [`architecture.md`](architecture.md) | Two-container pipeline, boot flow, squashfs, VFS containers-storage |
| [`ci.md`](ci.md) | `build-iso.yml`, `test-luks-install.yml`, R2 uploads, smoke test |
| [`luks-testing.md`](luks-testing.md) | LUKS E2E test — local QEMU, libvirt, CI-equivalent flow |
| [`r2-promotion.md`](r2-promotion.md) | Promoting ISOs to production, rclone, named releases |
| [`variants.md`](variants.md) | Adding/modifying variants, `payload_ref` pattern |

## Process / agentic docs

| File | Load when... |
|---|---|
| [`skills/onboarding.md`](skills/onboarding.md) | First-time setup, dev environment, build prerequisites |
| [`skills/label-workflow.md`](skills/label-workflow.md) | Issue lifecycle, labels, finding work |
| [`skills/human-gates.md`](skills/human-gates.md) | When to stop and ask a human |
| [`skills/skill-improvement.md`](skills/skill-improvement.md) | Writing skill updates in the same PR |

## How to add a lesson

1. Open the relevant file (or create a new one in `skills/` for process learnings)
2. Add a section at the bottom: `### <what you learned> (YYYY-MM-DD)`
3. What failed → why → the fix → code example
4. Commit it in the **same PR** as your change — never a follow-up
