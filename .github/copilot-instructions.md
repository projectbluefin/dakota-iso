# dakota-iso — Copilot Instructions

> This repo builds the bootable UEFI live ISO from projectbluefin images (GNOME OS / bootc / composefs).
> Active variant: `dakota` (NVIDIA-unified). `bluefin` and `bluefin-lts-hwe` are **dormant**
> since 2026-09-18 — off every PR check and schedule, `build-iso-bluefin.yml` disabled at the
> Actions level. Do not re-add them to CI matrices; see [`docs/variants.md`](../docs/variants.md).

## Fast path

```
1. AGENTS.md                          # repo operating contract — read first
2. docs/SKILL.md                      # find the skill for your task
3. justfile                           # all build tasks go through here
```

## Agentic model

This repo is part of the [projectbluefin factory](https://github.com/projectbluefin/common/blob/main/docs/factory/README.md).
Cross-repo hard rules, branch targets, and PR comment policy:
[`projectbluefin/common/docs/factory/agentic-model.md`](https://github.com/projectbluefin/common/blob/main/docs/factory/agentic-model.md)

**Branch target:** PRs go to `main`.

## 🚫 ublue-os absolute prohibition

**NEVER create issues, PRs, comments, or any programmatic write action targeting any `ublue-os/*` repository.**
Read-only `gh api` calls are permitted. No writes of any kind.

## Commit format

[Conventional Commits](https://www.conventionalcommits.org/): `<type>(<scope>): <description>`

Every AI-authored commit **must** include both trailers:
```
Assisted-by: <Model> via GitHub Copilot
Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
```

## Sensitive paths

Changes to these require maintainer review:
- `.github/workflows/` — CI pipeline
- `justfile` — canonical build interface
- `live/src/build-iso.sh` — ISO assembly
- `live/src/configure-live.sh` — live environment setup
- `scripts/build-live-squashfs.sh` — squashfs + OCI embed

## Pre-commit

```bash
just check   # run before every commit (agentic-model hard rule)
```

`check` runs the pytest suite plus the pre-commit hooks (yaml/json validation,
actionlint, action-pin policy) — the same two gates CI enforces.

## Human gates

Stop and ask a human at:
- **Design:** architecture change, new subsystem, user-visible behavior change
- **Security:** signing, supply chain, secrets, third-party sources
- **Breakage:** cross-repo breaking change
- **Merge:** always requires human approval

See [`docs/skills/human-gates.md`](../docs/skills/human-gates.md) for evidence requirements.

## Self-improvement loop

Every session produces two outputs: the work and the learning.
Skill updates go in the **same PR**, never a follow-up.
See [`docs/skills/skill-improvement.md`](../docs/skills/skill-improvement.md).

Use the shared [label workflow](https://github.com/projectbluefin/common/blob/main/docs/skills/label-workflow.md):
humans triage and approve, agents claim `status/queued`, and Clankers only
transports Hive assignments. Templates are synchronized from bonedigger.
Never write to `ublue-os/*`.
