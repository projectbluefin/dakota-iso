# dakota-iso — Copilot Instructions

> This repo builds bootable UEFI live ISOs from projectbluefin images (GNOME OS / bootc / composefs).
> Variants: `dakota`, `bluefin`, `bluefin-lts-hwe` — all NVIDIA-unified.

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
